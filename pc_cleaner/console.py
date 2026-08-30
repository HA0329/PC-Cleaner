"""控制台小工具：Windows 虚拟终端（ANSI 颜色）支持与 CJK 对齐。

全部零第三方依赖：
- 在 Windows 上通过 ctypes 打开 ``ENABLE_VIRTUAL_TERMINAL_PROCESSING``，
  让 10+ 的控制台支持 ANSI 颜色；
- 非 TTY（管道/重定向）或打开失败时自动降级为纯文本；
- 提供按东亚宽度对齐的 ``pad_cjk``，用于中文菜单对齐。
"""

from __future__ import annotations

import os
import sys
import unicodedata


def _force_utf8_io() -> None:
    """把 stdout/stderr 强制为 UTF-8 输出（errors=replace 兜底）。

    中文 Windows 控制台默认 GBK（cp936）编码，管道/重定向时打印 ✓ ● 🔍
    等非 GBK 字符会抛 UnicodeEncodeError 直接崩溃；强制 UTF-8 后既保证
    ``--json`` 等输出为合法 UTF-8，又用替换符兜底避免中断。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_io()


def enable_ansi() -> bool:
    """尝试启用 ANSI 转义序列支持（Windows）。返回是否可用。"""
    if sys.platform != "win32":
        return True
    if not (sys.stdout.isatty() and sys.stderr.isatty()):
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            return True
    except Exception:  # noqa: BLE001 着色失败只是外观问题，不影响功能
        pass
    return False


_ANSI = enable_ansi()

# 终端宽度检测
_TERMINAL_WIDTH: int | None = None


def get_terminal_width() -> int:
    """获取当前终端宽度，缓存结果。"""
    global _TERMINAL_WIDTH
    if _TERMINAL_WIDTH is not None:
        return _TERMINAL_WIDTH
    try:
        _TERMINAL_WIDTH = os.get_terminal_size().columns
    except (OSError, ValueError):
        _TERMINAL_WIDTH = 80
    return _TERMINAL_WIDTH


def reset_terminal_width() -> None:
    """重置终端宽度缓存（窗口大小变化后调用）。"""
    global _TERMINAL_WIDTH
    _TERMINAL_WIDTH = None


def style(text: str, code: str) -> str:
    """给文本包上 ANSI 样式；不可用时原样返回。"""
    if not _ANSI:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def bold(text: str) -> str:
    return style(text, "1")


def dim(text: str) -> str:
    return style(text, "2")


def red(text: str) -> str:
    return style(text, "31")


def green(text: str) -> str:
    return style(text, "32")


def yellow(text: str) -> str:
    return style(text, "33")


def cyan(text: str) -> str:
    return style(text, "36")


def magenta(text: str) -> str:
    return style(text, "35")


def white(text: str) -> str:
    return style(text, "37")


def bg_red(text: str) -> str:
    return style(text, "41")


def bg_green(text: str) -> str:
    return style(text, "42")


def bg_yellow(text: str) -> str:
    return style(text, "43")


def display_width(text: str) -> int:
    """按显示宽度计算字符串长度（东亚全角字符按 2 计）。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def pad_cjk(text: str, width: int) -> str:
    """把 text 左对齐填充到 width（按显示宽度），不足则原样返回。"""
    pad = width - display_width(text)
    if pad <= 0:
        return text
    return text + " " * pad


def truncate_path(path_str: str, max_width: int) -> str:
    """截断过长的路径，保留首尾部分，中间用 ... 替代。

    例如：C:\\Users\\xxx\\AppData\\Local\\Temp\\...\\file.tmp
    """
    dw = display_width(path_str)
    if dw <= max_width:
        return path_str
    # 尝试从中间截断
    head_len = max_width // 3
    tail_len = max_width - head_len - 3  # 3 for "..."
    if head_len < 4 or tail_len < 4:
        # 太短了，直接截断
        return path_str[:max_width - 3] + "..."
    # 保留路径开头和结尾
    head = path_str[:head_len]
    tail = path_str[-tail_len:]
    return f"{head}...{tail}"


def progress_bar(current: int, total: int, width: int = 30) -> str:
    """生成文本进度条。"""
    if total <= 0:
        return "[" + " " * width + "] 0%"
    pct = min(current / total, 1.0)
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:.0%}"


def separator(char: str = "─", width: int | None = None) -> str:
    """生成分隔线。"""
    w = width or get_terminal_width()
    return char * w


def box_header(title: str, width: int | None = None) -> str:
    """生成带标题的框线头部。"""
    w = width or get_terminal_width()
    title_dw = display_width(title)
    pad_total = w - title_dw - 4  # 4 for "│ " and " │"
    if pad_total < 0:
        pad_total = 0
    pad_left = pad_total // 2
    pad_right = pad_total - pad_left
    return f"┌{'─' * pad_left}┤ {title} ├{'─' * pad_right}┐"


def box_footer(width: int | None = None) -> str:
    """生成框线底部。"""
    w = width or get_terminal_width()
    return f"└{'─' * (w - 2)}┘"


def format_table_row(columns: list[tuple[str, int, str]]) -> str:
    """格式化表格行。

    columns: [(text, width, align), ...]  align: 'left' | 'right' | 'center'
    """
    parts = []
    for text, width, align in columns:
        dw = display_width(text)
        if dw > width:
            text = truncate_path(text, width)
            dw = display_width(text)
        pad = width - dw
        if align == "right":
            parts.append(" " * pad + text)
        elif align == "center":
            left = pad // 2
            right = pad - left
            parts.append(" " * left + text + " " * right)
        else:
            parts.append(text + " " * pad)
    return "  ".join(parts)
