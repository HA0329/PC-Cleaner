"""v0.3.0 升级功能的单元测试。

覆盖：白名单清空例外、安全擦除(shred)、管理员门控、风险分级、
历史记录、回收站解析与恢复、配置重定向（PC_CLEANER_HOME）。
"""

from __future__ import annotations

import os
import sys

import pytest

from pc_cleaner.config import config_dir, load_config
from pc_cleaner.engine import (
    CleanMode,
    _parse_recycle_info,
    _shred_file,
    delete_targets,
    restore_paths,
)
from pc_cleaner.history import append_session, last_session, load_history
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import (
    ALLOWED_CLEAR_ROOTS,
    get_all_category_specs,
    is_risky_spec,
    is_within_clear_root,
)
from pc_cleaner.scanner import (
    make_protect_check,
    scan_spec,
)

import pc_cleaner.rules as rules_mod
import pc_cleaner.scanner as scanner_mod


# ---------------------------------------------------------------------------
# 配置重定向（PC_CLEANER_HOME）
# ---------------------------------------------------------------------------
def test_config_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    assert config_dir() == tmp_path / "pc_cleaner"
    assert load_config()["enable_history"] is True
    assert load_config()["show_risky"] is False


# ---------------------------------------------------------------------------
# 风险分级（借鉴 windows-cleaner-cli 的安全分级）
# ---------------------------------------------------------------------------
def test_risk_metadata_and_helpers():
    specs = {s["key"]: s for s in get_all_category_specs(merge_custom=False)}
    assert specs["system_temp"]["risk"] == "safe"
    assert specs["downloads"]["risk"] == "risky"
    assert specs["dev_purge"]["risk"] == "risky"
    assert specs["system_admin"]["require_admin"] is True
    assert is_risky_spec(specs["downloads"]) is True
    assert is_risky_spec(specs["web_cache"]) is False


# ---------------------------------------------------------------------------
# 受保护路径下的白名单清空例外
# ---------------------------------------------------------------------------
def test_protect_check_allowlist(tmp_path, monkeypatch):
    # 用真实受保护模式 ".git" 作为祖先目录
    root = tmp_path / "proj" / ".git" / "objects" / "download"
    root.mkdir(parents=True)
    monkeypatch.setattr(
        rules_mod,
        "ALLOWED_CLEAR_ROOTS",
        {os.path.normcase(str(root))},
    )
    check = make_protect_check()
    # 白名单目录内部不再受保护（允许清空内容）
    assert check(root) is False
    assert check(root / "sub" / "x.tmp") is False
    # 白名单之外的受保护父目录仍然受保护
    parent = tmp_path / "proj" / ".git" / "objects"
    assert check(parent) is True


def test_engine_guard_allowlist(tmp_path, monkeypatch):
    root = tmp_path / "proj" / ".git" / "objects" / "download"
    root.mkdir(parents=True)
    (root / "junk.tmp").write_bytes(b"x" * 100)
    monkeypatch.setattr(
        rules_mod,
        "ALLOWED_CLEAR_ROOTS",
        {os.path.normcase(str(root))},
    )

    # CLEAR 白名单目录：允许（清空内容）
    res = delete_targets(
        [
            Target(
                path=root,
                kind=TargetKind.DIR,
                action=TargetAction.CLEAR,
                category="t",
                size=100,
            )
        ],
        CleanMode.PERMANENT,
        recycle_fallback=False,
    )
    assert res["failed"] == 0
    assert not (root / "junk.tmp").exists()

    # 恢复一个文件后，DELETE 整个白名单目录：仍被拒绝（二次防御）
    (root / "junk.tmp").write_bytes(b"x" * 100)
    res2 = delete_targets(
        [
            Target(
                path=root,
                kind=TargetKind.DIR,
                action=TargetAction.DELETE,
                category="t",
                size=100,
            )
        ],
        CleanMode.PERMANENT,
        recycle_fallback=False,
    )
    assert res2["failed"] == 1
    assert root.exists()

    # 白名单之外的受保护父目录：CLEAR 也被拒绝
    parent = tmp_path / "proj" / ".git" / "objects"
    res3 = delete_targets(
        [
            Target(
                path=parent,
                kind=TargetKind.DIR,
                action=TargetAction.CLEAR,
                category="t",
                size=1,
            )
        ],
        CleanMode.PERMANENT,
        recycle_fallback=False,
    )
    assert res3["failed"] == 1


# ---------------------------------------------------------------------------
# 安全擦除（shred，借鉴 BleachBit/KCleaner 的 secure delete）
# ---------------------------------------------------------------------------
def test_shred_overwrites_content(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_bytes(b"A" * 4096)
    _shred_file(f)
    content = f.read_bytes()
    assert content != b"A" * 4096
    assert len(content) == 4096


def test_delete_targets_shred_removes_file(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"B" * 2048)
    res = delete_targets(
        [
            Target(
                path=f,
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category="t",
                size=2048,
            )
        ],
        CleanMode.PERMANENT,
        recycle_fallback=False,
        shred=True,
    )
    assert res["deleted"] == 1
    assert not f.exists()


# ---------------------------------------------------------------------------
# 管理员门控（借鉴 sifty 的 Admin 分类）
# ---------------------------------------------------------------------------
def test_scan_spec_admin_blocked_when_not_elevated(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_mod, "is_admin", lambda: False)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "f").write_text("x")
    spec = {
        "key": "sys",
        "label": "系统",
        "require_admin": True,
        "risk": "moderate",
        "targets": [{"type": "clear_dir", "path": str(cache), "label": "c"}],
    }
    res = scan_spec(spec)
    assert res.admin_blocked is True
    assert res.targets == []


