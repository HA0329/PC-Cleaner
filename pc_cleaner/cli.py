"""命令行界面与交互式清理菜单。

用法原则：
- 加入 ``recycle_bin`` 特殊分类（由 engine.empty_recycle_bin 处理）。
- 默认进入交互式菜单；用 ``--list`` 只扫描、``--clean`` 直接清理指定分类。
- 所有删除前都会先预览并确认（除非显式 ``--yes``）。
- ``--json`` 输出模式默认**只扫描**；要真正删除必须同时给 ``--yes``
  （自动化场景下避免静默误删）。

v0.5 新增：
- ``--detail`` 详细展示所有目标目录/文件
- ``--tree`` 树形视图展示
- ``--sort`` 排序方式（size_desc/size_asc/name_asc/count_desc）
- ``--max-depth`` 控制扫描深度
- ``--export-scan`` 导出扫描结果到 JSON 文件
- 扫描进度实时提示
- 交互式菜单增强：查看详细列表、切换排序、切换树形视图

v0.6 新增：
- ``--deep`` 深度扫描（更大遍历深度 + 启用 deep_only 高级规则）
- ``--ext`` / ``--min-size-mb`` / ``--older-than-days`` 高级清理过滤
- ``--shred-passes`` 多遍安全擦除
- ``--show-rules`` 展示 rules.json 规则、``--validate-rules`` 校验规则格式
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULTS, load_config, save_config, update_config
from .console import (
    bold, cyan, dim, green, red, yellow, magenta,
    pad_cjk, separator, progress_bar, truncate_path,
    box_header, box_footer, display_width, get_terminal_width
)
from .engine import (
    CleanMode,
    delete_targets,
    empty_recycle_bin,
    recycle_available,
    restore_paths,
)
from .history import (
    append_session,
    load_history,
    make_session,
    record_deletion_audit,
)
from .models import CategoryResult, TargetKind, format_size
from .rules import (
    get_all_category_specs,
    is_risky_spec,
    spec_requires_admin,
)
from .scanner import is_admin, recycle_bin_size, scan_all, print_detail_report


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _echo(*args, **kwargs) -> None:
    print(*args, **kwargs)


def _echo_err(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def prompt_yes_no(question: str, default: bool = False) -> bool:
    """询问是/否。返回 bool。"""
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{question} [{hint}] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if raw == "":
            return default
        if raw in ("y", "yes", "是"):
            return True
        if raw in ("n", "no", "否"):
            return False
        _echo("请输入 y 或 n。")


def _wait_key() -> None:
    """回车继续 / 退出。"""
    try:
        if sys.stdin.isatty():
            input("按回车继续，q 退出...")
    except EOFError:
        pass


def _parse_keys(raw: str) -> list[str]:
    """把逗号/中文逗号/分号分隔的 key 列表拆成小写列表。"""
    return [
        k.strip().lower()
        for k in raw.replace("，", ",").replace("；", ",").replace("、", ",").split(",")
        if k.strip()
    ]


def _risk_badge(risk: str) -> str:
    """风险等级彩色徽标。"""
    if risk == "safe":
        return green("●")
    if risk == "moderate":
        return yellow("●")
    return red("●")


def _admin_tag(requires_admin: bool) -> str:
    return yellow(" [需管理员]") if requires_admin else ""


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
# 扫描进度显示
# ---------------------------------------------------------------------------
class ScanProgressDisplay:
    """扫描进度的终端显示控制器。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stderr.isatty()
        self._start_time = None
        self._last_update = ""

    def __call__(self, category_label: str, current: int, total: int):
        if not self.enabled:
            return
        if self._start_time is None:
            self._start_time = time.time()
            _echo_err("")  # 空行开始

        elapsed = time.time() - self._start_time
        bar = progress_bar(current, total, width=25)
        line = f"\r  {dim('🔍 扫描中')} {bar} {cyan(category_label)} {dim(f'{current}/{total} ({elapsed:.1f}s)')}"

        # 只有内容变化才刷新
        if line != self._last_update:
            _echo_err(line, end="", flush=True)
            self._last_update = line

    def finish(self, results: list):
        if not self.enabled:
            return
        elapsed = time.time() - self._start_time if self._start_time else 0
        total_targets = sum(len(r.targets) for r in results)
        total_size = sum(r.liberatable for r in results)
        _echo_err(f"\r  {green('✓')} 扫描完成：找到 {bold(str(total_targets))} 个目标，"
                  f"可释放 {green(format_size(total_size))}，"
                  f"耗时 {dim(f'{elapsed:.1f}s')}")
        _echo_err("")  # 空行


