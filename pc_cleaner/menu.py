"""交互式清理菜单、预览/汇总展示与清理执行流程。

从 cli.py 拆分出来的「交互层」：负责把扫描结果展示成菜单、预览待删目标、
执行清理流程（预览 -> 确认 -> 删除 -> 记历史）。参数解析与子命令管理
仍在 cli.py / commands.py。

热重载：交互菜单每轮循环重新读取配置与 rules.json（``get_enabled_category_specs``），
编辑 ``rules.json`` / ``config.json`` 后无需重启程序，下一次扫描即生效。

安全增强（v0.8.1）：
- 扩展名过滤增加安全字符校验（仅允许字母、数字、下划线、连字符、点）。
- 高风险操作（risky分类/回收站清空/永久删除）即使在 --yes 模式下也会强制二次确认。
- 将 _filters_from_args 移入本模块，消除与 cli.py 的循环导入。
"""

from __future__ import annotations

import shutil
import sys
import time
import re  # 安全增强：扩展名校验
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
from .service import EXIT_INTERRUPTED, EXIT_OK
from .ui import (
    CleanProgressDisplay,
    ScanProgressDisplay,
    _echo,
    _risk_badge,
    is_elevated,
    prompt_yes_no,
)

# 注意：不再从 .cli 导入任何内容，避免循环依赖


# ===========================================================================
# 高级清理过滤（安全增强：扩展名校验）
# ===========================================================================
def _parse_ext_filter(raw: str | None) -> list[str] | None:
    """把 ``--ext`` 的值解析成小写、带点号的扩展名列表。

    安全增强：
    - 仅允许字母、数字、下划线、连字符、点，其他字符将被忽略并警告。
    """
    if not raw:
        return None
    exts: list[str] = []
    for e in raw.replace("，", ",").replace("；", ",").split(","):
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        # 安全校验：只允许 字母、数字、下划线、连字符、点
        if not re.match(r'^[a-zA-Z0-9_.-]+$', e):
            _echo(yellow(f"忽略无效扩展名: {e}（仅允许字母、数字、下划线、连字符、点）"))
            continue
        if e not in exts:
            exts.append(e)
    return exts or None


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


# ===========================================================================
# 预览 / 展示
# ===========================================================================
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


def _print_summary_table(
    results: list[CategoryResult],
    *,
    selectable: list[int] | None = None,
) -> None:
    """打印分类汇总表（表格式）。列宽统一，边框动态生成。

    ``selectable``：可清理分类在 results 中的下标（0-based），只给这些分类
    编号展示；其它（0 项 / 需管理员未扫描）由菜单页脚以小结形式列出。
    为 None 时展示全部（用于只读报告场景）。
    """
    # ---- 列宽定义（显示宽度，不含两侧空格）----
    COL_NO = 5        # 编号，如 " 1."
    COL_CAT = 24      # 分类名（含风险标记 ● + 空格）
    COL_TARGET = 12   # 目标数，如 "18 个目标"
    COL_FILE = 8      # 文件数
    COL_SIZE = 14     # 可释放空间，如 "106.42 MB"

    # v0.9.8：在窄终端（默认 80 列的 cmd 窗口）里把分类列收窄，保证整张表
    # 能放进一行而不被折行撕碎。桌面终端（>=96 列）保持原有宽表。
    try:
        if get_terminal_width() < 88:
            COL_CAT = 18
    except Exception:  # noqa: BLE001 取宽度失败时按宽表处理
        pass

    # v0.9.8 表格宽度修复。
    #
    # 每一行的实际结构是：
    #     "  │ " + COL_NO + " │ " + COL_CAT + " │ " + COL_TARGET
    #           + " │ " + COL_FILE + " │ " + COL_SIZE + " │"
    # 即：前缀 "  │ "(4) + 5 个列宽 + 4 个内部 " │ "(3) + 后缀 " │"(2)。
    # 此前 BORDER_W 写成 `4 + 各列 + 3*4 + 2`，把后缀算成了前缀的一部分，
    # 比真实行宽多 1 列；同时 _cat_cell 假设风险徽标 ● 占 2 个显示列，而
    # U+25CF 是 East-Asian *Ambiguous*、实测 display_width("●") == 1，
    # 于是数据行又比表头短 1 列。实测结果：边框 85 / 表头 81 / 数据行 80，
    # 在默认 80 列的 cmd 窗口里整个表格会被折行撕碎。
    #
    # 现在统一从 ROW_W 反推边框长度，并由 _cat_cell 按徽标**实测**宽度补位，
    # 保证边框、表头、数据行三者显示宽度完全一致。
    ROW_W = (4 + COL_NO + 3 + COL_CAT + 3 + COL_TARGET + 3 + COL_FILE + 3
             + COL_SIZE + 2)
    BORDER_W = ROW_W - 4  # "  ┌" + BORDER_W*"─" + "┐" 恰好等于 ROW_W

    def _rjust(text: str, w: int) -> str:
        """按显示宽度右对齐（传入纯文本，不含 ANSI 码）。"""
        pad = w - display_width(text)
        return " " * max(pad, 0) + text

    def _cat_cell(badge: str, label: str) -> str:
        """构造固定宽度的分类单元格：badge(带颜色) + 空格 + label(pad/截断)。

        徽标宽度按 display_width 实测（● 为 1 列，emoji 风险标为 2 列），
        不再硬编码 2，避免关掉 ANSI 或换用 emoji 徽标时列宽对不上。
        """
        badge_w = display_width(badge)
        avail = COL_CAT - badge_w - 1  # 徽标 + 1 个分隔空格
        if avail < 1:
            avail = 1
        lbl = label
        if display_width(lbl) > avail:
            # 截断并加省略号（按显示宽度退字符，确保不超宽）
            while lbl and display_width(lbl) > avail - 1:
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

    if selectable is None:
        selectable = list(range(len(results)))
    sel_set = set(selectable)
    number_of = {orig: k for k, orig in enumerate(selectable, start=1)}

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

    for i, res in enumerate(results):
        if i not in sel_set:
            continue  # 空分类 / 未扫描分类由页脚小结，不占可选项编号
        k = number_of[i]
        badge = _risk_badge(res.risk)
        no_str = f"{k}."
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