def test_scan_spec_admin_scans_when_elevated(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_mod, "is_admin", lambda: True)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "f").write_text("x" * 10)
    spec = {
        "key": "sys",
        "label": "系统",
        "require_admin": True,
        "risk": "moderate",
        "targets": [{"type": "clear_dir", "path": str(cache), "label": "c"}],
    }
    res = scan_spec(spec)
    assert res.admin_blocked is False
    assert len(res.targets) == 1


# ---------------------------------------------------------------------------
# 历史记录（借鉴 sifty 的 audit log / history）
# ---------------------------------------------------------------------------
def test_history_append_and_last(tmp_path, monkeypatch):
    from pc_cleaner import history as history_mod

    monkeypatch.setattr(history_mod, "history_path", lambda: tmp_path / "history.json")
    monkeypatch.setattr(history_mod, "audit_path", lambda: tmp_path / "audit.log")

    append_session(
        {
            "ts": "2025-01-01 00:00:00",
            "mode": "recycle",
            "deleted": 3,
            "failed": 1,
            "freed": 1024,
            "categories": ["web_cache"],
            "targets": [{"path": r"C:\tmp\a", "size": 512}],
            "note": "",
        }
    )
    assert len(load_history()) == 1
    last = last_session()
    assert last is not None
    assert last["mode"] == "recycle"
    assert last["freed"] == 1024


# ---------------------------------------------------------------------------
# 回收站恢复（借鉴 sifty 的 undo）
# ---------------------------------------------------------------------------
def _make_recycle_info(original: str, size: int) -> bytes:
    header = b"\x02\x00\x00\x00" + b"\x00" * 4
    return (
        header
        + size.to_bytes(8, "little")
        + b"\x00" * 8
        + original.encode("utf-16-le")
        + b"\x00\x00"
    )


def test_parse_recycle_info(tmp_path):
    orig = r"D:\Users\me\AppData\Local\Temp\old.tmp"
    info = tmp_path / "$Iold.tmp"
    info.write_bytes(_make_recycle_info(orig, 4096))
    parsed = _parse_recycle_info(info)
    assert parsed is not None
    assert parsed[0] == orig
    assert parsed[1] == 4096


def test_restore_paths_from_recycle(tmp_path):
    # 构造假的回收站布局：<drive>/$Recycle.Bin/<SID>/$Ixxx + $Rxxx
    drive = tmp_path / "drive"
    sid = drive / "$Recycle.Bin" / "S-1-5-21-fake"
    sid.mkdir(parents=True)
    orig = tmp_path / "original" / "myfile.tmp"
    orig_str = str(orig)
    info = sid / "$Iabc"
    info.write_bytes(_make_recycle_info(orig_str, 100))
    data = sid / "$Rabc"
    data.write_bytes(b"recovered content")

    res = restore_paths([orig_str], drives=[str(drive)])
    assert len(res["restored"]) == 1
    assert orig.exists()
    assert orig.read_bytes() == b"recovered content"
    assert not data.exists()


def test_restore_paths_skips_when_missing(tmp_path):
    drive = tmp_path / "drive"
    sid = drive / "$Recycle.Bin" / "S-1-5-21-fake"
    sid.mkdir(parents=True)
    orig = tmp_path / "original" / "gone.tmp"
    res = restore_paths([str(orig)], drives=[str(drive)])
    assert res["restored"] == []
    assert len(res["skipped"]) == 1


# ---------------------------------------------------------------------------
# 内置分类包含新规则
# ---------------------------------------------------------------------------
def test_builtin_new_targets_present():
    specs = {s["key"]: s for s in get_all_category_specs(merge_custom=False)}
    # 系统深度清理（管理员）
    sys_admin_targets = specs["system_admin"]["targets"]
    labels = [t.get("label") for t in sys_admin_targets]
    assert "Windows 更新缓存" in labels
    assert "预读取(Prefetch)" in labels
    # 浏览器新增 Brave / Vivaldi
    web_labels = [t.get("label") for t in specs["web_cache"]["targets"]]
    assert any("Brave" in l for l in web_labels)
    assert any("Vivaldi" in l for l in web_labels)
    # 开发缓存新增 NuGet / Gradle
    dev_labels = [t.get("label") for t in specs["dev_caches"]["targets"]]
    assert any("NuGet" in l for l in dev_labels)
    assert any("Gradle" in l for l in dev_labels)
    # 图标缓存
    sys_labels = [t.get("label") for t in specs["system_temp"]["targets"]]
    assert "图标缓存" in sys_labels
    # 高风险分类
    assert specs["dev_purge"]["risk"] == "risky"
    assert specs["browser_privacy"]["risk"] == "risky"
