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
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import CategoryResult, Target, TargetAction, TargetKind, format_size
from .rules import (
    DEFAULT_SKIP_DIRNAMES,
    category_label,
    get_protected_patterns,
    is_within_clear_root,
    spec_requires_admin,
)

# 扫描进度回调类型
ScanProgressCB = Callable[[str, int, int], None]  # (category_label, current_spec, total_specs)


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


def is_admin() -> bool:
    """当前进程是否拥有管理员权限（Windows）。非 Windows 视为 False。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def make_protect_check(user_patterns: list[str] | None = None):
    """返回一个 ``(path) -> bool`` 的保护判断函数。

    True 表示该路径受保护（应跳过）。子串匹配，大小写不敏感。
    例外：位于 ``ALLOWED_CLEAR_ROOTS`` 白名单目录（含其内部）的路径
    不被视为受保护，从而允许内置规则清空其内容（如 Windows 更新缓存）。
    """
    patterns = [normalize(p) for p in get_protected_patterns()]
    if user_patterns:
        patterns.extend(normalize(p) for p in user_patterns)

    def _check(path: Path) -> bool:
        try:
            s = normalize(path)
        except (OSError, ValueError):
            return True  # 无法判断的路径，保守跳过
        if is_within_clear_root(path):
            return False
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
    spec: dict[str, Any], category_key: str, is_protected
) -> list[Target]:
    base = expand_path(spec["path"])
    if not base.exists() or not base.is_dir():
        return []
    if is_protected(base):
        return []
    size, count = _dir_size(base, is_protected)
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


def _scan_glob_dirs(spec: dict[str, Any], category_key: str, is_protected) -> list[Target]:
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
                label=label,
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
    label = spec.get("label", "")
    # 可选过滤：最小体积 + 足够老旧（与 files_by_rule 同语义，向后兼容）
    min_size_bytes = int(spec.get("min_size_mb", 0) or 0) * 1024 * 1024
    older_than_secs = int(spec.get("older_than_days", 0) or 0) * 86400
    now = time.time()
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
            st = m.stat()
        except OSError:
            continue
        if st.st_size < min_size_bytes or (now - st.st_mtime) < older_than_secs:
            continue
        targets.append(
            Target(
                path=m,
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category=category_key,
                size=st.st_size,
                file_count=1,
                label=label,
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


def _scan_find_dirs(spec: dict[str, Any], category_key: str, is_protected, max_depth: int = 20) -> list[Target]:
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
        for child, is_dir in _walk_dir(base, is_protected, max_depth=max_depth, find_names=names):
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
                        label=label,
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
    label = spec.get("label", "")
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
}


def scan_spec(
    spec: dict[str, Any],
    is_protected=None,
    scan_depth: int = 20,
    on_progress: ScanProgressCB | None = None,
    progress_idx: int = 0,
    progress_total: int = 0,
) -> CategoryResult:
    """扫描单个分类规格，返回 CategoryResult。"""
    key = spec["key"]
    label = spec.get("label") or category_label(key)
    if is_protected is None:
        is_protected = make_protect_check()

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
            handler = None  # 单独处理
            try:
                targets = _scan_find_dirs(target_spec, key, is_protected, max_depth=scan_depth)
            except Exception:  # noqa: BLE001
                result.skipped += 1
                continue
        else:
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
    result.scan_duration = time.time() - t0
    return result


def scan_all(
    specs: list[dict[str, Any]],
    scan_depth: int = 20,
    on_progress: ScanProgressCB | None = None,
) -> list[CategoryResult]:
    """扫描所有分类（不含回收站），返回结果列表。

    ``recycle_bin`` 由 CLI 特殊处理，不在此扫描。
    跨分类做全局去重：同一路径出现在多个分类时只保留第一次出现的 Target。
    ``scan_depth``：find_dirs 遍历深度限制（默认 20 层）。
    ``on_progress``：扫描进度回调 (category_label, current_idx, total)。
    """
    is_protected = make_protect_check()
    results: list[CategoryResult] = []
    seen_paths: set[str] = set()
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
        )
        deduped: list[Target] = []
        for t in res.targets:
            key = normalize(t.path)
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


# ---------------------------------------------------------------------------
# 输出报告
# ---------------------------------------------------------------------------
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