# ---------------------------------------------------------------------------
# 预览 / 菜单
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


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pc-junk-cleaner",
        description="为个人电脑定制的安全垃圾清理工具。",
        epilog="使用 --list 只扫描不删除；默认进入交互式菜单。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 扫描与展示
    scan_group = p.add_argument_group("扫描与展示")
    scan_group.add_argument("--list", "-l", action="store_true", help="仅扫描，不删任何东西")
    scan_group.add_argument("--detail", "-d", action="store_true",
                           help="详细展示所有目标目录/文件（不截断）")
    scan_group.add_argument("--tree", action="store_true",
                           help="以树形视图展示扫描结果")
    scan_group.add_argument("--sort", choices=["size_desc", "size_asc", "name_asc", "count_desc"],
                           default=None, help="排序方式（默认按体积从大到小）")
    scan_group.add_argument("--max-depth", type=int, default=None,
                           help="find_dirs 遍历深度限制（默认 20）")
    scan_group.add_argument("--deep", "-D", action="store_true",
                            help="深度扫描：更大遍历深度 + 启用 deep_only 高级清理规则")
    scan_group.add_argument("--export-scan", metavar="PATH",
                           help="将扫描结果导出为 JSON 文件")
    scan_group.add_argument("--no-progress", action="store_true",
                           help="不显示扫描进度")
    scan_group.add_argument("--json", action="store_true",
                           help="以 JSON 格式输出扫描结果（非交互终端/管道下自动启用）")

    # 清理操作
    clean_group = p.add_argument_group("清理操作")
    clean_group.add_argument("--clean", metavar="KEY[,KEY...]",
                            help="直接清理指定分类（如 system_temp,web_cache；支持 recycle_bin）")
    clean_group.add_argument("--all", action="store_true", help="选中所有分类（含回收站）")
    clean_group.add_argument("--exclude", metavar="KEY[,KEY...]",
                            help="与 --all/--clean 联用：排除指定分类")
    clean_group.add_argument("--dry-run", action="store_true", help="只预览，不真正删除")
    clean_group.add_argument("--recycle", dest="mode", action="store_const",
                            const=CleanMode.RECYCLE, help="删除进回收站（需安装 send2trash）")
    clean_group.add_argument("--permanent", dest="mode", action="store_const",
                            const=CleanMode.PERMANENT, help="永久删除")
    clean_group.add_argument("--recycle-fallback", action="store_true",
                            help="进回收站失败时回退为永久删除（默认保留原文件并计入失败）")
    clean_group.add_argument("--yes", "-y", action="store_true", help="跳过交互确认（谨慎使用）")
    clean_group.add_argument("--risky", action="store_true",
                            help="显示/允许高风险分类")
    clean_group.add_argument("--shred", action="store_true",
                            help="永久删除前随机覆写文件内容一遍（隐私增强）")

    # 高级清理（v0.6）
    adv_group = p.add_argument_group("高级清理")
    adv_group.add_argument("--ext", metavar="EXT[,EXT...]",
                           help="仅清理匹配扩展名的文件（如 .log,.tmp,.bak；目录目标不受影响）")
    adv_group.add_argument("--min-size-mb", type=int, default=None, metavar="MB",
                           help="全局最小体积过滤：只清理 >= 指定 MB 的目标")
    adv_group.add_argument("--older-than-days", type=int, default=None, metavar="DAYS",
                           help="全局最旧修改时间过滤：只清理 >= 指定天数的文件")
    adv_group.add_argument("--shred-passes", type=int, default=1, metavar="N",
                           help="shred 覆写遍数（默认 1，上限 7，需配合 --shred）")

    # 历史与管理
    mgmt_group = p.add_argument_group("历史与管理")
    mgmt_group.add_argument("--history", action="store_true",
                           help="显示清理历史")
    mgmt_group.add_argument("--undo-last", action="store_true",
                           help="恢复最近一次「进回收站」的清理")
    mgmt_group.add_argument("--checkup", action="store_true",
                           help="一键体检：只读汇总各项状态")
    mgmt_group.add_argument("--admin", action="store_true",
                           help="以管理员身份重新启动（UAC 提权）")

    # 配置
    cfg_group = p.add_argument_group("配置")
    cfg_group.add_argument("--export-config", metavar="PATH", help="导出配置到 JSON 文件")
    cfg_group.add_argument("--import-config", metavar="PATH", help="从 JSON 文件导入配置")
    cfg_group.add_argument("--show-config", action="store_true", help="显示当前配置")
    cfg_group.add_argument("--show-rules", action="store_true",
                           help="展示 rules.json 内置清理规则（配合 --deep 显示深度规则）")
    cfg_group.add_argument("--validate-rules", action="store_true",
                           help="校验 rules.json 规则格式")
    cfg_group.add_argument("--version", action="store_true", help="显示版本")

    p.set_defaults(mode=None)
    return p


