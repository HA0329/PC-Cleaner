"""v0.9.14「优化使用体验」的回归测试。

覆盖：
- 一键推荐（p）只选 safe + 少量可重建的 moderate 缓存；
- 推荐页脚 / 推荐概览 / 清理结果文案；
- 分类 note 从 rules.json 进入 CategoryResult 与详情页；
- 菜单 D 键切换深度模式并重扫；
- e 键提权重启参数不丢失当前深度状态；
- 推荐流程使用分类级预览，而不是把每个路径都铺满屏幕。
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from pc_cleaner import menu
from pc_cleaner.models import (
    CategoryResult,
    CleanMode,
    Target,
    TargetAction,
    TargetKind,
)


def _mk_target(tmp_path: Path, name: str = "cache", size: int = 1024) -> Target:
    return Target(
        path=tmp_path / name,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="system_temp",
        size=size,
        file_count=1,
    )


def _mk_category(
    tmp_path: Path,
    key: str,
    risk: str = "safe",
    size: int = 1024,
    *,
    all_excluded: bool = False,
    admin_blocked: bool = False,
    note: str = "",
) -> CategoryResult:
    return CategoryResult(
        key=key,
        label=key,
        risk=risk,
        targets=[] if admin_blocked else [_mk_target(tmp_path, key, size)],
        scanned=True,
        all_excluded=all_excluded,
        admin_blocked=admin_blocked,
        note=note,
    )


def _fake_args():
    class Args:
        dry_run = False
        shred = False
        shred_passes = 1
        recycle_fallback = False
        ext = None
        min_size_mb = None
        older_than_days = None
        max_depth = None

    return Args()


def test_recommended_categories_are_conservative(tmp_path: Path) -> None:
    results = [
        _mk_category(tmp_path, "system_temp", "safe"),
        _mk_category(tmp_path, "wechat_cache", "safe"),
        _mk_category(tmp_path, "game_caches", "moderate"),
        _mk_category(tmp_path, "usage_traces", "moderate"),
        _mk_category(tmp_path, "dev_caches", "moderate"),
        _mk_category(tmp_path, "downloads", "risky"),
        _mk_category(tmp_path, "downloads_installers", "moderate", all_excluded=True),
        _mk_category(tmp_path, "system_admin", "moderate", admin_blocked=True),
    ]
    picked = menu.recommended_categories(results)
    assert [r.key for r in picked] == [
        "system_temp",
        "wechat_cache",
        "game_caches",
    ]


def test_footer_advertises_one_key_recommendation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(menu, "get_terminal_width", lambda: 80)
    results = [
        _mk_category(tmp_path, "system_temp", "safe", 1024),
        _mk_category(tmp_path, "game_caches", "moderate", 2048),
        _mk_category(tmp_path, "usage_traces", "moderate", 4096),
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        menu._print_menu_footer(results, 0, [0, 1, 2], False)
    out = buf.getvalue()
    assert "★ p 推荐清理" in out
    assert "推荐一键清理" in out
    assert "不含高风险、下载、开发缓存、使用痕迹" in out


def test_category_note_reaches_result_and_detail(tmp_path: Path, monkeypatch) -> None:
    note = "这是给用户看的说明，不应该只留在 rules.json 里。"
    spec = {
        "key": "test_note",
        "label": "测试分类",
        "risk": "safe",
        "note": note,
        "targets": [
            {
                "type": "glob_files",
                "base": str(tmp_path),
                "pattern": "*.tmp",
                "ext": [".tmp"],
            }
        ],
    }
    (tmp_path / "x.tmp").write_bytes(b"x")
    from pc_cleaner.scanner import scan_spec

    res = scan_spec(spec)
    assert res.note == note

    buf = io.StringIO()
    monkeypatch.setattr(menu, "get_terminal_width", lambda: 80)
    with redirect_stdout(buf):
        menu._print_detail_for_category(res)
    out = buf.getvalue()
    assert "说明:" in out
    assert "不应该只留在 rules.json" in out


def test_result_summary_recycle_vs_permanent() -> None:
    recycle = menu._format_clean_result_summary(
        {"recycled": 100 * 1024 * 1024, "freed": 0, "deleted": 3},
        CleanMode.RECYCLE,
    )
    assert "已移入回收站" in recycle
    assert "清空回收站后才会释放" in recycle
    assert "处理 3 项目标" in recycle

    permanent = menu._format_clean_result_summary(
        {"freed": 50 * 1024 * 1024, "deleted": 2, "failed": 1},
        CleanMode.PERMANENT,
    )
    assert "已释放" in permanent
    assert "失败 1 项" in permanent


def test_relaunch_argv_keeps_current_deep_state() -> None:
    raw = ["--list", "--deep", "--admin", "--max-depth", "10"]
    off = menu._menu_relaunch_argv(raw, deep=False)
    assert "--deep" not in off and "-D" not in off
    assert off[-1] == "--admin"
    on = menu._menu_relaunch_argv(raw, deep=True)
    assert on.count("--deep") == 1
    assert on[-1] == "--admin"


def test_p_flow_uses_recommended_categories_and_compact_preview(
    tmp_path: Path, monkeypatch
) -> None:
    results = [
        _mk_category(tmp_path, "system_temp", "safe", 1024),
        _mk_category(tmp_path, "game_caches", "moderate", 2048),
        _mk_category(tmp_path, "usage_traces", "moderate", 4096),
    ]
    seen: dict = {}

    def fake_flow(selected, **kwargs):
        seen["keys"] = [r.key for r in selected]
        seen["show_preview"] = kwargs.get("show_preview")
        return {"deleted": 0}

    monkeypatch.setattr(menu, "_run_clean_flow", fake_flow)
    monkeypatch.setattr(menu, "_print_env_adaptation", lambda *a, **k: None)
    monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)
    monkeypatch.setattr(menu, "prompt_yes_no", lambda *a, **k: False)
    monkeypatch.setattr("builtins.input", lambda _p="": "p")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = menu._interactive(
            results,
            [],
            CleanMode.RECYCLE,
            _fake_args(),
            {"show_risky": False},
            False,
            scan_depth=20,
            show_progress=False,
            deep=False,
            workers=1,
        )
    out = buf.getvalue()
    assert rc == 0
    assert seen["keys"] == ["system_temp", "game_caches"]
    assert seen["show_preview"] is False
    assert "★ 推荐清理" in out


def test_D_toggles_deep_and_rescans_at_depth_50(tmp_path: Path, monkeypatch) -> None:
    results = [_mk_category(tmp_path, "system_temp", "safe", 1024)]
    seen: dict = {}

    def fake_scan(specs, scan_depth, workers, show_progress):
        seen.update(scan_depth=scan_depth, workers=workers)
        return results, 0

    monkeypatch.setattr(menu, "_scan_for_menu", fake_scan)
    monkeypatch.setattr(menu, "_print_env_adaptation", lambda *a, **k: None)
    monkeypatch.setattr(menu, "recycle_bin_size", lambda: 0)

    # 第一次 D 触发重扫；重扫后输入 q 退出，避免再次进入扫描。
    answers = iter(["D", "q"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = menu._interactive(
            results,
            [],
            CleanMode.RECYCLE,
            _fake_args(),
            {"show_risky": False, "scan_depth": 20},
            False,
            scan_depth=20,
            show_progress=False,
            deep=False,
            workers=1,
        )
    assert rc == 0
    assert seen["scan_depth"] == 50
    assert "已开启深度模式" in buf.getvalue()


def test_clean_next_steps_are_actionable() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        menu._print_clean_next_steps(
            {"skipped_in_use": 2, "failed": 1, "recycled": 1024},
            CleanMode.RECYCLE,
        )
    out = buf.getvalue()
    assert "关闭正在使用这些缓存的程序" in out
    assert "清空回收站" in out
