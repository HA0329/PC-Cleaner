"""管理类子命令：历史、撤销、体检、配置导入导出、规则展示/校验、提权重启。

从 cli.py 拆分出来的「管理命令」模块，每个函数对应一个独立子命令，
只做一件事，便于维护与单独测试。

安全增强（v0.8.1）：
- `_relaunch_as_admin` 在提权前设置环境变量 `PC_CLEANER_ELEVATED=1`，
  以便新进程检测到已提权状态，在菜单中显示 `[ADMIN]` 标识。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import os  # 安全增强：用于设置环境变量
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULTS, load_config, update_config
from .console import (
    bold, cyan, dim, green, red, yellow, magenta,
    pad_cjk, separator, progress_bar,
)
from .engine import CleanMode, recycle_available, restore_paths
from .history import load_history
from .models import format_size
from .rules import _builtin_specs, validate_rules
from .scanner import _system_drives, is_admin, recycle_bin_size, scan_all
from .ui import ScanProgressDisplay, _admin_tag, _echo, _risk_badge


# ---------------------------------------------------------------------------
# 历史 / 撤销
# ---------------------------------------------------------------------------
def _cmd_history() -> int:
    """显示清理历史（--history）。"""
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
    """恢复最近一次「进回收站」的清理（--undo-last）。"""
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


# ---------------------------------------------------------------------------
# 运行环境适配（--checkup 内嵌只读小节）
# ---------------------------------------------------------------------------
def _cmd_print_env_adaptation() -> None:
    """打印「运行环境适配」：本机实际装了什么、哪些缓存可清、什么只能应用内清。

    全部只读探测（见 :mod:`pc_cleaner.env`）；任何单项失败不影响其它输出。
    """
    from .env import probe_environment, wechat_data_summary

    try:
        env = probe_environment(measure_wechat_size=True)
    except Exception as exc:  # noqa: BLE001
        _echo(dim(f"  （运行环境探测失败，跳过本小节: {exc}）"))
        return

    _echo(f"  {bold('运行环境适配')}")
    try:
        _echo(f"    系统: {env.get('os_caption', '?')} · {env.get('arch', '?')} · Python {env.get('python', '?')}")
    except Exception:  # noqa: BLE001
        pass

    # 浏览器
    try:
        browsers = env.get("browsers") or []
        installed = [b for b in browsers if b.get("installed")]
        if installed:
            _echo("    浏览器: " + green(" · ".join(f"{b['name']} ✓" for b in installed)))
    except Exception:  # noqa: BLE001
        pass

    # GPU
    try:
        gpu = env.get("gpu") or []
        if gpu:
            _echo("    GPU: " + green(" · ".join(v.upper() for v in gpu)) + dim("（着色器缓存可安全清理并自动重建）"))
    except Exception:  # noqa: BLE001
        pass

    # 微信
    try:
        wechat = env.get("wechat") or {}
        layout = wechat.get("layout")
        if layout:
            layout_txt = {"wechat4": "微信 4.x（新版 Weixin）", "wechat3": "微信 3.x"}.get(layout, layout)
            loc = "roaming %APPDATA%/Tencent/xwechat" if wechat.get("roaming4") else "roaming %APPDATA%/Tencent/WeChat"
            _echo(f"    微信: {green(layout_txt)}（{loc} 运行缓存可清）")
            for line in wechat_data_summary(env):
                _echo(f"      {yellow('⚠')} {dim(line)}")
    except Exception:  # noqa: BLE001
        pass

    # Steam / pnpm store
    try:
        steam = env.get("steam") or {}
        if steam.get("installed"):
            libs = steam.get("library_dirs") or []
            txt = "Steam ✓"
            if libs:
                txt += dim(f"（库: {', '.join(libs)}）")
            _echo("    游戏: " + green(txt))
    except Exception:  # noqa: BLE001
        pass
    try:
        stores = env.get("pnpm_stores") or []
        stores = [s for s in stores if s.lower().endswith(".pnpm-store")]
        if stores:
            _echo("    pnpm store: " + green(" · ".join(stores)) + dim("（dev_caches 分类可清）"))
    except Exception:  # noqa: BLE001
        pass

    # 开发工具
    try:
        tools = sorted(k for k, v in (env.get("dev_tools") or {}).items() if v)
        if tools:
            _echo("    开发工具: " + green(", ".join(tools)))
    except Exception:  # noqa: BLE001
        pass

    # 未检测到的浏览器 → 对应缓存分类为空的原因提示
    try:
        absent = [b["name"] for b in (env.get("browsers") or []) if not b.get("installed")]
        if absent:
            _echo(dim(f"    未检测到: {', '.join(absent)}（相关浏览器缓存分类将显示为空）"))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 一键体检
# ---------------------------------------------------------------------------
def _cmd_checkup(
    specs: list[dict[str, Any]],
    show_risky: bool,
    scan_depth: int,
    show_progress: bool,
    deep: bool = False,
) -> int:
    """一键体检：只读汇总各项状态。"""
    _echo(bold(f"=== PC Junk Cleaner {__version__} 体检报告 ==="))
    mode_tag = "深度 (deep)" if deep else "标准"
    _echo(dim(f"  扫描模式: {mode_tag} · 遍历深度 {scan_depth} 层"))
    _echo("")

    # 系统信息
    _echo(f"  {bold('系统状态')}")
    _echo(f"    管理员权限: {'✓ ' + green('是') if is_admin() else '✗ ' + yellow('否（系统深度清理将跳过，可用 --admin 提权）')}")
    _echo(f"    回收站支持: {'✓ ' + green('是（删除可恢复）') if recycle_available() else '✗ ' + yellow('否（将永久删除，建议 pip install send2trash）')}")
    _echo("")

    # 运行环境适配（只读探测本机实际安装了哪些东西）
    _cmd_print_env_adaptation()
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


# ---------------------------------------------------------------------------
# 配置导入导出
# ---------------------------------------------------------------------------
def _cmd_export_config(path: str) -> int:
    """导出配置到 JSON 文件。"""
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
    """从 JSON 文件导入配置。"""
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


# ---------------------------------------------------------------------------
# 规则展示 / 校验
# ---------------------------------------------------------------------------
def _cmd_show_rules(deep: bool = False) -> int:
    """可视化展示 rules.json 中的内置清理规则。"""
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


# ---------------------------------------------------------------------------
# 扫描结果导出
# ---------------------------------------------------------------------------
def _cmd_export_scan(results, path: str) -> int:
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


# ---------------------------------------------------------------------------
# 提权重启（安全增强：设置环境变量标记）
# ---------------------------------------------------------------------------
def _relaunch_as_admin(argv: list[str]) -> int:
    """通过 UAC 以管理员身份重新启动（Windows）。

    安全增强：
    - 在提权前设置环境变量 `PC_CLEANER_ELEVATED=1`，新进程可检测到已提权状态。
    - 移除 `--admin` 参数，防止无限循环。
    """
    import ctypes

    # 移除 --admin 避免死循环
    new_argv = [a for a in argv if a != "--admin"]
    params = f"-m pc_cleaner {' '.join(new_argv)}".strip()
    _echo(yellow("请求管理员权限（UAC），将重新启动..."))

    # 安全增强：设置环境变量，标记当前已提权
    os.environ["PC_CLEANER_ELEVATED"] = "1"

    try:
        # 使用 ShellExecuteW 以管理员身份运行
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