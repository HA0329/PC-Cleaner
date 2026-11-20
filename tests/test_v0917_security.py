"""v0.9.17 安全回归：网络快捷方式、junction 剪枝、MCP 确认与保护名单。"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from pc_cleaner import commands, engine, lnk, mcp, scanner
from pc_cleaner.models import CategoryResult, CleanMode, TargetAction, TargetKind


def _make_dir_link(link: Path, target: Path) -> None:
    """建目录链接：Windows 用 junction（不需要符号链接特权），POSIX 用 symlink。"""
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        os.symlink(target, link)


def _make_network_shortcut(
    net_name: str, suffix: str, *, unicode_strings: bool = False
) -> bytes:
    """构造只含 LinkInfo / CommonNetworkRelativeLink 的最小 .lnk。"""
    has_link_info = 0x00000002
    is_unicode = 0x00000080
    flags = has_link_info | is_unicode
    header = struct.pack("<I", 0x4C) + b"\x01\x14\x02\x00" + b"\x00" * 12
    header += struct.pack("<I", flags)
    header += b"\x00" * (0x4C - len(header))

    if unicode_strings:
        net_bytes = net_name.encode("utf-16-le") + b"\x00\x00"
        suffix_bytes = suffix.encode("utf-16-le") + b"\x00\x00" if suffix else b""
        header_size = 0x24
    else:
        net_bytes = net_name.encode("cp936") + b"\x00"
        suffix_bytes = suffix.encode("cp936") + b"\x00" if suffix else b""
        header_size = 0x1C

    if unicode_strings:
        # 真正的 Unicode CNRL：0x1C 头 + NetNameOffsetUnicode 指向字符串。
        cnrl_size = 0x1C + len(net_bytes)
        cnrl = (
            struct.pack("<I", cnrl_size)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0)  # NetNameOffset（ANSI 不使用）
            + struct.pack("<I", 0)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0x1C)  # NetNameOffsetUnicode
            + struct.pack("<I", 0)
            + net_bytes
        )
    else:
        cnrl_size = 0x14 + len(net_bytes)
        cnrl = (
            struct.pack("<I", cnrl_size)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0x14)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0)
            + net_bytes
        )

    if unicode_strings:
        suffix_off = 0
        suffix_off_u = header_size + len(cnrl) if suffix_bytes else 0
        link_size = header_size + len(cnrl) + len(suffix_bytes)
        link_info = (
            struct.pack("<I", link_size)
            + struct.pack("<I", header_size)
            + struct.pack("<I", 0x00000002)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0)
            + struct.pack("<I", header_size)
            + struct.pack("<I", suffix_off)
            + struct.pack("<I", 0)
            + struct.pack("<I", suffix_off_u)
            + cnrl
            + suffix_bytes
        )
    else:
        suffix_off = header_size + len(cnrl) if suffix_bytes else 0
        link_size = header_size + len(cnrl) + len(suffix_bytes)
        link_info = (
            struct.pack("<I", link_size)
            + struct.pack("<I", header_size)
            + struct.pack("<I", 0x00000002)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0)
            + struct.pack("<I", header_size)
            + struct.pack("<I", suffix_off)
            + cnrl
            + suffix_bytes
        )
    return header + link_info


def test_network_shortcut_is_not_broken(tmp_path: Path) -> None:
    p = tmp_path / "network.lnk"
    p.write_bytes(_make_network_shortcut(r"\\server\share", r"\folder\app.exe"))
    info = lnk.parse_shortcut(p)
    assert info.network is True
    assert info.target.lower().startswith("\\\\server\\share")
    assert lnk.classify(info)[0] == lnk.V_UNKNOWN
    assert lnk.is_broken(info) is False


def test_network_shortcut_unicode_header(tmp_path: Path) -> None:
    p = tmp_path / "network-u.lnk"
    p.write_bytes(
        _make_network_shortcut(
            r"\\server\share", r"\folder\app.exe", unicode_strings=True
        )
    )
    info = lnk.parse_shortcut(p)
    assert info.network is True
    assert info.target.lower().startswith("\\\\server\\share")
    assert lnk.classify(info)[0] == lnk.V_UNKNOWN


def test_network_shortcut_without_parsed_name_still_unknown(tmp_path: Path) -> None:
    p = tmp_path / "network-bad.lnk"
    p.write_bytes(_make_network_shortcut(r"\\server\share", r"\folder\app.exe"))
    info = lnk.parse_shortcut(p)
    info.targets.clear()
    info.error = "模拟解析失败"
    assert lnk.classify(info)[0] == lnk.V_UNKNOWN
    assert lnk.is_broken(info) is False


def test_rmtree_refuses_directory_with_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "important.txt").write_text("keep me", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    (root / "ordinary.txt").write_text("ordinary", encoding="utf-8")
    _make_dir_link(root / "link", target)

    with pytest.raises(PermissionError):
        engine._delete_path(
            root,
            CleanMode.PERMANENT,
            is_dir=True,
            recycle_fallback=False,
            is_protected=lambda _p: False,
        )
    assert (target / "important.txt").read_text(encoding="utf-8") == "keep me"


def test_shred_does_not_follow_dir_link(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    important = external / "important.txt"
    important.write_text("do not overwrite", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    _make_dir_link(root / "link", external)

    with pytest.raises(PermissionError):
        engine._delete_path(
            root,
            CleanMode.PERMANENT,
            is_dir=True,
            recycle_fallback=False,
            is_protected=lambda _p: False,
            shred=True,
        )
    assert important.read_text(encoding="utf-8") == "do not overwrite"


def test_acknowledge_danger_rejects_string_false(tmp_path: Path, monkeypatch) -> None:
    state = {"called": False}

    monkeypatch.setattr(
        mcp,
        "_scan_categories",
        lambda cats, deep: (
            [
                CategoryResult(
                    key="downloads",
                    label="下载",
                    risk="risky",
                    scanned=True,
                    targets=[
                        mcp.Target(
                            path=tmp_path / "x.bin",
                            kind=TargetKind.FILE,
                            action=TargetAction.DELETE,
                            category="downloads",
                            size=1,
                        )
                    ],
                )
            ],
            [],
        ),
    )
    monkeypatch.setattr(mcp, "delete_targets", lambda *a, **k: state.__setitem__("called", True))
    resp = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "preview_delete", "arguments": {"categories": ["downloads"]}},
        },
        allow_delete=True,
    )
    token = resp["result"]["structuredContent"]["confirm_token"]
    resp = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "delete",
                "arguments": {"confirm_token": token, "acknowledge_danger": "false"},
            },
        },
        allow_delete=True,
    )
    assert resp["result"]["structuredContent"]["ok"] is False
    assert state["called"] is False


def test_installer_and_driverstore_are_protected() -> None:
    check = scanner.make_protect_check()
    assert check(Path("C:/Windows/Installer/cache.msi")) is True
    assert check(Path("C:/Windows/System32/DriverStore/FileRepository")) is True


def test_cmd_metacharacters_are_quoted_for_elevation() -> None:
    assert commands._quote_arg(r"C:\A&B\cfg.json") == r'"C:\A&B\cfg.json"'
    line = commands._elevated_command_line(
        ["--export-config", r"C:\A&B\cfg.json"],
        r"C:\Program Files\Python\python.exe",
        r"D:\app\_launcher.py",
    )
    assert r'"C:\A&B\cfg.json"' in line
