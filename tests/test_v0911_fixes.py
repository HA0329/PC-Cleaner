"""v0.9.11 修复的回归测试。

覆盖三件事：

1. **即时通讯数据目录的保护网补齐**（``rules.py``）：
   ``Documents/WeChat Files`` 是微信 3.x 的默认聊天数据目录，rules.json 的
   wechat_cache 说明里已明确"已从规则中移除，不再列入清理范围"，
   但保护名单（``DEFAULT_PROTECTED_PATTERNS``）里漏了它 ——
   实测 ``make_protect_check()(C:\\Users\\X\\Documents\\WeChat Files\\a\\b)``
   返回 ``False``，保护网对最常见的微信数据目录名是空的。
   这里同时守住"补保护不能过度封锁"：``%APPDATA%\\Tencent\\WeChat\\Logs`` /
   ``Temp`` 是 rules.json 有意清理的可重建内容，必须仍然放行。

2. **回收站 ``$I`` 记录 offset 24 字段的真实语义**（``engine.py``）：
   老注释写的是"DWORD 目录记录长度（仅当该记录是目录时非 0）"，与真实数据不符。
   实测 8 条 Windows 自己写出的记录后确认：该字段是"offset 28 起 UTF-16LE 的
   码元数，含结尾 NUL"，**文件与目录记录都非 0**。
   夹具必须写真实值，否则任何"文件记录该字段为 0，改从 offset 24 读路径"的
   错误改动都不会被测试挡住（那正是 ``--undo-last`` 全部匹配失败的成因）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pc_cleaner.config import audit_path, history_path
from pc_cleaner.engine import _parse_recycle_info, CleanMode, delete_targets
from pc_cleaner.history import (
    append_session,
    load_history,
    make_session,
    record_deletion_audit,
    save_history,
)
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import DEFAULT_SKIP_DIRNAMES
from pc_cleaner.scanner import make_protect_check


# ---------------------------------------------------------------------------
# 1. 即时通讯数据目录的保护网
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\X\Documents\WeChat Files\a\b",
        r"D:\WeChat Files\wxid_abc\Msg",
        r"C:\Users\X\Documents\WeChat Files",
        r"C:\Users\X\Documents\wechat files\Attach",
        r"C:\Users\X\Documents\Tencent Files\123456\FileRecv",
        r"D:\Tencent Files\987654\Image",
    ],
)
def test_im_data_dirs_are_protected(path: str) -> None:
    """微信 3.x / QQ 的聊天数据目录必须被保护（含大小写与自定义盘符）。"""
    assert make_protect_check()(Path(path)) is True, (
        f"{path} 应受保护：这是即时通讯的聊天数据，绝不能被自动清理"
    )


@pytest.mark.parametrize(
    "path",
    [
        # rules.json 有意清理的可重建内容：补保护绝不能把它们一起封掉
        r"C:\Users\X\AppData\Roaming\Tencent\xwechat\log\x.log",
        r"C:\Users\X\AppData\Roaming\Tencent\xwechat\update\pkg",
        r"C:\Users\X\AppData\Local\Tencent\WeChat\Temp\x",
        r"C:\Users\X\AppData\Roaming\Tencent\WeChat\Logs\x.log",
        # 常规缓存/临时目录
        r"C:\Users\X\AppData\Local\Temp\x.tmp",
        r"C:\Windows\Temp\x.tmp",
    ],
)
def test_im_data_protection_does_not_over_block(path: str) -> None:
    """保护网只按整体目录名匹配，不得因为补了 "wechat files" 就封掉可清理的缓存。

    这条守的是一个具体的坑：如果把保护模式写成子串 "wechat"（而不是
    完整目录名 "wechat files"），``%APPDATA%\\Tencent\\WeChat\\Logs`` 会连带
    受保护，rules.json 里针对它的清理规则就永远失效了。
    """
    assert make_protect_check()(Path(path)) is False, (
        f"{path} 应当允许清理（是可重建的缓存），被过度保护了"
    )


def test_im_data_dirnames_are_pruned_from_walk() -> None:
    """与 xwechat_files / weixinshuju 保持一致：遍历时也不下降进入。"""
    for name in ("wechat files", "tencent files", "xwechat_files", "weixinshuju"):
        assert name in DEFAULT_SKIP_DIRNAMES, f"{name} 应在 DEFAULT_SKIP_DIRNAMES 中"


# ---------------------------------------------------------------------------
# 2. 回收站 $I 记录 offset 24 的真实语义
# ---------------------------------------------------------------------------
def _real_layout_info(path: Path, original: str, size: int, deleted_at: int = 132000000000000000) -> bytes:
    """按 Windows 真实写出的字节布局构造 $I 记录（见本模块 docstring）。

    offset 24 = 路径码元数（含结尾 NUL），文件/目录一致。
    """
    payload = original.encode("utf-16-le") + b"\x00\x00"
    blob = (
        b"\x02\x00\x00\x00\x00\x00\x00\x00"
        + int(size).to_bytes(8, "little")
        + deleted_at.to_bytes(8, "little")
        + (len(payload) // 2).to_bytes(4, "little")
        + payload
    )
    path.write_bytes(blob)
    return blob


@pytest.mark.parametrize("is_dir", [False, True], ids=["file_record", "dir_record"])
def test_parse_recycle_info_real_layout(tmp_path: Path, is_dir: bool) -> None:
    """真实布局下，文件与目录记录的路径都要从 offset 28 完整读出、不带垃圾字符。"""
    original = (
        r"C:\Users\X\Documents\WeChat Files\wxid_abc\FileStorage"
        if is_dir
        else r"C:\Users\X\AppData\Local\Temp\old file.tmp"
    )
    info = tmp_path / ("$Idir" if is_dir else "$Ifile")
    blob = _real_layout_info(info, original, 4321)

    parsed = _parse_recycle_info(info)
    assert parsed is not None
    assert parsed[0] == original, "路径必须逐字相等（多一个字节就会让 --undo-last 全部落空）"
    assert parsed[1] == 4321

    # 固化真实语义：offset 24 == (len - 28) // 2，文件与目录**都**非 0。
    dword24 = int.from_bytes(blob[24:28], "little")
    assert dword24 != 0, "真实 Windows 记录里文件与目录该字段都非 0"
    assert dword24 == (len(blob) - 28) // 2


def test_offset24_field_is_not_zero_for_files(tmp_path: Path) -> None:
    """押住"文件记录该字段为 0，可以从 offset 24 读路径"这个错误假设。

    如果哪天有人照老注释把文件记录改成从 offset 24 读，取到的会是
    ``b'\\x57\\x00...'`` 这类垃圾前缀，本用例会立刻失败。
    """
    original = r"C:\Users\X\AppData\Local\Temp\plain.tmp"
    info = tmp_path / "$Iplain.tmp"
    _real_layout_info(info, original, 10)

    raw = info.read_bytes()
    from_offset_24 = raw[24:].decode("utf-16-le", errors="ignore")
    assert not from_offset_24.startswith(original), (
        "offset 24 起并不是路径起点；真实起点是 offset 28"
    )
    assert raw[28:].decode("utf-16-le", errors="ignore").startswith(original)


@pytest.mark.skipif(sys.platform != "win32", reason="回收站仅 Windows")
def test_parse_recycle_info_tolerates_trailing_nul(tmp_path: Path) -> None:
    """结尾 NUL 必须被剥掉，不能变成路径尾部的 \\x00。"""
    original = r"D:\proj\build\out.bin"
    info = tmp_path / "$Inul"
    _real_layout_info(info, original, 7)
    parsed = _parse_recycle_info(info)
    assert parsed is not None
    assert parsed[0] == original
    assert "\x00" not in parsed[0]


# ---------------------------------------------------------------------------
# 3. 含孤立代理对的文件名：删除必须仍然留下可撤销记录
# ---------------------------------------------------------------------------
# Windows 文件名是任意 UTF-16，未配对的代理码元是**合法文件名**，Python 用
# ``\udXXX`` 表示。此前这类名字会让 history.json / audit.log 的 utf-8 写入抛
# UnicodeEncodeError —— 它是 ValueError 子类而非 OSError，穿透了 history.py 的
# `except OSError`，后果是：文件被真的删掉，但撤销记录没写出来，
# 且该目标被误报为 failed、history.json.tmp-<pid> 残留。
SURROGATE_NAME = "cache\udfffentry.tmp"


def test_surrogate_filename_makes_a_real_file(tmp_path: Path) -> None:
    """先确认前提成立：这个文件名真的能建出来（否则下面的用例是空转）。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows 允许未配对代理码元出现在文件名中")
    victim = tmp_path / SURROGATE_NAME
    victim.write_bytes(b"z" * 20)
    assert victim.exists()
    # 目录枚举会把这个名字原样带回来（说明它确实是文件系统里的真实名字）
    assert SURROGATE_NAME in os.listdir(tmp_path)


