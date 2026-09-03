"""共享 UI 工具：终端输出、确认提示、风险徽标与扫描进度显示。

从 cli.py 拆分出来的公共小工具，供 cli / menu / commands 三个模块复用：
- ``_echo`` / ``_echo_err``：标准输出 / 错误输出（进度与警告走 stderr，
  避免污染 stdout 上的 ``--json`` 输出）；
- ``prompt_yes_no``：交互确认；
- ``_risk_badge`` / ``_admin_tag``：风险与管理员徽标；
- ``is_elevated``：检测当前进程是否通过 UAC 提权运行（基于环境变量）；
- ``ScanProgressDisplay``：扫描进度的终端显示控制器（非 TTY 自动静默）。

安全增强（v0.8.1）：
- 新增 ``is_elevated()`` 函数，用于检测提权状态，可在菜单中显示 [ADMIN] 标识。
"""

from __future__ import annotations

import sys
import time
import os  # 用于读取环境变量

from .console import bold, cyan, dim, green, red, yellow, progress_bar
from .models import format_size


def _echo(*args, **kwargs) -> None:
    """向 stdout 打印信息（常规输出）。"""
    print(*args, **kwargs)


def _echo_err(*args, **kwargs) -> None:
    """向 stderr 打印信息（进度、警告、错误）。"""
    print(*args, file=sys.stderr, **kwargs)


def prompt_yes_no(question: str, default: bool = False) -> bool:
    """交互式确认：询问是/否，返回 bool 值。

    Args:
        question: 提示问题
        default: 默认返回值（当用户直接按回车时）

    Returns:
        bool: True 表示是，False 表示否
    """
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


def _risk_badge(risk: str) -> str:
    """返回风险等级的彩色徽标。

    - safe:     绿色 ●
    - moderate: 黄色 ●
    - risky:    红色 ●
    """
    if risk == "safe":
        return green("●")
    if risk == "moderate":
        return yellow("●")
    return red("●")


def _admin_tag(requires_admin: bool) -> str:
    """返回管理员权限标记（如果分类需要管理员权限）。"""
    return yellow(" [需管理员]") if requires_admin else ""


def is_elevated() -> bool:
    """检测当前进程是否通过 UAC 提权运行（基于环境变量）。

    该函数由 commands._relaunch_as_admin 在提权成功后设置
    PC_CLEANER_ELEVATED=1，新进程可通过此函数检测，从而在
    交互菜单中显示 [ADMIN] 标识，提醒用户当前为高权限模式。

    Returns:
        bool: 如果环境变量 PC_CLEANER_ELEVATED 为 "1" 则返回 True，否则 False。
    """
    return os.environ.get("PC_CLEANER_ELEVATED", "0") == "1"


class ScanProgressDisplay:
    """扫描进度的终端显示控制器。

    用法：
        progress = ScanProgressDisplay(enabled=show_progress)
        results = scan_all(specs, on_progress=progress)
        progress.finish(results)

    当 enabled=False 或 stderr 不是 TTY 时自动静默，避免污染脚本输出。
    """

    def __init__(self, enabled: bool = True):
        """初始化进度显示器。

        Args:
            enabled: 是否启用进度显示（通常根据命令行 --no-progress 决定）
        """
        self.enabled = enabled and sys.stderr.isatty()
        self._start_time: float | None = None
        self._last_update: str = ""

    def __call__(self, category_label: str, current: int, total: int) -> None:
        """更新扫描进度（每次扫描一个分类时调用）。

        Args:
            category_label: 当前正在扫描的分类名称
            current: 当前分类索引（从1开始）
            total: 分类总数
        """
        if not self.enabled:
            return
        if self._start_time is None:
            self._start_time = time.time()
            _echo_err("")  # 空行开始，避免覆盖已有输出

        elapsed = time.time() - self._start_time
        bar = progress_bar(current, total, width=25)
        line = (
            f"\r  {dim('🔍 扫描中')} {bar} {cyan(category_label)} "
            f"{dim(f'{current}/{total} ({elapsed:.1f}s)')}"
        )

        # 只有内容变化才刷新，减少闪烁
        if line != self._last_update:
            _echo_err(line, end="", flush=True)
            self._last_update = line

    def finish(self, results: list) -> None:
        """扫描完成时调用，输出最终汇总信息。

        Args:
            results: CategoryResult 列表（扫描结果）
        """
        if not self.enabled:
            return
        elapsed = time.time() - self._start_time if self._start_time else 0
        total_targets = sum(len(r.targets) for r in results)
        total_size = sum(r.liberatable for r in results)
        _echo_err(
            f"\r  {green('✓')} 扫描完成：找到 {bold(str(total_targets))} 个目标，"
            f"可释放 {green(format_size(total_size))}，"
            f"耗时 {dim(f'{elapsed:.1f}s')}"
        )
        _echo_err("")  # 空行，为后续输出留出间距