def _mode_label(mode: CleanMode) -> str:
    """当前删除方式的人类可读标签（v0.9.8）。"""
    if mode is CleanMode.PERMANENT:
        return red("永久删除（不可恢复）")
    return green("进回收站（可恢复）")


def _mode_label_plain(mode: CleanMode) -> str:
    """同 :func:`_mode_label`，但不带 ANSI 颜色，用于拼接确认提示语。"""
    if mode is CleanMode.PERMANENT:
        return "永久删除（不可恢复）"
    return "删除到回收站（可恢复）"


def _print_menu_status(
    mode: CleanMode,
    show_risky: bool,
    scan_depth: int,
    workers: int,
    deep: bool,
    bin_size: int,
) -> None:
    """常驻状态栏（v0.9.8）。

    此前「模式 / 深度 / 线程」只在进入菜单前的一次性横幅里出现过，表格滚屏后
    用户根本看不到自己处在哪个模式；更糟的是表头那行 `回收站支持: 是（删除可
    恢复）` 只反映 send2trash 是否可用，配置成永久删除时它会一边承诺"可恢复"
    一边真的永久删。这里把真实模式直接印在每轮表格之前。
    """
    mode_part = f"模式: {_mode_label(mode)}"
    risk_part = f"风险显示: {'显示' if show_risky else '隐藏'}(x 切换)"
    depth_part = f"深度: {'deep ' if deep else ''}{scan_depth}"
    thread_part = f"线程: {workers if workers and workers > 1 else '串行'}"
    rest: list[str] = [depth_part, thread_part]
    if sys.platform == "win32":
        rest.append(f"回收站: {format_size(bin_size)}" if bin_size > 0 else "回收站: 空")

    # 窄终端（默认 80 列 cmd 窗口）折成两行，避免状态栏自己先被折行撕碎
    one_line = "  " + " · ".join([mode_part, risk_part] + rest)
    if display_width(one_line) <= get_terminal_width():
        _echo(one_line)
    else:
        _echo("  " + " · ".join([mode_part, risk_part]))
        _echo("  " + " · ".join(rest))