def _resolve_mode(args, cfg: dict[str, Any]) -> CleanMode:
    """确定删除模式：命令行优先，其次读配置 recycle_by_default。"""
    if args.mode is not None:
        return args.mode
    if recycle_available() and cfg.get("recycle_by_default", True):
        return CleanMode.RECYCLE
    return CleanMode.PERMANENT


# ---------------------------------------------------------------------------
# 子命令：历史 / 撤销 / 体检 / 配置导入导出 / 提权重启
# ---------------------------------------------------------------------------
def _cmd_history() -> int:
    sessions = load_history()
    if not sessions:
        _echo(yellow("暂无清理历史。"))
        return 0
    _echo(bold(f"清理历史（共 {len(sessions)} 次会话，最新在前）："))
    for i, s in enumerate(reversed(sessions), start=1):
        note = f" [{s.get('note')}]" if s.get("note") else ""
        _echo(
            f"  {cyan(str(i))}. {s.get('ts', '?')}  mode={s.get('mode', '?')}{note}"
        )
        _echo(
            f"      删除 {green(str(s.get('deleted', 0)))} 项, "
            f"失败 {yellow(str(s.get('failed', 0)))} 项, "
            f"释放 {green(format_size(s.get('freed', 0)))} | "
            f"分类: {', '.join(s.get('categories') or [])}"
        )
        # 显示本次涉及的目标数
        targets = s.get("targets") or []
        if targets:
            _echo(f"      涉及 {len(targets)} 个目标")
    return 0


def _cmd_undo_last() -> int:
    sessions = load_history()
    if not sessions:
        _echo(yellow("暂无清理历史，无法撤销。"))
        return 0
    last = sessions[-1]
    targets = [t.get("path") for t in (last.get("targets") or []) if t.get("path")]
    if last.get("mode") != CleanMode.RECYCLE.value:
        _echo(yellow("最近一次清理不是「进回收站」模式，无法恢复（永久删除不可撤销）。"))
        return 1
    if not targets:
        _echo(yellow("最近一次会话没有可恢复的文件目标。"))
        return 0
    _echo(
        bold(
            f"将尝试从回收站恢复 {len(targets)} 个目标"
            f"（来自 {last.get('ts', '?')} 的清理）："
        )
    )
    res = restore_paths(targets)
    for p in res["restored"]:
        _echo(f"  {green('✓')} 已恢复: {p}")
    for s in res["skipped"]:
        _echo(f"  {yellow('✗')} 跳过: {s}")
    _echo(f"恢复成功 {len(res['restored'])} 项, 跳过 {len(res['skipped'])} 项。")
    return 0


