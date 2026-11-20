"""v0.9.15「增强工程质量」的回归测试。

覆盖：
- lint / 类型检查 / CI 闸门确实写进了项目配置；
- UTF-8 流重配助手能安全处理 StringIO、无 reconfigure 流和抛 OSError 的流；
- 仓库不再跟踪 ``*.egg-info`` 这类构建产物。
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from pc_cleaner import console

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_quality_gates_are_declared_and_wired_into_ci() -> None:
    data = _pyproject()

    dev = data["project"]["optional-dependencies"]["dev"]
    dev_names = {item.split(">=", 1)[0].split("==", 1)[0] for item in dev}
    assert {"pytest", "ruff", "mypy"} <= dev_names

    ruff = data["tool"]["ruff"]["lint"]
    assert {"E4", "E7", "E9", "F", "I", "B"} <= set(ruff["select"])
    assert data["tool"]["ruff"]["lint"]["per-file-ignores"]["pc_cleaner/__main__.py"] == [
        "E402"
    ]

    mypy = data["tool"]["mypy"]
    assert mypy["platform"] == "win32"
    assert mypy["files"] == ["pc_cleaner"]
    assert mypy["check_untyped_defs"] is True

    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m ruff check pc_cleaner tests" in ci
    assert "python -m mypy" in ci


def test_reconfigure_helper_is_safe_for_plain_and_broken_streams() -> None:
    class Plain:
        pass

    class Broken:
        def reconfigure(self, **_kwargs) -> None:
            raise OSError("stream is closed")

    class Recording:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def reconfigure(self, **kwargs) -> None:
            self.calls.append(kwargs)

    # 没有 reconfigure 的 StringIO / 自定义流：跳过，不抛 AttributeError
    console._reconfigure_utf8_stream(Plain())
    # 有 reconfigure 但流已关闭：吞掉 OSError，不影响程序启动
    console._reconfigure_utf8_stream(Broken())

    recording = Recording()
    console._reconfigure_utf8_stream(recording)
    assert recording.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_generated_egg_info_is_not_tracked() -> None:
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("源码压缩包没有 .git，无法校验跟踪状态")
    if shutil.which("git") is None:
        pytest.skip("当前环境没有 git")

    result = subprocess.run(
        ["git", "ls-files", "pc_junk_cleaner.egg-info"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "构建产物 *.egg-info 不应被跟踪；它已经写在 .gitignore 里，"
        "提交它会让源码树随每次构建产生噪声。"
    )