def _print_menu_footer(
    results: list[CategoryResult],
    bin_size: int,
    selectable: list[int],
    show_risky: bool,
) -> None:
    """打印菜单页脚：回收站行、空分类小结与操作图例。

    汇总表只编号「有内容」的分类（selectable），这里补充说明未编号的行
    （当前为空 / 需管理员未扫描），并列出可用的操作键。
    """
    # 回收站行
    if bin_size > 0:
        _echo(f"  {red('r')}. 回收站 (清空, 占用 {yellow(format_size(bin_size))})")
    else:
        _echo(f"  {dim('r')}. 回收站 (清空)")
    _echo("")

    # 空分类 / 未编号小结
    notes: list[str] = []
    sel_set = set(selectable)
    for i, res in enumerate(results):
        if i in sel_set:
            continue
        if res.admin_blocked:
            notes.append(f"{yellow(res.label)}(需管理员)")
        elif res.targets:
            notes.append(res.label)  # 理论上有内容但没进 selectable，正常不应发生
        else:
            notes.append(dim(f"{res.label}(空)"))
    if notes:
        _echo(dim("  其余分类(未编号, 当前无可清理): " + " · ".join(notes)))
    if not show_risky:
        risky_hidden = [r.label for r in results if r.risk == "risky"]
        if risky_hidden:
            _echo(dim("  （高风险分类已隐藏: " + " · ".join(risky_hidden) + "，按 x 查看）"))

    # v0.9.8：风险图例。此前风险**只靠颜色**表达（●），既没有任何图例说明，
    # 关掉 ANSI（非 TTY / 老控制台）后三个等级还会渲染成一模一样的 ●，
    # 色觉障碍用户也完全无法区分。这里补文字图例；分行书写以保证窄终端
    # （80 列）下也不会折行。
    _echo(dim(f"  图例: {green('●')} 安全（随便清） · "
              f"{yellow('●')} 一般（清理后按需重建）"))
    _echo(dim(f"        {red('●')} 高风险（默认隐藏，需 x 显示 / --risky）"))

    _echo("")
    _echo(dim("  操作: "))
    _echo(dim("    编号 如 1,3-5   选择分类（逗号/区间分隔，可多选）"))
    _echo(dim("    all / a         全选 · r 回收站 · 0 清空选择"))
    _echo(dim("    d <编号> 详情    t <编号> 树形 · s <排序> 切换"))
    _echo(dim("    x 高风险显示     f 重新扫描 · h 帮助 · q 退出"))
    _echo("")


def _print_env_adaptation(env: dict[str, Any] | None = None) -> None:
    """打印「本机适配」一行：这台机器实际装了哪些东西（只读探测结果）。"""
    try:
        if env is None:
            from .env import probe_environment
            from .ui import Spinner

            # v0.9.7：菜单首屏的环境探测在慢盘上要几秒，给个加载指示器
            with Spinner("正在检测本机运行环境"):
                env = probe_environment(measure_wechat_size=False)
        if not isinstance(env, dict):
            return
    except Exception:  # noqa: BLE001 探测失败不影响菜单可用
        return

    parts: list[str] = []
    try:
        browsers = [b for b in env.get("browsers", []) if b.get("installed")]
        if browsers:
            parts.append(green("浏览器 " + ", ".join(b["name"] for b in browsers)))
    except Exception:  # noqa: BLE001
        pass
    try:
        gpu = env.get("gpu") or []
        if gpu:
            parts.append(green("GPU " + ", ".join(g.upper() for g in gpu)))
    except Exception:  # noqa: BLE001
        pass
    try:
        wechat = env.get("wechat") or {}
        if wechat.get("layout") == "wechat4":
            parts.append(green("微信 4.x"))
        elif wechat.get("layout") == "wechat3":
            parts.append(green("微信 3.x"))
    except Exception:  # noqa: BLE001
        pass
    try:
        stores = env.get("pnpm_stores") or []
        stores = [s for s in stores if s.lower().endswith(".pnpm-store")]
        if stores:
            parts.append(green(f"pnpm store({stores[0]})"))
    except Exception:  # noqa: BLE001
        pass
    try:
        steam = env.get("steam") or {}
        if steam.get("installed"):
            libs = steam.get("library_dirs") or []
            loc = ""
            if libs:
                # 形如 D:\Program Files (x86)\Steam\steamapps → 去掉 \steamapps
                parent = libs[0][:-10].rstrip("\\/")
                loc = f"({parent})"
            parts.append(green(f"Steam{loc}"))
    except Exception:  # noqa: BLE001
        pass

    if parts:
        _echo("")
        _echo(dim("  本机适配: ") + "  ".join(parts))
        try:
            hints = []
            for b in env.get("browsers", []):
                if not b.get("installed") and b["key"] in ("chrome", "firefox", "brave", "vivaldi", "opera"):
                    hints.append(b["name"])
            tools = {k: v for k, v in (env.get("dev_tools") or {}).items() if v}
            if hints:
                _echo(dim("  未检测到: ") + dim(" · ".join(hints)) + dim("  (相关缓存分类将显示为空)"))
            if tools:
                _echo(dim("  开发工具: ") + dim(", ".join(sorted(tools))))
        except Exception:  # noqa: BLE001
            pass
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


# ===========================================================================
# 用户选择解析（支持编号 / 区间 / all / none / r(回收站)）
# ===========================================================================
def _split_choice_tokens(raw: str) -> list[str]:
    """把用户输入拆成小写 token：兼容中文逗号/顿号/分号与空白分隔。"""
    norm = raw.replace("，", ",").replace("；", ",").replace("、", ",")
    return [t.strip().lower() for t in norm.replace(",", " ").split() if t.strip()]


def _expand_range(tok: str, n: int) -> list[int]:
    """把 ``2-5`` 这类区间 token 展开为编号列表；非法 token 返回空列表。"""
    if "-" not in tok:
        return []
    left, _, right = tok.partition("-")
    if not left.isdigit() or not right.isdigit():
        return []
    lo, hi = int(left), int(right)
    if lo > hi:
        lo, hi = hi, lo
    return [i for i in range(max(lo, 1), min(hi, n) + 1)]


