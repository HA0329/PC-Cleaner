"""v0.9.13「改革清理收益」的回归测试。

覆盖：

1. Office / Adobe 缓存不再藏在 ``--deep`` 后面；
2. 缓存分类的 ``partial_if_in_use`` 已进入规则库，且危险分类未误开；
3. ``scan_spec`` 把分类级 ``partial_if_in_use`` 传到每个 Target；
4. CLEAR 目标被占用时，开启 partial 只清未锁定子项；关闭时仍整目标跳过；
5. 所有子项都锁定/失败时，计入 ``skipped_in_use`` 而不是假装成功；
6. 规则校验保持 0 error / 0 warning。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pc_cleaner import engine
from pc_cleaner.engine import CleanMode, delete_targets
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import get_all_category_specs, validate_rules
from pc_cleaner.scanner import scan_spec


def test_office_and_adobe_caches_are_default() -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    by_key = {s["key"]: s for s in specs}
    for key in ("office_caches", "media_caches", "wechat_cache"):
        assert all(not t.get("deep_only") for t in by_key[key]["targets"]), key
    assert sum(1 for s in specs for t in s["targets"] if t.get("deep_only")) == 36
    assert sum(len(s["targets"]) for s in specs) == 300


def test_partial_flag_on_cache_categories_only() -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    by_key = {s["key"]: s for s in specs}
    for key in ("web_cache", "office_caches", "media_caches", "system_temp"):
        assert by_key[key].get("partial_if_in_use") is True, key
    # 微信运行时可能正在使用小程序包，保持整类跳过
    assert not by_key["wechat_cache"].get("partial_if_in_use")
    # npm _npx / 系统更新缓存等危险目标不能开 partial
    assert not by_key["dev_caches"].get("partial_if_in_use")
    assert not by_key["system_admin"].get("partial_if_in_use")


def test_scan_spec_propagates_partial(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"x" * 1024)
    result = scan_spec(
        {
            "key": "demo_partial",
            "label": "demo",
            "risk": "safe",
            "partial_if_in_use": True,
            "targets": [
                {
                    "type": "clear_dir",
                    "path": str(cache),
                    "label": "cache",
                    "skip_if_in_use": True,
                }
            ],
        },
        is_protected=lambda _p: False,
    )
    assert result.targets
    assert all(t.partial_if_in_use for t in result.targets)


def _make_target(cache: Path, *, partial: bool) -> Target:
    return Target(
        path=cache,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="demo",
        size=1024,
        skip_if_in_use=True,
        partial_if_in_use=partial,
    )


def test_clear_in_use_partial_removes_unlocked_files(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = cache / "a.bin"
    payload.write_bytes(b"x" * 1024)

    monkeypatch.setattr(engine, "_in_use_reason", lambda _p: "proc")
    deleted: list[Path] = []

    def fake_delete(path, mode, is_dir, **kwargs):
        deleted.append(path)
        path.unlink()

    monkeypatch.setattr(engine, "_delete_path", fake_delete)
    stats = delete_targets([_make_target(cache, partial=True)], CleanMode.PERMANENT)

    assert stats["deleted"] == 1
    assert stats["skipped_in_use"] == 0
    assert stats["freed"] == 1024
    assert not payload.exists()


def test_clear_in_use_whole_skip_when_disabled(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = cache / "a.bin"
    payload.write_bytes(b"x" * 1024)

    monkeypatch.setattr(engine, "_in_use_reason", lambda _p: "proc")
    stats = delete_targets([_make_target(cache, partial=False)], CleanMode.PERMANENT)

    assert stats["deleted"] == 0
    assert stats["skipped_in_use"] == 1
    assert payload.exists()


def test_clear_in_use_counts_skipped_if_nothing_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = cache / "a.bin"
    payload.write_bytes(b"x" * 1024)

    monkeypatch.setattr(engine, "_in_use_reason", lambda _p: "proc")

    def locked_delete(path, mode, is_dir, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(engine, "_delete_path", locked_delete)
    stats = delete_targets([_make_target(cache, partial=True)], CleanMode.PERMANENT)

    assert stats["deleted"] == 0
    assert stats["failed"] == 0
    assert stats["skipped_in_use"] == 1
    assert payload.exists()


def test_rules_still_validate_clean() -> None:
    report = validate_rules()
    assert list(report) == []
    assert list(report.warnings) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
