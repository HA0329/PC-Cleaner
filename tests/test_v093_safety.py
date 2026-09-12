"""v0.9.3 安全加固的回归测试（v0.9.7 恢复）。

这些用例在整理测试目录时被删掉过，但**被修复的缺陷依然存在风险**，因此以
``test_v093_safety.py`` 重新加入，专门守住当年修掉的每一类问题：

1. 路径词法规范化（``..`` / 尾随点与空格 / ``\\\\?\\`` 前缀）；
2. CLEAR 目标本身是 junction 时拒绝（防止顺着链接清空目标）；
3. ``glob_dirs`` 缺 ``pattern`` 不再退化成 ``*``；``action`` 大小写与非法值；
4. ``skip_if_in_use`` 从规则传到 Target、并在引擎侧真正跳过；
5. 进回收站只计 ``recycled`` 不计 ``freed``；
6. 危险操作闸门与退出码契约；
7. ``history.json`` 原子写与损坏备份；
8. ``compact_db`` 体积改为"空闲页估算"；
9. 白名单清空根不再短路名称级保护；
10. ``--clean ""`` 明确报错（不再静默退化成只扫描）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from pc_cleaner import cli as cli_mod
from pc_cleaner import history as history_mod
from pc_cleaner.engine import (
    CleanMode,
    _clear_dir_content,
    delete_targets,
    recycle_available,
)
from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind
from pc_cleaner.scanner import (
    _dir_size,
    _scan_clear_dir,
    _scan_glob_dirs,
    _sqlite_reclaimable,
    expand_path,
    make_protect_check,
    normalize,
)
from pc_cleaner.service import (
    EXIT_DELETE_FAILED,
    EXIT_NEEDS_CONFIRM,
    EXIT_INTERRUPTED,
    EXIT_OK,
    dangerous_reasons,
    envelope,
    exit_code_for,
    status_for,
)

WIN = sys.platform == "win32"
needs_windows = pytest.mark.skipif(not WIN, reason="仅在 Windows 上有效")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _mk_dir_with_file(base: Path, name: str = "cache", size: int = 16) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "payload.bin").write_bytes(b"x" * size)
    return d


def _mklink_junction(link: Path, target: Path) -> bool:
    """创建目录 junction（失败返回 False，例如权限不足）。"""
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _risky_category(path: Path) -> CategoryResult:
    return CategoryResult(
        key="downloads",
        label="下载/旧文件",
        risk="risky",
        scanned=True,
        targets=[
            Target(
                path=path,
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category="downloads",
                size=1,
            )
        ],
    )


# ---------------------------------------------------------------------------
# 1. 路径规范化
# ---------------------------------------------------------------------------
def test_expand_path_folds_dotdot(tmp_path):
    assert expand_path(str(tmp_path / "a" / ".." / "b")) == tmp_path / "b"
    assert expand_path(str(tmp_path / "a" / "." / "b")) == tmp_path / "a" / "b"


def test_normalize_strips_trailing_dot_and_space(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    assert normalize(Path(str(d) + ".")) == normalize(d)
    assert normalize(Path(str(d) + " ")) == normalize(d)


@needs_windows
def test_expand_path_removes_extended_prefix():
    assert str(expand_path(r"\\?\C:\Windows")) == "C:\\Windows"


def test_protect_check_matches_after_normalization():
    chk = make_protect_check()
    assert chk(Path("C:/Windows/Temp/../System32")) is True


@needs_windows
def test_protect_check_handles_extended_prefix():
    chk = make_protect_check()
    assert chk(Path(r"\\?\C:\Windows\System32")) is True


# ---------------------------------------------------------------------------
# 2. 重解析点防护
# ---------------------------------------------------------------------------
@needs_windows
def test_clear_dir_content_refuses_junction_root(tmp_path):
    real = _mk_dir_with_file(tmp_path, "real")
    link = tmp_path / "link"
    if not _mklink_junction(link, real):
        pytest.skip("无法创建 junction")
    chk = make_protect_check()
    deleted, failed = _clear_dir_content(link, CleanMode.PERMANENT, lambda p: None, False, chk)
    assert (deleted, failed) == (0, 0)
    assert (real / "payload.bin").exists()  # 链接目标必须完好


@needs_windows
def test_scan_clear_dir_skips_junction(tmp_path):
    real = _mk_dir_with_file(tmp_path, "real")
    link = tmp_path / "link"
    if not _mklink_junction(link, real):
        pytest.skip("无法创建 junction")
    assert _scan_clear_dir({"path": str(link)}, "test", make_protect_check()) == []


@needs_windows
def test_dir_size_does_not_follow_root_link(tmp_path):
    real = _mk_dir_with_file(tmp_path, "real", size=1024)
    link = tmp_path / "link"
    if not _mklink_junction(link, real):
        pytest.skip("无法创建 junction")
    assert _dir_size(link, make_protect_check()) == (0, 0)
    assert _dir_size(real, make_protect_check())[0] >= 1024


# ---------------------------------------------------------------------------
# 3/4. glob 规则：pattern 必填、action 大小写与非法值
# ---------------------------------------------------------------------------
def test_glob_dirs_requires_pattern(tmp_path):
    base = tmp_path / "base"
    _mk_dir_with_file(base, "a")
    assert _scan_glob_dirs({"base": str(base)}, "test", make_protect_check()) == []


def test_glob_dirs_action_case_and_invalid(tmp_path):
    base = tmp_path / "base"
    _mk_dir_with_file(base, "a")
    chk = make_protect_check()
    clear = _scan_glob_dirs({"base": str(base), "pattern": "*", "action": "Clear"}, "test", chk)
    assert clear and clear[0].action is TargetAction.CLEAR
    invalid = _scan_glob_dirs({"base": str(base), "pattern": "*", "action": "boom"}, "test", chk)
    assert invalid and invalid[0].action is TargetAction.CLEAR
    delete = _scan_glob_dirs({"base": str(base), "pattern": "*", "action": "DELETE"}, "test", chk)
    assert delete and delete[0].action is TargetAction.DELETE


# ---------------------------------------------------------------------------
# 5. skip_if_in_use 链路
# ---------------------------------------------------------------------------
def test_skip_if_in_use_flag_reaches_target(tmp_path):
    d = _mk_dir_with_file(tmp_path, "cache")
    chk = make_protect_check()
    out = _scan_clear_dir({"path": str(d), "skip_if_in_use": True}, "test", chk)
    assert out and out[0].skip_if_in_use is True
    plain = _scan_clear_dir({"path": str(d)}, "test", chk)
    assert plain and plain[0].skip_if_in_use is False


def test_engine_skips_target_in_use(tmp_path, monkeypatch):
    d = _mk_dir_with_file(tmp_path, "cache")
    payload = d / "payload.bin"
    t = Target(
        path=d,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="test",
        size=payload.stat().st_size,
        file_count=1,
        skip_if_in_use=True,
    )
    monkeypatch.setattr("pc_cleaner.engine._in_use_reason", lambda p: "node.exe (pid 1)")
    res = delete_targets([t], CleanMode.PERMANENT)
    assert res["skipped_in_use"] == 1
    assert res["deleted"] == 0
    assert payload.exists()  # 被占用时绝不删除


# ---------------------------------------------------------------------------
# 6. 回收站释放量语义
# ---------------------------------------------------------------------------
def test_recycle_counts_recycled_not_freed(tmp_path, monkeypatch):
    if not recycle_available():
        pytest.skip("未安装 send2trash")
    f = tmp_path / "junk.bin"
    f.write_bytes(b"y" * 128)
    t = Target(
        path=f,
        kind=TargetKind.FILE,
        action=TargetAction.DELETE,
        category="test",
        size=128,
    )
    called: list[str] = []
    monkeypatch.setattr(
        "pc_cleaner.engine.send2trash",
        types.SimpleNamespace(send2trash=lambda p: called.append(str(p))),
    )
    res = delete_targets([t], CleanMode.RECYCLE)
    assert called == [str(f)]
    assert res["recycled"] == 128
    assert res["freed"] == 0
    assert f.exists()  # 假 send2trash 不会真的删


# ---------------------------------------------------------------------------
# 7. 危险操作闸门与退出码契约
# ---------------------------------------------------------------------------
def test_dangerous_reasons_detects_risky_permanent_and_bin():
    res = _risky_category(Path("C:/nonexistent"))
    assert dangerous_reasons([res], "recycle", []) != []
    assert dangerous_reasons([], "permanent", []) != []
    assert dangerous_reasons([], "recycle", ["recycle_bin"]) != []
    assert dangerous_reasons([], "recycle", []) == []


def test_exit_code_mapping():
    assert exit_code_for({"failed": 0}) == EXIT_OK
    assert exit_code_for({"failed": 2}) == EXIT_DELETE_FAILED
    assert exit_code_for({"needs_confirmation": True}) == EXIT_NEEDS_CONFIRM
    assert exit_code_for({"cancelled": True}) == EXIT_NEEDS_CONFIRM
    assert exit_code_for({"interrupted": True}) == EXIT_INTERRUPTED
    assert status_for({"needs_confirmation": True}) == "needs_confirmation"
    assert status_for({"failed": 1}) == "partial"
    env = envelope("deleted", EXIT_OK)
    assert env["schema_version"] == 1 and env["ok"] is True


def test_json_mode_refuses_risky_without_risky_flag(tmp_path, monkeypatch, capsys):
    fake = [_risky_category(tmp_path / "junk.bin")]
    monkeypatch.setattr(cli_mod, "scan_all", lambda *a, **k: fake)
    code = cli_mod.main(["--json", "--clean", "downloads", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_NEEDS_CONFIRM
    assert payload["status"] == "needs_confirmation"
    assert payload["ok"] is False
    assert payload["action"]["needs_confirmation"] is True


def test_json_mode_rejects_unknown_category(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "scan_all", lambda *a, **k: [])
    code = cli_mod.main(["--json", "--clean", "no_such_category", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "error"


def test_json_mode_rejects_empty_clean(monkeypatch, capsys):
    """``--json --clean ""`` 必须报错，不能静默退化成只扫描（v0.9.4 修复）。"""
    monkeypatch.setattr(cli_mod, "scan_all", lambda *a, **k: [])
    code = cli_mod.main(["--json", "--clean", ""])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "error"
    assert payload["action"]["not_executed"] is True


# ---------------------------------------------------------------------------
# 8. history 原子写与损坏保护
# ---------------------------------------------------------------------------
def test_history_atomic_write_and_corrupt_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    history_mod.save_history([{"ts": "a"}])
    p = history_mod.history_path()
    assert json.loads(p.read_text(encoding="utf-8")) == [{"ts": "a"}]
    assert not list(p.parent.glob("*.tmp-*"))

    p.write_text("{ this is not json", encoding="utf-8")
    assert history_mod.load_history() == []
    assert list(p.parent.glob("*.corrupt-*"))  # 损坏文件被保留

    history_mod.append_session({"ts": "b"})
    assert [d["ts"] for d in history_mod.load_history()] == ["b"]


# ---------------------------------------------------------------------------
# 9. compact_db 体积口径
# ---------------------------------------------------------------------------
def test_sqlite_reclaimable_estimates_free_pages(tmp_path):
    db = tmp_path / "History"
    con = sqlite3.connect(db)
    con.execute("create table t(a integer, b text)")
    con.executemany("insert into t values (?, ?)", [(i, "x" * 50) for i in range(3000)])
    con.commit()
    con.close()
    con = sqlite3.connect(db)
    con.execute("delete from t")
    con.commit()
    con.close()
    reclaimable = _sqlite_reclaimable(db)
    assert 0 < reclaimable <= db.stat().st_size
    assert _sqlite_reclaimable(tmp_path / "not_a_db.txt") == 0


# ---------------------------------------------------------------------------
# 10. 白名单清空根不短路名称级保护
# ---------------------------------------------------------------------------
@needs_windows
def test_clear_root_does_not_short_circuit_name_protection():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    chk = make_protect_check()
    assert chk(Path(windir) / "Temp") is False  # 白名单根可清空
    assert chk(Path(windir) / "Temp" / "xwechat_files") is True  # 根内受保护名仍拦
