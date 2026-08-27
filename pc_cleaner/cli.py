"""命令行界面与交互式清理菜单。

用法原则：
- 加入 ``recycle_bin`` 特殊分类（由 engine.empty_recycle_bin 处理）。
- 默认进入交互式菜单；用 ``--list`` 只扫描、``--clean`` 直接清理指定分类。
- 所有删除前都会先预览并确认（除非显式 ``--yes``）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .config import load_config, save_config
from .engine import (
    delete_targets,
    empty_recycle_bin,
    recycle_available,
    CleanMode,
)
from .models import CategoryResult, ScanReport, format_size
from .rules import get_all_category_specs
from .scanner import scan_all


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
        if raw in ("", ""):
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        _echo("请输入 y 或 n。")


def _wait_key() -> None:
    """回车继续 / 退出。"""
    try:
        if sys.stdin.isatty():
            input("按回车继续，q 退出...")
    except EOFError:
        pass


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------
def _preview_targets(targets: list[Any], max_lines: int = 12) -> None:
    """预览一组目标，限制展示行数，避免刷屏。"""
    if not targets:
        _echo("  （无内容）")
        return
    for i, t in enumerate(targets[:max_lines], start=1):
        _echo(f"    {t.describe()}")
    rest = len(targets) - max_lines
    if rest > 0:
        _echo(f"    ... 以及另外 {rest} 项")
    _echo(f"    总计: {len(targets)} 项, 可释放 {format_size(sum(t.size for t in targets))}")


def _print_full_menu(results: list[CategoryResult]) -> None:
    _echo("")
    _echo("可清理的分类：")
    for i, res in enumerate(results, start=1):
        if res.targets:
            _echo(
                f"  {i}. {res.label}  "
                f"({res.total_count} 项, 约 {format_size(res.liberatable)})"
            )
        else:
            _echo(f"  {i}. {res.label}  (0 项)")
    _echo("  r. 回收站 (清空)")
    _echo("  q. 退出")
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


# ---------------------------------------------------------------------------
# 主清理流程
# ---------------------------------------------------------------------------
def _run_clean_flow(
    report: list[CategoryResult],
    specs: list[dict[str, Any]],
    selected: list[CategoryResult],
    *,
    dry_run: bool,
    mode: CleanMode,
    auto_confirm: bool,
    empty_bin: bool,
) -> dict[str, Any]:
    """对选中的分类执行：预览 -> 确认 -> 删除。"""
    targets = _collect_targets(selected)

    _echo("")
    if targets:
        _echo("将删除以下内容（预览）：")
        for res in selected:
            if res.targets:
                _echo(f"  【{res.label}】")
                _preview_targets(res.targets)

    if empty_bin and not dry_run:
        _echo("  【回收站】将清空回收站（不可恢复）。")

    if dry_run:
        _echo("")
        _echo("dry-run 模式，未删除任何内容。")
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
    result: dict[str, Any] = {"deleted": 0, "failed": 0, "freed": 0, "empty_bin": empty_bin}
    if targets:
        res = delete_targets(
            targets,
            mode,
            on_progress=_progress_line,
        )
        result["deleted"] += res["deleted"]
        result["failed"] += res["failed"]
        result["freed"] += res["freed"]

    if empty_bin:
        bin_res = empty_recycle_bin()
        _echo("回收站清空完成。")

    _echo("")
    _echo(
        f"完成：删除 {result['deleted']} 项, "
        f"跳过/失败 {result['failed']} 项, 释放约 {format_size(result['freed'])}。"
    )
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
    p.add_argument("--all", action="store_true", help="选中所有分类")
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
    p.add_argument("--yes", action="store_true", help="跳过交互确认（谨慎使用）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument(
        "--show-config",
        action="store_true",
        help="显示当前配置文件的路径与内容",
    )
    p.add_argument("--version", action="store_true", help="显示版本")
    p.set_defaults(mode=None)
    return p


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
    specs = get_all_category_specs(merge_custom=True)

    # 确定删除模式
    if args.mode is not None:
        mode = args.mode
    elif cfg.get("recycle_by_default", True) and recycle_available():
        mode = CleanMode.RECYCLE
    else:
        mode = CleanMode.RECYCLE if recycle_available() else CleanMode.PERMANENT

    if args.json:
        _json_stdout_mode(args, specs, cfg)
        return 0

    # 扫描（不含回收站）
    results, _ = scan_all(specs)
    report = ScanReport(categories=results)

    if args.list:
        from .scanner import print_report

        print_report(results)
        return 0

    # --- 交互式菜单 ---
    if not args.clean and not args.all:
        return _interactive(results, specs, mode, args)

    # --- 选择分类 ---
    selected: list[CategoryResult] = []
    empty_bin = False
    keys: list[str] = []
    if args.all:
        selected = results
        empty_bin = True
    else:
        keys = [k.strip().lower() for k in args.clean.split(",") if k.strip()]
        known = {r.key.lower() for r in results}
        selected = [r for r in results if r.key.lower() in keys]
        empty_bin = "recycle_bin" in keys
        missing = [k for k in keys if k not in known and k != "recycle_bin"]
        if missing:
            _echo(f"无法识别的分类: {', '.join(missing)}")
            _echo("可用分类: " + ", ".join(r.key for r in results))
            return 1

    _run_clean_flow(
        report.categories,
        specs,
        selected,
        dry_run=args.dry_run,
        mode=mode,
        auto_confirm=args.yes,
        empty_bin=empty_bin,
    )
    return 0


def _interactive(results: list[CategoryResult], specs: list[dict[str, Any]], mode: CleanMode, args) -> int:
    """交互式菜单。"""
    from .scanner import print_report

    _echo("=== PC Junk Cleaner ===")
    _echo(f"回收站支持: {'是' if recycle_available() else '否（将永久删除，请谨慎）'}")
    print_report(results)

    selected: list[CategoryResult] = []
    empty_bin = False
    n = len(results)
    while True:
        _print_full_menu(results)
        choice = ""
        try:
            choice = input("请选择要清理的分类（如 1,3 或 all；q 退出）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice.lower() in ("q", "quit", "exit"):
            return 0
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

    if not selected and not empty_bin:
        _echo("未选择任何内容，退出。")
        return 0

    _run_clean_flow(
        results,
        specs,
        selected,
        dry_run=args.dry_run,
        mode=mode,
        auto_confirm=False,
        empty_bin=empty_bin,
    )
    return 0


def _json_stdout_mode(args, specs: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    """--json 输出模式：只做扫描（+可选清理），把结果以 JSON 打印到 stdout。"""
    results, _ = scan_all(specs)
    payload: dict[str, Any] = {
        "version": __version__,
        "recycle_available": recycle_available(),
        "categories": [
            {
                "key": r.key,
                "label": r.label,
                "count": r.total_count,
                "size_bytes": r.liberatable,
                "size": format_size(r.liberatable),
            }
            for r in results
        ],
        "total_size_bytes": sum(r.liberatable for r in results),
    }
    if args.clean or args.all:
        keys = (
            ["recycle_bin"]
            if args.all
            else [k.strip().lower() for k in args.clean.split(",") if k.strip()]
        )
        selected = [r for r in results if r.key.lower() in keys]
        targets = _collect_targets(selected)
        mode = args.mode or (CleanMode.RECYCLE if recycle_available() else CleanMode.PERMANENT)
        res = delete_targets(
            targets,
            mode,
            on_progress=_progress_line,
        )
        payload["action"] = {
            "mode": mode.value,
            "deleted": res["deleted"],
            "failed": res["failed"],
            "freed_bytes": res["freed"],
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