def _parse_selection(raw: str, n: int) -> dict[str, Any]:
    """解析用户输入的分类选择。

    返回 ``{"all": bool, "none": bool, "recycle": bool, "indexes": set[int]}``：
    - ``indexes`` 是 1..n 的选中编号；
    - ``recycle=True`` 表示同时要求清空回收站（输入 ``r`` / ``rb``）；
    - ``all``（``all/a/*``）表示选择全部；``none``（``none/n/0``）表示清空。
    """
    tokens = _split_choice_tokens(raw)
    out: dict[str, Any] = {"all": False, "none": False, "recycle": False, "indexes": set()}
    if not tokens:
        return out
    for tok in tokens:
        if tok in ("all", "a", "*"):
            out["all"] = True
        elif tok in ("none", "n", "0"):
            out["none"] = True
        elif tok in ("r", "rb"):
            out["recycle"] = True
        elif tok.isdigit():
            idx = int(tok)
            if 1 <= idx <= n:
                out["indexes"].add(idx)
        elif "-" in tok:
            out["indexes"].update(_expand_range(tok, n))
    # none（0/n）优先：清空选择；除非同时给了 all（矛盾输入时以 all 为准）
    if out["none"] and not out["all"]:
        out["indexes"] = set()
        out["recycle"] = False
    return out


def selectable_rows(results: list[CategoryResult]) -> list[int]:
    """返回「有内容可选」的分类在 results 中的下标（0-based），按原顺序。

    交互菜单只给这些分类编号，避免列出大量 0 项分类造成选择困扰；
    ``d <编号>`` / ``t <编号>`` 用同一份编号（1-based 对应本列表下标）。
    """
    return [i for i, r in enumerate(results) if r.targets]


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