def test_save_history_roundtrips_surrogate_path(tmp_path: Path, monkeypatch) -> None:
    """history.json 必须能存下含代理码元的路径，且读回来逐字符一致。"""
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    raw_path = str(tmp_path / SURROGATE_NAME)

    save_history([make_session(
        mode="permanent", deleted=1, failed=0, freed=20,
        categories=["t"], targets=[{"path": raw_path, "size": 20}],
    )])

    hp = history_path()
    assert hp.exists(), "历史必须落盘（此前 UnicodeEncodeError 会让整个文件写不出来）"
    sessions = load_history()
    assert len(sessions) == 1
    assert sessions[0]["targets"][0]["path"] == raw_path, "路径必须无损往返"
    # 原子写的临时文件不得残留
    leftovers = list(hp.parent.glob(f"{hp.name}.tmp-*"))
    assert not leftovers, f"残留临时文件: {leftovers}"


def test_record_deletion_audit_survives_surrogate_path(tmp_path: Path, monkeypatch) -> None:
    """"尽力而为，失败不报错"必须对代理码元也成立（不能抛出 UnicodeEncodeError）。"""
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    record_deletion_audit(Path(str(tmp_path / SURROGATE_NAME)), 20, "permanent", 20)
    ap = audit_path()
    assert ap.exists() and ap.stat().st_size > 0, "审计行必须真的写进去"
    assert "\\udfff" in ap.read_text(encoding="utf-8"), (
        "代理码元应以 \\udfff 转义形式留存，便于事后追溯"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 文件名可含代理码元")
def test_delete_targets_keeps_undo_record_for_surrogate_name(
    tmp_path: Path, monkeypatch
) -> None:
    """端到端：删除含代理码元文件名的目标后，仍必须有可撤销记录。

    这条守的是实际事故现场：文件被删掉了，但 history.json 写不出来，
    ``--undo-last`` 永远找不回；同时目标被误报为 failed。
    """
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path / "home"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    victim = sandbox / SURROGATE_NAME
    victim.write_bytes(b"z" * 20)
    assert victim.exists()

    # 与 menu.py:791-794 的 audit_log 一致：审计回调只写 audit.log
    recorded: list[tuple] = []

    def audit(path, size, mode_name, freed=0):
        record_deletion_audit(path, size, mode_name, freed)
        recorded.append((path, size, mode_name, freed))

    t = Target(
        path=victim,
        kind=TargetKind.FILE,
        action=TargetAction.DELETE,
        category="t",
        size=20,
        file_count=1,
    )
    res = delete_targets([t], CleanMode.PERMANENT, audit=audit)

    assert not victim.exists(), "文件应当已被删除"
    assert res["failed"] == 0, f"审计写入失败不得把已删目标误报为 failed: {res}"
    assert res["deleted"] == 1, f"确实删掉了，必须计入 deleted: {res}"
    assert recorded, "audit 回调应当被调用"

    # 与 menu.py:849-866 一致：删除结束后追加历史会话（撤销记录的唯一来源）
    append_session(make_session(
        mode="permanent",
        deleted=res["deleted"],
        failed=res["failed"],
        freed=res["freed"],
        categories=["t"],
        targets=[{"path": str(victim), "size": 20}],
    ))

    hp = history_path()
    assert hp.exists(), "history.json 必须落盘，否则 --undo-last 无法恢复"
    sessions = load_history()
    assert sessions, "历史里必须有本次会话"
    paths = [t_["path"] for s in sessions for t_ in s.get("targets", [])]
    assert str(victim) in paths, "含代理码元的路径必须无损存进历史，撤销才可能匹配上"
    # 原子写临时文件不得残留
    assert not list(hp.parent.glob(f"{hp.name}.tmp-*"))
    # 审计日志也要有内容（此前 size 为 0）
    assert audit_path().stat().st_size > 0
