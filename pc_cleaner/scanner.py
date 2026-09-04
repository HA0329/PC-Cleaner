"""安全扫描器。

职责：
- 能安全地把「分类规格」展开成一组候选 Target；
- 处理路径展开（环境变量）、黑名单过滤、符号链接/联接（junction）跳过；
- 计算每个目标的体积与文件数量；
- 对权限错误等异常逐项容错，不中断整体扫描。

绝不删除任何东西——扫描只负责"发现并测量"，删除交给 engine.py。

v0.8.2 改进：
- 目录大小统计（_dir_size）不再跳过 node_modules、__pycache__ 等目录，
  确保可释放空间统计准确（修复低估问题）。
- 抽取公共过滤函数 _filter_and_build_file_targets，消除 _scan_glob_files
  与 _scan_files_by_rule 之间的代码重复。

v0.8.3 修复（安全）：
- make_protect_check 改为两层匹配：绝对/环境变量模式做前缀匹配；相对模式
  （windows\\system32、weixinshuju、.git 等）按路径组件全等匹配任意层——
  修复旧实现把相对模式展开成"相对当前工作目录"导致真实系统路径失去保护。
- 白名单清空根内的路径放行；不再对不存在的路径一律保守拒绝。
"""

from __future__ import annotations

import os
import sys
import time
import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import CategoryResult, Target, TargetAction, TargetKind, format_size
from .rules import (
    DEFAULT_SKIP_DIRNAMES,
    category_label,
    get_protected_patterns,
    is_within_clear_root,
    spec_requires_admin,
    SYSTEM_CRITICAL_FILES,      # 安全增强：系统关键文件黑名单（虽然扫描器不删除，但传递给引擎）
)

# ===========================================================================
# 日志配置（安全增强：记录异常）
# ===========================================================================
logger = logging.getLogger("pc_cleaner.scanner")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

# 扫描进度回调类型
ScanProgressCB = Callable[[str, int, int], None]  # (category_label, current_spec, total_specs)


# ===========================================================================
# 路径与保护判断
# ===========================================================================
def expand_path(path_str: str) -> Path:
    """扩展环境变量与 ~，返回绝对 Path。"""
    expanded = os.path.expandvars(os.path.expanduser(path_str))
    return Path(expanded).absolute()


def normalize(path: Path) -> str:
    """返回用于大小写不敏感比较的规范化字符串。"""
    return os.path.normcase(str(path))


