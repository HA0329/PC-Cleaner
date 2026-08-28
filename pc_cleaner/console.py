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
