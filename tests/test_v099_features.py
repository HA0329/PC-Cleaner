"""v0.9.9 回归测试：快捷方式清理、使用痕迹、注册表只读扫描与四处修复。

覆盖内容：

* **修复**
  - ``menu._interactive`` 刷新扫描时参数不再错位（此前 ``scan_depth=True`` /
    ``workers=20``，导致 find_dirs 只遍历 1 层）；
  - MCP ``delete`` 写 ``history.json`` 与 ``audit.log``（此前完全不留痕，
    导致 MCP 删掉的东西无法 ``--undo-last``）；
  - ``scanner.print_detail_report`` 不再调用死代码分支；
  - ``rules.validate_rules`` 的 docstring 恢复为单一（此前有一段孤立的字符串）。
* **新增：失效快捷方式**
  - ``lnk.parse_shortcut`` 解析 ``LinkInfo`` / ``EnvironmentVariableDataBlock`` /
    ``StringData`` 三条路径（纯 Python，测试里手工拼字节，不依赖真实 .lnk）；
  - ``classify`` 的分级：有效 / 失效 / 卷不可用 / URL / 命名空间 / 损坏；
  - ``_scan_broken_shortcuts`` 只产出"确定失效"，``exclude_dirs`` 生效，
    目录递归生效。
* **新增：使用痕迹** —— ``Recent`` 已从 ``system_temp``（safe）移出，
  由 ``usage_traces``（moderate）单独管理。
* **新增：注册表只读扫描** —— 辅助函数行为 + 报告结构 + 绝不写入的保证。
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from pc_cleaner import lnk, menu, registry, rules, scanner
from pc_cleaner.models import CleanMode

pytestmark = pytest.mark.filterwarnings("error::SyntaxWarning")


# ---------------------------------------------------------------------------
# 工具：手工构造 .lnk 字节（不依赖 Windows / COM）
# ---------------------------------------------------------------------------
def _make_lnk(
    *,
    local_base_path: str = "",
    env_target: str = "",
    relative_path: str = "",
    with_idlist: bool = True,
    force_no_link_info: bool = False,
    url: str = "",
    namespace_idlist: bool = False,
) -> bytes:
    """按 MS-SHLLINK 手工拼一个最小 ``.lnk``。

    只包含本模块解析所需的字段，用于测试解析器而不是 Windows 本身。
    """
    HAS_IDLIST = 0x00000001
    HAS_LINK_INFO = 0x00000002
    HAS_RELATIVE = 0x00000008
    IS_UNICODE = 0x00000080
    FORCE_NO_LINK_INFO = 0x00000100
    HAS_EXP_STRING = 0x00000200

    flags = IS_UNICODE
    if with_idlist:
        flags |= HAS_IDLIST
    if local_base_path and not force_no_link_info:
        flags |= HAS_LINK_INFO
    if relative_path:
        flags |= HAS_RELATIVE
    if force_no_link_info:
        flags |= FORCE_NO_LINK_INFO
    if env_target:
        flags |= HAS_EXP_STRING

    header = struct.pack("<I", 0x4C) + b"\x01\x14\x02\x00" + b"\x00" * 12
    header += struct.pack("<I", flags)
    header += b"\x00" * (0x4C - len(header))

    body = b""
    if with_idlist:
        if namespace_idlist:
            # 命名空间型：只有 shell 扩展段（GUID），**没有**文件系统盘符根项
            guid = bytes.fromhex("1f50e04fd020ea3a6910a2d808002b30309d")
            item = guid + b"\x00" * 6
        else:
            # 一个"文件系统盘符根项"：2F <盘符> 3A 5C …（本机实测形态，共 25 字节）
            item = b"\x2f" + b"C:" + b"\\" + b"\x00" * 17
        item = struct.pack("<H", len(item) + 3) + item + b"\x00\x00"
        body += struct.pack("<H", len(item)) + item

    if local_base_path and not force_no_link_info:
        lbp = local_base_path.encode("cp936") + b"\x00"
        # LinkInfo: size + header_size + flags + volid_off + lbp_off + cnrl_off + suffix_off
        header_size = 0x1C
        flags_li = 0x00000001  # VolumeIDAndLocalBasePath
        lbp_off = header_size
        li = (
            struct.pack("<I", header_size + len(lbp))
            + struct.pack("<I", header_size)
            + struct.pack("<I", flags_li)
            + struct.pack("<I", 0)          # VolumeIDOffset
            + struct.pack("<I", lbp_off)    # LocalBasePathOffset
            + struct.pack("<I", 0)          # CommonNetworkRelativeLinkOffset
            + struct.pack("<I", 0)          # CommonPathSuffixOffset
            + lbp
        )
        body += li

    if relative_path:
        raw = relative_path.encode("utf-16-le")
        body += struct.pack("<H", len(relative_path)) + raw

    if env_target:
        payload = env_target.encode("cp936") + b"\x00"
        payload += (env_target + "\x00").encode("utf-16-le") + b"\x00\x00"
        body += struct.pack("<I", len(payload) + 8) + struct.pack("<I", 0xA0000001) + payload

    if url:
        payload = b"\x61" + b"\x00" * 6 + url.encode("utf-16-le") + b"\x00\x00"
        item = struct.pack("<H", len(payload) + 3) + payload + b"\x00\x00"
        # 替换掉 IDList 里的根项，改成 URL 段
        head = header
        idl = struct.pack("<H", len(item)) + item
        return head + idl

    return header + body


def _write_lnk(path: Path, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_make_lnk(**kwargs))
    return path


def _offline_volume_target() -> str:
    """一个"所在卷当前不可用"的目标路径（跨平台等价写法）。

    v0.9.10：原用例硬编码 ``Q:\\Tools\\x.exe``。Windows 上这表示"Q: 盘未挂载"，
    正是要考的 ``V_UNAVAILABLE`` 分支；但 POSIX 上 ``Q:\\...`` 没有盘符概念，
    ``os.path.splitdrive`` 返回空盘符 → ``_volume_root_available`` 直接 True →
    被误判成 ``BROKEN``（Linux CI 因此必红）。这里按平台给出等价目标：
    Windows 用未挂载盘符，POSIX 用不存在的挂载点（同样"卷不可用"）。
    """
    if sys.platform == "win32":
        return r"Q:\Tools\x.exe"
    return "/nonexistent-volume/Tools/x.exe"


# ===========================================================================
# 修复 1：菜单刷新扫描的参数不再错位
# ===========================================================================
class TestMenuScanArguments:
    """``_interactive`` 必须以正确顺序/关键字把参数交给 ``_scan_for_menu``。

    回归背景：v0.9.8 的调用是 ``_scan_for_menu(specs, show_progress, scan_depth,
    workers)``，而定义是 ``(specs, scan_depth, workers, show_progress)``，
    于是 ``scan_all`` 收到 ``scan_depth=True``（=1 层）、``workers=20``（超上限）。
    现象：菜单里按 ``x`` / ``f`` 重扫后，find_dirs 类规则扫不到深层目标。
    """

    def _run_refresh(self, monkeypatch) -> dict:
        captured: dict = {}

        def fake_scan_all(specs, scan_depth=20, on_progress=None, workers=0,
                          on_category_done=None):
            captured.update(scan_depth=scan_depth, workers=workers)
            return []

        monkeypatch.setattr(menu, "scan_all", fake_scan_all)
        monkeypatch.setattr(menu, "get_enabled_category_specs", lambda cfg, deep=False: [])
        monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)
        monkeypatch.setattr(menu, "_print_env_adaptation", lambda *a, **k: None)
        monkeypatch.setattr(menu, "_print_summary_table", lambda *a, **k: None)
        monkeypatch.setattr(menu, "_print_disk_free", lambda *a, **k: None)
        monkeypatch.setattr(menu, "_print_menu_status", lambda *a, **k: None)
        monkeypatch.setattr(menu, "_print_menu_footer", lambda *a, **k: None)
        monkeypatch.setattr(menu, "_risk_badge", lambda r: "o")

        answers = iter(["x", "q"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
        monkeypatch.setattr(menu, "load_config", lambda: {})

        class _Args:
            dry_run = False
            recycle_fallback = False
            shred = False
            shred_passes = 1
            ext = None
            min_size_mb = None
            older_than_days = None

        with redirect_stdout(io.StringIO()):
            menu._interactive(
                [], [], CleanMode.RECYCLE, _Args(), {}, True,
                scan_depth=20, show_progress=True, workers=4,
            )
        return captured

    def test_refresh_passes_real_depth_and_workers(self, monkeypatch) -> None:
        captured = self._run_refresh(monkeypatch)
        assert captured["scan_depth"] == 20, (
            f"scan_depth 应为 20，实际 {captured['scan_depth']!r}"
            "（=True 说明参数又错位了）"
        )
        assert captured["workers"] == 4, f"workers 应为 4，实际 {captured['workers']!r}"
        assert not isinstance(captured["scan_depth"], bool)

    def test_helper_signature_is_keyword_compatible(self) -> None:
        import inspect

        params = list(inspect.signature(menu._scan_for_menu).parameters)
        assert params == ["specs", "scan_depth", "workers", "show_progress"]


# ===========================================================================
# 修复 2：MCP delete 必须留痕
# ===========================================================================
class TestMcpDeleteRecordsHistory:
    """MCP ``delete`` 必须写 history + audit，否则 undo 永远找不到记录。"""

    def _manifest(self, tmp_path: Path, monkeypatch):
        from pc_cleaner import mcp
        from pc_cleaner.models import CategoryResult

        f = tmp_path / "junk.tmp"
        f.write_bytes(b"x" * 32)
        target = mcp.Target(
            path=f,
            kind=mcp.TargetKind.FILE,
            action=mcp.TargetAction.DELETE,
            category="system_temp",
            size=32,
        )
        monkeypatch.setattr(
            mcp, "_scan_categories", lambda cats, deep: ([CategoryResult(
                key="system_temp", label="系统临时文件", risk="safe",
                scanned=True, targets=[target],
            )], [])
        )
        preview = mcp._tool_preview_delete({"categories": ["system_temp"]}, False)
        return mcp, preview["confirm_token"]

    def test_delete_writes_history_and_audit(self, tmp_path, monkeypatch) -> None:
        from pc_cleaner import history

        mcp, token = self._manifest(tmp_path, monkeypatch)
        sessions: list[dict] = []
        audits: list[tuple] = []
        monkeypatch.setattr(mcp, "append_session", lambda s: sessions.append(s))
        monkeypatch.setattr(
            mcp, "record_deletion_audit",
            lambda p, s, m, f=0: audits.append((str(p), s, m, f)),
        )
        def fake_delete_targets(targets, mode, **kw):
            # 真实实现会对每个目标回调 audit；这里必须照做，否则测不出留痕
            cb = kw.get("audit")
            if cb is not None:
                for t in targets:
                    cb(t.path, t.size, mode.value, t.size)
            return {
                "deleted": len(targets), "failed": 0, "freed": 32,
                "recycled": 0, "skipped": 0, "skipped_in_use": 0,
            }

        monkeypatch.setattr(mcp, "delete_targets", fake_delete_targets)
        monkeypatch.setattr(mcp, "load_config", lambda: {"enable_history": True})

        payload = mcp._tool_delete({"confirm_token": token}, True)
        assert payload["status"] == "deleted"
        assert payload["history_recorded"] is True
        assert len(sessions) == 1, "delete 必须落一条历史会话"
        assert sessions[0]["mode"] == "recycle"
        assert sessions[0]["categories"] == ["system_temp"]
        assert sessions[0]["targets"], "历史会话必须带目标清单（undo 依赖它）"
        assert sessions[0].get("session_id")
        assert audits, "必须记录审计日志"
        assert audits[0][1] == 32  # 字节数
        assert history is not None  # 模块可导入（不依赖真实文件系统）

    def test_delete_skips_history_when_disabled(self, tmp_path, monkeypatch) -> None:
        mcp, token = self._manifest(tmp_path, monkeypatch)
        sessions: list[dict] = []
        monkeypatch.setattr(mcp, "append_session", lambda s: sessions.append(s))
        monkeypatch.setattr(mcp, "record_deletion_audit", lambda *a, **k: None)
        monkeypatch.setattr(
            mcp, "delete_targets",
            lambda targets, mode, **kw: {
                "deleted": 1, "failed": 0, "freed": 0, "recycled": 0,
                "skipped": 0, "skipped_in_use": 0,
            },
        )
        monkeypatch.setattr(mcp, "load_config", lambda: {"enable_history": False})
        payload = mcp._tool_delete({"confirm_token": token}, True)
        assert payload["history_recorded"] is False
        assert sessions == []


# ===========================================================================
# 新增：失效快捷方式解析与判定
# ===========================================================================
class TestShortcutParsing:
    def test_link_info_local_base_path(self, tmp_path) -> None:
        exe = tmp_path / "app.exe"
        exe.write_bytes(b"MZ")
        p = _write_lnk(tmp_path / "a.lnk", local_base_path=str(exe))
        info = lnk.parse_shortcut(p)
        assert info.error == ""
        assert info.target.lower() == str(exe).lower()

    def test_env_data_block_target(self, tmp_path) -> None:
        """``ForceNoLinkInfo`` 型（任务管理器一类）：目标在环境变量块里。"""
        fake = str(tmp_path / "missing" / "tool.exe")
        p = _write_lnk(
            tmp_path / "b.lnk", env_target=fake, force_no_link_info=True
        )
        info = lnk.parse_shortcut(p)
        assert info.target == fake
        assert info.force_no_link_info is True

    def test_relative_path_is_joined_and_normalised(self, tmp_path) -> None:
        # v0.9.10：用 os.sep 拼相对段 —— 旧写法硬编码 "..\\other\\app.exe"，
        # 在 POSIX 上 os.path.normpath 只认 "/"，于是反斜杠被当成普通文件名字符，
        # 期望值/实际值对不上（Windows 语义的用例，Linux CI 必红）。
        rel = os.path.join("..", "other", "app.exe")
        p = _write_lnk(tmp_path / "c.lnk", relative_path=rel)
        info = lnk.parse_shortcut(p)
        expected = os.path.normpath(os.path.join(str(tmp_path), "..", "other", "app.exe"))
        assert info.target == expected
        assert ".." not in info.target, "相对段必须被折叠"

    def test_short_file_is_invalid(self, tmp_path) -> None:
        p = tmp_path / "empty.lnk"
        p.write_bytes(b"")
        info = lnk.parse_shortcut(p)
        assert lnk.classify(info)[0] == lnk.V_INVALID

    def test_non_lnk_header_is_invalid(self, tmp_path) -> None:
        p = tmp_path / "fake.lnk"
        p.write_bytes(b"NOTALNK" + b"\x00" * 100)
        assert lnk.classify(lnk.parse_shortcut(p))[0] == lnk.V_INVALID

    def test_missing_file_is_invalid(self, tmp_path) -> None:
        assert lnk.classify(lnk.parse_shortcut(tmp_path / "nope.lnk"))[0] == lnk.V_INVALID


class TestShortcutVerdicts:
    def test_existing_target_is_ok(self, tmp_path) -> None:
        exe = tmp_path / "ok.exe"
        exe.write_bytes(b"MZ")
        p = _write_lnk(tmp_path / "ok.lnk", local_base_path=str(exe))
        assert lnk.classify(lnk.parse_shortcut(p))[0] == lnk.V_OK

    def test_missing_target_is_broken(self, tmp_path) -> None:
        p = _write_lnk(
            tmp_path / "dead.lnk",
            local_base_path=str(tmp_path / "gone" / "app.exe"),
        )
        info = lnk.parse_shortcut(p)
        assert lnk.classify(info)[0] == lnk.V_BROKEN
        assert lnk.is_broken(info) is True

    def test_offline_volume_is_not_broken(self, tmp_path) -> None:
        """未挂载盘上的目标不能判成失效 —— 插上盘还能用。"""
        p = _write_lnk(tmp_path / "offline.lnk", env_target=_offline_volume_target(),
                       force_no_link_info=True)
        info = lnk.parse_shortcut(p)
        assert lnk.classify(info)[0] == lnk.V_UNAVAILABLE
        assert lnk.is_broken(info) is False

    def test_url_shortcut_is_unknown(self, tmp_path) -> None:
        p = tmp_path / "web.lnk"
        p.write_bytes(_make_lnk(url="https://example.com/"))
        info = lnk.parse_shortcut(p)
        assert info.url.startswith("https://")
        assert lnk.classify(info)[0] == lnk.V_UNKNOWN

    def test_namespace_shortcut_is_unknown(self, tmp_path) -> None:
        """控制面板 / 回收站这类命名空间快捷方式不是死链。"""
        p = _write_lnk(tmp_path / "ns.lnk", namespace_idlist=True)
        info = lnk.parse_shortcut(p)
        assert lnk.classify(info)[0] == lnk.V_UNKNOWN
        assert lnk.is_broken(info) is False


class TestBrokenShortcutScanner:
    def _spec(self, base: Path, **extra) -> dict:
        spec = {
            "type": "broken_shortcuts",
            "bases": [str(base)],
            "pattern": "*.lnk",
            "label": "测试",
        }
        spec.update(extra)
        return spec

    def test_only_broken_targets_are_returned(self, tmp_path) -> None:
        good = tmp_path / "good.exe"
        good.write_bytes(b"MZ")
        _write_lnk(tmp_path / "good.lnk", local_base_path=str(good))
        _write_lnk(tmp_path / "dead.lnk", local_base_path=str(tmp_path / "gone.exe"))
        _write_lnk(tmp_path / "offline.lnk", env_target=_offline_volume_target(),
                   force_no_link_info=True)
        (tmp_path / "placeholder.lnk").write_bytes(b"")

        chk = scanner.make_protect_check()
        targets = scanner._scan_broken_shortcuts(self._spec(tmp_path), "cat", chk)
        names = sorted(t.path.name for t in targets)
        assert names == ["dead.lnk"], names
        assert "目标不存在" in targets[0].label

    def test_include_unavailable_opt_in(self, tmp_path) -> None:
        _write_lnk(tmp_path / "offline.lnk", env_target=_offline_volume_target(),
                   force_no_link_info=True)
        chk = scanner.make_protect_check()
        assert scanner._scan_broken_shortcuts(self._spec(tmp_path), "c", chk) == []
        got = scanner._scan_broken_shortcuts(
            self._spec(tmp_path, include_unavailable=True), "c", chk
        )
        assert [t.path.name for t in got] == ["offline.lnk"]

    def test_exclude_dirs_skips_startup(self, tmp_path) -> None:
        (tmp_path / "Startup").mkdir()
        _write_lnk(tmp_path / "Startup" / "auto.lnk",
                   local_base_path=str(tmp_path / "gone.exe"))
        _write_lnk(tmp_path / "desk.lnk", local_base_path=str(tmp_path / "gone.exe"))
        chk = scanner.make_protect_check()
        spec = self._spec(tmp_path, exclude_dirs=["Startup"])
        names = sorted(t.path.name for t in scanner._scan_broken_shortcuts(spec, "c", chk))
        assert names == ["desk.lnk"], names

    def test_recursive_by_default(self, tmp_path) -> None:
        (tmp_path / "a" / "b").mkdir(parents=True)
        _write_lnk(tmp_path / "a" / "b" / "deep.lnk",
                   local_base_path=str(tmp_path / "gone.exe"))
        chk = scanner.make_protect_check()
        names = [t.path.name for t in scanner._scan_broken_shortcuts(self._spec(tmp_path), "c", chk)]
        assert names == ["deep.lnk"], "默认必须递归（开始菜单是多层目录）"
        names_flat = [
            t.path.name
            for t in scanner._scan_broken_shortcuts(
                self._spec(tmp_path, recursive=False), "c", chk
            )
        ]
        assert names_flat == []

    def test_no_bases_returns_empty(self) -> None:
        assert scanner._scan_broken_shortcuts({"type": "broken_shortcuts"}, "c",
                                             scanner.make_protect_check()) == []


# ===========================================================================
# 新增：规则与分类（使用痕迹 / 快捷方式）
# ===========================================================================
class TestNewCategories:
    def _specs(self) -> dict[str, dict]:
        return {s["key"]: s for s in rules._builtin_specs(deep=True)}

    def test_broken_shortcuts_category_exists_and_is_risky(self) -> None:
        spec = self._specs()["broken_shortcuts"]
        assert spec["risk"] == "risky"
        assert rules.category_label("broken_shortcuts") == "失效快捷方式"
        types = {t["type"] for t in spec["targets"]}
        assert types == {"broken_shortcuts"}

    def test_usage_traces_category_exists(self) -> None:
        spec = self._specs()["usage_traces"]
        assert spec["risk"] == "moderate"
        assert rules.category_label("usage_traces") == "使用痕迹"

    def test_recent_moved_out_of_system_temp(self) -> None:
        """Recent 属使用痕迹，不能继续挂在 safe 的 system_temp 下被静默清掉。"""
        specs = self._specs()
        st_locs = " ".join(
            str(t.get("path") or t.get("base") or t.get("paths") or "")
            for t in specs["system_temp"]["targets"]
        )
        assert "Windows/Recent" not in st_locs

    def test_broken_shortcuts_requires_base(self) -> None:
        report = rules.validate_rules(
            [{"key": "x", "label": "x", "risk": "safe",
              "targets": [{"type": "broken_shortcuts", "pattern": "*.lnk"}]}]
        )
        assert any("缺少 base" in e for e in report), list(report)

    def test_broken_shortcuts_with_bases_is_valid(self) -> None:
        report = rules.validate_rules(
            [{"key": "x", "label": "x", "risk": "safe",
              "targets": [{"type": "broken_shortcuts", "bases": ["%TEMP%"]}]}]
        )
        assert list(report) == [], list(report)

    def test_builtin_rules_all_valid(self) -> None:
        report = rules.validate_rules(rules._builtin_specs(deep=True))
        assert list(report) == [], list(report)

    def test_docstring_is_single(self) -> None:
        """validate_rules 此前有"孤立字符串"（第一段文档被第二段顶掉）。"""
        doc = rules.validate_rules.__doc__ or ""
        assert doc.strip().startswith("校验规则列表")
        assert "audit_local" in doc and "错误（errors" in doc


# ===========================================================================
# 新增：注册表只读扫描
# ===========================================================================
class TestRegistryScan:
    def test_exe_parsing_variants(self, tmp_path) -> None:
        real = tmp_path / "uninst.exe"
        real.write_bytes(b"MZ")
        cases = {
            "MsiExec.exe /X{5EE4D2B6-A5DC-4321-B6BD-3EBC98120A51}": "MsiExec.exe",
            "msiexec.exe /I{89850E15-F7D6-476D-972E-F8F5215E4498}": "MsiExec.exe",
            "": "",
            "rundll32.exe foo.dll": "",  # 整串不是绝对 .exe 路径 → 证据不足
        }
        for raw, expected in cases.items():
            assert registry._exe_from_uninstall_string(raw) == expected, raw
        # 带引号（含参数）
        assert registry._exe_from_uninstall_string(f'"{real}" /S') == str(real)
        # 不带引号但**确实存在**的绝对路径 → 逐段切分应能切对
        assert registry._exe_from_uninstall_string(f"{real} /S") == str(real)
        # 不带引号、不存在的绝对路径 → 原样返回（调用方据此判定"程序已不存在"）
        ghost = r"C:\Program Files\Gone\uninst.exe"
        assert registry._exe_from_uninstall_string(ghost) == ghost
        assert registry._is_absolute_path(ghost)

    def test_absolute_path_detection(self) -> None:
        assert registry._is_absolute_path(r"C:\x\y.exe")
        assert registry._is_absolute_path(r"\\server\share\y.exe")
        assert not registry._is_absolute_path("MsiExec.exe")
        assert not registry._is_absolute_path("")

    def test_hive_constants_resolve(self) -> None:
        """``HKEY_HKLM`` 这种拼法在 winreg 里不存在，必须走映射表。"""
        assert registry._HIVE_CONSTANTS["HKLM"] == "HKEY_LOCAL_MACHINE"
        assert registry._HIVE_CONSTANTS["HKCU"] == "HKEY_CURRENT_USER"
        assert registry._hive_of(object(), "HKLM") is None  # 无属性时不抛异常

    @pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有意义")
    def test_hive_lookup_returns_root_key(self) -> None:
        import winreg

        assert registry._hive_of(winreg, "HKLM") is winreg.HKEY_LOCAL_MACHINE
        assert registry._hive_of(winreg, "HKCU") is winreg.HKEY_CURRENT_USER
        assert registry._hive_of(winreg, "NOPE") is None

    @pytest.mark.skipif(sys.platform != "win32", reason="msi.dll 仅 Windows")
    def test_msi_state_installed_vs_unknown(self) -> None:
        """已安装产品返回 5，不存在的产品码返回 -1（无法判定 → None）。"""
        state = registry._msi_query_product_state("{00000000-0000-0000-0000-000000000000}")
        assert state in (-1, None)
        winreg = registry._import_winreg()
        assert registry._msi_product_installed(
            winreg, "{00000000-0000-0000-0000-000000000000}"
        ) is None

    def test_report_shape_and_readonly_flag(self) -> None:
        report = registry.scan_registry()
        data = report.to_dict()
        assert data["read_only"] is True
        assert isinstance(data["findings"], list)
        assert isinstance(data["scanned"], dict)
        # 报告里不能出现任何"删除"入口
        assert not hasattr(report, "clean")
        assert "不会" in report.to_text() or not report.available

    def test_non_windows_reports_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(registry, "_import_winreg", lambda: None)
        report = registry.scan_registry()
        assert report.available is False
        assert report.findings == []
        assert "Windows" in report.to_text()

    def test_finding_helpers(self) -> None:
        finding = registry.RegistryFinding(
            kind="muicache_orphan",
            severity=registry.SEV_LOW,
            hive="HKCU",
            key_path=r"SOFTWARE\X",
            value_name="a.exe.FriendlyAppName",
            detail="d",
        )
        assert finding.location == r"HKCU\SOFTWARE\X"
        assert "a.exe" in finding.describe()
        assert finding.to_dict()["kind"] == "muicache_orphan"


# ===========================================================================
# 新增：MCP registry_scan 工具（只读）
# ===========================================================================
class TestMcpRegistryScanTool:
    def test_tool_listed_and_readonly(self) -> None:
        from pc_cleaner import mcp

        names = [t["name"] for t in mcp._tool_definitions(allow_delete=False)]
        assert "registry_scan" in names
        # 写入类工具默认不暴露，且不存在任何注册表清理工具
        assert "delete" not in names
        assert not any("registry_clean" in n for n in names)

    def test_tool_call_returns_readonly_payload(self, monkeypatch) -> None:
        from pc_cleaner import mcp

        called: dict = {}
        monkeypatch.setattr(
            registry, "scan_registry",
            lambda *a, **k: called.setdefault("yes", registry.RegistryReport()),
        )
        resp = mcp.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "registry_scan", "arguments": {}}}
        )
        payload = resp["result"]["structuredContent"]
        assert payload["read_only"] is True
        assert called.get("yes") is not None

    def test_write_tools_still_allow_delete_guarded(self) -> None:
        from pc_cleaner import mcp

        names = [t["name"] for t in mcp._tool_definitions(allow_delete=True)]
        assert names.count("delete") == 1
        assert "registry_clean" not in names


# ===========================================================================
# --checkup 的注册表小节（只读，且不得因扫描失败而中断）
# ===========================================================================
class TestCheckupRegistrySection:
    def test_checkup_reports_registry_summary(self, monkeypatch, capsys) -> None:
        from pc_cleaner import commands

        monkeypatch.setattr(commands, "scan_all", lambda *a, **k: [])
        monkeypatch.setattr(commands, "_cmd_print_env_adaptation", lambda *a, **k: None)
        monkeypatch.setattr(commands, "_system_drives", lambda: [])
        monkeypatch.setattr(commands, "load_history", lambda: [])
        monkeypatch.setattr(
            registry, "scan_registry",
            lambda *a, **k: registry.RegistryReport(
                findings=[
                    registry.RegistryFinding(
                        kind="uninstall_orphan", severity=registry.SEV_LOW,
                        hive="HKCU", key_path="SOFTWARE\\X", value_name="v",
                        detail="d",
                    )
                ]
            ),
        )
        code = commands._cmd_checkup([], show_risky=False, scan_depth=20,
                                     show_progress=False)
        assert code == 0
        out = capsys.readouterr().out
        assert "注册表垃圾" in out
        assert "不会删除注册表项" in out

    def test_checkup_survives_registry_failure(self, monkeypatch, capsys) -> None:
        from pc_cleaner import commands

        monkeypatch.setattr(commands, "scan_all", lambda *a, **k: [])
        monkeypatch.setattr(commands, "_cmd_print_env_adaptation", lambda *a, **k: None)
        monkeypatch.setattr(commands, "_system_drives", lambda: [])
        monkeypatch.setattr(commands, "load_history", lambda: [])

        def _boom(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(registry, "scan_registry", _boom)
        assert commands._cmd_checkup([], show_risky=False, scan_depth=20,
                                     show_progress=False) == 0
        assert "已跳过" in capsys.readouterr().out