def is_admin() -> bool:
    """当前进程是否拥有管理员权限（Windows）。非 Windows 视为 False。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# 安全增强：保护路径检查
# ===========================================================================
def _path_components(s: str) -> list[str]:
    """把路径拆成小写组件列表（按 / 与 \\ 分割），用于组件级全等匹配。"""
    parts = str(s).replace("/", os.sep).split(os.sep)
    return [p.lower() for p in parts if p and p not in (".",)]


def _contains_sequence(target: list[str], pattern: list[str]) -> bool:
    """target 组件序列是否连续包含 pattern 组件序列（全等匹配）。"""
    if not pattern:
        return False
    n = len(target)
    m = len(pattern)
    for i in range(n - m + 1):
        if target[i : i + m] == pattern:
            return True
    return False


def _is_abs_style_pattern(pattern: str) -> bool:
    """模式是否为「绝对/环境变量」形态（应展开为绝对前缀），
    而不是相对的系统路径片段 / 目录名（应做组件级全等匹配）。

    注意：单个前导反斜杠（如内置的 ``\\.git`` 片段）**不是**绝对路径，
    不应被当成 UNC/根路径处理。
    """
    p = pattern.strip()
    if not p:
        return False
    if "%" in p or p.startswith("~"):
        return True
    if len(p) >= 2 and p[0].isalpha() and p[1] == ":" and p[2:3] in ("\\", "/"):
        return True
    # UNC 路径（\\server\share）才算绝对；单反斜杠开头只是路径片段
    return p.startswith("\\\\")


def make_protect_check(user_patterns: list[str] | None = None):
    """返回一个 (path) -> bool 的保护判断函数。

    安全增强（v0.8.3 修复）：
    - 两层匹配语义：
        1) 含环境变量 / ``~`` / 盘符 / 绝对形式的模式 → 展开为规范化绝对路径，
           做**前缀匹配**（等于或以之为父目录）；
        2) 其余相对模式（如 ``windows\\system32``、``weixinshuju``、``.git``）
           → 按**路径组件（目录名序列）全等匹配**，命中任意一层即受保护。
           这修复了旧实现把相对模式展开成"相对当前工作目录"导致
           System32 / 微信数据目录等真实路径完全失去保护的问题；
           组件全等不会像子串那样误伤（"windows" 不会误匹配 "windows.old"）。
    - 白名单清空根（ALLOWED_CLEAR_ROOTS）内的路径放行（其内容可被清空）；
    - 不做 strict resolve：路径不存在时按字面组件参与匹配，避免
      "无法解析 → 一律保守拒绝" 造成误伤（如把普通临时路径当受保护）。
    """
    # 绝对前缀模式（env / ~ / 盘符 / 绝对路径）
    prefixes: list[str] = []
    # 相对模式 → 组件序列（内置的相对系统路径片段 + 名称级保护）
    name_patterns: list[list[str]] = []
    raw_patterns = list(get_protected_patterns())
    if user_patterns:
        raw_patterns.extend(str(p) for p in user_patterns)

    for p in raw_patterns:
        p = p.strip()
        if not p:
            continue
        try:
            if _is_abs_style_pattern(p):
                prefixes.append(normalize(str(expand_path(p))))
            else:
                comps = _path_components(p)
                if comps:
                    name_patterns.append(comps)
        except Exception:  # 路径展开失败则忽略
            continue

    # 去重并按长度降序排列（优先匹配更具体的路径）
    prefixes = sorted(set(prefixes), key=len, reverse=True)

    def _check(path: Path) -> bool:
        try:
            target = normalize(str(path))
        except Exception:  # noqa: BLE001 无法转字符串则保守拒绝
            return True

        # 白名单清空根内的路径不受保护（内容允许被清空，根目录本身由引擎拦截）
        if is_within_clear_root(target):
            return False

        # 1) 绝对前缀匹配
        for pattern in prefixes:
            if target == pattern or target.startswith(pattern + os.sep):
                return True

        # 2) 相对模式：组件级全等匹配（命中任意层即受保护）
        if name_patterns:
            tokens = _path_components(target)
            for pat in name_patterns:
                if _contains_sequence(tokens, pat):
                    return True
        return False

    return _check


# ===========================================================================
# 安全遍历（支持跳过指定目录名，也可不跳过）
# ===========================================================================
def _walk_dir(
    root: Path,
    is_protected,
    skip_names: set[str] | None = DEFAULT_SKIP_DIRNAMES,
    max_depth: int | None = None,
    find_names: set[str] | None = None,
) -> Iterator[tuple[Path, bool]]:
    """安全遍历目录树，产出 (路径, is_dir)。

    - 不跟随符号链接/联接（junction）；
    - 跳过受保护目录；
    - 跳过 skip_names 中的目录名（若 skip_names 为 None 或空集合，则不跳过任何目录名，
      仅保留受保护路径的跳过）；
    - ``find_names``：这些目录名本应被 skip 跳过，但需作为目标被**发现**；
      命中的目录只产出、不再下探，避免放大遍历进巨大目录；
    - max_depth 限制深度（根为 0）。

    Args:
        root: 起始目录
        is_protected: 保护检查函数
        skip_names: 要跳过的目录名集合。传 None 或空集合表示不跳过任何目录名
                    （仅跳过受保护路径）。默认值为 DEFAULT_SKIP_DIRNAMES。
        max_depth: 最大遍历深度，None 表示无限制
        find_names: 需要特别发现的目标目录名集合（这些目录即使被 skip_names 包含也会被产出）
    """
    skip = skip_names or set()   # 关键：如果传 None 或空集，则不跳过任何目录名
    root = Path(root)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        if max_depth is not None and depth > max_depth:
            continue
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        # 不跟随符号链接/联接（junction），避免越界误删或造成指数级扫描
                        is_link = entry.is_symlink() or (
                            hasattr(entry, "is_junction") and entry.is_junction()
                        )
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    child = Path(entry.path)
                    if is_protected(child):
                        continue
                    if is_dir:
                        if is_link:
                            continue
                        low = entry.name.lower()
                        if low in skip and not (find_names and low in find_names):
                            continue
                        yield child, True
                        if find_names and low in find_names:
                            continue  # 命中的目标目录不再下探
                        stack.append((child, depth + 1))
                    else:
                        if is_link:
                            continue
                        yield child, False
        except (PermissionError, OSError):
            continue


# ===========================================================================
# 目录大小统计（不跳过任何特定目录名，保证准确性）
# ===========================================================================
def _dir_size(
    path: Path,
    is_protected,
    memo: dict[str, tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """计算目录内可清理的字节数与文件数量（不跟随链接、跳过受保护项）。

    注意：此函数统计目录下**所有**文件（除受保护路径外），
    不会跳过 node_modules、__pycache__ 等，以保证空间统计准确。
    若需加速，可考虑设置 max_depth 限制，但当前保持完整遍历。

    ``memo``：同一扫描内的全局大小缓存（key 为规范化绝对路径）。
    同一目录被多个规则命中时只递归遍历一次，避免重复统计（目录内容
    在扫描期间视为不变；跨扫描/删除前后对比请使用新的 memo 或 None）。
    """
    if memo is not None:
        key = normalize(path)
        hit = memo.get(key)
        if hit is not None:
            return hit
    total = 0
    count = 0
    # ★ 关键：传入空集合，不跳过任何特定目录名，只跳过受保护路径
    for child, is_dir in _walk_dir(path, is_protected, skip_names=set(), max_depth=None):
        if is_dir:
            continue
        try:
            total += child.stat().st_size
            count += 1
        except OSError:
            continue
    if memo is not None:
        memo[key] = (total, count)
    return total, count


# ===========================================================================
# 公共过滤函数（消除重复）
# ===========================================================================
def _filter_and_build_file_targets(
    file_paths: Iterator[Path],
    category_key: str,
    is_protected,
    min_size_bytes: int = 0,
    older_than_secs: int = 0,
    label: str = "",
) -> list[Target]:
    """从文件路径迭代器中筛选符合条件的文件，构造 Target 列表。

    该函数抽取了 _scan_glob_files 和 _scan_files_by_rule 的公共逻辑，
    用于统一处理文件过滤、保护检查、stat 获取和 Target 构造。

    Args:
        file_paths: 文件路径迭代器（如 glob 或 walk 产出）
        category_key: 分类键名
        is_protected: 保护检查函数
        min_size_bytes: 最小体积阈值（字节），0 表示不限制
        older_than_secs: 最旧修改时间阈值（秒），0 表示不限制
        label: 目标标签

    Returns:
        list[Target]: 满足条件的文件 Target 列表
    """
    now = time.time()
    targets: list[Target] = []
    for p in file_paths:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if is_protected(p):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        # 应用过滤条件
        if st.st_size < min_size_bytes:
            continue
        if older_than_secs > 0 and (now - st.st_mtime) < older_than_secs:
            continue
        targets.append(
            Target(
                path=p,
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category=category_key,
                size=st.st_size,
                file_count=1,
                label=label,
            )
        )
    return targets


# ===========================================================================
# 各类 target 的扫描实现
# ===========================================================================
def _scan_clear_dir(
    spec: dict[str, Any], category_key: str, is_protected, size_memo: dict | None = None
) -> list[Target]:
    base = expand_path(spec["path"])
    if not base.exists() or not base.is_dir():
        return []
    if is_protected(base):
        return []
    size, count = _dir_size(base, is_protected, memo=size_memo)
    if size == 0 and count == 0:
        return []
    label = spec.get("label", "")
    return [
        Target(
            path=base,
            kind=TargetKind.DIR,
            action=TargetAction.CLEAR,
            category=category_key,
            size=size,
            file_count=count,
            label=label,
        )
    ]


def _scan_delete_dir(
    spec: dict[str, Any], category_key: str, is_protected, size_memo: dict | None = None
) -> list[Target]:
    base = expand_path(spec["path"])
    if not base.exists() or not base.is_dir():
        return []
    if is_protected(base):
        return []
    size, count = _dir_size(base, is_protected, memo=size_memo)
    label = spec.get("label", "")
    return [
        Target(
            path=base,
            kind=TargetKind.DIR,
            action=TargetAction.DELETE,
            category=category_key,
            size=size,
            file_count=count,
            label=label,
        )
    ]


def _scan_glob_dirs(
    spec: dict[str, Any], category_key: str, is_protected, size_memo: dict | None = None
) -> list[Target]:
    base_str = spec.get("base", "")
    if not base_str:
        return []
    base = expand_path(base_str)
    if not base.exists():
        return []
    pattern = spec.get("pattern", "*")
    action = spec.get("action", "clear")
    label = spec.get("label", "")
    targets: list[Target] = []
    try:
        matches = base.glob(pattern)
    except (OSError, ValueError):
        return []
    for m in matches:
        try:
            if not m.is_dir():
                continue
        except OSError:
            continue
        if is_protected(m):
            continue
        size, count = _dir_size(m, is_protected, memo=size_memo)
        if size == 0 and count == 0:
            continue
        act = TargetAction.CLEAR if action == "clear" else TargetAction.DELETE
        targets.append(
            Target(
                path=m,
                kind=TargetKind.DIR,
                action=act,
                category=category_key,
                size=size,
                file_count=count,
                label=label,
            )
        )
    return targets


def _scan_glob_files(
    spec: dict[str, Any], category_key: str, is_protected, size_memo: dict | None = None
) -> list[Target]:
    base_str = spec.get("base", "")
    if not base_str:
        return []
    base = expand_path(base_str)
    if not base.exists():
        return []
    pattern = spec.get("pattern", "*")
    label = spec.get("label", "")
    min_size_bytes = int(spec.get("min_size_mb", 0) or 0) * 1024 * 1024
    older_than_secs = int(spec.get("older_than_days", 0) or 0) * 86400

    try:
        matches = base.glob(pattern)
    except (OSError, ValueError):
        return []

    # 复用公共过滤函数：传入生成器，惰性过滤
    return _filter_and_build_file_targets(
        (m for m in matches if m.is_file()),  # 仅保留文件
        category_key,
        is_protected,
        min_size_bytes=min_size_bytes,
        older_than_secs=older_than_secs,
        label=label,
    )


def _resolve_bases(spec: dict[str, Any]) -> list[Path]:
    """find_dirs 的扫描基目录。

    - ``<CWD>`` 占位符替换为当前工作目录；
    - 追加配置文件 ``dev_artifact_bases`` 中用户指定的目录。
    """
    from .config import load_config  # 延迟导入，避免循环依赖

    bases: list[Path] = []
    for b in spec.get("bases", []):
        if b == "<CWD>":
            bases.append(Path.cwd())
        elif isinstance(b, str) and b:
            bases.append(expand_path(b))
    for extra in (load_config().get("dev_artifact_bases") or []) or []:
        if isinstance(extra, str) and extra:
            bases.append(expand_path(extra))
    # 去重
    seen: set[str] = set()
    ordered: list[Path] = []
    for p in bases:
        key = normalize(p)
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return ordered


def _scan_find_dirs(
    spec: dict[str, Any],
    category_key: str,
    is_protected,
    max_depth: int = 20,
    size_memo: dict | None = None,
) -> list[Target]:
    names = {n.lower() for n in spec.get("names", [])}
    bases = _resolve_bases(spec)
    if not bases:
        return []
    action = (
        TargetAction.CLEAR
        if spec.get("action", "delete") == "clear"
        else TargetAction.DELETE
    )
    label = spec.get("label", "")
    seen: set[str] = set()
    targets: list[Target] = []
    for base in bases:
        if not base.exists() or not base.is_dir():
            continue
        if is_protected(base):
            continue
        # 只遍历一层找到候选目录名；找到后不下降（它们本身就是待删目标）
        # 这里仍然使用 DEFAULT_SKIP_DIRNAMES 以加速查找
        for child, is_dir in _walk_dir(base, is_protected, max_depth=max_depth, find_names=names):
            if not is_dir:
                continue
            if child.name.lower() in names:
                key = normalize(child)
                if key in seen:
                    continue
                seen.add(key)
                size, count = _dir_size(child, is_protected, memo=size_memo)
                targets.append(
                    Target(
                        path=child,
                        kind=TargetKind.DIR,
                        action=action,
                        category=category_key,
                        size=size,
                        file_count=count,
                        label=label,
                    )
                )
    return targets


def _scan_files_by_rule(
    spec: dict[str, Any], category_key: str, is_protected, size_memo: dict | None = None
) -> list[Target]:
    base_str = spec.get("base", "")
    if not base_str:
        return []
    import time

    base = expand_path(base_str)
    if not base.exists() or not base.is_dir():
        return []
    label = spec.get("label", "")
    min_size_bytes = int(spec.get("min_size_mb", 0) or 0) * 1024 * 1024
    older_than_secs = int(spec.get("older_than_days", 0) or 0) * 86400

    # 使用 _walk_dir 生成所有文件路径，传入空集合以不跳过任何子目录（确保完整覆盖）
    file_paths = (child for child, is_dir in _walk_dir(base, is_protected, skip_names=set()) if not is_dir)

    # 复用公共过滤函数
    return _filter_and_build_file_targets(
        file_paths,
        category_key,
        is_protected,
        min_size_bytes=min_size_bytes,
        older_than_secs=older_than_secs,
        label=label,
    )


def _scan_compact_db(
    spec: dict[str, Any], category_key: str, is_protected, size_memo: dict | None = None
) -> list[Target]:
    """扫描 SQLite 数据库文件，构造 COMPACT（压缩）目标。

    对应 BleachBit「整理优化数据库」：对浏览器 History / Web Data / Login Data /
    Cookies / places.sqlite 等执行 VACUUM 释放碎片，**不删除数据**。
    引擎侧通过 :func:`engine.compact_database` 执行。
    """
    base_str = spec.get("base", "")
    if not base_str:
        return []
    base = expand_path(base_str)
    if not base.exists() or not base.is_dir():
        return []
    pattern = spec.get("pattern", "*")
    label = spec.get("label", "")
    try:
        matches = base.glob(pattern)
    except (OSError, ValueError):
        return []
    targets: list[Target] = []
    for m in matches:
        try:
            if not m.is_file():
                continue
        except OSError:
            continue
        if is_protected(m):
            continue
        try:
            st = m.stat()
        except OSError:
            continue
        targets.append(
            Target(
                path=m,
                kind=TargetKind.FILE,
                action=TargetAction.COMPACT,
                category=category_key,
                size=st.st_size,
                file_count=1,
                label=label,
            )
        )
    return targets


# target 类型 -> 处理函数映射（find_dirs 特殊处理，支持 max_depth 参数）
_TARGET_HANDLERS = {
    "clear_dir": _scan_clear_dir,
    "delete_dir": _scan_delete_dir,
    "glob_dirs": _scan_glob_dirs,
    "glob_files": _scan_glob_files,
    "files_by_rule": _scan_files_by_rule,
    "compact_db": _scan_compact_db,
}


def scan_spec(
    spec: dict[str, Any],
    is_protected=None,
    scan_depth: int = 20,
    on_progress: ScanProgressCB | None = None,
    progress_idx: int = 0,
    progress_total: int = 0,
    size_memo: dict[str, tuple[int, int]] | None = None,
) -> CategoryResult:
    """扫描单个分类规格，返回 CategoryResult。

    ``size_memo``：跨 target/分类共享的目录大小缓存（见 ``_dir_size``）；
    None 时在本分类内新建一个。
    """
    key = spec["key"]
    label = spec.get("label") or category_label(key)
    if is_protected is None:
        is_protected = make_protect_check()
    if size_memo is None:
        size_memo = {}

    t0 = time.time()

    result = CategoryResult(
        key=key,
        label=label,
        risk=str(spec.get("risk") or "safe"),
        requires_admin=spec_requires_admin(spec),
    )

    if on_progress:
        on_progress(label, progress_idx, progress_total)

    # 需要管理员权限但当前未提权：跳过扫描并给出提示
    if result.requires_admin and not is_admin():
        result.admin_blocked = True
        result.scanned = True
        result.scan_duration = time.time() - t0
        return result

    seen: set[str] = set()
    for target_spec in spec.get("targets", []):
        ttype = target_spec.get("type")
        # find_dirs 特殊处理，传入 max_depth
        if ttype == "find_dirs":
            try:
                targets = _scan_find_dirs(
                    target_spec, key, is_protected,
                    max_depth=scan_depth, size_memo=size_memo,
                )
            except (PermissionError, OSError, ValueError):
                # 预期的遍历/权限类错误：跳过该 target，不影响整体扫描
                result.skipped += 1
                continue
            except Exception as exc:  # 安全增强：记录未知异常（控制台 + 日志）
                logger.exception("分类 %s 的 find_dirs 目标扫描异常: %s", key, exc)
                print(f"[扫描警告] 分类 {key} 的 find_dirs 目标扫描异常: {exc}", file=sys.stderr)
                result.skipped += 1
                continue
        else:
            handler = _TARGET_HANDLERS.get(ttype)
            if handler is None:
                continue
            try:
                targets = handler(target_spec, key, is_protected, size_memo=size_memo)
            except (PermissionError, OSError, ValueError):
                result.skipped += 1
                continue
            except Exception as exc:  # 安全增强：记录未知异常（控制台 + 日志）
                logger.exception("分类 %s 的 %s 目标扫描异常: %s", key, ttype, exc)
                print(f"[扫描警告] 分类 {key} 的 {ttype} 目标扫描异常: {exc}", file=sys.stderr)
                result.skipped += 1
                continue
        for t in targets:
            tpath = normalize(t.path)
            if tpath in seen:
                continue
            seen.add(tpath)
            result.targets.append(t)
    result.scanned = True
    result.scan_duration = time.time() - t0
    return result


def scan_all(
    specs: list[dict[str, Any]],
    scan_depth: int = 20,
    on_progress: ScanProgressCB | None = None,
) -> list[CategoryResult]:
    """扫描所有分类（不含回收站），返回结果列表。

    ``recycle_bin`` 由 CLI 特殊处理，不在此扫描。
    跨分类做全局去重：同一路径出现在多个分类时只保留第一次出现的 Target，
    且共享同一个目录大小缓存（``_dir_size`` 对同一目录只递归遍历一次）。
    例外：**COMPACT（数据库压缩）目标不参与删除类去重** —— 压缩与删除是两种
    不同意图，用户可能只选 ``database_compact`` 而删除类分类先行占用了同一路径，
    此时应保留 COMPACT 目标（对 COMPACT 自身单独去重即可）。
    ``scan_depth``：find_dirs 遍历深度限制（默认 20 层）。
    ``on_progress``：扫描进度回调 (category_label, current_idx, total)。
    """
    is_protected = make_protect_check()
    size_memo: dict[str, tuple[int, int]] = {}
    results: list[CategoryResult] = []
    seen_paths: set[str] = set()
    seen_compact: set[str] = set()
    total = len(specs)
    for idx, spec in enumerate(specs, start=1):
        if spec.get("key") == "recycle_bin":
            continue
        res = scan_spec(
            spec, is_protected,
            scan_depth=scan_depth,
            on_progress=on_progress,
            progress_idx=idx,
            progress_total=total,
            size_memo=size_memo,
        )
        deduped: list[Target] = []
        for t in res.targets:
            key = normalize(t.path)
            if t.action is TargetAction.COMPACT:
                if key in seen_compact:
                    continue
                seen_compact.add(key)
            else:
                if key in seen_paths:
                    continue
                seen_paths.add(key)
            deduped.append(t)
        res.targets = deduped
        results.append(res)
    return results


def recycle_bin_size() -> int:
    """估算回收站占用字节数（只读扫描各驱动器的 ``$Recycle.Bin``，不删除）。"""
    if sys.platform != "win32":
        return 0
    total = 0
    for drive in _system_drives():
        root = Path(drive) / "$Recycle.Bin"
        if not root.is_dir():
            continue
        try:
            for child in root.rglob("*"):
                try:
                    if child.is_file() and not child.is_symlink():
                        total += child.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def _system_drives() -> list[str]:
    """返回本机存在的盘符根路径列表，如 ``["C:\\", "D:\\"]``。"""
    import string

    out: list[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive):
                out.append(drive)
        except OSError:
            continue
    return out


# ===========================================================================
# 输出报告（供 CLI 使用）
# ===========================================================================
def print_report(results: list[CategoryResult]) -> None:
    """人类可读的扫描结果摘要输出（简略模式）。"""
    from .console import green, pad_cjk

    total_ref = 0
    print("已扫描候选清理目标：")
    print("-" * 56)
    for res in results:
        if not res.scanned or not res.targets:
            print(f"  {pad_cjk(res.label, 14)} (0 项)")
            continue
        total_ref += res.liberatable
        print(
            f"  {green(pad_cjk(res.label, 14))} {res.total_count} 项, "
            f"可释放 {green(format_size(res.liberatable))}"
        )
    print("-" * 56)
    print(f"  合计可释放: {green(format_size(total_ref))}")


def print_detail_report(
    results: list[CategoryResult],
    sort_by: str = "size_desc",
    max_items: int = 0,
) -> None:
    """详细扫描结果输出，展示每个分类下的具体目标目录/文件。

    sort_by: 'size_desc' | 'size_asc' | 'name_asc' | 'count_desc'
    max_items: 每个分类最多展示条数，0 表示不限制
    """
    from .console import bold, cyan, dim, green, red, yellow, pad_cjk, separator

    print("")
    print(bold("╔══════════════════════════════════════════════════════════╗"))
    print(bold("║              详细扫描结果（完整目录列表）                ║"))
    print(bold("╚══════════════════════════════════════════════════════════╝"))
    print("")

    total_size = 0
    total_count = 0
    total_targets = 0

    for res in results:
        if not res.scanned:
            continue

        # 风险徽标
        if res.risk == "safe":
            badge = green("●")
        elif res.risk == "moderate":
            badge = yellow("●")
        else:
            badge = red("●")

        admin_tag = yellow(" [需管理员]") if res.requires_admin else ""

        if res.admin_blocked:
            print(f"  {badge} {bold(res.label)}{admin_tag}")
            print(f"    {yellow('⚠ 需管理员权限，已跳过扫描')}")
            print("")
            continue

        if not res.targets:
            print(f"  {badge} {bold(res.label)}{admin_tag}  (0 项)")
            print("")
            continue

        targets = res.sorted_targets(sort_by)
        if max_items > 0:
            shown = targets[:max_items]
            hidden_count = len(targets) - max_items
        else:
            shown = targets
            hidden_count = 0

        cat_size = res.liberatable
        cat_count = res.total_count
        total_size += cat_size
        total_count += cat_count
        total_targets += len(targets)

        print(f"  {badge} {bold(res.label)}{admin_tag}")
        print(f"    {dim(f'{len(targets)} 个目标, {cat_count} 个文件, 可释放 {green(format_size(cat_size))}')}")
        print(f"    {dim('─' * 60)}")

        for i, t in enumerate(shown, 1):
            # 紧凑格式：序号 + 图标 + 路径 + 大小
            icon = t.kind_icon
            label_tag = f" {dim(f'({t.label})')}" if t.label else ""
            action_tag = dim(f"[{t.action_label}]")

            path_str = str(t.path)
            size_str = t.display_size
            count_str = f"{t.file_count} 文件" if t.kind is TargetKind.DIR else ""

            if t.kind is TargetKind.DIR:
                print(f"    {cyan(str(i).rjust(3))}. {icon} {path_str}{label_tag}")
                print(f"         {action_tag} {count_str}, {green(size_str)}")
            else:
                print(f"    {cyan(str(i).rjust(3))}. {icon} {path_str}{label_tag}")
                print(f"         {green(size_str)}")

        if hidden_count > 0:
            print(f"    {dim(f'... 以及另外 {hidden_count} 项（使用 --detail 查看更多或设置 preview_lines 调整）')}")

        print("")

    # 汇总
    print(separator("═") if hasattr(separator, '__call__') else "=" * 60)
    print(f"  {bold('合计')}：{total_targets} 个目标, {total_count} 个文件, 可释放 {green(format_size(total_size))}")
    print(separator("═") if hasattr(separator, '__call__') else "=" * 60)


def print_tree_report(results: list[CategoryResult]) -> None:
    """以树形结构展示扫描结果。"""
    from .console import bold, cyan, dim, green, red, yellow

    print("")
    print(bold("📁 扫描结果（树形视图）"))
    print("")

    total_size = 0

    for res in results:
        if not res.scanned or not res.targets:
            continue

        if res.risk == "safe":
            badge = green("●")
        elif res.risk == "moderate":
            badge = yellow("●")
        else:
            badge = red("●")

        cat_size = res.liberatable
        total_size += cat_size

        print(f"  {badge} {bold(res.label)} {dim(f'[{res.key}]')}")
        print(f"  │  {dim(f'{len(res.targets)} 个目标, 可释放 {green(format_size(cat_size))}')}")

        targets = res.sorted_targets("size_desc")
        for i, t in enumerate(targets):
            connector = "├──" if i < len(targets) - 1 else "└──"
            label_tag = f" {dim(f'← {t.label}')}" if t.label else ""
            print(f"  │  {connector} {t.kind_icon} {t.path.name}{label_tag}  {green(t.display_size)}")
            if t.kind is TargetKind.DIR:
                print(f"  │  {'│' if i < len(targets) - 1 else ' '}     {dim(f'{t.file_count} 文件, {t.action_label}')}")

        print(f"  │")
        print("")

    print(f"  {bold(f'合计可释放: {green(format_size(total_size))}')}")