# ===========================================================================
# 主清理流程（安全增强：高风险操作强制二次确认）
# ===========================================================================
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
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """对选中的分类执行：预览 -> 确认 -> 删除。

    安全增强：
    - 即使 auto_confirm=True，如果操作包含高风险分类、回收站清空、或永久删除，
      也强制要求用户二次确认（输入 yes）。
    - v0.9.3：``allow_dangerous`` 表示调用方已**显式授权**危险操作（CLI ``--risky``）；
      未授权且非交互（管道 / Agent 调用）时**直接拒绝**并返回
      ``{"needs_confirmation": True}``（退出码 4），不再依赖 stdin EOF 兜底。
    """
    # 预览与删除使用同一份过滤结果，避免“预览了但没删/删了没预览”的偏差
    per_category: list[tuple[Any, list[Any]]] = []
    for res in selected:
        fl = _apply_target_filters(
            res.targets,
            ext_filter=ext_filter,
            min_size_bytes=min_size_bytes,
            older_than_secs=older_than_secs,
        )
        if fl:
            per_category.append((res, fl))
    targets = [t for _, fl in per_category for t in fl]

    max_lines = int(cfg.get("preview_lines", 12) or 12)
    sort_by = cfg.get("default_sort", "size_desc")

    _echo("")
    if targets:
        _echo(bold("将删除以下内容（预览）："))
        for res, fl in per_category:
            _echo(f"  【{res.label}】")
            _preview_targets(fl, max_lines=max_lines, sort_by=sort_by)

    if empty_bin and not dry_run:
        bin_sz = recycle_bin_size() if sys.platform == "win32" else 0
        if bin_sz > 0:
            _echo(
                f"  【回收站】将清空回收站（约 {yellow(format_size(bin_sz))}，"
                f"{red('不可恢复')}）。"
            )
        else:
            _echo(f"  【回收站】将清空回收站（{red('不可恢复')}）。")

    if shred and not dry_run and mode is CleanMode.PERMANENT:
        passes = max(1, min(int(shred_passes or 1), 7))
        _echo(yellow(f"  shred 模式：永久删除前将随机覆写文件内容 {passes} 遍。"))

    if dry_run:
        _echo("")
        _echo(yellow("dry-run 模式，未删除任何内容。"))
        return {"dry_run": True}

    # =======================================================================
    # 安全增强：高风险操作强制二次确认（即使 --yes）
    # =======================================================================
    has_risky = any(r.risk == "risky" for r in selected)
    is_permanent = mode is CleanMode.PERMANENT
    dangerous = has_risky or empty_bin or is_permanent

    if not auto_confirm:
        # 普通确认模式
        #
        # v0.9.8 安全修复：此前交互式确认只有一句「确认删除以上内容？ [y/N]」——
        # **从不说明删除方式**。实测在 PERMANENT + 高风险分类下，整场输出里
        # 没有"永久"、没有"不可恢复"、没有"回收站"，一个 y 就永久删除了；
        # 而 `dangerous` 早在上面就算出来了，却只用在 auto_confirm 分支。
        # 安全提示正好给反了：最需要保护的真人交互路径提示最少。
        # 现在把模式、条目数、体积、风险原因都写进提示，并且危险操作要求
        # 输入完整的 yes（而不是一个 y），与 --yes 分支的闸门保持同一标准。
        if targets:
            total_targets = sum(len(fl) for _, fl in per_category)
            total_bytes = sum(t.size for _, fl in per_category for t in fl)
            risky_labels = sorted({r.label for r in selected if r.risk == "risky"})
            prompt = (
                f"确认{_mode_label_plain(mode)}以上 {total_targets} 个目标"
                f"（约 {format_size(total_bytes)}）？"
            )
            if dangerous:
                reasons = []
                if has_risky and risky_labels:
                    reasons.append(f"包含高风险分类：{'、'.join(risky_labels)}")
                if empty_bin:
                    reasons.append("将清空回收站")
                if is_permanent:
                    reasons.append("永久删除，不可恢复")
                _echo("")
                _echo(red("⚠️  警告：当前操作包含不可恢复的删除！"))
                for r in reasons:
                    _echo(red(f"    · {r}"))
            if not prompt_yes_no(prompt, default=False, require_typed=dangerous):
                return {"cancelled": True}
        if empty_bin:
            # 清空回收站一定不可恢复 → 同样要求完整输入 yes
            if not prompt_yes_no("确认清空回收站？（不可恢复）", default=False,
                                 require_typed=True):
                _echo("已取消清空回收站。")
                empty_bin = False
    else:
        # --yes 模式下，仍对危险操作进行二次警示
        if dangerous and not allow_dangerous:
            from .service import dangerous_reasons

            reasons = dangerous_reasons(
                selected, mode, ["recycle_bin"] if empty_bin else []
            )
            if not sys.stdin.isatty():
                # 非交互（管道 / Agent 调用）：明确拒绝，而不是让 EOF 把危险操作
                # 变成"静默取消但退出码 0"（v0.9.3）。
                _echo(
                    red(
                        "⚠️  危险操作在非交互环境下被拒绝："
                        + "；".join(reasons)
                        + "。请先用 --json --dry-run 预览，"
                        "确认后显式加 --risky 授权再执行。"
                    )
                )
                return {"needs_confirmation": True, "reasons": reasons}
            _echo(red("⚠️  警告：当前操作包含不可恢复的删除！"))
            if reasons:
                _echo(red("    " + "；".join(reasons)))
            if not prompt_yes_no("  确认要继续吗？(输入 yes 继续)", default=False):
                return {"cancelled": True}

    # 执行删除
    enable_history = bool(cfg.get("enable_history", True))
    # v0.9.7：清理过程的实时状态（进度条 + 当前目标 + 已处理/跳过 + 已释放字节）。
    # 非 TTY 时自动静默，因此管道 / Agent 调用不受影响。
    cleaner = CleanProgressDisplay(enabled=not dry_run, mode=mode.value)
    audit_log = None
    if enable_history or cleaner.enabled:
        def audit_log(path, size, mode_name, freed=0) -> None:
            if enable_history:
                record_deletion_audit(path, size, mode_name, freed)
            cleaner.record(path, size, mode_name, freed)

    result: dict[str, Any] = {
        "deleted": 0,
        "failed": 0,
        "skipped": 0,
        "skipped_in_use": 0,
        "freed": 0,
        "recycled": 0,
        "empty_bin": empty_bin,
    }
    # v0.9.3：Ctrl+C（KeyboardInterrupt 是 BaseException，此前会直接穿出 main()）
    # 也要把"已处理的目标"落盘，否则已进回收站的文件无法用 --undo-last 找回。
    try:
        if targets:
            res = delete_targets(
                targets,
                mode,
                on_progress=cleaner,
                recycle_fallback=recycle_fallback,
                shred=shred,
                shred_passes=shred_passes,
                audit=audit_log,
            )
            result["deleted"] += res["deleted"]
            result["failed"] += res["failed"]
            result["skipped"] += res.get("skipped", 0)
            result["skipped_in_use"] += res.get("skipped_in_use", 0)
            result["freed"] += res["freed"]
            result["recycled"] += res.get("recycled", 0)
        # 清掉实时状态行（汇总由下面的"完成：…"统一输出）
        cleaner.finish(None)

        if empty_bin:
            before_bin = recycle_bin_size() if sys.platform == "win32" else 0
            bin_res = empty_recycle_bin()
            result["empty_bin_result"] = bin_res
            after_bin = recycle_bin_size() if sys.platform == "win32" else 0
            # 清空回收站释放的空间单独计入，让"释放约 X"更真实
            result["freed"] += max(before_bin - after_bin, 0)
            # v0.9.3：清空失败不再谎报"完成"（engine 返回 failed=1 时此前被忽略）
            if bin_res.get("failed"):
                result["failed"] += 1
                _echo(red("回收站清空失败（可能有文件被占用或权限不足）。"))
            else:
                _echo("回收站清空完成。")
    except KeyboardInterrupt:
        result["interrupted"] = True
        _echo("")
        _echo(yellow("已中断（Ctrl+C）。已处理的部分会记入历史，可用 --undo-last 恢复。"))
    finally:
        # 记录历史会话（--history / --undo-last 使用）—— 即使中断也要落盘
        if enable_history and (targets or empty_bin):
            session = make_session(
                mode=mode.value,
                deleted=result["deleted"],
                failed=result["failed"],
                freed=result["freed"],
                categories=[r.key for r in selected]
                + (["recycle_bin"] if empty_bin else []),
                targets=[
                    {
                        "path": str(t.path),
                        "size": t.size,
                        "action": t.action.value,
                    }
                    for t in targets
                ],
                note="shred" if shred else "",
            )
            append_session(session)

    _echo("")
    summary = (
        f"完成：删除 {green(str(result['deleted']))} 项, "
        f"跳过/失败 {yellow(str(result['failed']))} 项, "
        f"释放约 {green(format_size(result['freed']))}。"
    )
    if result.get("skipped"):
        partial = result["skipped"]
        summary += f" {dim(f'（另有 {partial} 个目录部分清理，剩余文件被占用）')}"
    if result.get("recycled"):
        recycled_txt = format_size(result["recycled"])
        summary += (
            f" {dim(f'（另有 {recycled_txt} 已移入回收站，'
                     f'清空回收站后才真正释放）')}"
        )
    if result.get("skipped_in_use"):
        in_use = result["skipped_in_use"]
        summary += f" {dim(f'（{in_use} 项目标因被运行中程序占用而跳过）')}"
    _echo(summary)
    _print_disk_free(selected)
    return result


