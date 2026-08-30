"""共享 UI 工具：终端输出、确认提示、风险徽标与扫描进度显示。

从 cli.py 拆分出来的公共小工具，供 cli / menu / commands 三个模块复用：
- ``_echo`` / ``_echo_err``：标准输出 / 错误输出（进度与警告走 stderr，
  避免污染 stdout 上的 ``--json`` 输出）；
- ``prompt_yes_no``：交互确认；
- ``_risk_badge`` / ``_admin_tag``：风险与管理员徽标；
- ``ScanProgressDisplay``：扫描进度的终端显示控制器（非 TTY 自动静默）。
"""

from __future__ import annotations

import sys
import time

from .console import bold, cyan, dim, green, red, yellow, progress_bar
from .models import format_size


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


def _risk_badge(risk: str) -> str:
    """风险等级彩色徽标。"""
    if risk == "safe":
        return green("●")
    if risk == "moderate":
        return yellow("●")
    return red("●")


def _admin_tag(requires_admin: bool) -> str:
    return yellow(" [需管理员]") if requires_admin else ""


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
