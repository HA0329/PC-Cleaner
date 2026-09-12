"""v0.9.10 回归测试：11 处修复的守门测试。

覆盖内容（每条都对应 CHANGELOG 0.9.10 里的一项）：

1. 回收站不可用时**整批拒绝**（文件目标 + 清空目录两条路径），绝不永久删除；
2. MCP 遵守配置 ``enabled_categories``（被禁用的分类不能预览/删除）；
3. MCP ``undo`` 如实上报（restored / partial / failed 三态）；
4. ``deleted`` / ``freed`` 如实：扫描后消失的目标记 ``vanished``，不计删除与释放；
5. ``--undo-last`` 按实际结果返回退出码；
6. ``--admin`` 提权：参数逐个转义 + 提权标记写进子进程命令行；
7. ``--clean recycle_bin --exclude recycle_bin`` 矛盾输入被拒绝（CLI 与 --json）；
8. glob 不产出起点目录 → ``glob_dirs`` / ``glob_files`` 补上 base 自身；
9. ``pc_cleaner.bat`` 转发带空格的参数时保留引号；
10. 规则审计：多候选路径、base 自身匹配、按 pattern 分组；
11. 文档/口径：``--deep`` 的隐含深度、分类数、测试数与实现一致。
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from pc_cleaner import cli, commands, engine, mcp, rules, scanner, ui
from pc_cleaner.models import (
    CategoryResult,
    CleanMode,
    Target,
    TargetAction,
    TargetKind,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _file_target(path: Path, size: int = 128) -> Target:
    return Target(
        path=path,
        kind=TargetKind.FILE,
        action=TargetAction.DELETE,
        category="test",
        size=size,
        file_count=1,
        label="test",
    )


def _dir_clear_target(path: Path, size: int = 0, count: int = 0) -> Target:
    return Target(
        path=path,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="test",
        size=size,
        file_count=count,
        label="test",
    )


def _payload(response: dict) -> dict:
    """取出 MCP tools/call 返回的结构化 payload。"""
    assert response is not None and "result" in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _call(name: str, args: dict, *, allow_delete: bool = True) -> dict:
    return mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        allow_delete=allow_delete,
    )


# ===========================================================================
# 1. 回收站不可用 → 整批拒绝（绝不静默永久删除）
# ===========================================================================
class TestRecycleUnavailableRefusal:
    def test_file_target_is_preserved_not_permanently_deleted(self, tmp_path, monkeypatch):
        victim = tmp_path / "cache.bin"
        victim.write_bytes(b"x" * 512)
        monkeypatch.setattr(engine, "HAS_SEND2TRASH", False)

        res = engine.delete_targets([_file_target(victim)], CleanMode.RECYCLE)

        assert victim.exists(), "回收站不可用时绝不能删除原文件"
        assert res["deleted"] == 0
        assert res["failed"] == 1
        assert res["freed"] == 0 and res["recycled"] == 0
        assert res["recycle_unavailable"] is True

    def test_clear_dir_target_is_refused(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "a.tmp").write_bytes(b"x" * 1024)
        monkeypatch.setattr(engine, "HAS_SEND2TRASH", False)

        res = engine.delete_targets([_dir_clear_target(cache)], CleanMode.RECYCLE)

        assert (cache / "a.tmp").exists()
        assert res["deleted"] == 0 and res["failed"] == 1

    def test_delete_path_raises_instead_of_downgrading(self, tmp_path, monkeypatch):
        victim = tmp_path / "one.bin"
        victim.write_bytes(b"y" * 32)
        monkeypatch.setattr(engine, "HAS_SEND2TRASH", False)

        with pytest.raises(PermissionError) as exc:
            engine._delete_path(
                victim,
                CleanMode.RECYCLE,
                is_dir=False,
                recycle_fallback=False,
                is_protected=lambda _p: False,
            )
        assert "send2trash" in str(exc.value)
        assert victim.exists()

    def test_cli_rejects_recycle_mode_early(self, monkeypatch, capsys):
        monkeypatch.setattr(engine, "HAS_SEND2TRASH", False)
        monkeypatch.setattr(cli, "recycle_available", lambda: False)

        code = cli.main(["--clean", "system_temp", "--recycle", "--yes", "--no-progress"])
        out = capsys.readouterr().out

        assert code == cli.EXIT_ERROR
        assert "send2trash" in out

    def test_permanent_mode_still_works_without_send2trash(self, tmp_path, monkeypatch):
        victim = tmp_path / "perm.bin"
        victim.write_bytes(b"z" * 256)
        monkeypatch.setattr(engine, "HAS_SEND2TRASH", False)

        res = engine.delete_targets([_file_target(victim)], CleanMode.PERMANENT)

        assert not victim.exists()
        assert res["deleted"] == 1 and res["freed"] == 256


# ===========================================================================
# 2. MCP 遵守 enabled_categories
# ===========================================================================
class TestMcpRespectsEnabledCategories:
    def test_disabled_category_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            mcp, "get_enabled_category_specs", lambda deep=False: [
                {"key": "system_temp", "label": "系统临时文件", "risk": "safe", "targets": []}
            ]
        )
        monkeypatch.setattr(
            mcp, "get_all_category_specs", lambda deep=False: [
                {"key": "system_temp", "label": "系统临时文件", "risk": "safe", "targets": []},
                {"key": "dev_caches", "label": "开发工具缓存", "risk": "moderate", "targets": []},
            ]
        )
        payload = _payload(_call("preview_delete", {"categories": ["dev_caches"]}))
        assert payload["ok"] is False
        assert "enabled_categories" in payload["error"]
        assert "confirm_token" not in payload

    def test_enabled_category_still_works(self, monkeypatch):
        enabled = [
            {"key": "system_temp", "label": "系统临时文件", "risk": "safe", "targets": []}
        ]
        monkeypatch.setattr(mcp, "get_enabled_category_specs", lambda deep=False: enabled)
        monkeypatch.setattr(mcp, "get_all_category_specs", lambda deep=False: enabled)
        monkeypatch.setattr(mcp, "scan_all", lambda specs, **kw: [])
        payload = _payload(_call("preview_delete", {"categories": ["system_temp"]}))
        assert payload["ok"] is True and payload["confirm_token"]

# ===========================================================================
# 3. MCP undo 三态
# ===========================================================================
class TestMcpUndoHonesty:
    def _session(self, mode: str = "recycle") -> dict:
        return {
            "session_id": "s-1",
            "ts": "2026-01-01 00:00:00",
            "mode": mode,
            "targets": [{"path": "C:/tmp/a.bin", "size": 1}],
        }

    def test_nothing_restored_reports_failure(self, monkeypatch):
        monkeypatch.setattr(mcp, "load_history", lambda: [self._session()])
        monkeypatch.setattr(
            mcp, "restore_paths", lambda paths: {"restored": [], "skipped": ["无记录"]}
        )
        payload = _payload(_call("undo", {}))
        assert payload["ok"] is False
        assert payload["status"] == "failed"
        assert payload["restored"] == []
        assert payload["restored_count"] == 0 and payload["requested_count"] == 1
        assert "error" in payload

    def test_partial_restore_reports_partial(self, monkeypatch):
        monkeypatch.setattr(mcp, "load_history", lambda: [self._session()])
        monkeypatch.setattr(
            mcp,
            "restore_paths",
            lambda paths: {"restored": ["C:/tmp/a.bin"], "skipped": ["C:/tmp/b.bin（占用）"]},
        )
        payload = _payload(_call("undo", {}))
        assert payload["ok"] is False and payload["status"] == "partial"
        assert payload["restored"] == ["C:/tmp/a.bin"]

    def test_full_restore_reports_restored(self, monkeypatch):
        monkeypatch.setattr(mcp, "load_history", lambda: [self._session()])
        monkeypatch.setattr(
            mcp, "restore_paths", lambda paths: {"restored": ["C:/tmp/a.bin"], "skipped": []}
        )
        payload = _payload(_call("undo", {}))
        assert payload["ok"] is True and payload["status"] == "restored"

    def test_session_id_matched_exactly_first(self, monkeypatch):
        """同一秒内的两个会话：必须按 session_id 精确命中，不能靠 ts 撞车。"""
        sessions = [
            {"session_id": "a", "ts": "2026-01-01 00:00:00", "mode": "recycle",
             "targets": [{"path": "C:/tmp/a.bin"}]},
            {"session_id": "b", "ts": "2026-01-01 00:00:00", "mode": "recycle",
             "targets": [{"path": "C:/tmp/b.bin"}]},
        ]
        monkeypatch.setattr(mcp, "load_history", lambda: sessions)
        monkeypatch.setattr(
            mcp, "restore_paths", lambda paths: {"restored": list(paths), "skipped": []}
        )
        payload = _payload(_call("undo", {"session_id": "b"}))
        assert payload["session_id"] == "b" and payload["restored"] == ["C:/tmp/b.bin"]


# ===========================================================================
# 4. deleted / freed / vanished 如实
# ===========================================================================
class TestHonestAccounting:
    def test_vanished_target_is_not_deleted_nor_freed(self, tmp_path):
        ghost = tmp_path / "ghost.tmp"
        ghost.write_bytes(b"q" * 4096)
        target = _file_target(ghost, size=4096)
        ghost.unlink()  # 扫描之后、删除之前消失

        res = engine.delete_targets([target], CleanMode.PERMANENT)

        assert res["deleted"] == 0, "消失的目标不能算作删除成功"
        assert res["vanished"] == 1
        assert res["freed"] == 0, "没删掉的东西不能算释放"
        assert res["failed"] == 0

    def test_freed_uses_measured_delta(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"x" * 100)
        # 扫描时声称 9999 字节（模拟扫描后被截断/漂移）
        res = engine.delete_targets([_file_target(f, size=9999)], CleanMode.PERMANENT)
        assert res["deleted"] == 1
        assert res["freed"] == 100, "freed 必须是删除前后实测差，而不是扫描时的估计值"

    def test_protected_child_not_counted_as_freed(self, tmp_path):
        cache = tmp_path / "cache"
        (cache / ".git").mkdir(parents=True)
        (cache / ".git" / "config").write_bytes(b"g" * 5000)
        (cache / "junk.tmp").write_bytes(b"j" * 1000)

        res = engine.delete_targets(
            [_dir_clear_target(cache, size=6000, count=2)], CleanMode.PERMANENT
        )

        assert (cache / ".git" / "config").exists(), "受保护子目录必须保留"
        assert not (cache / "junk.tmp").exists()
        assert res["freed"] == 1000

    def test_json_contract_exposes_vanished(self, monkeypatch):
        from pc_cleaner.service import ENVELOPE_SCHEMA

        assert "vanished" in ENVELOPE_SCHEMA["properties"]["action"]["properties"]


# ===========================================================================
# 4b. 颜色与显示宽度（Linux CI 上暴露的表格折行问题）
# ===========================================================================
class TestColorAndDisplayWidth:
    """v0.9.10：``display_width`` 必须先剥离 ANSI；非 TTY 不得输出颜色。

    回归背景：``enable_ansi()`` 在 POSIX 分支无条件返回 True，CI（stdout 是管道）
    里表格仍带 ``\\x1b[1m``；而 ``display_width`` 把转义码当可见字符计数
    （``\\x1b[1m`` 算 4 列），于是边框 83 / 表头 92 / 数据行 93 各不相同，
    菜单在 80 列窗口里被折行撕碎。Windows 上因 ``enable_ansi()`` 对非 TTY 返回
    False 而侥幸没暴露。
    """

    def test_display_width_ignores_ansi(self):
        from pc_cleaner.console import display_width

        plain = "  │    1. │ ● 系统临时文件 │"
        colored = "\x1b[1m  │\x1b[0m \x1b[36m   1.\x1b[0m │ \x1b[33m●\x1b[0m 系统临时文件 │"
        assert display_width(colored) == display_width(plain)
        assert display_width("\x1b[0m\x1b[1m\x1b[36m") == 0

    def test_ansi_disabled_when_not_a_tty(self, monkeypatch):
        """非 TTY（管道/重定向/CI）不应启用颜色 —— 与 Windows 分支同一语义。"""
        from pc_cleaner import console

        class _NotATty(io.StringIO):
            def isatty(self) -> bool:  # noqa: D102
                return False

        monkeypatch.setattr(console.sys, "stdout", _NotATty())
        monkeypatch.setattr(console.sys, "stderr", _NotATty())
        assert console.enable_ansi() is False

    def test_table_rows_same_width_even_with_colors(self, monkeypatch):
        """开色时表格每行显示宽度仍必须完全一致（CI 强制 _ANSI=True 的等价条件）。"""
        from pc_cleaner import console, menu
        from pc_cleaner.console import display_width

        monkeypatch.setattr(console, "_ANSI", True)
        monkeypatch.setattr(menu, "get_terminal_width", lambda: 80)
        results = [
            CategoryResult(
                key="system_temp", label="系统临时文件", risk="safe", scanned=True,
                targets=[Target(path=Path(r"C:\tmp\system_temp"), kind=TargetKind.DIR,
                                action=TargetAction.CLEAR, category="system_temp",
                                size=26_620_470, file_count=3, label="系统临时文件")],
            ),
            CategoryResult(
                key="web_cache", label="浏览器/网页缓存", risk="safe", scanned=True,
                targets=[Target(path=Path(r"C:\tmp\web_cache"), kind=TargetKind.DIR,
                                action=TargetAction.CLEAR, category="web_cache",
                                size=39_680_000, file_count=3, label="浏览器/网页缓存")],
            ),
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            menu._print_summary_table(results, selectable=[0, 1])
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        widths = {display_width(ln) for ln in lines}
        assert len(widths) == 1, f"开色后表格行宽不一致: {sorted(widths)}"
        assert max(widths) <= 80, f"80 列窗口里放不下: {max(widths)}"


# ===========================================================================
# 5. --undo-last 退出码
# ===========================================================================
class TestUndoLastExitCode:
    def _drive(self, monkeypatch, restore_result, platform="win32"):
        monkeypatch.setattr(commands.sys, "platform", platform)
        monkeypatch.setattr(
            commands,
            "load_history",
            lambda: [{"session_id": "s", "ts": "2026-01-01 00:00:00",
                      "mode": "recycle", "targets": [{"path": "C:/tmp/a.bin"}]}],
        )
        monkeypatch.setattr(commands, "restore_paths", lambda paths: restore_result)
        return commands._cmd_undo_last()

    def test_all_restored_returns_zero(self, monkeypatch, capsys):
        code = self._drive(monkeypatch, {"restored": ["a"], "skipped": []})
        capsys.readouterr()
        assert code == 0

    def test_nothing_restored_returns_delete_failed(self, monkeypatch, capsys):
        code = self._drive(monkeypatch, {"restored": [], "skipped": ["无记录"]})
        capsys.readouterr()
        assert code == 3

    def test_partial_restore_returns_delete_failed(self, monkeypatch, capsys):
        code = self._drive(monkeypatch, {"restored": ["a"], "skipped": ["b"]})
        capsys.readouterr()
        assert code == 3

    def test_non_windows_returns_error(self, monkeypatch, capsys):
        """平台门禁：非 Windows 直接拒绝（退出码 1），而不是给出半套行为。

        v0.9.10：本工具**只适配 Windows**，``_cmd_undo_last`` 在非 Windows 上
        直接返回错误码（回收站是 Windows 概念）。
        """
        code = self._drive(monkeypatch, {"restored": [], "skipped": []}, platform="linux")
        capsys.readouterr()
        assert code == 1


# ===========================================================================
# 6. --admin 提权：命令转义 + 环境标记
# ===========================================================================
class TestElevationCommandLine:
    def test_spaced_paths_keep_one_argument(self):
        cmd = commands._elevated_command_line(
            ["--export-config", r"C:\My Dir\cfg.json"], r"C:\Program Files\Python\python.exe",
            r"D:\app dir\_launcher.py",
        )
        # python 与 launcher 都带空格 → 必须各自被引号包裹
        assert r'"C:\Program Files\Python\python.exe"' in cmd
        assert r'"D:\app dir\_launcher.py"' in cmd
        assert r'"C:\My Dir\cfg.json"' in cmd
        assert cmd.startswith("cmd /c set ")

    def test_elevated_marker_is_injected_into_child(self):
        cmd = commands._elevated_command_line([], "python", "_launcher.py")
        assert f"{commands.ELEVATED_ENV_VAR}={commands.ELEVATED_ENV_VALUE}" in cmd
        assert "&&" in cmd

    def test_quote_arg_escapes_trailing_backslash_and_quotes(self):
        assert commands._quote_arg("plain") == "plain"
        assert commands._quote_arg("with space") == '"with space"'
        assert commands._quote_arg('say "hi"') == '"say \\"hi\\""'
        assert commands._quote_arg("dir\\") == "dir\\"
        assert commands._quote_arg("dir with space\\") == '"dir with space\\\\"'

    def test_round_trip_through_windows_command_line(self):
        """交叉校验：不带内嵌引号的参数，转义结果必须与 subprocess 的权威实现一致。"""
        args = [r"C:\My Dir\file.json", "a b", "plain", "trail\\", r"D:\x\y.json"]
        ours = " ".join(commands._quote_arg(a) for a in args)
        assert ours == subprocess.list2cmdline(args)

    def test_embedded_quote_is_quoted_and_balanced(self):
        """带内嵌引号的参数必须被包裹（避免 cmd 解析歧义），且引号成对。"""
        for arg in ('q"x', 'say "hi" now'):
            quoted = commands._quote_arg(arg)
            assert quoted.startswith('"') and quoted.endswith('"'), quoted
            # 结尾的引号不能被反斜杠转义掉（否则 cmd 会吞掉后半段参数）
            trailing = len(quoted) - len(quoted.rstrip("\\"))
            assert trailing % 2 == 0, quoted
            # 去掉外层引号并把 \" 还原后，应得到原参数
            assert quoted[1:-1].replace('\\"', '"') == arg, quoted

    def test_is_elevated_reads_env(self, monkeypatch):
        monkeypatch.setenv(commands.ELEVATED_ENV_VAR, commands.ELEVATED_ENV_VALUE)
        assert ui.is_elevated() is True
        monkeypatch.delenv(commands.ELEVATED_ENV_VAR, raising=False)
        assert ui.is_elevated() is False

    def test_relaunch_is_refused_on_non_windows(self, monkeypatch, capsys):
        """`--admin` 的提权只在 Windows 可用（POSIX 上 ctypes 没有 windll）。"""
        monkeypatch.setattr(commands.sys, "platform", "linux")
        code = commands._relaunch_as_admin(["--admin", "--list"])
        out = capsys.readouterr().out
        assert code == 1
        assert "Windows" in out


# ===========================================================================
# 6b. 平台门禁：只适配 Windows
# ===========================================================================
class TestPlatformGate:
    """v0.9.10：只适配 Windows —— 平台与解释器版本都在启动阶段拦下。

    背景：CI 曾在 ubuntu 矩阵上长期飘红，根因是"半个跨平台"—— 规则路径、回收站、
    注册表、失效快捷方式、UAC 提权全是 Windows 专属语义，Linux 上只能靠一堆
    skipif/等价写法维持绿灯。与其到处打补丁，不如把边界写清楚：非 Windows 与旧
    解释器在**导入 cli 之前**就得到一句中文说明，而不是 traceback 或半套行为。
    """

    def test_main_module_declares_windows_only_gate(self):
        text = (PROJECT_ROOT / "pc_cleaner" / "__main__.py").read_text(encoding="utf-8")
        assert 'sys.platform != "win32"' in text, "缺少平台门禁"
        assert text.index('sys.platform != "win32"') < text.index("from .cli import main"), (
            "平台门禁必须在导入 cli 之前"
        )
        assert "MIN_PYTHON" in text and "(3, 12)" in text

    def test_launcher_declares_same_gate(self):
        text = (PROJECT_ROOT / "_launcher.py").read_text(encoding="utf-8")
        assert 'sys.platform != "win32"' in text
        assert text.index('sys.platform != "win32"') < text.index(
            "from pc_cleaner.cli import main"
        )
        assert "(3, 12)" in text

    def test_pyproject_requires_windows_python(self):
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'requires-python = ">=3.12"' in pyproject
        assert "Programming Language :: Python :: 3.12" in pyproject
        assert "Programming Language :: Python :: 3.10" not in pyproject
        assert "Programming Language :: Python :: 3.13" not in pyproject

    def test_ci_never_runs_non_windows(self):
        ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "ubuntu" not in ci, "CI 不应再跑非 Windows 矩阵"
        assert "windows-latest" in ci
        assert '"3.12"' in ci

    def test_readme_states_windows_only(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert "只适配 Windows" in readme or "仅支持 Windows" in readme
        assert "Python 3.12+" in readme


# ===========================================================================
# 7. recycle_bin 矛盾输入
# ===========================================================================
class TestRecycleBinConflict:
    def test_conflict_rejected_in_cli(self, monkeypatch, capsys):
        code = cli.main(
            ["--clean", "recycle_bin", "--exclude", "recycle_bin", "--yes", "--no-progress"]
        )
        out = capsys.readouterr().out
        assert code == cli.EXIT_ERROR
        assert "recycle_bin" in out

    def test_conflict_rejected_in_json_mode(self, monkeypatch, capsys):
        code = cli.main(
            ["--json", "--clean", "recycle_bin", "--exclude", "recycle_bin", "--yes"]
        )
        raw = capsys.readouterr().out
        payload = json.loads(raw)
        assert code == cli.EXIT_ERROR
        assert payload["status"] == "error"
        assert payload["action"]["not_executed"] is True

    def test_all_with_exclude_recycle_bin_is_fine(self, monkeypatch):
        """--all --exclude recycle_bin 是正常用法（只是不要清空回收站），不得报错。"""
        assert cli._recycle_bin_conflict(["system_temp"], {"recycle_bin"}) is False
        assert cli._recycle_bin_conflict([], {"recycle_bin"}) is False


# ===========================================================================
# 8. glob 起点目录
# ===========================================================================
class TestGlobBaseDirectory:
    def test_pattern_matcher_follows_glob_semantics(self, tmp_path):
        base = tmp_path
        assert scanner._pattern_matches_dir(base, base, "*") is True
        assert scanner._pattern_matches_dir(base, base, "cache") is False
        nested = base / "Default" / "Cache"
        assert scanner._pattern_matches_dir(base, nested, "*/Cache") is True
        assert scanner._pattern_matches_dir(base, nested, "Default/Cache") is True
        assert scanner._pattern_matches_dir(base, base / "Default" / "x", "Default/Cache") is False

    def test_glob_dirs_includes_base_itself(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "f").write_bytes(b"x" * 8)
        is_p = scanner.make_protect_check()
        targets = scanner._scan_glob_dirs(
            {"base": str(tmp_path), "pattern": "*", "action": "clear", "label": "t"},
            "k",
            is_p,
        )
        names = {t.path.name for t in targets}
        assert tmp_path.name in names, "base 自身匹配 pattern 时必须被扫描到（否则规则永久空转）"

    def test_glob_files_includes_base_itself(self, tmp_path):
        db = tmp_path / "data.db"
        db.write_bytes(b"y" * 16)
        is_p = scanner.make_protect_check()
        targets = scanner._scan_glob_files(
            {"base": str(db), "pattern": "*.db", "label": "t"}, "k", is_p
        )
        assert [t.path for t in targets] == [db]


# ===========================================================================
# 9. bat 参数引号
# ===========================================================================
class TestBatArgForwarding:
    def test_bat_quotes_each_argument(self):
        text = (PROJECT_ROOT / "pc_cleaner.bat").read_text(encoding="utf-8", errors="replace")
        # 转发时必须重新加引号，否则带空格的路径会被拆成两个参数
        assert re.search(r'set ARGS=%ARGS% "%~1"', text), "bat 必须保留参数引号"

    @pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有 cmd.exe")
    def test_bat_round_trip(self, tmp_path):
        """真正跑一遍 bat 的参数解析循环（把启动器换成 argv 打印器）。"""
        script = tmp_path / "echo_args.bat"
        script.write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            'set "ARGS="\r\n'
            ":parse\r\n"
            'if "%~1"=="" goto parsed\r\n'
            'if /i "%~1"=="--no-pause" goto mark\r\n'
            'set ARGS=%ARGS% "%~1"\r\n'
            "shift\r\n"
            "goto parse\r\n"
            ":mark\r\n"
            'set "NOPAUSE=1"\r\n'
            "shift\r\n"
            "goto parse\r\n"
            ":parsed\r\n"
            'python -c "import sys;print(\'|ARGV|\',sys.argv[1:])" %ARGS%\r\n',
            encoding="utf-8",
        )
        out = subprocess.run(
            ["cmd", "/c", str(script), "--export-scan", r"D:\My Dir\scan.json",
             "--clean", "system_temp", "--no-pause"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        marker = "|ARGV|"
        assert marker in out, out
        argv = json.loads(out.split(marker, 1)[1].strip().replace("'", '"'))
        assert argv == ["--export-scan", r"D:\My Dir\scan.json", "--clean", "system_temp"], (
            f"带空格的路径必须仍是一个参数，且 --no-pause 不得转发: {argv}"
        )


# ===========================================================================
# 10. 规则审计
# ===========================================================================
class TestRuleAudit:
    def _spec(self, targets):
        return {"key": "k", "label": "k", "risk": "safe", "targets": targets}

    def test_any_candidate_alive_means_rule_alive(self, tmp_path, monkeypatch):
        """候选 A 不存在、候选 B 存在 → 规则不能报成失效（v0.9.8 的 paths 语义）。"""
        real = tmp_path / "real"
        real.mkdir()
        (real / "f.log").write_bytes(b"x" * 4)
        spec = self._spec([
            {
                "type": "glob_files",
                "base": str(tmp_path / "missing"),
                "bases": [str(real)],
                "pattern": "*.log",
                "label": "t",
            }
        ])
        warnings: list[str] = []
        rules._warn_dead_targets([spec], warnings)
        assert not any("匹配不到任何内容" in w for w in warnings), warnings

    def test_base_itself_matching_counts_as_alive(self, tmp_path):
        base = tmp_path / "winevt-logs"
        base.mkdir()
        (base / "Archive-a.evtx").write_bytes(b"x" * 4)
        spec = self._spec([
            {"type": "glob_files", "base": str(base), "pattern": "*.evtx", "label": "t"}
        ])
        warnings: list[str] = []
        rules._warn_dead_targets([spec], warnings)
        assert not warnings

    def test_directory_only_match_is_not_reported_dead(self, tmp_path):
        """glob.glob 的 `*` 不匹配目录，误报曾让审计大面积失真。"""
        base = tmp_path / "User Data"
        (base / "Default" / "Cache").mkdir(parents=True)
        spec = self._spec([
            {"type": "glob_dirs", "base": str(base), "pattern": "*/Cache",
             "action": "clear", "label": "t"}
        ])
        warnings: list[str] = []
        rules._warn_dead_targets([spec], warnings)
        assert not warnings, warnings

    def test_warning_shows_pattern_and_dedups_per_pattern(self, tmp_path):
        base = tmp_path / "exists"
        base.mkdir()
        spec = self._spec([
            {"type": "glob_dirs", "base": str(base), "pattern": "*.gone", "label": "a"},
            {"type": "glob_dirs", "base": str(base), "pattern": "*.gone", "label": "b"},
            {"type": "glob_dirs", "base": str(base), "pattern": "*.other", "label": "c"},
        ])
        warnings: list[str] = []
        rules._warn_dead_targets([spec], warnings)
        joined = "\n".join(warnings)
        assert "pattern='*.gone'" in joined and "pattern='*.other'" in joined
        assert "另有 1 条重复规则" in joined, joined

    def test_builtin_rules_still_valid(self):
        report = rules.validate_rules()
        assert list(report) == []

    def test_no_safe_target_inside_user_documents(self):
        """safe 分类（会被 --all 选中）不得包含用户文档目录内的目标。"""
        offenders = []
        for cat in rules._builtin_specs(deep=True):
            if str(cat.get("risk") or "safe") != "safe":
                continue
            for t in cat.get("targets", []):
                for raw in rules._target_locations(t):
                    norm = rules._expand_for_validation(raw) or ""
                    parts = [p for p in norm.replace("/", os.sep).split(os.sep) if p]
                    if any(p in ("documents", "desktop") for p in parts):
                        offenders.append((cat.get("key"), raw))
        assert offenders == [], f"safe 分类不应触碰用户文档/桌面: {offenders}"

    def test_rdp_category_is_moderate(self):
        specs = {c["key"]: c for c in rules._builtin_specs(deep=True)}
        assert specs["rdp_legacy_cache"]["risk"] == "moderate"
        assert specs["java_rdp_legacy"]["risk"] == "safe"
        labels = {c["key"] for c in rules._builtin_specs(deep=True)}
        assert "rdp_legacy_cache" in labels
        assert "rdp_legacy_cache" in rules.CATEGORY_META


# ===========================================================================
# 11. 文档与实现口径一致
# ===========================================================================
class TestDocsMatchImplementation:
    def test_readme_category_and_rule_counts(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        specs = rules._builtin_specs(deep=True)
        total = sum(len(c.get("targets", [])) for c in specs)
        assert f"内置清理分类（{len(specs)} 个）" in readme
        assert f"{len(specs)} 个分类、{total} 条内置规则" in readme
        assert f"（{len(specs)} 分类 {total} 目标）" in readme

    def test_readme_has_no_broken_table_row(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        # 曾经的 `--detail / -d、--tree | ... || --deep / -D | ...` 把两行挤成一行
        assert "||" not in readme.replace("|||", ""), "README 里存在被挤在一行的表格"
        assert "| `--deep` / `-D` | 深度扫描" in readme

    def test_deep_depth_behavior_documented(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert "--max-depth" in readme and "50" in readme

    def test_version_is_consistent(self):
        import pc_cleaner

        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert f'version = "{pc_cleaner.__version__}"' in pyproject
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert pc_cleaner.__version__ in readme

    def test_changelog_has_current_version(self):
        import pc_cleaner

        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## {pc_cleaner.__version__}" in changelog

    def test_readme_test_count_is_current(self):
        """README 里写的测试数量必须等于实际收集到的用例数（防止文档再次失真）。"""
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        env = dict(os.environ)
        # 子进程强制 UTF-8：Windows 上默认 gbk 会让 pytest 输出解码失败（此前因此静默跳过）
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "--collect-only",
                "-p", "no:cacheprovider", "-q", "tests",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )
        output = (result.stdout or "") + (result.stderr or "")
        # pytest 在不同模式下汇总格式不同：优先取 "N tests collected"，
        # 否则累加每个测试文件的 "path: N" 计数行（pytest 9 的 -q 输出）。
        match = re.search(r"(\d+) tests? collected", output)
        if match:
            total = int(match.group(1))
        else:
            per_file = re.findall(r"^tests[/\\]\S+\.py: (\d+)$", output, re.MULTILINE)
            assert per_file, f"无法解析 pytest 收集结果: {output[-300:]!r}"
            total = sum(int(n) for n in per_file)
        assert f"pytest        # {total} passed" in readme, (
            f"README 的测试数量已过时（实际收集到 {total} 个用例）"
        )
        assert f"tests/                     # {total} 个单元测试" in readme

    def test_gitignore_covers_build_artifacts(self):
        """.gitignore 必须覆盖 __pycache__ / *.pyc / .pytest_cache。"""
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("__pycache__/", "*.py[cod]", ".pytest_cache/"):
            assert pattern in gitignore, f".gitignore 缺少 {pattern}"

    def test_readme_category_table_lists_every_category(self):
        """README 的分类表必须覆盖 rules.json 里的每一个分类（含新增的 rdp_legacy_cache）。"""
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        block = readme.split("## 🗂 内置清理分类")[1].split("\n---")[0]
        rows = set()
        for line in block.splitlines():
            if not line.startswith("| `"):
                continue
            rows.add(line.split("|")[1].strip().strip("`"))
        keys = {c["key"] for c in rules._builtin_specs(deep=True)}
        assert keys - rows == set(), f"README 缺少分类: {sorted(keys - rows)}"
        assert rows - keys == set(), f"README 多出分类: {sorted(rows - keys)}"

    def test_locales_have_no_stray_artifacts(self):
        """发布物（locales）不得混入构建产物路径。"""
        import json as _json

        for name in ("zh_CN.json", "en.json"):
            path = PROJECT_ROOT / "pc_cleaner" / "locales" / name
            data = _json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict) and data, f"{name} 必须是非空对象"
