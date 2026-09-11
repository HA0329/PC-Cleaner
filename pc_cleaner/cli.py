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

v0.8.1 安全增强：
- 移除了本地 `_filters_from_args` 定义，统一由 `menu.py` 提供，避免循环导入。
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
from .config import load_config, resolve_workers, save_config
from .console import dim, red, yellow
from .engine import CleanMode, delete_targets, empty_recycle_bin, recycle_available
from .service import (
    EXIT_DELETE_FAILED,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_NEEDS_CONFIRM,
    EXIT_OK,
    SCHEMA_VERSION,
    dangerous_reasons,
    envelope,
    exit_code_for,
)
from .menu import (          # 从 menu 导入所有需要的交互函数，包括 _filters_from_args
    _apply_target_filters,
    _collect_targets,
    _filters_from_args,      # 现在由 menu 提供
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
from .ui import (
    ScanProgressDisplay,
    _echo,
    _echo_err,
    is_elevated,
    print_startup_banner,
)


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
    scan_group.add_argument("--workers", type=int, default=None, metavar="N",
                           help="并行扫描线程数（1=串行，0=按 CPU 自动，默认取配置 scan_workers）")
    scan_group.add_argument("--json", action="store_true",
                           help="以 JSON 格式输出扫描结果（非交互终端/管道下自动启用）")

    # 清理操作
    clean_group = p.add_argument_group("清理操作")
    clean_group.add_argument("--clean", metavar="KEY[,KEY...]",
                            help="直接清理指定分类（如 system_temp,web_cache；支持 recycle_bin）")
    clean_group.add_argument("--all", action="store_true",
                            help="选中所有非高风险分类（默认不含回收站，见 all_includes_recycle_bin）")
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
    mgmt_group.add_argument("--health", action="store_true",
                           help="只读体检报告（16 项：更新/安全启动/设备/磁盘/日志等，"
                                "不修改任何设置）")
    mgmt_group.add_argument("--mcp", action="store_true",
                           help="以 MCP 服务端（stdio JSON-RPC）运行，供 AI Agent 调用"
                                "（默认只读：scan/health/history/preview_delete）")
    mgmt_group.add_argument("--mcp-allow-delete", action="store_true",
                           help="配合 --mcp：额外暴露 delete/undo 写能力（仍需两阶段确认）")
    mgmt_group.add_argument("--lang", metavar="LANG",
                           help="界面语言，如 zh_CN / en（默认读配置 language、"
                                "环境变量 PC_CLEANER_LANG，未知语言回退 zh_CN）")
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
    cfg_group.add_argument("--audit-rules", action="store_true",
                           help="配合 --validate-rules：额外列出在本机匹配不到任何"
                                "路径的规则（软件升级改了目录名时会失效，v0.9.8）")
    cfg_group.add_argument("--json-schema", action="store_true",
                           help="输出 --json 的 JSON Schema（机器可读契约，供调用方校验）")
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

    # v0.9.4：MCP 服务端（stdio JSON-RPC）。必须在自动 JSON 切换之前处理，
    # 且 stdout 只承载协议——因此这里不做任何 _echo。
    if getattr(args, "mcp", False):
        from .mcp import serve

        return serve(allow_delete=bool(getattr(args, "mcp_allow_delete", False)), deep=bool(args.deep))

    cfg = load_config()

    # v0.9.4：语言（--lang > 配置 language > 环境变量 PC_CLEANER_LANG > zh_CN）
    try:
        from .i18n import set_language

        set_language(args.lang or cfg.get("language") or None)
    except Exception:  # noqa: BLE001 i18n 不可用时保持原样
        pass

    if args.version:
        _echo(f"pc-junk-cleaner {__version__}")
        return 0

    if args.show_config:
        path = save_config(load_config())
        _echo(f"配置文件: {path}")
        _echo(json.dumps(load_config(), ensure_ascii=False, indent=2))
        return 0

    if args.validate_rules:
        return _cmd_validate_rules(audit_local=bool(getattr(args, "audit_rules", False)))
    if args.show_rules:
        return _cmd_show_rules(deep=args.deep)
    if getattr(args, "json_schema", False):
        # v0.9.4：输出机器可读的 JSON 契约（供 Agent / 脚本校验输出）
        from .service import ENVELOPE_SCHEMA

        _echo(json.dumps(ENVELOPE_SCHEMA, ensure_ascii=False, indent=2))
        return EXIT_OK

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

    # 并行扫描线程数（--workers 优先，其次配置 scan_workers）
    workers = args.workers if args.workers is not None else resolve_workers(cfg)

    # 高级清理过滤参数（从 menu 导入的 _filters_from_args）
    ext_filter, min_size_bytes, older_than_secs, shred_passes = _filters_from_args(args)

    # 是否显示进度
    show_progress = not args.no_progress and cfg.get("show_scan_progress", True)

    # 是否详细展示
    show_detail = args.detail or cfg.get("default_detail", False)
    show_tree = args.tree or cfg.get("compact_tree_view", False)

    # v0.9.6：交互启动先给一行反馈，避免「双击后长时间只有光标闪烁」。
    # 扫描进度条第一帧出现后会自动接续到下一行（进度行自带 \r 与行尾清除）。
    # 只在 stderr 是 TTY 时打印，--json / 重定向场景不产生多余输出。
    # v0.9.7：启动横幅 + 加载提示（版本 / 模式 / 深度 / 线程），让用户第一眼就知道
    # 当前配置与"程序在干活"。与进度一样只在 stderr 是 TTY 时输出；
    # --json / 管道 / MCP 场景完全静默（不污染 stdout）。
    banner_enabled = (
        show_progress
        and not show_detail
        and not show_tree
        and sys.stderr.isatty()
        and not args.export_scan
    )
    if banner_enabled:
        print_startup_banner(
            version=__version__,
            mode=mode.value,
            workers=workers,
            depth=scan_depth,
            deep=bool(args.deep),
        )
        _echo_err(dim("  正在加载规则并扫描缓存目录 ..."), end="", flush=True)

    # 只导出扫描结果：先于「管道自动 JSON」处理，避免重定向时只输出 JSON 而没写文件
    if args.export_scan:
        progress = ScanProgressDisplay(
            enabled=show_progress, total_categories=len(specs)
        )
        # v0.9.8：同主扫描，Ctrl+C 不再抛 traceback
        try:
            results = scan_all(
                specs,
                scan_depth=scan_depth,
                on_progress=progress if show_progress else None,
                workers=workers,
                on_category_done=progress.category_done if show_progress else None,
            )
        except KeyboardInterrupt:
            progress.finish([])
            print()
            _echo(yellow("已取消扫描（Ctrl+C），未做任何修改。"))
            return EXIT_INTERRUPTED
        progress.finish(results)
        return _cmd_export_scan(results, args.export_scan)

    # --health：只读体检（在自动 JSON 切换之前处理，避免被当成普通扫描）
    if args.health:
        return _cmd_health(json_mode=bool(args.json))

    # 管道输出时自动切换 JSON
    if (
        not args.json
        and not sys.stdout.isatty()
        and not (args.clean or args.all)
        and not args.checkup
    ):
        args.json = True

    if args.json:
        return _json_stdout_mode(
            args, specs, cfg, mode, excluded, show_risky, scan_depth, workers
        )

    if args.checkup:
        return _cmd_checkup(
            specs, show_risky, scan_depth, show_progress, deep=args.deep, workers=workers
        )

    # 全量扫描
    # v0.9.8：扫描是最耗时的阶段（本机约 25 秒），此前没有任何 KeyboardInterrupt
    # 处理，Ctrl+C 会直接抛出 traceback 并丢掉全部扫描结果，约定的退出码 130
    # 也走不到。现在统一捕获，给出中文提示并返回 130。
    progress = ScanProgressDisplay(
        enabled=show_progress and not show_detail and not show_tree,
        total_categories=len(specs),
    )
    try:
        all_results = scan_all(
            specs,
            scan_depth=scan_depth,
            on_progress=progress if show_progress else None,
            workers=workers,
            on_category_done=progress.category_done if show_progress else None,
        )
    except KeyboardInterrupt:
        progress.finish([])
        print()
        _echo(yellow("已取消扫描（Ctrl+C），未做任何修改。"))
        return EXIT_INTERRUPTED
    progress.finish(all_results)

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
    # v0.9.4：用 `is None` 判断"是否给了 --clean"，这样 `--clean ""` 会走下面的
    # 参数校验分支报错，而不是静默进入交互菜单。
    if args.clean is None and not args.all:
        return _interactive(
            results, specs, mode, args, cfg, show_risky,
            sort_by, scan_depth, show_progress, deep=args.deep, workers=workers,
        )

    # --- 选择分类 ---
    selected: list[CategoryResult] = []
    empty_bin = False
    keys: list[str] = []
    # v0.9.3：--exclude 的分类名必须校验。此前拼错（如 download 而非 downloads）
    # 会被静默忽略，等于"把本想排除的分类照常清理了"。
    known_all = {r.key.lower() for r in all_results}
    bad_excluded = [k for k in excluded if k not in known_all and k != "recycle_bin"]
    if bad_excluded:
        _echo(red(f"无法识别的 --exclude 分类: {', '.join(bad_excluded)}"))
        _echo("可用分类: " + ", ".join(r.key for r in results))
        return EXIT_ERROR
    if args.all:
        selected = [r for r in results if r.key.lower() not in excluded]
        # 安全默认：--all 不再隐式清空回收站（清空不可恢复），
        # 需显式 --clean recycle_bin 或配置 all_includes_recycle_bin=true
        empty_bin = bool(cfg.get("all_includes_recycle_bin", False)) and "recycle_bin" not in excluded
    else:
        keys = _parse_keys(args.clean)
        # v0.9.4：--clean 给了但解析为空（如 --clean "" 或只有分隔符）→ 明确报错，
        # 此前会静默退化成"只扫描、exit 0"，自动化场景下看起来像"清理成功"。
        if not keys:
            _echo(red("--clean 参数为空：请指定分类名（用 --list 查看可用分类）"))
            _echo("可用分类: " + ", ".join(r.key for r in results))
            return EXIT_ERROR
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
        # 只选了 recycle_bin（或其它分类都被 --exclude 排除）时：
        # 只要还有「清空回收站」这件事要做，就必须继续走清理流程，
        # 不能因为 selected 为空就静默返回（旧版会直接 return 0，什么也没发生）。
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

    result = _run_clean_flow(
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
        allow_dangerous=bool(args.risky),
    )
    return exit_code_for(result)


def _cmd_health(json_mode: bool = False) -> int:
    """``--health``：只读体检（复用 :mod:`pc_cleaner.health`）。

    - 有 ``error`` 级项 → 退出码 1；只有 ``warn`` → 退出码 0（警告不算失败）；
    - ``--json`` 时输出带契约版本的信封，便于自动化解析。
    """
    try:
        from .health import collect
    except Exception as exc:  # noqa: BLE001
        _echo(red(f"体检模块不可用: {exc}"))
        return EXIT_ERROR
    try:
        report = collect()
    except Exception as exc:  # noqa: BLE001
        _echo(red(f"体检执行失败: {exc}"))
        return EXIT_ERROR
    counts = report.counts()
    code = EXIT_ERROR if counts.get("error") else EXIT_OK
    if json_mode:
        payload = envelope("ok" if code == EXIT_OK else "error", code)
        payload["health"] = report.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _echo(report.to_text())
    return code


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
    workers: int = 0,
) -> int:
    """--json 输出模式（返回退出码，见 :mod:`pc_cleaner.service` 的 ``EXIT_*``）。

    行为约定（自动化安全，v0.9.3 收紧）：
    - 不带 ``--clean/--all``：只返回扫描结果（退出码 0）；
    - ``--clean/--all --dry-run``：返回 ``action.dry_run=true`` 与
      ``action.would_delete``（目标预览），**不删除**；
    - ``--clean/--all --yes``（且非 ``--dry-run``）：真正删除并返回结果；
      **但若包含危险操作（高风险分类 / 永久删除 / 清空回收站）且未显式
      ``--risky`` 授权，则拒绝执行**（``status="needs_confirmation"``，退出码 4）；
    - ``--clean/--all`` 未给 ``--yes``：返回 ``action.skipped=true`` 与预览
      （退出码 4，表示"什么都没做"）；
    - 删除过程中有目标失败 → 退出码 3。
    """
    # v0.9.8：扫描期间 Ctrl+C 也要遵守 JSON 契约——stdout 仍然只输出**一个**
    # 合法 JSON 对象（status="interrupted" / 退出码 130），而不是把 Python
    # 堆栈打到 stderr、让调用方拿到半个信封。
    try:
        results = scan_all(specs, scan_depth=scan_depth, workers=workers)
    except KeyboardInterrupt:
        interrupted = envelope("interrupted", EXIT_INTERRUPTED)
        interrupted["categories"] = []
        interrupted["action"] = {"not_executed": True, "reason": "用户中断（Ctrl+C）"}
        print(json.dumps(interrupted, ensure_ascii=False, indent=2))
        return EXIT_INTERRUPTED
    payload: dict[str, Any] = {
        "version": __version__,
        "recycle_available": recycle_available(),
        "admin": is_admin(),
        "elevated": is_elevated(),
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

    exit_code = EXIT_OK
    status = "scan"
    # v0.9.4：用 `is not None` 判断，`--clean ""` 也要进入分支（随后报错）
    if args.clean is not None or args.all:
        if args.all:
            keys = [r.key for r in results if show_risky or r.risk != "risky"]
            # 与交互模式保持一致：--all 默认不含回收站，避免自动化场景误清空
            if cfg.get("all_includes_recycle_bin", False):
                keys.append("recycle_bin")
        else:
            keys = _parse_keys(args.clean)
        keys = [k for k in keys if k not in excluded]

        # v0.9.4：--clean 给了但解析为空 → 报错（不再静默退化成只扫描）
        if args.clean is not None and not keys and not args.all:
            payload["action"] = {
                "not_executed": True,
                "error": "--clean 参数为空：请指定分类名",
                "known_categories": sorted({r.key.lower() for r in results}),
            }
            payload.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "status": "error",
                    "exit_code": EXIT_ERROR,
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_ERROR

        # v0.9.3：分类名必须校验——此前 --clean/--exclude 拼错一个字母会被静默忽略，
        # 自动化场景下表现为"exit 0 但其实什么都没删/删错了分类"。
        known = {r.key.lower() for r in results}
        bad_excluded = [
            k for k in excluded if k not in known and k != "recycle_bin"
        ]
        if bad_excluded:
            payload["action"] = {
                "not_executed": True,
                "error": f"无法识别的 --exclude 分类: {', '.join(bad_excluded)}",
                "known_categories": sorted(known),
            }
            payload.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "status": "error",
                    "exit_code": EXIT_ERROR,
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_ERROR
        missing = [k for k in keys if k.lower() not in known and k.lower() != "recycle_bin"]
        if missing:
            payload["action"] = {
                "not_executed": True,
                "error": f"无法识别的分类: {', '.join(missing)}",
                "known_categories": sorted(known),
            }
            payload.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "status": "error",
                    "exit_code": EXIT_ERROR,
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_ERROR

        keyset = {k.lower() for k in keys}
        selected = [
            r for r in results
            if r.key.lower() in keyset and not r.admin_blocked
        ]
        reasons = dangerous_reasons(selected, mode, keys)
        preview = _json_target_preview(results, keys, args)

        if args.dry_run:
            payload["action"] = {
                "dry_run": True,
                "selected": keys,
                "target_count": len(preview),
                "would_delete": preview,
                "would_empty_recycle_bin": "recycle_bin" in keys,
                "dangerous": reasons,
            }
            status = "dry_run"
        elif args.yes:
            if reasons and not args.risky:
                # v0.9.3 安全闸门：非交互自动化下，危险操作必须显式 --risky 授权
                payload["action"] = {
                    "not_executed": True,
                    "needs_confirmation": True,
                    "reasons": reasons,
                    "selected": keys,
                    "target_count": len(preview),
                    "would_delete_with_yes": preview,
                }
                exit_code = EXIT_NEEDS_CONFIRM
                status = "needs_confirmation"
            else:
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
                    "skipped": res.get("skipped", 0),
                    "skipped_in_use": res.get("skipped_in_use", 0),
                    "freed_bytes": res["freed"],
                    "recycled_bytes": res.get("recycled", 0),
                    "selected": keys,
                }
                if "recycle_bin" in keys:
                    bin_res = empty_recycle_bin()
                    action["recycle_bin"] = bin_res
                    # v0.9.3：清空失败要计入 failed，不能只看删除结果
                    if bin_res.get("failed"):
                        action["failed"] = action["failed"] + 1
                payload["action"] = action
                if action["failed"]:
                    exit_code = EXIT_DELETE_FAILED
                    status = "partial"
                else:
                    status = "deleted"
        else:
            payload["action"] = {
                "not_executed": True,
                "reason": "--json 模式下删除需要同时使用 --yes（且不要 --dry-run）",
                "selected": keys,
                "target_count": len(preview),
                "would_delete_with_yes": preview,
                "would_empty_recycle_bin": "recycle_bin" in keys,
                "dangerous": reasons,
            }
            exit_code = EXIT_NEEDS_CONFIRM
            status = "needs_confirmation"

    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "ok": exit_code == EXIT_OK,
            "status": status,
            "exit_code": exit_code,
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())