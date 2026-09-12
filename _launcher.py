"""``pc_cleaner.bat`` 的启动入口（v0.9.6 新增）。

旧版 bat 先 ``python -c "版本检查"``（输出被 ``>nul`` 丢弃、屏幕无任何反馈）
再 ``python -m pc_cleaner``，等于冷启动两次 Python；在装有杀软（360 / 火绒 /
Defender 等）的机器上，每次冷启动都会被实时扫描拖慢数秒，叠加起来就是
「双击后 cmd 只有光标闪烁几十秒」。

v0.9.6 改为：只启动一次 Python，由本文件先做版本检查、再进入主程序；
bat 侧在启动 Python 前就打印提示，用户立刻能看到反馈。

v0.9.10：门禁前置到**导入 cli 之前** —— Python 版本与运行平台两道检查都在本文件
完成，旧解释器 / 非 Windows 上得到的是一句中文说明，而不是 traceback。
"""

from __future__ import annotations

import sys

#: 只适配 Python 3.12+（见 pyproject.toml 的 requires-python 与 README）。
#: 为什么是硬要求：源码使用 PEP 701 的 f-string 写法（内层 f-string 与外层同引号，
#: 例如 ``f"{dim(f'…')}"``），3.10/3.11 在**解析阶段**就报
#: "SyntaxError: unterminated string literal" —— 用户完全看不出是版本问题。
MIN_PYTHON = (3, 12)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        need = ".".join(str(n) for n in MIN_PYTHON)
        have = ".".join(str(n) for n in sys.version_info[:3])
        print(
            f"[ERROR] 需要 Python {need} 或更高，当前是 {have}。\n"
            f"        本工具使用了 Python {need} 才支持的 f-string 语法（PEP 701），\n"
            f"        在 3.10/3.11 上会在解析阶段直接报 "
            f"'SyntaxError: unterminated string literal'。\n"
            f"        请升级 Python：https://www.python.org/downloads/",
            file=sys.stderr,
        )
        return 1
    if sys.platform != "win32":
        print(
            f"[ERROR] 本工具只适配 Windows，当前平台是 {sys.platform}。\n"
            f"        清理规则、回收站、注册表扫描、失效快捷方式、UAC 提权\n"
            f"        全部依赖 Windows 专有 API 与路径语义。",
            file=sys.stderr,
        )
        return 1
    from pc_cleaner.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
