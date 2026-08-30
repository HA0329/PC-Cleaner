"""交互式清理菜单、预览/汇总展示与清理执行流程。

从 cli.py 拆分出来的「交互层」：负责把扫描结果展示成菜单、预览待删目标、
执行清理流程（预览 -> 确认 -> 删除 -> 记历史）。参数解析与子命令管理
仍在 cli.py / commands.py。

热重载：交互菜单每轮循环重新读取配置与 rules.json（``get_enabled_category_specs``），
编辑 ``rules.json`` / ``config.json`` 后无需重启程序，下一次扫描即生效。
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import Any

from . import __version__
from .config import load_config
from .console import (
    bold, cyan, dim, green, red, yellow,
    pad_cjk, display_width, truncate_path, get_terminal_width,
)
from .engine import (
    CleanMode,
    delete_targets,
    empty_recycle_bin,
    recycle_available,
)
from .history import append_session, make_session, record_deletion_audit
from .models import CategoryResult, TargetKind, format_size
from .rules import get_enabled_category_specs
from .scanner import is_admin, recycle_bin_size, scan_all
from .ui import (
    ScanProgressDisplay,
    _admin_tag,
    _echo,
    _risk_badge,
    prompt_yes_no,
)


# ---------------------------------------------------------------------------
# 高级清理过滤（--ext / --min-size-mb / --older-than-days / --shred-passes）
# ---------------------------------------------------------------------------
def _parse_ext_filter(raw: str | None) -> list[str] | None:
    """把 ``--ext`` 的值解析成小写、带点号的扩展名列表。"""
    if not raw:
        return None
    exts: list[str] = []
    for e in raw.replace("，", ",").replace("；", ",").split(","):
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        if e not in exts:
            exts.append(e)
    return exts or None


def _apply_target_filters(
    targets: list[Any],
    *,
    ext_filter: list[str] | None = None,
    min_size_bytes: int | None = None,
    older_than_secs: int | None = None,
) -> list[Any]:
    """高级清理过滤：按扩展名 / 最小体积 / 最旧修改时间筛选目标。

    - 扩展名过滤只作用于文件目标（目录目标始终保留）；
    - 最小体积作用于所有目标（目录按内部总字节数比较）；
    - 最旧修改时间只作用于文件目标。
    """
    if not any((ext_filter, min_size_bytes, older_than_secs)):
        return targets
    now = time.time()
    out: list[Any] = []
    for t in targets:
        is_dir = t.kind is TargetKind.DIR
        if not is_dir and ext_filter and t.path.suffix.lower() not in ext_filter:
            continue
        if min_size_bytes is not None and t.size < min_size_bytes:
            continue
        if not is_dir and older_than_secs is not None:
            try:
                if (now - t.path.stat().st_mtime) < older_than_secs:
                    continue
            except OSError:
                continue
        out.append(t)
    return out


def _filters_from_args(args) -> tuple[list[str] | None, int | None, int | None, int]:
    """从命令行参数解析出高级清理过滤参数。

    返回 ``(ext_filter, min_size_bytes, older_than_secs, shred_passes)``。
    """
    ext_filter = _parse_ext_filter(getattr(args, "ext", None))
    min_size_bytes = None
    if getattr(args, "min_size_mb", None):
        min_size_bytes = int(args.min_size_mb) * 1024 * 1024
    older_than_secs = None
    if getattr(args, "older_than_days", None):
        older_than_secs = int(args.older_than_days) * 86400
    shred_passes = int(getattr(args, "shred_passes", 1) or 1)
    return ext_filter, min_size_bytes, older_than_secs, shred_passes


# ---------------------------------------------------------------------------
# 预览 / 展示
# ---------------------------------------------------------------------------
def _preview_targets(
    targets: list[Any],
    max_lines: int = 12,
    sort_by: str = "size_desc",
    compact: bool = False,
) -> None:
    """预览一组目标，支持排序和完整展示。

    compact: 紧凑模式，只显示路径和大小
    """
    if not targets:
        _echo("  （无内容）")
        return

    # 排序
    if sort_by == "size_desc":
        ordered = sorted(targets, key=lambda t: t.size, reverse=True)
    elif sort_by == "size_asc":
        ordered = sorted(targets, key=lambda t: t.size)
    elif sort_by == "name_asc":
        ordered = sorted(targets, key=lambda t: str(t.path).lower())
    elif sort_by == "name_desc":
        ordered = sorted(targets, key=lambda t: str(t.path).lower(), reverse=True)
    elif sort_by == "count_desc":
        ordered = sorted(targets, key=lambda t: t.file_count, reverse=True)
    else:
        ordered = sorted(targets, key=lambda t: t.size, reverse=True)

    shown = ordered[:max_lines] if max_lines > 0 else ordered
    hidden = len(ordered) - len(shown)

    for i, t in enumerate(shown, start=1):
        if compact:
            _echo(f"    {cyan(str(i).rjust(4))}. {t.kind_icon} {truncate_path(str(t.path), 60)}  {green(t.display_size)}")
        else:
            _echo(f"    {cyan(str(i).rjust(4))}. {t.describe_compact()}")

    if hidden > 0:
        _echo(f"    {dim(f'      ... 以及另外 {hidden} 项')}")
        _echo(f"    {dim('      💡 提示：使用 --detail 或交互菜单中按 d 查看完整列表')}")

    # 汇总
    total_size = sum(t.size for t in ordered)
    _echo(f"    {dim('─' * 56)}")
    _echo(f"    总计: {len(ordered)} 项, 可释放 {green(format_size(total_size))}")


def _print_summary_table(results: list[CategoryResult], bin_size: int = 0) -> None:
    """打印分类汇总表（表格式）。列宽统一，边框动态生成。"""
    # ---- 列宽定义（显示宽度，不含两侧空格）----
    COL_NO = 5        # 编号，如 " 1."
    COL_CAT = 24      # 分类名（含风险标记 ● + 空格）
    COL_TARGET = 12   # 目标数，如 "18 个目标"
    COL_FILE = 8      # 文件数
    COL_SIZE = 14     # 可释放空间，如 "106.42 MB"

    # 边框总宽 = 前缀"  │ "(4) + 各列 + 分隔符" │ "(3)×4 + 后缀" │"(2)
    BORDER_W = 4 + COL_NO + 3 + COL_CAT + 3 + COL_TARGET + 3 + COL_FILE + 3 + COL_SIZE + 2

    def _rjust(text: str, w: int) -> str:
        """按显示宽度右对齐（传入纯文本，不含 ANSI 码）。"""
        pad = w - display_width(text)
        return " " * max(pad, 0) + text

    def _cat_cell(badge: str, label: str) -> str:
        """构造固定宽度的分类单元格：badge(带颜色) + 空格 + label(pad/截断)。"""
        avail = COL_CAT - 3  # badge 显示宽 2 + 分隔空格 1
        lbl = label
        if display_width(lbl) > avail:
            # 截断并加省略号（按字符粗略截断，确保不超宽）
            while display_width(lbl) > avail - 1:
                lbl = lbl[:-1]
            lbl = lbl + "…"
        else:
            lbl = pad_cjk(lbl, avail)
        return f"{badge} {lbl}"

    def _row(no: str, cat_cell: str, target: str, file_cnt: str, size: str,
             no_color=None, size_color=None) -> str:
        """组装一行。no/size 先按显示宽度 pad，再应用颜色。"""
        no_padded = _rjust(no, COL_NO)
        size_padded = _rjust(size, COL_SIZE)
        no_disp = no_color(no_padded) if no_color else no_padded
        size_disp = size_color(size_padded) if size_color else size_padded
        return (
            f"  │ {no_disp} "
            f"│ {cat_cell} "
            f"│ {_rjust(target, COL_TARGET)} "
            f"│ {_rjust(file_cnt, COL_FILE)} "
            f"│ {size_disp} │"
        )

    _echo("")
    _echo(bold(f"  ┌{'─' * BORDER_W}┐"))
    # 表头（纯文本居中/左对齐）
    _echo(bold(
        f"  │ {_rjust('#', COL_NO)} "
        f"│ {pad_cjk('分类', COL_CAT)} "
        f"│ {pad_cjk('目标数', COL_TARGET)} "
        f"│ {pad_cjk('文件数', COL_FILE)} "
        f"│ {pad_cjk('可释放空间', COL_SIZE)} │"
    ))
    _echo(bold(f"  ├{'─' * BORDER_W}┤"))

    total_size = 0
    total_targets = 0
    total_files = 0

    for i, res in enumerate(results, start=1):
        badge = _risk_badge(res.risk)
        no_str = f"{i}."
        cat = _cat_cell(badge, res.label)

        if res.admin_blocked:
            _echo(_row(no_str, cat, "需管理员", "", "",
                       no_color=cyan))
            continue

        if not res.targets:
            _echo(_row(no_str, cat, "0 项", "0", "0 B",
                       no_color=cyan))
            continue

        cat_size = res.liberatable
        cat_count = res.total_count
        cat_targets = len(res.targets)
        total_size += cat_size
        total_targets += cat_targets
        total_files += cat_count

        _echo(_row(
            no_str, cat,
            f"{cat_targets} 个目标",
            str(cat_count),
            format_size(cat_size),
            no_color=cyan, size_color=green,
        ))

    _echo(bold(f"  ├{'─' * BORDER_W}┤"))
    # 合计行
    total_cat = pad_cjk("合计", COL_CAT)
    _echo(
        f"  │ {_rjust('', COL_NO)} "
        f"│ {bold(total_cat)} "
        f"│ {_rjust(f'{total_targets} 个目标', COL_TARGET)} "
        f"│ {_rjust(str(total_files), COL_FILE)} "
        f"│ {green(_rjust(format_size(total_size), COL_SIZE))} │"
    )
    _echo(bold(f"  └{'─' * BORDER_W}┘"))

    if bin_size > 0:
        _echo(f"  回收站占用: {red(format_size(bin_size))}")


def _print_full_menu(results: list[CategoryResult], bin_size: int, show_risky: bool, sort_by: str = "size_desc") -> None:
    _echo("")
    _echo(bold("可清理的分类："))
    for i, res in enumerate(results, start=1):
        badge = _risk_badge(res.risk)
        tag = _admin_tag(res.requires_admin)
        if res.admin_blocked:
            _echo(
                f"  {cyan(str(i))}. {badge} {res.label}{tag}  "
                f"({yellow('需管理员权限，当前未提权，已跳过')})"
            )
        elif res.targets:
            _echo(
                f"  {cyan(str(i))}. {badge} {res.label}{tag}  "
                f"({res.total_count} 项, 约 {green(format_size(res.liberatable))})"
            )
        else:
            _echo(f"  {cyan(str(i))}. {badge} {res.label}{tag}  (0 项)")

    if bin_size > 0:
        _echo(f"  {red('r')}. 回收站 (清空, 占用 {yellow(format_size(bin_size))})")
    else:
        _echo("  r. 回收站 (清空)")
    _echo("  q. 退出")

    _echo("")
    _echo(dim("  操作："))
    _echo(dim("    d <编号>  → 查看该分类详细目录列表    t <编号>  → 树形视图"))
    _echo(dim("    s <方式>  → 切换排序(size/name/count)  x        → 切换高风险分类"))
    _echo("")


def _print_detail_for_category(res: CategoryResult, sort_by: str = "size_desc") -> None:
    """打印单个分类的详细目标列表。"""
    _echo("")
    if res.risk == "safe":
        badge = green("●")
    elif res.risk == "moderate":
        badge = yellow("●")
    else:
        badge = red("●")

    _echo(bold(f"  {badge} {res.label} 详细列表"))
    if res.admin_blocked:
        _echo(f"    {yellow('⚠ 需管理员权限，未扫描')}")
        return

    if not res.targets:
        _echo(f"    （无目标）")
        return

    _echo(dim(f"    共 {len(res.targets)} 个目标, {res.total_count} 个文件, 可释放 {green(format_size(res.liberatable))}"))
    _echo(dim(f"    {'─' * 70}"))

    _preview_targets(res.targets, max_lines=0, sort_by=sort_by, compact=False)


def _parse_selection(raw: str, n: int) -> set[int] | str:
    """解析用户输入的分类选择。

    返回集合（选中的索引 1..n），或字符串 'all' / 'none'。
    """
    tokens = [t.strip().lower() for t in raw.replace("，", ",").split(",") if t.strip()]
    if not tokens:
        return set()
    sel: set[int] = set()
    for tok in tokens:
        if tok in ("all", "a", "*"):
            return "all"
        if tok in ("none", "n", "0"):
            return "none"
        try:
            idx = int(tok)
        except ValueError:
            continue
        if 1 <= idx <= n:
            sel.add(idx)
    return sel


def _print_disk_free(results: list[CategoryResult]) -> None:
    """展示涉及盘符的当前可用空间。"""
    drives = sorted({t.path.anchor for r in results for t in r.targets})
    parts: list[str] = []
    for d in drives:
        try:
            u = shutil.disk_usage(d)
            used_pct = u.used / u.total * 100 if u.total > 0 else 0
            # 根据使用率着色
            if used_pct > 90:
                size_str = red(format_size(u.free))
            elif used_pct > 75:
                size_str = yellow(format_size(u.free))
            else:
                size_str = green(format_size(u.free))
            parts.append(f"{d} 可用 {size_str} ({used_pct:.0f}% 已用)")
        except OSError:
            continue
    if parts:
        _echo(dim("磁盘可用: " + " | ".join(parts)))


# ---------------------------------------------------------------------------
# 主清理流程
# ---------------------------------------------------------------------------
def _collect_targets(selected: list[CategoryResult]) -> list[Any]:
    """把多个分类的 Target 合并为一组。"""
    out: list[Any] = []
    for res in selected:
        out.extend(res.targets)
    return out


def _progress_line(i: int, total: int, msg: str) -> None:
    # 紧凑进度，使用 stderr 避免污染 stdout 的 --json 输出
    term_w = get_terminal_width()
    prefix = f"\r[进度] {i}/{total}  "
    prefix_w = len(prefix)
    available = max(term_w - prefix_w - 1, 20)

    # 分离操作前缀（如 "[目录-清空] "）和路径，对路径做智能截断
    if "] " in msg:
        op_tag, path = msg.split("] ", 1)
        op_tag += "] "
        path_w = max(available - len(op_tag), 10)
        msg_clean = op_tag + truncate_path(path, path_w)
    else:
        msg_clean = truncate_path(msg, available)

    print(f"{prefix}{msg_clean}", end="", file=sys.stderr, flush=True)
    if i >= total:
        print(file=sys.stderr, flush=True)


def _run_clean_flow(
    selected: list[CategoryResult],
    *,
    dry_run: bool,
    mode: CleanMode,
    auto_confirm: bool,
    empty_bin: bool,
    cfg: dict[str, Any],
    recycle_fallback: bool = False,
    shred: bool = False,
    shred_passes: int = 1,
    ext_filter: list[str] | None = None,
    min_size_bytes: int | None = None,
    older_than_secs: int | None = None,
) -> dict[str, Any]:
    """对选中的分类执行：预览 -> 确认 -> 删除。"""
    targets = _collect_targets(selected)
    targets = _apply_target_filters(
        targets,
        ext_filter=ext_filter,
        min_size_bytes=min_size_bytes,
        older_than_secs=older_than_secs,
    )
    max_lines = int(cfg.get("preview_lines", 12) or 12)
    sort_by = cfg.get("default_sort", "size_desc")

    _echo("")
    if targets:
        _echo(bold("将删除以下内容（预览）："))
        for res in selected:
            if res.targets:
                _echo(f"  【{res.label}】")
                _preview_targets(res.targets, max_lines=max_lines, sort_by=sort_by)

    if empty_bin and not dry_run:
        _echo(f"  【回收站】将清空回收站（{red('不可恢复')}）。")

    if shred and not dry_run and mode is CleanMode.PERMANENT:
        passes = max(1, min(int(shred_passes or 1), 7))
        _echo(yellow(f"  shred 模式：永久删除前将随机覆写文件内容 {passes} 遍。"))

    if dry_run:
        _echo("")
        _echo(yellow("dry-run 模式，未删除任何内容。"))
        return {"dry_run": True}

    # 确认
    if not auto_confirm:
        _echo("")
        if targets:
            if not prompt_yes_no("确认删除以上内容？", default=False):
                _echo("已取消。")
                return {"cancelled": True}
        if empty_bin:
            if not prompt_yes_no("确认清空回收站？", default=False):
                _echo("已取消清空回收站。")
                empty_bin = False

    # 执行
    enable_history = bool(cfg.get("enable_history", True))
    audit_log = None
    if enable_history:
        def audit_log(path, size, mode_name) -> None:
            record_deletion_audit(path, size, mode_name)

    result: dict[str, Any] = {"deleted": 0, "failed": 0, "freed": 0, "empty_bin": empty_bin}
    if targets:
        res = delete_targets(
            targets,
            mode,
            on_progress=_progress_line,
            recycle_fallback=recycle_fallback,
            shred=shred,
            shred_passes=shred_passes,
            audit=audit_log,
        )
        result["deleted"] += res["deleted"]
        result["failed"] += res["failed"]
        result["freed"] += res["freed"]

    if empty_bin:
        bin_res = empty_recycle_bin()
        result["empty_bin_result"] = bin_res
        _echo("回收站清空完成。")

    # 记录历史会话（--history / --undo-last 使用）
    if enable_history and (targets or empty_bin):
        session = make_session(
            mode=mode.value,
            deleted=result["deleted"],
            failed=result["failed"],
            freed=result["freed"],
            categories=[r.key for r in selected] + (["recycle_bin"] if empty_bin else []),
            targets=[{"path": str(t.path), "size": t.size} for t in targets],
            note="shred" if shred else "",
        )
        append_session(session)

    _echo("")
    _echo(
        f"完成：删除 {green(str(result['deleted']))} 项, "
        f"跳过/失败 {yellow(str(result['failed']))} 项, "
        f"释放约 {green(format_size(result['freed']))}。"
    )
    _print_disk_free(selected)
    return result


# ---------------------------------------------------------------------------
# 交互式菜单
# ---------------------------------------------------------------------------
def _interactive(
    results: list[CategoryResult],
    specs: list[dict[str, Any]],
    mode: CleanMode,
    args,
    cfg: dict[str, Any],
    show_risky: bool,
    sort_by: str = "size_desc",
    scan_depth: int = 20,
    show_progress: bool = True,
    deep: bool = False,
) -> int:
    """交互式菜单：选择 -> 预览 -> 确认 -> 清理，可循环继续。

    ``deep``：传入 ``--deep`` 标志，每轮循环重新加载 rules.json / 配置
    （热重载：编辑规则或配置后无需重启，下一轮扫描即生效）。
    """
    from .scanner import print_report, print_tree_report

    _echo("")
    _echo(bold(f"=== PC Junk Cleaner v{__version__} ==="))
    if recycle_available():
        _echo(green("  回收站支持: 是（删除可恢复）"))
    else:
        _echo(yellow("  回收站支持: 否（将永久删除，请谨慎！建议 pip install send2trash）"))
    if is_admin():
        _echo(green("  管理员权限: 是"))
    else:
        _echo(yellow("  管理员权限: 否（系统深度清理分类将跳过，可用 --admin 提权）"))

    while True:
        # 热重载：每轮重新读取配置与 rules.json，修改无需重启即可生效
        cfg = load_config()
        specs = get_enabled_category_specs(cfg, deep=deep)

        _print_summary_table(results)
        _echo("")
        _print_disk_free(results)
        bin_size = recycle_bin_size() if sys.platform == "win32" else 0
        if bin_size > 0:
            _echo(dim(f"  回收站占用: {format_size(bin_size)}"))

        selected: list[CategoryResult] = []
        empty_bin = False
        n = len(results)

        while True:
            _print_full_menu(results, bin_size, show_risky, sort_by)
            choice = ""
            try:
                choice = input("请选择（编号/d+t查看/s排序/x切换/q退出）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if not choice:
                continue

            cmd = choice.lower().split()
            action = cmd[0]

            if action in ("q", "quit", "exit"):
                return 0

            # d <编号> → 查看详细
            if action == "d":
                if len(cmd) > 1:
                    try:
                        idx = int(cmd[1])
                        if 1 <= idx <= n:
                            _print_detail_for_category(results[idx - 1], sort_by)
                        else:
                            _echo(red(f"  编号超出范围（1-{n}）"))
                    except ValueError:
                        _echo(red("  请输入有效编号，如: d 3"))
                else:
                    # 无参数时显示所有分类的详细列表
                    for res in results:
                        _print_detail_for_category(res, sort_by)
                continue

            # t <编号> → 树形视图
            if action == "t":
                if len(cmd) > 1:
                    try:
                        idx = int(cmd[1])
                        if 1 <= idx <= n:
                            print_tree_report([results[idx - 1]])
                        else:
                            _echo(red(f"  编号超出范围（1-{n}）"))
                    except ValueError:
                        _echo(red("  请输入有效编号，如: t 2"))
                else:
                    print_tree_report(results)
                continue

            # s <方式> → 切换排序
            if action == "s":
                if len(cmd) > 1:
                    new_sort = cmd[1]
                    valid_sorts = {"size_desc": "体积↓", "size_asc": "体积↑",
                                  "name_asc": "名称↑", "name_desc": "名称↓",
                                  "count_desc": "文件数↓"}
                    if new_sort in valid_sorts:
                        sort_by = new_sort
                        _echo(green(f"  已切换排序为: {valid_sorts[new_sort]}"))
                    else:
                        _echo(red(f"  无效排序方式。可选: {', '.join(valid_sorts.keys())}"))
                else:
                    # 循环切换排序
                    sort_options = ["size_desc", "size_asc", "name_asc", "count_desc"]
                    sort_labels = {"size_desc": "体积↓", "size_asc": "体积↑",
                                  "name_asc": "名称↑", "count_desc": "文件数↓"}
                    current_idx = sort_options.index(sort_by) if sort_by in sort_options else 0
                    sort_by = sort_options[(current_idx + 1) % len(sort_options)]
                    _echo(green(f"  已切换排序为: {sort_labels[sort_by]}"))
                continue

            # x → 切换高风险显示
            if action in ("x", "risky"):
                show_risky = not show_risky
                if show_risky:
                    _echo(yellow("  已显示高风险分类（删除前请仔细核对预览）。"))
                else:
                    _echo(dim("  已隐藏高风险分类。"))
                progress = ScanProgressDisplay(enabled=show_progress)
                fresh = scan_all(specs, scan_depth=scan_depth, on_progress=progress if show_progress else None)
                progress.finish(fresh)
                results = [r for r in fresh if show_risky or r.risk != "risky"]
                n = len(results)
                continue

            # 数字选择
            sel = _parse_selection(choice, n)
            if sel == "none":
                selected = []
                empty_bin = False
                _echo("  已清空选择。")
                continue
            if sel == "all":
                selected = list(results)
                empty_bin = True
                break
            if sel:
                selected = [results[i - 1] for i in sorted(sel)]
                empty_bin = False
                break

            _echo(red("  无效输入，请重新选择。"))

        # 过滤掉没有内容的分类
        selected = [r for r in selected if r.targets]
        if not selected and not empty_bin:
            _echo(yellow("  所选分类没有可清理的内容。"))
            continue

        ext_filter, min_size_bytes, older_than_secs, shred_passes = _filters_from_args(args)
        _run_clean_flow(
            selected,
            dry_run=args.dry_run,
            mode=mode,
            auto_confirm=False,
            empty_bin=empty_bin,
            cfg=cfg,
            recycle_fallback=args.recycle_fallback or bool(cfg.get("recycle_error_fallback", False)),
            shred=args.shred,
            shred_passes=shred_passes,
            ext_filter=ext_filter,
            min_size_bytes=min_size_bytes,
            older_than_secs=older_than_secs,
        )

        # 循环：重新扫描并继续
        try:
            again = prompt_yes_no("是否继续扫描并清理其他内容？", default=True)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not again:
            return 0
        progress = ScanProgressDisplay(enabled=show_progress)
        fresh = scan_all(specs, scan_depth=scan_depth, on_progress=progress if show_progress else None)
        progress.finish(fresh)
        results = [r for r in fresh if show_risky or r.risk != "risky"]
