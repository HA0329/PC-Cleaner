"""安全扫描器。

职责：
- 能安全地把「分类规格」展开成一组候选 Target；
- 处理路径展开（环境变量）、黑名单过滤、符号链接/联接（junction）跳过；
- 计算每个目标的体积与文件数量；
- 对权限错误等异常逐项容错，不中断整体扫描。

绝不删除任何东西——扫描只负责"发现并测量"，删除交给 engine.py。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from .models import CategoryResult, Target, TargetAction, TargetKind, format_size
from .rules import (
    DEFAULT_SKIP_DIRNAMES,
    category_label,
    get_protected_patterns,
)


# ---------------------------------------------------------------------------
# 路径与保护判断
# ---------------------------------------------------------------------------
def expand_path(path_str: str) -> Path:
    """扩展环境变量与 ~，返回绝对 Path。"""
    expanded = os.path.expandvars(os.path.expanduser(path_str))
    return Path(expanded).absolute()


def normalize(path: Path) -> str:
    """返回用于大小写不敏感比较的规范化字符串。"""
    return os.path.normcase(str(path))


def make_protect_check(user_patterns: list[str] | None = None):
    """返回一个 ``(path) -> bool`` 的保护判断函数。

    True 表示该路径受保护（应跳过）。子串匹配，大小写不敏感。
    """
    patterns = [normalize(p) for p in get_protected_patterns()]
    if user_patterns:
        patterns.extend(normalize(p) for p in user_patterns)

    def _check(path: Path) -> bool:
        try:
            s = normalize(path)
        except (OSError, ValueError):
            return True  # 无法判断的路径，保守跳过
        return any(p in s for p in patterns if p)

    return _check


# ---------------------------------------------------------------------------
# 遍历
# ---------------------------------------------------------------------------
def _walk_dir(
    root: Path,
    is_protected,
    skip_names: set[str] | None = None,
    max_depth: int | None = None,
    find_names: set[str] | None = None,
) -> Iterator[tuple[Path, bool]]:
    """安全遍历目录树，产出 (路径, is_dir)。

    - 不跟随符号链接/联接（junction）；
    - 跳过受保护目录、以及 skip_names 中的目录名；
    - ``find_names``：这些目录名本应被 skip 跳过，但需作为目标被**发现**；
      命中的目录只产出、不再下探，避免放大遍历进巨大目录；
    - max_depth 限制深度（根为 0）。
    """
    skip = skip_names or DEFAULT_SKIP_DIRNAMES
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
                        # 不跟随符号链接/联接，避免越界误删或造成指数级扫描
                        is_link = entry.is_symlink()
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


def _dir_size(path: Path, is_protected) -> tuple[int, int]:
    """计算目录内可清理的字节数与文件数量（不跟随链接、跳过受保护项）。"""
    total = 0
    count = 0
    for child, is_dir in _walk_dir(path, is_protected, max_depth=None):
        if is_dir:
            continue
        try:
            total += child.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count


# ---------------------------------------------------------------------------
# 各类 target 的扫描实现
# ---------------------------------------------------------------------------
def _scan_clear_dir(
    spec: dict[str, Any], category_key: str, is_protected
) -> list[Target]:
    base = expand_path(spec["path"])
    if not base.exists() or not base.is_dir():
        return []
    if is_protected(base):
        return []
    size, count = _dir_size(base, is_protected)
    if size == 0 and count == 0:
        return []
    return [
        Target(
            path=base,
            kind=TargetKind.DIR,
            action=TargetAction.CLEAR,
            category=category_key,
            size=size,
            file_count=count,
        )
    ]


def _scan_delete_dir(
    spec: dict[str, Any], category_key: str, is_protected
) -> list[Target]:
    base = expand_path(spec["path"])
    if not base.exists() or not base.is_dir():
        return []
    if is_protected(base):
        return []
    size, count = _dir_size(base, is_protected)
    return [
        Target(
            path=base,
            kind=TargetKind.DIR,
            action=TargetAction.DELETE,
            category=category_key,
            size=size,
            file_count=count,
        )
    ]


def _scan_glob_dirs(spec: dict[str, Any], category_key: str, is_protected) -> list[Target]:
    base_str = spec.get("base", "")
    if not base_str:
        return []
    base = expand_path(base_str)
    if not base.exists():
        return []
    pattern = spec.get("pattern", "*")
    action = spec.get("action", "clear")
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
        size, count = _dir_size(m, is_protected)
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
            )
        )
    return targets


def _scan_glob_files(spec: dict[str, Any], category_key: str, is_protected) -> list[Target]:
    base_str = spec.get("base", "")
    if not base_str:
        return []
    base = expand_path(base_str)
    if not base.exists():
        return []
    pattern = spec.get("pattern", "*")
    targets: list[Target] = []
    try:
        matches = base.glob(pattern)
    except (OSError, ValueError):
        return []
    for m in matches:
        try:
            if not m.is_file():
                continue
        except OSError:
            continue
        if is_protected(m):
            continue
        try:
            size = m.stat().st_size
        except OSError:
            continue
        targets.append(
            Target(
                path=m,
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category=category_key,
                size=size,
                file_count=1,
            )
        )
    return targets


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


def _scan_find_dirs(spec: dict[str, Any], category_key: str, is_protected) -> list[Target]:
    names = {n.lower() for n in spec.get("names", [])}
    bases = _resolve_bases(spec)
    if not bases:
        return []
    action = TargetAction.DELETE if spec.get("action", "delete") == "delete" else TargetAction.DELETE
    seen: set[str] = set()
    targets: list[Target] = []
    for base in bases:
        if not base.exists() or not base.is_dir():
            continue
        if is_protected(base):
            continue
        # 只遍历一层找到候选目录名；找到后不下降（它们本身就是待删目标）
        for child, is_dir in _walk_dir(base, is_protected, max_depth=12, find_names=names):
            if not is_dir:
                continue
            if child.name.lower() in names:
                key = normalize(child)
                if key in seen:
                    continue
                seen.add(key)
                size, count = _dir_size(child, is_protected)
                targets.append(
                    Target(
                        path=child,
                        kind=TargetKind.DIR,
                        action=action,
                        category=category_key,
                        size=size,
                        file_count=count,
                    )
                )
    return targets


def _scan_files_by_rule(spec: dict[str, Any], category_key: str, is_protected) -> list[Target]:
    base_str = spec.get("base", "")
    if not base_str:
        return []
    import time

    base = expand_path(base_str)
    if not base.exists() or not base.is_dir():
        return []
    min_size_bytes = int(spec.get("min_size_mb", 0) or 0) * 1024 * 1024
    older_than_secs = int(spec.get("older_than_days", 0) or 0) * 86400
    now = time.time()
    targets: list[Target] = []
    for child, is_dir in _walk_dir(base, is_protected):
        if is_dir:
            continue
        if is_protected(child):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        # 只清理「既达到最小体积、又足够老旧」的文件：任一条件不满足即跳过
        if st.st_size < min_size_bytes or (now - st.st_mtime) < older_than_secs:
            continue
        targets.append(
            Target(
                path=child,
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category=category_key,
                size=st.st_size,
                file_count=1,
            )
        )
    return targets


# target 类型 -> 处理函数
_TARGET_HANDLERS = {
    "clear_dir": _scan_clear_dir,
    "delete_dir": _scan_delete_dir,
    "glob_dirs": _scan_glob_dirs,
    "glob_files": _scan_glob_files,
    "find_dirs": _scan_find_dirs,
    "files_by_rule": _scan_files_by_rule,
}


def scan_spec(spec: dict[str, Any], is_protected=None) -> CategoryResult:
    """扫描单个分类规格，返回 CategoryResult。"""
    key = spec["key"]
    if is_protected is None:
        is_protected = make_protect_check()
    result = CategoryResult(key=key, label=spec.get("label") or category_label(key))
    seen: set[str] = set()
    for target_spec in spec.get("targets", []):
        ttype = target_spec.get("type")
        handler = _TARGET_HANDLERS.get(ttype)
        if handler is None:
            continue
        try:
            targets = handler(target_spec, key, is_protected)
        except Exception:  # noqa: BLE001 单个 target 扫描失败不影响整体
            result.skipped += 1
            continue
        for t in targets:
            tpath = normalize(t.path)
            if tpath in seen:
                continue
            seen.add(tpath)
            result.targets.append(t)
    result.scanned = True
    return result


def scan_all(specs: list[dict[str, Any]]) -> tuple[list[CategoryResult], None]:
    """扫描所有分类（不含回收站），返回 (结果列表, None)。

    ``recycle_bin`` 由 CLI 特殊处理，不在此扫描。
    """
    is_protected = make_protect_check()
    results: list[CategoryResult] = []
    for spec in specs:
        if spec.get("key") == "recycle_bin":
            continue
        results.append(scan_spec(spec, is_protected))
    return results, None


def print_report(results: list[CategoryResult]) -> None:
    """人类可读的扫描结果输出。"""
    total_ref = 0
    print("已扫描候选清理目标：")
    print("-" * 56)
    for res in results:
        if not res.scanned or not res.targets:
            print(f"  {res.label:<12} (0 项)")
            continue
        total_ref += res.liberatable
        print(f"  {res.label:<12} {res.total_count} 项, 可释放 {format_size(res.liberatable)}")
    print("-" * 56)
    print(f"  合计可释放: {format_size(total_ref)}")
