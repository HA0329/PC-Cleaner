"""``pc_cleaner.bat`` 的启动入口（v0.9.6 新增）。

旧版 bat 先 ``python -c "版本检查"``（输出被 ``>nul`` 丢弃、屏幕无任何反馈）
再 ``python -m pc_cleaner``，等于冷启动两次 Python；在装有杀软（360 / 火绒 /
Defender 等）的机器上，每次冷启动都会被实时扫描拖慢数秒，叠加起来就是
「双击后 cmd 只有光标闪烁几十秒」。

v0.9.6 改为：只启动一次 Python，由本文件先做版本检查、再进入主程序；
bat 侧在启动 Python 前就打印提示，用户立刻能看到反馈。
"""

from __future__ import annotations

import sys


def main() -> int:
    if sys.version_info < (3, 10):
        print(
            "[ERROR] Python 3.10+ is required, but an older version was found.",
            file=sys.stderr,
        )
        return 1
    from pc_cleaner.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
