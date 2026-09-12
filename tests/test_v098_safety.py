"""v0.9.8 安全 / 兼容性 / 交互修复的回归测试。

覆盖三批改动：

* **P0 安全**
  - ``engine._guard_path`` 不再对白名单清空根之下的子项短路掉 ``is_protected``；
  - ``engine._clear_dir_content`` 的受保护子项跳过分支真正生效；
  - ``restore_paths`` 用规范化路径查找，兼容 8.3 短名 / 长名混用。
* **P1 规则兼容**
  - ``glob_dirs`` 支持 ``bases``、``clear_dir`` 支持 ``paths``（多候选布局）；
  - ``--validate-rules`` 接受新写法，``--audit-rules`` 输出本机存活性审计。
* **P2 交互**
  - 汇总表边框 / 表头 / 数据行显示宽度完全一致；
  - 菜单 ``x`` 切换高风险后编号与 results 保持一致（不再 IndexError）；
  - ``prompt_yes_no(require_typed=True)`` 不接受单个 ``y``。

全部测试都不删除真实文件（需要"删除"的地方一律用 monkeypatch 打桩）。
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from pc_cleaner import engine, menu, rules, scanner
from pc_cleaner.console import display_width
from pc_cleaner.models import (
    CategoryResult,
    CleanMode,
    Target,
    TargetAction,
    TargetKind,
)
from pc_cleaner.ui import prompt_yes_no


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------
def _mk_category(key: str, label: str, risk: str, size: int) -> CategoryResult:
    t = Target(
        path=Path(rf"C:\tmp\{key}"),
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category=key,
        size=size,
        file_count=3,
        label=label,
    )
    return CategoryResult(key=key, label=label, risk=risk, targets=[t], scanned=True)


# ===========================================================================
# P0-1：白名单清空根内的受保护名必须被拒绝
# ===========================================================================
class TestGuardProtectsInsideClearRoot:
    """``_guard_path`` 对 clear root 之下的子项也必须跑 ``is_protected``。

    v0.9.10：本工具只适配 Windows，因此这里直接用字面 ``C:\\Windows\\...`` 路径
    （``%WINDIR%`` 展开后即该目录），不再为 POSIX 造等价路径。
    """

    CASES_PROTECTED = [
        r"C:\Windows\Temp\xwechat_files",
        r"C:\Windows\Temp\.git",
        r"C:\Windows\Temp\.venv",
        r"C:\Windows\Prefetch\weixinshuju",
        r"C:\Windows\System32\LogFiles\weixinshuju",
    ]

    CASES_ALLOWED = [
        r"C:\Windows\Temp\someapp.tmp",
        r"C:\Windows\Temp\subdir",
        r"C:\Windows\Prefetch\FOO.EXE-1234.pf",
    ]

    @pytest.mark.parametrize("path", CASES_PROTECTED)
    def test_protected_name_inside_clear_root_is_refused(self, path: str) -> None:
        is_protected = scanner.make_protect_check()
        with pytest.raises(PermissionError):
            engine._guard_path(Path(path), is_protected, TargetAction.DELETE)

    @pytest.mark.parametrize("path", CASES_ALLOWED)
    def test_ordinary_cache_content_still_allowed(self, path: str) -> None:
        """清空功能不能被削弱：普通缓存内容必须照旧放行。"""
        is_protected = scanner.make_protect_check()
        engine._guard_path(Path(path), is_protected, TargetAction.DELETE)

    def test_clear_action_on_clear_root_still_allowed(self) -> None:
        is_protected = scanner.make_protect_check()
        engine._guard_path(Path(r"C:\Windows\Temp"), is_protected, TargetAction.CLEAR)

    def test_delete_of_clear_root_itself_refused(self) -> None:
        is_protected = scanner.make_protect_check()
        with pytest.raises(PermissionError):
            engine._guard_path(Path(r"C:\Windows\Temp"), is_protected,
                               TargetAction.DELETE)

    def test_guard_calls_is_protected_for_clear_root_children(self) -> None:
        """回归：此前第 4 步无条件 return，is_protected 对子项根本不会被调用。"""
        calls: list[str] = []

        def spy(path) -> bool:
            calls.append(str(path))
            return False

        engine._guard_path(Path(r"C:\Windows\Temp\anything.tmp"), spy,
                           TargetAction.DELETE)
        assert calls, "is_protected 未被调用 —— 白名单短路回归了"


class TestClearDirContentSkipsProtected:
    """``_clear_dir_content`` 的受保护子项必须真的被跳过。"""

    def test_protected_child_is_skipped(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "Temp"
        root.mkdir()
        keep = root / "keep.tmp"
        keep.write_text("x", encoding="utf-8")
        protected = root / "secret"
        protected.mkdir()
        (protected / "data.txt").write_text("y", encoding="utf-8")

        def is_protected(p) -> bool:
            return Path(p).name == "secret"

        monkeypatch.setattr(engine, "_delete_path",
                            lambda *a, **k: None)
        deleted, failed = engine._clear_dir_content(
            root, CleanMode.PERMANENT, lambda _p: None,
            recycle_fallback=False, is_protected=is_protected,
        )
        # 只有 keep.tmp 被"删除"，secret 被跳过
        assert deleted == 1, f"受保护子项未被跳过（deleted={deleted}）"
        assert failed == 0


# ===========================================================================
# P0-2：undo / restore 的短名 · 长名兼容
# ===========================================================================
class TestRestorePathCanonicalization:
    """``restore_paths`` 必须能处理 8.3 短名与长名混用。"""

    def test_short_and_long_forms_resolve_equal(self, tmp_path: Path) -> None:
        long_form = str(tmp_path.resolve()).lower()
        assert str(Path(long_form).resolve(strict=False)).lower() == long_form

    def test_lookup_by_canonical_key_hits_long_record(self, tmp_path: Path) -> None:
        """模拟「历史存短名、回收站存长名」：规范化后应命中。"""
        f = tmp_path / "sub" / "victim.tmp"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        orig_long = str(f.resolve())
        # 历史记录里的写法（这里用同一路径的另一种写法模拟）
        hist_spelling = str(tmp_path / "sub" / "victim.tmp")

        def canon(p) -> str:
            return str(Path(p).resolve(strict=False)).lower()

        by_orig: dict[str, dict] = {}
        entries = [{"original": orig_long, "size": 1,
                    "data": Path("nope"), "info": Path("nope")}]
        for e in entries:
            raw = str(e["original"]).lower()
            by_orig.setdefault(raw, e)
            c = canon(e["original"])
            if c != raw:
                by_orig.setdefault(c, e)

        # 关键：删除目标后仍能用历史写法查到记录（父目录还在）
        f.unlink()
        assert canon(hist_spelling) in by_orig

    def test_restore_not_found_message_is_not_misleading(self, monkeypatch) -> None:
        """找不到记录时的文案不应断言"已被手动删除"（会诱导用户清空回收站）。"""
        monkeypatch.setattr(engine, "recycle_entries", lambda drives=None: [])
        res = engine.restore_paths([r"C:\definitely\not\in\recycle\bin\x.tmp"])
        assert res["restored"] == []
        joined = " ".join(res["skipped"])
        assert "已被手动删除" not in joined, f"文案仍有误导性: {joined}"
        assert "没有对应记录" in joined


# ===========================================================================
# P1：多候选路径（兼容不同版本的目录布局）
# ===========================================================================
class TestMultiCandidatePaths:
    def test_clear_dir_supports_paths_list(self, tmp_path: Path) -> None:
        a = tmp_path / "old_layout"
        b = tmp_path / "new_layout"
        b.mkdir()
        (b / "cache.bin").write_bytes(b"0" * 4096)

        spec = {"type": "clear_dir",
                "paths": [str(a), str(b)],   # a 不存在，b 存在
                "label": "x"}
        is_protected = scanner.make_protect_check()
        targets = scanner._scan_clear_dir(spec, "cat", is_protected)
        assert len(targets) == 1
        assert targets[0].path == b
        assert targets[0].size > 0

    def test_clear_dir_both_layouts_present(self, tmp_path: Path) -> None:
        a = tmp_path / "old"
        b = tmp_path / "new"
        for d in (a, b):
            d.mkdir()
            (d / "f.bin").write_bytes(b"0" * 1024)
        spec = {"type": "clear_dir", "paths": [str(a), str(b)], "label": "x"}
        targets = scanner._scan_clear_dir(spec, "cat", scanner.make_protect_check())
        assert {t.path for t in targets} == {a, b}

    def test_glob_dirs_supports_bases_list(self, tmp_path: Path) -> None:
        old = tmp_path / "old"
        new = tmp_path / "new"
        new.mkdir()
        want = new / "cache"
        want.mkdir()
        (want / "f.bin").write_bytes(b"0" * 2048)
        old.mkdir()  # 老布局存在但没有 web 子目录

        spec = {"type": "glob_dirs", "bases": [str(old), str(new)],
                "pattern": "cache", "action": "clear", "label": "x"}
        targets = scanner._scan_glob_dirs(spec, "cat",
                                         scanner.make_protect_check())
        assert [t.path for t in targets] == [want]

    def test_glob_dirs_dedupes_same_dir_from_two_bases(self, tmp_path: Path) -> None:
        """两个候选 base 指向同一物理目录时只能产出一个目标。"""
        base = tmp_path / "b"
        target = base / "cache"
        target.mkdir(parents=True)
        (target / "f.bin").write_bytes(b"0" * 1024)
        spec = {"type": "glob_dirs", "bases": [str(base), str(base) + "\\"],
                "pattern": "cache", "action": "clear", "label": "x"}
        targets = scanner._scan_glob_dirs(spec, "cat",
                                         scanner.make_protect_check())
        assert len(targets) == 1


class TestRulesValidationAcceptsNewKeys:
    def test_paths_satisfies_clear_dir(self) -> None:
        specs = [{"key": "k", "label": "L", "risk": "safe",
                  "targets": [{"type": "clear_dir", "paths": ["%TEMP%/x"],
                               "label": "x"}]}]
        report = rules.validate_rules(specs)
        assert not list(report), f"paths 写法被判为缺 path: {list(report)}"

    def test_bases_satisfies_glob_dirs(self) -> None:
        specs = [{"key": "k", "label": "L", "risk": "safe",
                  "targets": [{"type": "glob_dirs", "bases": ["%TEMP%"],
                               "pattern": "x", "action": "clear",
                               "label": "x"}]}]
        report = rules.validate_rules(specs)
        assert not list(report), f"bases 写法被判为缺 base: {list(report)}"

    def test_still_rejects_missing_path_entirely(self) -> None:
        specs = [{"key": "k", "label": "L", "risk": "safe",
                  "targets": [{"type": "clear_dir", "label": "x"}]}]
        report = rules.validate_rules(specs)
        assert list(report), "既没有 path 也没有 paths 应当报错"

    def test_audit_local_is_opt_in(self) -> None:
        """默认不做存活性审计（否则 100+ 条"没装该软件"会淹没真正的问题）。"""
        specs = [{"key": "k", "label": "L", "risk": "safe",
                  "targets": [{"type": "clear_dir",
                               "path": r"C:\definitely\not\here",
                               "label": "x"}]}]
        assert not rules.validate_rules(specs).warnings
        assert rules.validate_rules(specs, audit_local=True).warnings


# ===========================================================================
# P2：交互修复
# ===========================================================================
class TestSummaryTableGeometry:
    """边框 / 表头 / 数据行 / 合计行显示宽度必须完全一致。"""

    def _render(self, results, selectable) -> list[str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            menu._print_summary_table(results, selectable=selectable)
        return [ln for ln in buf.getvalue().splitlines() if ln.strip()]

    @pytest.mark.parametrize("width", [80, 100, 140])
    def test_all_lines_same_display_width(self, width: int, monkeypatch) -> None:
        monkeypatch.setattr(menu, "get_terminal_width", lambda: width)
        results = [
            _mk_category("system_temp", "系统临时文件", "safe", 26_620_470),
            _mk_category("web_cache", "浏览器/网页缓存", "safe", 39_680_000),
            _mk_category("hibernation", "休眠文件(高风险)", "risky", 9_000_000),
        ]
        lines = self._render(results, [0, 1, 2])
        widths = {display_width(ln) for ln in lines}
        assert len(widths) == 1, f"表格行宽不一致: {sorted(widths)}"

    @pytest.mark.parametrize("width", [80, 100, 140])
    def test_table_fits_terminal(self, width: int, monkeypatch) -> None:
        monkeypatch.setattr(menu, "get_terminal_width", lambda: width)
        results = [_mk_category("system_temp", "系统临时文件", "safe", 1_000_000)]
        lines = self._render(results, [0])
        assert max(display_width(ln) for ln in lines) <= width


class TestMenuFooterFitsTerminal:
    """状态栏 / 图例 / 操作说明在 80 列窗口里都不能折行。"""

    def _render_footer(self, width: int, monkeypatch) -> list[str]:
        monkeypatch.setattr(menu, "get_terminal_width", lambda: width)
        results = [
            _mk_category("dev_caches", "开发工具缓存", "moderate", 358_340_000),
            _mk_category("web_cache", "浏览器/网页缓存", "safe", 39_680_000),
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            menu._print_summary_table(results, selectable=[0, 1])
            print()
            menu._print_disk_free(results)
            menu._print_menu_status(CleanMode.RECYCLE, False, 20, 4, False,
                                    459_410_000)
            menu._print_menu_footer(results, 459_410_000, [0, 1], False)
        return [ln for ln in buf.getvalue().splitlines() if ln.strip()]

    @pytest.mark.parametrize("width", [80, 120])
    def test_no_line_exceeds_terminal(self, width: int, monkeypatch) -> None:
        lines = self._render_footer(width, monkeypatch)
        over = [ln for ln in lines if display_width(ln) > width]
        assert not over, f"以下行超出 {width} 列: {over}"

    def test_status_bar_wraps_on_narrow_terminal(self, monkeypatch) -> None:
        """80 列时状态栏应折成两行，而不是被终端自己折行撕碎。

        v0.9.10 修正：原断言假设"折成一行的那种写法一定超过 80 列"，但实测该文本
        恰好是 79~80 列（卡在边界），于是**不折行才是正确行为**，测试却要求折行 ——
        在 Windows 上是"侥幸过"（渲染文本略长），到 Linux 上直接翻车。
        现在改为按**真实渲染结果**判定，不再依赖对前提的猜测。
        """
        lines = self._render_footer(80, monkeypatch)
        assert all(display_width(ln) <= 80 for ln in lines), "有行超出 80 列"
        status = [ln for ln in lines if "模式:" in ln or "深度:" in ln]
        assert 1 <= len(status) <= 2, f"状态栏行数异常: {status}"
        # 关键行为：状态栏要么一行放得下，要么被拆成"模式/风险"与"深度/线程/回收站"两行，
        # 不允许出现"半截被终端自己折行"的第三种形态。
        if len(status) == 2:
            assert "模式:" in status[0] and "深度:" not in status[0]
            assert "深度:" in status[1]

    def test_status_bar_wraps_when_content_is_too_long(self, monkeypatch) -> None:
        """内容确实超宽时必须主动折成两行（而不是交给终端折行）。"""
        monkeypatch.setattr(menu, "get_terminal_width", lambda: 80)
        buf = io.StringIO()
        with redirect_stdout(buf):
            # 超长回收站体积 → 必超 80 列
            menu._print_menu_status(CleanMode.RECYCLE, True, 20, 4, True,
                                    999_999_999_999_999)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2, f"超宽状态栏未折行: {lines}"
        assert all(display_width(ln) <= 80 for ln in lines), lines

    def test_status_bar_single_line_on_wide_terminal(self, monkeypatch) -> None:
        lines = self._render_footer(120, monkeypatch)
        status = [ln for ln in lines if "模式:" in ln]
        assert len(status) == 1
        assert "线程:" in status[0]

class TestMenuXToggleKeepsIndicesConsistent:
    """按 x 切换高风险后，屏幕编号必须仍然指向同一个分类。"""

    def _drive(self, seq, show_risky: bool = True, monkeypatch=None):
        """驱动 _interactive；删除流程被打桩，绝不真删。"""
        results = [
            _mk_category("system_temp", "系统临时文件", "safe", 1000),
            _mk_category("hibernation", "休眠文件(高风险)", "risky", 9000),
        ]
        seen: list[str] = []

        def fake_flow(selected, **_kw):
            seen.append("CLEAN:" + ",".join(r.label for r in selected))
            return {"deleted": 0}

        it = iter(seq)

        def fake_input(_prompt: str = "") -> str:
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        monkeypatch.setattr(menu, "_run_clean_flow", fake_flow)
        monkeypatch.setattr("builtins.input", fake_input)

        class Args:
            dry_run = False
            recycle_fallback = False
            shred = False
            ext = None
            min_size_mb = None
            older_than_days = None

        buf = io.StringIO()
        with redirect_stdout(buf):
            menu._interactive(
                results, specs=[], mode=CleanMode.RECYCLE, args=Args(),
                cfg={"show_risky": show_risky}, show_risky=show_risky,
                sort_by="size_desc", scan_depth=20, show_progress=False,
                deep=False, workers=4,
            )
        return seen, buf.getvalue()

    def test_x_then_out_of_range_number_does_not_crash(self, monkeypatch) -> None:
        """回归：旧代码按 x 隐藏高风险后选编号 2 → IndexError。"""
        monkeypatch.setattr(menu, "scan_all", lambda *a, **k: [
            _mk_category("system_temp", "系统临时文件", "safe", 1000),
            _mk_category("hibernation", "休眠文件(高风险)", "risky", 9000),
        ])
        monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)
        # 不应抛异常
        self._drive(["x", "2", "n", "n"], show_risky=True, monkeypatch=monkeypatch)

    def test_selection_matches_displayed_category(self, monkeypatch) -> None:
        monkeypatch.setattr(menu, "scan_all", lambda *a, **k: [
            _mk_category("system_temp", "系统临时文件", "safe", 1000),
            _mk_category("hibernation", "休眠文件(高风险)", "risky", 9000),
        ])
        monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)
        seen, _ = self._drive(["1", "n", "n"], monkeypatch=monkeypatch)
        assert seen == ["CLEAN:系统临时文件"]

    def test_h_key_shows_help_not_invalid_input(self, monkeypatch) -> None:
        monkeypatch.setattr(menu, "scan_all", lambda *a, **k: [
            _mk_category("system_temp", "系统临时文件", "safe", 1000),
        ])
        monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)
        _, out = self._drive(["h", "q"], monkeypatch=monkeypatch)
        assert "无效输入" not in out
        assert "重新扫描" in out and "从回收站恢复" in out

    def test_status_bar_shows_real_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(menu, "scan_all", lambda *a, **k: [
            _mk_category("system_temp", "系统临时文件", "safe", 1000),
        ])
        monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)
        results = [_mk_category("system_temp", "系统临时文件", "safe", 1000)]

        it = iter(["q"])

        def fake_input(_p: str = "") -> str:
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        class Args:
            dry_run = False
            recycle_fallback = False
            shred = False
            ext = None
            min_size_mb = None
            older_than_days = None

        monkeypatch.setattr("builtins.input", fake_input)
        buf = io.StringIO()
        with redirect_stdout(buf):
            menu._interactive(results, specs=[], mode=CleanMode.PERMANENT,
                              args=Args(), cfg={"show_risky": False},
                              show_risky=False, sort_by="size_desc",
                              scan_depth=20, show_progress=False,
                              deep=False, workers=4)
        out = buf.getvalue()
        assert "永久删除" in out, "状态栏没有反映真实的永久删除模式"
        assert "图例:" in out, "缺少风险图例（风险此前只靠颜色表达）"


class TestPromptYesNoRequireTyped:
    def test_single_y_is_rejected_when_typed_required(self, monkeypatch) -> None:
        monkeypatch.setattr("builtins.input", lambda _p="": "y")
        assert prompt_yes_no("危险操作？", default=False, require_typed=True) is False

    def test_full_yes_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setattr("builtins.input", lambda _p="": "yes")
        assert prompt_yes_no("危险操作？", default=False, require_typed=True) is True

    def test_enter_is_rejected_when_typed_required(self, monkeypatch) -> None:
        monkeypatch.setattr("builtins.input", lambda _p="": "")
        assert prompt_yes_no("危险操作？", default=False, require_typed=True) is False

    def test_default_behaviour_unchanged(self, monkeypatch) -> None:
        """非危险场景保持原语义：回车取默认值、y 即确认。"""
        monkeypatch.setattr("builtins.input", lambda _p="": "")
        assert prompt_yes_no("普通确认？", default=True) is True
        monkeypatch.setattr("builtins.input", lambda _p="": "y")
        assert prompt_yes_no("普通确认？", default=False) is True


class TestScanKeyboardInterrupt:
    """扫描中的 Ctrl+C 必须返回 130 并给出中文提示，而不是抛 traceback。"""

    def test_menu_scan_helper_returns_interrupted(self, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(menu, "scan_all", boom)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fresh, code = menu._scan_for_menu([], show_progress=False,
                                              scan_depth=20, workers=4)
        assert fresh is None
        assert code == 130
        assert "已取消扫描" in buf.getvalue()
