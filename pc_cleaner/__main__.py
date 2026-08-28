"""``python -m pc_cleaner`` 入口。"""

from __future__ import annotations

import sys

if __package__ in (None, ""):
    # 直接以脚本方式运行（python pc_cleaner/__main__.py）时相对导入会失败，
    # 给出友好提示而不是一屏 traceback。
    print(
        "请从项目根目录（含 pc_cleaner/ 包与 pyproject.toml 的那一层）运行：\n"
        "  python -m pc_cleaner\n"
        "Windows 用户可直接双击 pc_cleaner.bat。",
        file=sys.stderr,
    )
    raise SystemExit(1)

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
