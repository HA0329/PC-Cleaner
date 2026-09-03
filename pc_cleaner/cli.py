"""命令行入口：参数解析、主流程编排与 ``--json`` 输出模式。

职责划分（v0.7 起）：
- 本文件只保留参数解析、``main`` 主流程与 ``--json`` 输出；
- 交互菜单 / 预览 / 清理执行 → :mod:`pc_cleaner.menu`；
- 管理子命令（history / undo / checkup / 配置导入导出 / 规则展示校验 / 提权）
  → :mod:`pc_cleaner.commands`；
- 共享 UI 工具（输出、确认、进度）→ :mod:`pc_cleaner.ui`。

用法原则：
- 加入 ``recycle_bin`` 特殊分类（由 engine.empty_recycle_bin 处理）。
- 默认进入交互式菜单；用 ``--list`` 只扫描、``--clean`` 直接清理指定分类。
- 所有删除前都会先预览并确认（除非显式 ``--yes``）。
- ``--json`` 输出模式默认**只扫描**；要真正删除必须同时给 ``--yes``
  （自动化场景下避免静默误删）；``--dry-run`` 与 ``--yes`` 同时使用时
  以 ``--dry-run`` 为准（不会删除），并返回 ``would_delete`` 目标预览。

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

v0.7 变更：
- cli.py 拆分：交互菜单/清理执行 → menu.py，管理子命令 → commands.py，
  共享 UI 工具 → ui.py（本文件保持向后兼容的导出名）；
- 交互菜单每轮热重载 rules.json / 配置（编辑后无需重启）；
- ``--json --dry-run``（或未给 ``--yes``）时返回 ``would_delete`` 目标预览。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .commands import (
    _cmd_checkup,
    _cmd_export_config,
    _cmd_export_scan,
    _cmd_history,
    _cmd_import_config,
    _cmd_show_rules,
    _cmd_undo_last,
    _cmd_validate_rules,
    _relaunch_as_admin,
)
from .config import load_config, save_config
from .console import dim, red, yellow
from .engine import CleanMode, delete_targets, empty_recycle_bin, recycle_available
from .menu import (
    _apply_target_filters,
    _collect_targets,
    _filters_from_args,
    _interactive,
    _parse_ext_filter,
    _parse_selection,
    _print_disk_free,
    _progress_line,
    _run_clean_flow,
)
from .models import CategoryResult, format_size
from .rules import get_enabled_category_specs
from .scanner import is_admin, print_detail_report, recycle_bin_size, scan_all
from .ui import ScanProgressDisplay, _echo


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _parse_keys(raw: str) -> list[str]:
    """把逗号/中文逗号/分号分隔的 key 列表拆成小写列表。"""
    return [
        k.strip().lower()
        for k in raw.replace("，", ",").replace("；", ",").replace("、", ",").split(",")
        if k.strip()
    ]


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
    scan_group.add_argument("--sort", choices=["size_desc", "size_asc", "name_asc", "name_desc", "count_desc"],
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

    specs = get_enabled_category_specs(cfg, deep=args.deep)

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
        return _interactive(
            results, specs, mode, args, cfg, show_risky,
            sort_by, scan_depth, show_progress, deep=args.deep,
        )

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


# ---------------------------------------------------------------------------
# --json 输出模式
# ---------------------------------------------------------------------------
def _json_target_preview(
    results: list[CategoryResult], keys: list[str], args
) -> list[dict[str, Any]]:
    """构造 ``--json`` 模式下"将要删除"的目标预览列表。

    用于 ``--dry-run`` 或未给 ``--yes`` 时的自动化预览：
    返回按当前过滤参数（--ext / --min-size-mb / --older-than-days）
    过滤后的目标清单。
    """
    keyset = {k.lower() for k in keys}
    selected = [r for r in results if r.key.lower() in keyset and not r.admin_blocked]
    targets = _collect_targets(selected)
    ext_filter, min_size_bytes, older_than_secs, _ = _filters_from_args(args)
    targets = _apply_target_filters(
        targets,
        ext_filter=ext_filter,
        min_size_bytes=min_size_bytes,
        older_than_secs=older_than_secs,
    )
    return [
        {
            "path": str(t.path),
            "kind": t.kind.value,
            "action": t.action.value,
            "size_bytes": t.size,
            "size": t.display_size,
        }
        for t in targets
    ]


def _json_stdout_mode(
    args,
    specs: list[dict[str, Any]],
    cfg: dict[str, Any],
    mode: CleanMode,
    excluded: set[str],
    show_risky: bool = False,
    scan_depth: int = 20,
) -> None:
    """--json 输出模式。

    行为约定（自动化安全）：
    - 不带 ``--clean/--all``：只返回扫描结果；
    - ``--clean/--all --dry-run``：返回 ``action.dry_run=true`` 与
      ``action.would_delete``（目标预览），**不删除**；
    - ``--clean/--all --yes``（且非 ``--dry-run``）：真正删除并返回结果；
    - ``--clean/--all`` 未给 ``--yes``：返回 ``action.skipped=true`` 与
      ``action.would_delete_with_yes``（预览），**不删除**。
    """
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
            payload["action"] = {
                "dry_run": True,
                "selected": keys,
                "target_count": len(_json_target_preview(results, keys, args)),
                "would_delete": _json_target_preview(results, keys, args),
            }
        elif args.yes:
            keyset = {k.lower() for k in keys}
            selected = [
                r for r in results
                if r.key.lower() in keyset and not r.admin_blocked
            ]
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
                "selected": keys,
                "target_count": len(_json_target_preview(results, keys, args)),
                "would_delete_with_yes": _json_target_preview(results, keys, args),
            }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