def _cmd_checkup(
    specs: list[dict[str, Any]],
    show_risky: bool,
    scan_depth: int,
    show_progress: bool,
    deep: bool = False,
) -> int:
    """一键体检：只读汇总。"""
    from .scanner import _system_drives
    from .console import progress_bar

    _echo(bold(f"=== PC Junk Cleaner {__version__} 体检报告 ==="))
    mode_tag = "深度 (deep)" if deep else "标准"
    _echo(dim(f"  扫描模式: {mode_tag} · 遍历深度 {scan_depth} 层"))
    _echo("")

    # 系统信息
    _echo(f"  {bold('系统状态')}")
    _echo(f"    管理员权限: {'✓ ' + green('是') if is_admin() else '✗ ' + yellow('否（系统深度清理将跳过，可用 --admin 提权）')}")
    _echo(f"    回收站支持: {'✓ ' + green('是（删除可恢复）') if recycle_available() else '✗ ' + yellow('否（将永久删除，建议 pip install send2trash）')}")
    _echo("")

    # 磁盘信息
    _echo(f"  {bold('磁盘空间')}")
    for drive in _system_drives():
        try:
            u = shutil.disk_usage(drive)
            used_pct = u.used / u.total * 100 if u.total > 0 else 0
            bar = progress_bar(int(used_pct), 100, width=20)
            if used_pct > 90:
                free_str = red(format_size(u.free))
            elif used_pct > 75:
                free_str = yellow(format_size(u.free))
            else:
                free_str = green(format_size(u.free))
            _echo(f"    {drive} {bar} {used_pct:.0f}% 已用 | 可用 {free_str} / 总 {format_size(u.total)}")
        except OSError:
            continue
    if sys.platform == "win32":
        bin_size = recycle_bin_size()
        if bin_size > 0:
            _echo(f"    回收站: {red(format_size(bin_size))}")
    _echo("")

    # 扫描可清理内容
    _echo(f"  {bold('可清理分类')}")
    progress = ScanProgressDisplay(enabled=show_progress)
    results = scan_all(specs, scan_depth=scan_depth, on_progress=progress if show_progress else None)
    progress.finish(results)

    visible = [r for r in results if show_risky or r.risk != "risky"]

    for r in visible:
        if r.admin_blocked:
            _echo(f"    {_risk_badge(r.risk)} {r.label}{_admin_tag(True)}  需管理员，已跳过")
        elif r.targets:
            _echo(
                f"    {_risk_badge(r.risk)} {r.label}  "
                f"{len(r.targets)} 个目标, {r.total_count} 个文件, "
                f"约 {green(format_size(r.liberatable))}"
            )
        else:
            _echo(f"    {_risk_badge(r.risk)} {r.label}  0 项")

    if not show_risky:
        hidden = [r for r in results if r.risk == "risky"]
        if hidden:
            _echo(dim(f"    （另有 {len(hidden)} 个高风险分类未显示，可用 --risky 查看）"))

    total = sum(r.liberatable for r in visible)
    _echo(separator("─"))
    _echo(f"    合计可释放: {green(format_size(total))}")

    sessions = load_history()
    if sessions:
        last = sessions[-1]
        _echo(dim(f"    上次清理: {last.get('ts', '?')} 释放 {format_size(last.get('freed', 0))}"))
    _echo("")
    return 0


