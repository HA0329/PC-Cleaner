"""``python -m pc_cleaner`` 入口。

本工具**只适配 Windows**（v0.9.10 起明确化）：
- Python 要求 3.12+（源码使用 PEP 701 的 f-string 语法，3.10/3.11 在解析阶段即
  ``SyntaxError: unterminated string literal``）；
- 运行平台要求 Windows（规则路径、回收站、注册表、快捷方式、UAC 提权全是
  Windows 专属语义）。
两道门禁都在**导入 cli 之前**完成，因此在旧解释器 / 非 Windows 上得到的是
一句中文说明，而不是一屏莫名其妙的 traceback。
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)

if sys.version_info < MIN_PYTHON:
    _need = ".".join(str(n) for n in MIN_PYTHON)
    _have = ".".join(str(n) for n in sys.version_info[:3])
    print(
        f"[ERROR] 需要 Python {_need} 或更高，当前是 {_have}。\n"
        f"        本工具使用了 Python {_need} 才支持的 f-string 语法（PEP 701）；\n"
        f"        在 3.10/3.11 上会直接报 'SyntaxError: unterminated string literal'。\n"
        f"        请升级 Python：https://www.python.org/downloads/",
        file=sys.stderr,
    )
    raise SystemExit(1)

if sys.platform != "win32":
    print(
        f"[ERROR] 本工具只适配 Windows，当前平台是 {sys.platform}。\n"
        f"        清理规则、回收站、注册表扫描、失效快捷方式、UAC 提权\n"
        f"        全部依赖 Windows 专有 API 与路径语义。\n"
        f"        如只是想了解实现，可阅读源码与 tests/（测试同样以 Windows 为准）。",
        file=sys.stderr,
    )
    raise SystemExit(1)

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