# ===========================================================================
# 交互式菜单
# ===========================================================================
def _scan_for_menu(
    specs: list[dict[str, Any]],
    scan_depth: int,
    workers: int,
    show_progress: bool,
) -> tuple[list[CategoryResult] | None, int]:
    """菜单内统一的扫描入口（v0.9.8）。

    返回 ``(results, exit_code)``；``results is None`` 表示用户按 Ctrl+C 中断。

    v0.9.8 修复：此前菜单里的 ``scan_all`` 调用**没有任何 KeyboardInterrupt
    处理**，扫描（最耗时的阶段，本机约 25 秒）中途按 Ctrl+C 会直接抛出
    Python traceback，屏幕上留下半行进度条，几十秒的扫描结果全部丢失，
    ``service.py`` 约定的退出码 130 也走不到。现在统一捕获并给出中文提示。
    """
    progress = ScanProgressDisplay(enabled=show_progress)
    try:
        fresh = scan_all(
            specs, scan_depth=scan_depth,
            on_progress=progress if show_progress else None,
            workers=workers,
        )
    except KeyboardInterrupt:
        # 终止进度行，避免提示语跟在半行进度后面
        progress.finish([])
        print()
        _echo(yellow("已取消扫描（Ctrl+C），未做任何修改。"))
        return None, EXIT_INTERRUPTED
    progress.finish(fresh)
    return fresh, EXIT_OK


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
    workers: int = 0,
) -> int:
    """交互式菜单：选择 -> 预览 -> 确认 -> 清理，可循环继续。

    ``deep``：传入 ``--deep`` 标志，每轮循环重新加载 rules.json / 配置
    （热重载：编辑规则或配置后无需重启，下一轮扫描即生效）。
    ``workers``：并行扫描线程数（>1 时多线程扫描，结果顺序不变）。
    """
    from .scanner import print_tree_report

    _echo("")
    _echo(bold(f"=== PC Junk Cleaner v{__version__} ==="))
    if recycle_available():
        _echo(green("  回收站支持: 是（删除可恢复）"))
    else:
        _echo(yellow("  回收站支持: 否（将永久删除，请谨慎！建议 pip install send2trash）"))
    if is_admin():
        admin_line = green("  管理员权限: 是")
        if is_elevated():
            admin_line += dim(" [UAC 提权]")
        _echo(admin_line)
    else:
        _echo(yellow("  管理员权限: 否（系统深度清理分类将跳过，可用 --admin 提权）"))

    # 本机环境适配一行（只读探测：浏览器 / GPU / 微信 / Steam / pnpm store）
    _print_env_adaptation()

    # v0.9.8：把「重新扫描」上提到外层循环。此前 `x`（切换高风险显示）在内层
    # 循环里重扫并改写 results，却**没有重算 rows / n，也没有重画表格**，于是：
    #   * 隐藏高风险后 n 仍按旧值，选那个编号 → IndexError 崩溃（menu.py 旧 926 行）；
    #   * 极端情况下屏幕上的编号会对应到另一个分类，删错东西。
    # 现在任何需要重扫的操作都通过 refresh_requested 回到外层循环，由外层统一
    # 重新扫描、重新计算编号并重画表格，编号与 results 永远一致。
    refresh_requested = False
    while True:
        # 热重载：每轮重新读取配置与 rules.json，修改无需重启即可生效
        cfg = load_config()
        specs = get_enabled_category_specs(cfg, deep=deep)

        # 只在需要时重扫（首次进入 / 用户按 x / 用户按 f 主动刷新）
        if refresh_requested:
            fresh, code = _scan_for_menu(
                specs, show_progress, scan_depth, workers
            )
            if fresh is None:      # Ctrl+C：已给出提示，直接退出菜单
                return code
            results = [r for r in fresh if show_risky or r.risk != "risky"]
        refresh_requested = False

        # 只给「有内容」的分类编号，其余由页脚小结
        rows = selectable_rows(results)
        n = len(rows)
        bin_size = recycle_bin_size() if sys.platform == "win32" else 0

        _print_summary_table(results, selectable=rows)
        _echo("")
        _print_disk_free(results)
        _print_menu_status(mode, show_risky, scan_depth, workers, deep, bin_size)
        _print_menu_footer(results, bin_size, rows, show_risky)

        # v0.9.8：空结果时给出明确文案，而不是只显示一个空表格。
        # 此前 0 项时表格是空的、也没有任何说明，用户会以为程序坏了；
        # 而且 advertised 的 `all` 键在 n==0 时会被判成"无效输入"
        # （set(range(1,1)) 为空 → 落到最后的 else 分支）。
        if n == 0:
            blocked = [r.label for r in results if r.admin_blocked]
            msg = "  ✔ 没有发现可清理的内容（已扫描 "
            msg += f"{len(results)} 个分类"
            if blocked:
                msg += f"，其中 {len(blocked)} 个需管理员权限：{' · '.join(blocked)}"
            msg += "）。"
            _echo(green(msg))
            _echo(dim("    可按 f 重新扫描、x 显示高风险分类，或 q 退出。"))
            _echo("")

        selected: list[CategoryResult] = []
        empty_bin = False

        while True:
            try:
                choice = input(
                    "请选择（编号/all/r/d/t/s/x/f/h/q）: "
                    if n else "请选择（all/r/d/t/s/x/f/h/q）: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                _echo(dim("已退出。"))
                return 0

            if not choice:
                continue

            cmd = choice.lower().split()
            action = cmd[0]

            if action in ("q", "quit", "exit"):
                return 0

            # h → 菜单内帮助（v0.9.8：此前输入 h 只会得到"无效输入"）
            if action in ("h", "help", "?", "？"):
                _print_menu_status(mode, show_risky, scan_depth, workers, deep, bin_size)
                _echo(dim("  操作: "))
                _echo(dim("    编号 如 1,3-5   选择要清理的分类（逗号/区间分隔，可多选）"))
                _echo(dim("    all / a         选中所有非高风险分类（不含回收站）"))
                _echo(dim("    r               连回收站一起清空（不可恢复）"))
                _echo(dim("    0               清空当前选择"))
                _echo(dim("    d <编号>        查看该分类的详细目标列表（d 不带编号=全部）"))
                _echo(dim("    t <编号>        以树形视图展示"))
                _echo(dim("    s <方式>        切换排序：size_desc/size_asc/name_asc/name_desc/count_desc"))
                _echo(dim("    x               显示/隐藏高风险分类（会重新扫描）"))
                _echo(dim("    f               重新扫描（清空后想再看最新占用时用）"))
                _echo(dim("    u               从回收站恢复上一次清理（等价 --undo-last）"))
                _echo(dim("    h               显示本帮助 · q 退出"))
                _echo("")
                continue

            # f → 主动重新扫描（v0.9.8：此前没有任何刷新键，
            # 唯一的办法是退出程序或按两次 x）
            if action in ("f", "refresh", "rescan"):
                _echo(dim("  正在重新扫描 ..."))
                refresh_requested = True
                break

            # u → 从回收站恢复上一次清理（v0.9.8：此前交互菜单里没有撤销入口）
            if action in ("u", "undo"):
                try:
                    from .commands import _cmd_undo_last  # 延迟导入，避免循环依赖
                except Exception:  # noqa: BLE001
                    _echo(red("  当前版本不支持在菜单内撤销，请退出后运行 --undo-last。"))
                    continue
                try:
                    _cmd_undo_last()
                except KeyboardInterrupt:
                    print()
                    _echo(yellow("  已取消撤销。"))
                except Exception as exc:  # noqa: BLE001
                    _echo(red(f"  撤销失败: {exc}"))
                continue

            # d <编号> → 查看详细（编号对应汇总表里的可选项）
            if action == "d":
                if len(cmd) > 1:
                    if cmd[1].isdigit():
                        k = int(cmd[1])
                        if 1 <= k <= n:
                            _print_detail_for_category(results[rows[k - 1]], sort_by)
                        else:
                            _echo(red(f"  编号超出范围（1-{n}）"))
                    else:
                        _echo(red("  请输入有效编号，如: d 3"))
                else:
                    for i in rows:
                        _print_detail_for_category(results[i], sort_by)
                continue

            # t <编号> → 树形视图
            if action == "t":
                if len(cmd) > 1:
                    if cmd[1].isdigit():
                        k = int(cmd[1])
                        if 1 <= k <= n:
                            print_tree_report([results[rows[k - 1]]])
                        else:
                            _echo(red(f"  编号超出范围（1-{n}）"))
                    else:
                        _echo(red("  请输入有效编号，如: t 2"))
                else:
                    print_tree_report([results[i] for i in rows])
                continue

            # s <方式> → 切换排序
            if action == "s":
                valid_sorts = {"size_desc": "体积↓", "size_asc": "体积↑",
                               "name_asc": "名称↑", "name_desc": "名称↓",
                               "count_desc": "文件数↓"}
                sort_options = ["size_desc", "size_asc", "name_asc", "name_desc", "count_desc"]
                if len(cmd) > 1:
                    new_sort = cmd[1]
                    if new_sort in valid_sorts:
                        sort_by = new_sort
                        _echo(green(f"  已切换排序为: {valid_sorts[new_sort]}"))
                    else:
                        _echo(red(f"  无效排序方式。可选: {', '.join(valid_sorts.keys())}"))
                else:
                    current_idx = sort_options.index(sort_by) if sort_by in sort_options else 0
                    sort_by = sort_options[(current_idx + 1) % len(sort_options)]
                    _echo(green(f"  已切换排序为: {valid_sorts[sort_by]}"))
                continue

            # x → 切换高风险显示（v0.9.8：回到外层循环统一重扫 + 重画表格，
            # 保证屏幕上的编号与 results 一致；此前就地重扫导致编号错位/崩溃）
            if action in ("x", "risky"):
                show_risky = not show_risky
                if show_risky:
                    _echo(yellow("  已显示高风险分类（删除前请仔细核对预览）。"))
                else:
                    _echo(dim("  已隐藏高风险分类。"))
                refresh_requested = True
                break

            # 分类选择：编号 / 区间 / all / r（回收站）
            sel = _parse_selection(choice, n)
            if sel["none"] and not sel["all"] and not sel["indexes"] and not sel["recycle"]:
                selected = []
                empty_bin = False
                _echo("  已清空选择。")
                continue
            if sel["all"]:
                sel["indexes"] = set(range(1, n + 1))
            if sel["indexes"] or sel["recycle"]:
                selected = [results[rows[i - 1]] for i in sorted(sel["indexes"])]
                empty_bin = sel["recycle"]
                break

            _echo(red("  无效输入，请重新选择（编号/all/r/d/t/s/x/f/h/q）。"))

        # 过滤掉没有内容的分类
        selected = [r for r in selected if r.targets]
        if not selected and not empty_bin:
            _echo(yellow("  所选分类没有可清理的内容。"))
            continue

        ext_filter, min_size_bytes, older_than_secs, shred_passes = _filters_from_args(args)
        flow_result = _run_clean_flow(
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

        # v0.9.8：用户取消（或所选分类被过滤空了）时**回到菜单**，而不是逼他
        # 重新扫描。此前无论取消还是完成，都只会问「是否继续扫描并清理其他内容？
        # [Y/n]」——默认 Yes，按回车就开始新一轮多分钟扫描，唯一的选择是退出程序，
        # 选错编号想反悔的代价极高。
        if isinstance(flow_result, dict) and flow_result.get("cancelled"):
            _echo(dim("  已取消，返回菜单（可按 f 重新扫描）。"))
            continue

        # 完成后：默认**不**重扫，直接回到菜单（结果已是最新占用情况）；
        # 需要重新扫描时按 f 主动触发。
        _echo("")
        try:
            again = prompt_yes_no("继续清理其他内容？（返回菜单，不会重新扫描）",
                                  default=True)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not again:
            return 0
        # 继续 → 回到外层循环复用当前 results，无需重扫
        continue