def _cmd_export_config(path: str) -> int:
    try:
        Path(path).write_text(
            json.dumps(load_config(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        _echo(red(f"导出配置失败: {exc}"))
        return 1
    _echo(green(f"配置已导出到: {path}"))
    return 0


def _cmd_import_config(path: str) -> int:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _echo(red(f"读取配置文件失败: {exc}"))
        return 1
    if not isinstance(data, dict):
        _echo(red("配置必须是 JSON 对象。"))
        return 1
    patch = {k: v for k, v in data.items() if k in DEFAULTS}
    update_config(patch)
    _echo(green(f"已导入 {len(patch)} 个配置项: {', '.join(sorted(patch))}"))
    return 0


def _cmd_show_rules(deep: bool = False) -> int:
    """可视化展示 rules.json 中的内置清理规则。"""
    from .rules import _builtin_specs

    specs = _builtin_specs(deep=deep)
    _echo("")
    _echo(
        bold(
            f"内置清理规则（rules.json · {len(specs)} 个分类"
            + (" · 深度模式" if deep else "")
            + "）："
        )
    )
    for cat in specs:
        badge = _risk_badge(cat.get("risk", "safe"))
        admin = yellow(" [需管理员]") if cat.get("require_admin") else ""
        deep_tag = magenta(" [deep_only]") if cat.get("deep_only") else ""
        _echo(
            f"  {badge} {bold(cat.get('label', ''))}  "
            f"{cyan(cat.get('key', ''))}{admin}{deep_tag}"
        )
        targets = cat.get("targets", [])
        _echo(f"    {dim(f'{len(targets)} 个目标')}")
        for t in targets:
            ttype = t.get("type", "?")
            label = t.get("label", "")
            d_tag = magenta(" [deep]") if t.get("deep_only") else ""
            loc = (
                t.get("path")
                or t.get("base")
                or (", ".join(t.get("bases", [])) if t.get("bases") else "")
                or "-"
            )
            extra = []
            if t.get("pattern"):
                extra.append(f"pattern={t.get('pattern')}")
            if t.get("action"):
                extra.append(f"action={t.get('action')}")
            if t.get("min_size_mb") is not None:
                extra.append(f"min_size_mb={t.get('min_size_mb')}")
            if t.get("older_than_days") is not None:
                extra.append(f"older_than_days={t.get('older_than_days')}")
            if t.get("names"):
                extra.append(f"names={','.join(t.get('names'))}")
            suffix = f"  [{', '.join(extra)}]" if extra else ""
            _echo(f"      - {dim(ttype)}{suffix}{d_tag}  {loc}  {dim(label)}")
        _echo("")
    _echo(dim("提示：直接编辑 pc_cleaner/rules.json 增删规则；改完用 --validate-rules 校验。"))
    return 0


def _cmd_validate_rules() -> int:
    """校验 rules.json 规则格式。"""
    from .rules import _builtin_specs, validate_rules

    specs = _builtin_specs(deep=True)
    errors = validate_rules(specs)
    if errors:
        _echo(red(f"规则校验失败：发现 {len(errors)} 个问题："))
        for e in errors:
            _echo(f"  {red('✗')} {e}")
        return 1
    total_targets = sum(len(c.get("targets", [])) for c in specs)
    deep_only = sum(
        1 for c in specs for t in c.get("targets", []) if t.get("deep_only")
    )
    _echo(
        green(
            f"✓ 规则校验通过：{len(specs)} 个分类，{total_targets} 个目标，"
            f"其中 {deep_only} 个 deep_only。"
        )
    )
    return 0


def _cmd_export_scan(results: list[CategoryResult], path: str) -> int:
    """将扫描结果导出为 JSON 文件。"""
    data = {
        "version": __version__,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_size_bytes": sum(r.liberatable for r in results),
        "total_targets": sum(len(r.targets) for r in results),
        "categories": []
    }
    for r in results:
        cat_data = {
            "key": r.key,
            "label": r.label,
            "risk": r.risk,
            "requires_admin": r.requires_admin,
            "admin_blocked": r.admin_blocked,
            "target_count": len(r.targets),
            "file_count": r.total_count,
            "size_bytes": r.liberatable,
            "size": format_size(r.liberatable),
            "targets": [
                {
                    "path": str(t.path),
                    "kind": t.kind.value,
                    "action": t.action.value,
                    "label": t.label,
                    "size_bytes": t.size,
                    "size": t.display_size,
                    "file_count": t.file_count,
                }
                for t in r.targets
            ]
        }
        data["categories"].append(cat_data)

    try:
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        _echo(red(f"导出扫描结果失败: {exc}"))
        return 1
    _echo(green(f"扫描结果已导出到: {path}"))
    _echo(f"  共 {data['total_targets']} 个目标, 可释放 {green(format_size(data['total_size_bytes']))}")
    return 0


def _relaunch_as_admin(argv: list[str]) -> int:
    """通过 UAC 以管理员身份重新启动（Windows）。"""
    import ctypes

    args = [a for a in argv if a != "--admin"]
    params = f"-m pc_cleaner {' '.join(args)}".strip()
    _echo(yellow("请求管理员权限（UAC），将重新启动..."))
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        if result <= 32:
            _echo(red("提权启动失败，请手动以管理员身份运行。"))
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001
        _echo(red(f"提权失败: {exc}"))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        _echo(f"pc-junk-cleaner {__version__}")
        return 0

    if args.show_config:
        path = save_config(load_config())
        _echo(f"配置文件: {path}")
        _echo(json.dumps(load_config(), ensure_ascii=False, indent=2))
        return 0

    if args.validate_rules:
        return _cmd_validate_rules()
    if args.show_rules:
        return _cmd_show_rules(deep=args.deep)

    cfg = load_config()

    if args.export_config:
        return _cmd_export_config(args.export_config)
    if args.import_config:
        return _cmd_import_config(args.import_config)
    if args.history:
        return _cmd_history()
    if args.undo_last:
        return _cmd_undo_last()

    # 需管理员权限时经 UAC 提权重启（Windows）
    if args.admin and not is_admin():
        return _relaunch_as_admin(argv if argv is not None else sys.argv[1:])

    specs = get_all_category_specs(merge_custom=True, deep=args.deep)

    # 配置可限制只扫描部分分类
    enabled = [k.lower() for k in (cfg.get("enabled_categories") or []) if k]
    if enabled:
        specs = [s for s in specs if s["key"].lower() in enabled]

    show_risky = args.risky or bool(cfg.get("show_risky", False))
    mode = _resolve_mode(args, cfg)
    excluded = set(_parse_keys(args.exclude)) if args.exclude else set()

    # 排序方式：命令行优先，其次配置
    sort_by = args.sort or cfg.get("default_sort", "size_desc")

    # 扫描深度（--deep 模式使用更大深度，除非显式 --max-depth）
    scan_depth = args.max_depth if args.max_depth is not None else int(cfg.get("scan_depth", 20))
    if args.deep and args.max_depth is None:
        scan_depth = max(scan_depth, 50)

    # 高级清理过滤参数
    ext_filter, min_size_bytes, older_than_secs, shred_passes = _filters_from_args(args)

    # 是否显示进度
    show_progress = not args.no_progress and cfg.get("show_scan_progress", True)

    # 是否详细展示
    show_detail = args.detail or cfg.get("default_detail", False)
    show_tree = args.tree or cfg.get("compact_tree_view", False)

    # 管道输出时自动切换 JSON
    if (
        not args.json
        and not sys.stdout.isatty()
        and not (args.clean or args.all)
        and not args.checkup
    ):
        args.json = True

    if args.json:
        _json_stdout_mode(args, specs, cfg, mode, excluded, show_risky, scan_depth)
        return 0

    if args.checkup:
        return _cmd_checkup(specs, show_risky, scan_depth, show_progress, deep=args.deep)

    # 全量扫描
    progress = ScanProgressDisplay(enabled=show_progress and not show_detail and not show_tree)
    all_results = scan_all(specs, scan_depth=scan_depth, on_progress=progress if show_progress else None)
    progress.finish(all_results)

    # 导出扫描结果
    if args.export_scan:
        _cmd_export_scan(all_results, args.export_scan)
        return 0

    # 展示用：默认隐藏高风险分类
    results = [r for r in all_results if show_risky or r.risk != "risky"]

    if args.list:
        if show_tree:
            from .scanner import print_tree_report
            print_tree_report(results)
        elif show_detail:
            print_detail_report(results, sort_by=sort_by)
        else:
            from .scanner import print_report
            print_report(results)
        _echo("")
        _print_disk_free(results)
        if sys.platform == "win32":
            bin_size = recycle_bin_size()
            if bin_size > 0:
                _echo(f"回收站占用: {format_size(bin_size)}")
        if not show_risky:
            hidden = [r for r in all_results if r.risk == "risky"]
            if hidden:
                _echo(dim("（高风险分类未显示，可用 --risky 查看）"))
        return 0

    # --- 交互式菜单 ---
    if not args.clean and not args.all:
        return _interactive(results, specs, mode, args, cfg, show_risky, sort_by, scan_depth, show_progress)

    # --- 选择分类 ---
    selected: list[CategoryResult] = []
    empty_bin = False
    keys: list[str] = []
    if args.all:
        selected = [r for r in results if r.key.lower() not in excluded]
        empty_bin = "recycle_bin" not in excluded
    else:
        keys = _parse_keys(args.clean)
        risky_named = [k for k in keys if k != "recycle_bin"]
        if any(
            r.risk == "risky" and r.key.lower() in risky_named for r in all_results
        ) and not show_risky:
            _echo(
                yellow(
                    "注意：--clean 显式指定了高风险分类，"
                    "请仔细核对下面的预览清单后再确认。"
                )
            )
        known = {r.key.lower() for r in all_results}
        selected = [
            r
            for r in all_results
            if r.key.lower() in keys and r.key.lower() not in excluded
        ]
        empty_bin = "recycle_bin" in keys and "recycle_bin" not in excluded
        missing = [k for k in keys if k not in known and k != "recycle_bin"]
        if missing:
            _echo(red(f"无法识别的分类: {', '.join(missing)}"))
            _echo("可用分类: " + ", ".join(r.key for r in results))
            return 1
        if not selected and not empty_bin and keys:
            _echo(yellow("所选分类均已被 --exclude 排除，未执行任何操作。"))
            return 0

    # 需要管理员但未提权的分类：提示并跳过
    blocked = [r for r in selected if r.admin_blocked]
    if blocked:
        for r in blocked:
            _echo(
                yellow(
                    f"分类「{r.label}」需要管理员权限，当前未提权，已跳过"
                    "（可用 --admin 提权后重试）。"
                )
            )
        selected = [r for r in selected if not r.admin_blocked]
        if not selected and not empty_bin:
            return 0

    _run_clean_flow(
        selected,
        dry_run=args.dry_run,
        mode=mode,
        auto_confirm=args.yes,
        empty_bin=empty_bin,
        cfg=cfg,
        recycle_fallback=args.recycle_fallback or bool(cfg.get("recycle_error_fallback", False)),
        shred=args.shred,
        shred_passes=shred_passes,
        ext_filter=ext_filter,
        min_size_bytes=min_size_bytes,
        older_than_secs=older_than_secs,
    )
    return 0


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
) -> int:
    """交互式菜单：选择 -> 预览 -> 确认 -> 清理，可循环继续。"""
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


def _json_stdout_mode(
    args,
    specs: list[dict[str, Any]],
    cfg: dict[str, Any],
    mode: CleanMode,
    excluded: set[str],
    show_risky: bool = False,
    scan_depth: int = 20,
) -> None:
    """--json 输出模式。"""
    results = scan_all(specs, scan_depth=scan_depth)
    payload: dict[str, Any] = {
        "version": __version__,
        "recycle_available": recycle_available(),
        "admin": is_admin(),
        "dry_run": args.dry_run,
        "categories": [
            {
                "key": r.key,
                "label": r.label,
                "risk": r.risk,
                "requires_admin": r.requires_admin,
                "admin_blocked": r.admin_blocked,
                "count": r.total_count,
                "target_count": len(r.targets),
                "size_bytes": r.liberatable,
                "size": format_size(r.liberatable),
            }
            for r in results
        ],
        "total_size_bytes": sum(r.liberatable for r in results),
        "total_targets": sum(len(r.targets) for r in results),
    }
    if sys.platform == "win32":
        payload["recycle_bin_size_bytes"] = recycle_bin_size()

    if args.clean or args.all:
        if args.all:
            keys = [r.key for r in results if show_risky or r.risk != "risky"]
            keys.append("recycle_bin")
        else:
            keys = _parse_keys(args.clean)
        keys = [k for k in keys if k not in excluded]

        if args.dry_run:
            payload["action"] = {"dry_run": True, "selected": keys}
        elif args.yes:
            selected = [r for r in results if r.key in keys and not r.admin_blocked]
            targets = _collect_targets(selected)
            ext_filter, min_size_bytes, older_than_secs, shred_passes = _filters_from_args(args)
            targets = _apply_target_filters(
                targets,
                ext_filter=ext_filter,
                min_size_bytes=min_size_bytes,
                older_than_secs=older_than_secs,
            )
            res = delete_targets(
                targets,
                mode,
                on_progress=_progress_line,
                recycle_fallback=args.recycle_fallback
                or bool(cfg.get("recycle_error_fallback", False)),
                shred=args.shred,
                shred_passes=shred_passes,
            )
            action: dict[str, Any] = {
                "mode": mode.value,
                "deleted": res["deleted"],
                "failed": res["failed"],
                "freed_bytes": res["freed"],
                "selected": keys,
            }
            if "recycle_bin" in keys:
                action["recycle_bin"] = empty_recycle_bin()
            payload["action"] = action
        else:
            payload["action"] = {
                "skipped": True,
                "reason": "--json 模式下删除需要同时使用 --yes（且不要 --dry-run）",
            }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
