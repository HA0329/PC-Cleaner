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
from typing import Any

from . import __version__
from .config import load_config, save_config
from .console import bold, cyan, dim, green, pad_cjk, red, yellow
from .engine import (
    CleanMode,
    delete_targets,
    empty_recycle_bin,
    recycle_available,
)
from .models import CategoryResult, format_size
from .rules import get_all_category_specs
from .scanner import recycle_bin_size, scan_all


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


def _print_full_menu(results: list[CategoryResult], bin_size: int) -> None:
    _echo("")
    _echo(bold("可清理的分类："))
    for i, res in enumerate(results, start=1):
        if res.targets:
            _echo(
                f"  {cyan(str(i))}. {res.label}  "
                f"({res.total_count} 项, 约 {green(format_size(res.liberatable))})"
            )
        else:
            _echo(f"  {cyan(str(i))}. {res.label}  (0 项)")
    if bin_size > 0:
        _echo(f"  {red('r')}. 回收站 (清空, 占用 {yellow(format_size(bin_size))})")
    else:
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
    result: dict[str, Any] = {"deleted": 0, "failed": 0, "freed": 0, "empty_bin": empty_bin}
    if targets:
        res = delete_targets(
            targets,
            mode,
            on_progress=_progress_line,
            recycle_fallback=recycle_fallback,
        )
        result["deleted"] += res["deleted"]
        result["failed"] += res["failed"]
        result["freed"] += res["freed"]

    if empty_bin:
        bin_res = empty_recycle_bin()
        result["empty_bin_result"] = bin_res
        _echo("回收站清空完成。")

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

    # 配置可限制只扫描部分分类
    enabled = [k.lower() for k in (cfg.get("enabled_categories") or []) if k]
    if enabled:
        specs = [s for s in specs if s["key"].lower() in enabled]

    mode = _resolve_mode(args, cfg)
    excluded = set(_parse_keys(args.exclude)) if args.exclude else set()

    if args.json:
        _json_stdout_mode(args, specs, cfg, mode, excluded)
        return 0

    # 扫描（不含回收站）
    results = scan_all(specs)

    if args.list:
        from .scanner import print_report

        print_report(results)
        _echo("")
        _print_disk_free(results)
        if sys.platform == "win32":
            _echo(f"回收站占用: {format_size(recycle_bin_size())}")
        return 0

    # --- 交互式菜单 ---
    if not args.clean and not args.all:
        return _interactive(results, specs, mode, args, cfg)

    # --- 选择分类 ---
    selected: list[CategoryResult] = []
    empty_bin = False
    keys: list[str] = []
    if args.all:
        selected = [r for r in results if r.key.lower() not in excluded]
        empty_bin = "recycle_bin" not in excluded
    else:
        keys = _parse_keys(args.clean)
        known = {r.key.lower() for r in results}
        selected = [r for r in results if r.key.lower() in keys and r.key.lower() not in excluded]
        empty_bin = "recycle_bin" in keys and "recycle_bin" not in excluded
        missing = [k for k in keys if k not in known and k != "recycle_bin"]
        if missing:
            _echo(red(f"无法识别的分类: {', '.join(missing)}"))
            _echo("可用分类: " + ", ".join(r.key for r in results))
            return 1
        if not selected and not empty_bin and keys:
            _echo(yellow("所选分类均已被 --exclude 排除，未执行任何操作。"))
            return 0

    _run_clean_flow(
        selected,
        dry_run=args.dry_run,
        mode=mode,
        auto_confirm=args.yes,
        empty_bin=empty_bin,
        cfg=cfg,
        recycle_fallback=args.recycle_fallback or bool(cfg.get("recycle_error_fallback", False)),
    )
    return 0


def _interactive(
    results: list[CategoryResult],
    specs: list[dict[str, Any]],
    mode: CleanMode,
    args,
    cfg: dict[str, Any],
) -> int:
    """交互式菜单：选择 -> 预览 -> 确认 -> 清理，可循环继续。"""
    from .scanner import print_report

    _echo("")
    _echo(bold(f"=== PC Junk Cleaner v{__version__} ==="))
    if recycle_available():
        _echo(green("回收站支持: 是（删除可恢复）"))
    else:
        _echo(yellow("回收站支持: 否（将永久删除，请谨慎！建议 pip install send2trash）"))

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
            _print_full_menu(results, bin_size)
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
        )

        # 循环：重新扫描并继续
        try:
            again = prompt_yes_no("是否继续扫描并清理其他内容？", default=True)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not again:
            return 0
        results = scan_all(specs)


def _json_stdout_mode(
    args,
    specs: list[dict[str, Any]],
    cfg: dict[str, Any],
    mode: CleanMode,
    excluded: set[str],
) -> None:
    """--json 输出模式。

    默认只扫描。仅当同时给出 ``--yes``（且非 ``--dry-run``）时才会真正删除，
    避免自动化场景下静默误删。
    """
    results = scan_all(specs)
    payload: dict[str, Any] = {
        "version": __version__,
        "recycle_available": recycle_available(),
        "dry_run": args.dry_run,
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
    if sys.platform == "win32":
        payload["recycle_bin_size_bytes"] = recycle_bin_size()

    if args.clean or args.all:
        if args.all:
            keys = [r.key for r in results] + ["recycle_bin"]
        else:
            keys = _parse_keys(args.clean)
        keys = [k for k in keys if k not in excluded]

        if args.dry_run:
            payload["action"] = {"dry_run": True, "selected": keys}
        elif args.yes:
            selected = [r for r in results if r.key in keys]
            targets = _collect_targets(selected)
            res = delete_targets(
                targets,
                mode,
                on_progress=_progress_line,
                recycle_fallback=args.recycle_fallback
                or bool(cfg.get("recycle_error_fallback", False)),
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
