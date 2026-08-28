"""命令行界面与交互式清理菜单。

用法原则：
- 加入 ``recycle_bin`` 特殊分类（由 engine.empty_recycle_bin 处理）。
- 默认进入交互式菜单；用 ``--list`` 只扫描、``--clean`` 直接清理指定分类。
- 所有删除前都会先预览并确认（除非显式 ``--yes``）。
- ``--json`` 输出模式默认**只扫描**；要真正删除必须同时给 ``--yes``
  （自动化场景下避免静默误删）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULTS, load_config, save_config, update_config
from .console import bold, cyan, dim, green, pad_cjk, red, yellow
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
from .models import CategoryResult, format_size
from .rules import (
    get_all_category_specs,
    is_risky_spec,
    spec_requires_admin,
)
from .scanner import is_admin, recycle_bin_size, scan_all


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _echo(*args) -> None:
    print(*args)


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
    """风险等级彩色徽标（借鉴 windows-cleaner-cli 的安全色分级）。"""
    if risk == "safe":
        return green("●")
    if risk == "moderate":
        return yellow("●")
    return red("●")


def _admin_tag(requires_admin: bool) -> str:
    return yellow(" [需管理员]") if requires_admin else ""


# ---------------------------------------------------------------------------
# 预览 / 菜单
# ---------------------------------------------------------------------------
def _preview_targets(targets: list[Any], max_lines: int = 12) -> None:
    """预览一组目标（按体积从大到小），限制展示行数，避免刷屏。"""
    if not targets:
        _echo("  （无内容）")
        return
    ordered = sorted(targets, key=lambda t: t.size, reverse=True)
    for i, t in enumerate(ordered[:max_lines], start=1):
        _echo(f"    {t.describe()}")
    rest = len(ordered) - max_lines
    if rest > 0:
        _echo(f"    ... 以及另外 {rest} 项")
    _echo(
        f"    总计: {len(ordered)} 项, 可释放 "
        f"{green(format_size(sum(t.size for t in ordered)))}"
    )


def _print_full_menu(results: list[CategoryResult], bin_size: int, show_risky: bool) -> None:
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
    if not show_risky:
        _echo(dim("  提示：输入 x 可查看高风险分类（需 --risky 或配置 show_risky）"))
    _echo("")


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
            parts.append(f"{d} 可用 {format_size(u.free)}")
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
) -> dict[str, Any]:
    """对选中的分类执行：预览 -> 确认 -> 删除。"""
    targets = _collect_targets(selected)
    max_lines = int(cfg.get("preview_lines", 12) or 12)

    _echo("")
    if targets:
        _echo(bold("将删除以下内容（预览）："))
        for res in selected:
            if res.targets:
                _echo(f"  【{res.label}】")
                _preview_targets(res.targets, max_lines=max_lines)

    if empty_bin and not dry_run:
        _echo(f"  【回收站】将清空回收站（{red('不可恢复')}）。")

    if shred and not dry_run and mode is CleanMode.PERMANENT:
        _echo(yellow("  shred 模式：永久删除前将随机覆写文件内容一遍。"))

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
    print(f"\r[进度] {i}/{total}  {msg[:70]:<70}", end="", file=sys.stderr, flush=True)
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
    )
    p.add_argument("--list", action="store_true", help="仅扫描并展示，不删除")
    p.add_argument(
        "--clean",
        metavar="KEY[,KEY...]",
        help="直接清理指定分类（如 system_temp,web_cache；支持 recycle_bin）",
    )
    p.add_argument("--all", action="store_true", help="选中所有分类（含回收站）")
    p.add_argument(
        "--exclude",
        metavar="KEY[,KEY...]",
        help="与 --all/--clean 联用：排除指定分类（如 downloads,recycle_bin）",
    )
    p.add_argument("--dry-run", action="store_true", help="只预览，不真正删除")
    p.add_argument(
        "--recycle",
        dest="mode",
        action="store_const",
        const=CleanMode.RECYCLE,
        help="删除进回收站（需安装 send2trash）",
    )
    p.add_argument(
        "--permanent",
        dest="mode",
        action="store_const",
        const=CleanMode.PERMANENT,
        help="永久删除",
    )
    p.add_argument(
        "--recycle-fallback",
        action="store_true",
        help="进回收站失败时回退为永久删除（默认保留原文件并计入失败）",
    )
    p.add_argument("--yes", action="store_true", help="跳过交互确认（谨慎使用）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果（默认只扫描）")
    p.add_argument("--risky", action="store_true", help="显示/允许高风险分类（下载旧文件、构建产物、隐私数据等）")
    p.add_argument("--shred", action="store_true", help="永久删除前随机覆写文件内容一遍（隐私增强）")
    p.add_argument(
        "--history",
        action="store_true",
        help="显示清理历史（时间/模式/释放空间/分类）",
    )
    p.add_argument(
        "--undo-last",
        action="store_true",
        help="把最近一次「进回收站」的清理从回收站恢复回来",
    )
    p.add_argument(
        "--checkup",
        action="store_true",
        help="一键体检：只读汇总管理员状态、磁盘可用、回收站、可清理分类",
    )
    p.add_argument(
        "--admin",
        action="store_true",
        help="以管理员身份重新启动（UAC 提权）后再执行",
    )
    p.add_argument("--export-config", metavar="PATH", help="把当前配置导出到指定 JSON 文件")
    p.add_argument("--import-config", metavar="PATH", help="从 JSON 文件导入配置")
    p.add_argument(
        "--show-config",
        action="store_true",
        help="显示当前配置文件的路径与内容",
    )
    p.add_argument("--version", action="store_true", help="显示版本")
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


def _cmd_checkup(specs: list[dict[str, Any]], show_risky: bool) -> int:
    """一键体检：只读汇总（借鉴 sifty 的 checkup）。"""
    from .scanner import _system_drives

    _echo(bold(f"=== PC Junk Cleaner {__version__} 体检报告 ==="))
    _echo(
        f"  管理员权限: {'是' if is_admin() else '否（系统深度清理分类将被跳过，可用 --admin 提权）'}"
    )
    _echo(
        f"  回收站支持: {'是（删除可恢复）' if recycle_available() else '否（将永久删除，建议 pip install send2trash）'}"
    )
    for drive in _system_drives():
        try:
            u = shutil.disk_usage(drive)
            _echo(
                f"  磁盘 {drive} 总 {format_size(u.total)} / 已用 {format_size(u.used)}"
                f" / 可用 {green(format_size(u.free))}"
            )
        except OSError:
            continue
    if sys.platform == "win32":
        _echo(f"  回收站占用: {format_size(recycle_bin_size())}")

    results = scan_all(specs)
    visible = [r for r in results if show_risky or r.risk != "risky"]
    total = sum(r.liberatable for r in visible)
    _echo("")
    _echo(bold("可清理分类（只读扫描，未删除任何内容）："))
    for r in visible:
        if r.admin_blocked:
            _echo(f"  {_risk_badge(r.risk)} {r.label}{_admin_tag(True)}  需管理员，已跳过")
        elif r.targets:
            _echo(
                f"  {_risk_badge(r.risk)} {r.label}  {r.total_count} 项, "
                f"约 {green(format_size(r.liberatable))}"
            )
        else:
            _echo(f"  {_risk_badge(r.risk)} {r.label}  0 项")
    if not show_risky:
        hidden = [r for r in results if r.risk == "risky"]
        if hidden:
            _echo(
                dim(
                    f"  （另有 {len(hidden)} 个高风险分类未显示，"
                    "可用 --risky 查看，如 downloads / dev_purge / browser_privacy）"
                )
            )
    _echo("-" * 56)
    _echo(f"  合计可释放: {green(format_size(total))}")
    sessions = load_history()
    if sessions:
        last = sessions[-1]
        _echo(
            dim(
                f"  上次清理: {last.get('ts', '?')} 释放 "
                f"{format_size(last.get('freed', 0))}"
            )
        )
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

    specs = get_all_category_specs(merge_custom=True)

    # 配置可限制只扫描部分分类
    enabled = [k.lower() for k in (cfg.get("enabled_categories") or []) if k]
    if enabled:
        specs = [s for s in specs if s["key"].lower() in enabled]

    show_risky = args.risky or bool(cfg.get("show_risky", False))
    mode = _resolve_mode(args, cfg)
    excluded = set(_parse_keys(args.exclude)) if args.exclude else set()

    # 管道输出时自动切换 JSON（借鉴 sifty：只对只读扫描命令生效，绝不自动删除）
    if (
        not args.json
        and not sys.stdout.isatty()
        and not (args.clean or args.all)
        and not args.checkup
    ):
        args.json = True

    if args.json:
        _json_stdout_mode(args, specs, cfg, mode, excluded, show_risky)
        return 0

    if args.checkup:
        return _cmd_checkup(specs, show_risky)

    # 全量扫描（含高风险与需管理员分类，后者会被标记跳过）
    all_results = scan_all(specs)
    # 展示用：默认隐藏高风险分类
    results = [r for r in all_results if show_risky or r.risk != "risky"]

    if args.list:
        from .scanner import print_report

        print_report(results)
        _echo("")
        _print_disk_free(results)
        if sys.platform == "win32":
            _echo(f"回收站占用: {format_size(recycle_bin_size())}")
        if not show_risky:
            hidden = [r for r in all_results if r.risk == "risky"]
            if hidden:
                _echo(dim("（高风险分类未显示，可用 --risky 查看）"))
        return 0

    # --- 交互式菜单 ---
    if not args.clean and not args.all:
        return _interactive(results, specs, mode, args, cfg, show_risky)

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
    )
    return 0


def _interactive(
    results: list[CategoryResult],
    specs: list[dict[str, Any]],
    mode: CleanMode,
    args,
    cfg: dict[str, Any],
    show_risky: bool,
) -> int:
    """交互式菜单：选择 -> 预览 -> 确认 -> 清理，可循环继续。"""
    from .scanner import print_report

    _echo("")
    _echo(bold(f"=== PC Junk Cleaner v{__version__} ==="))
    if recycle_available():
        _echo(green("回收站支持: 是（删除可恢复）"))
    else:
        _echo(yellow("回收站支持: 否（将永久删除，请谨慎！建议 pip install send2trash）"))
    if is_admin():
        _echo(green("管理员权限: 是"))
    else:
        _echo(yellow("管理员权限: 否（系统深度清理分类将跳过，可用 --admin 提权）"))

    while True:
        print_report(results)
        _echo("")
        _print_disk_free(results)
        bin_size = recycle_bin_size() if sys.platform == "win32" else 0
        if bin_size > 0:
            _echo(dim(f"回收站占用: {format_size(bin_size)}"))

        selected: list[CategoryResult] = []
        empty_bin = False
        n = len(results)
        while True:
            _print_full_menu(results, bin_size, show_risky)
            choice = ""
            try:
                choice = input("请选择要清理的分类（如 1,3 或 all；q 退出）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if choice.lower() in ("q", "quit", "exit"):
                return 0
            if choice.lower() in ("x", "risky"):
                # 切换高风险分类显示并重新扫描
                show_risky = not show_risky
                if show_risky:
                    _echo(yellow("已显示高风险分类（删除前请仔细核对预览）。"))
                else:
                    _echo(dim("已隐藏高风险分类。"))
                fresh = scan_all(specs)
                results = [r for r in fresh if show_risky or r.risk != "risky"]
                n = len(results)
                continue
            sel = _parse_selection(choice, n)
            if sel == "none":
                selected = []
                empty_bin = False
                _echo("已清空选择。")
                continue
            if sel == "all":
                selected = list(results)
                empty_bin = True
                break
            selected = [results[i - 1] for i in sorted(sel)]
            empty_bin = False
            break

        # 过滤掉没有内容的分类
        selected = [r for r in selected if r.targets]
        if not selected and not empty_bin:
            _echo(yellow("所选分类没有可清理的内容。"))
            continue

        _run_clean_flow(
            selected,
            dry_run=args.dry_run,
            mode=mode,
            auto_confirm=False,
            empty_bin=empty_bin,
            cfg=cfg,
            recycle_fallback=args.recycle_fallback or bool(cfg.get("recycle_error_fallback", False)),
            shred=args.shred,
        )

        # 循环：重新扫描并继续
        try:
            again = prompt_yes_no("是否继续扫描并清理其他内容？", default=True)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not again:
            return 0
        fresh = scan_all(specs)
        results = [r for r in fresh if show_risky or r.risk != "risky"]


def _json_stdout_mode(
    args,
    specs: list[dict[str, Any]],
    cfg: dict[str, Any],
    mode: CleanMode,
    excluded: set[str],
    show_risky: bool = False,
) -> None:
    """--json 输出模式。

    默认只扫描。仅当同时给出 ``--yes``（且非 ``--dry-run``）时才会真正删除，
    避免自动化场景下静默误删。
    """
    results = scan_all(specs)
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
                "size_bytes": r.liberatable,
                "size": format_size(r.liberatable),
            }
            for r in results
        ],
        "total_size_bytes": sum(r.liberatable for r in results),
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
            res = delete_targets(
                targets,
                mode,
                on_progress=_progress_line,
                recycle_fallback=args.recycle_fallback
                or bool(cfg.get("recycle_error_fallback", False)),
                shred=args.shred,
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
