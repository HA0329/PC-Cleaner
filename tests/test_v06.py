"""v0.6.0 升级功能的单元测试：规则外置 + 高级清理过滤 + 多遍擦除。"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from pc_cleaner.cli import _apply_target_filters, _filters_from_args, _parse_ext_filter
from pc_cleaner.engine import _shred_file
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import get_all_category_specs, validate_rules


# ---------------------------------------------------------------------------
# 规则外置（rules.json）
# ---------------------------------------------------------------------------
def test_rules_json_loads_all_categories():
    specs = {s["key"]: s for s in get_all_category_specs(merge_custom=False)}
    for key in (
        "system_temp",
        "gpu_caches",
        "web_cache",
        "wechat_cache",
        "game_caches",
        "dev_caches",
        "downloads",
        "system_admin",
        "windows_old",
        "dev_purge",
        "browser_privacy",
    ):
        assert key in specs
    # 向后兼容：既有风险/权限元数据仍在
    assert specs["system_temp"]["risk"] == "safe"
    assert specs["downloads"]["risk"] == "risky"
    assert specs["system_admin"]["require_admin"] is True


def test_validate_rules_passes_on_builtin():
    errors = validate_rules(get_all_category_specs(merge_custom=False, deep=True))
    assert errors == []


def test_validate_rules_catches_errors():
    bad = [
        {"key": "ok", "label": "OK", "risk": "safe",
         "targets": [{"type": "clear_dir", "path": "%TEMP%"}]},
        {"key": "dup", "label": "D", "risk": "weird", "targets": []},
        {"key": "dup", "label": "D2", "risk": "safe",
         "targets": [{"type": "nope", "path": "x"}]},
        {"key": "f", "label": "F", "risk": "risky",
         "targets": [{"type": "find_dirs", "bases": []}]},
    ]
    errors = validate_rules(bad)
    assert any("非法 risk" in e for e in errors)
    assert any("重复" in e for e in errors)
    assert any("非法 type" in e for e in errors)
    assert any("缺少 bases" in e for e in errors)


def test_deep_mode_gates_deep_only_targets():
    normal = {s["key"]: s for s in get_all_category_specs(merge_custom=False, deep=False)}
    deep = {s["key"]: s for s in get_all_category_specs(merge_custom=False, deep=True)}

    def _has_deep_only(spec):
        return any(t.get("deep_only") for t in spec.get("targets", []))

    # 默认模式不包含 deep_only 规则
    assert not any(_has_deep_only(s) for s in normal.values())
    # deep 模式包含 deep_only 规则
    assert any(_has_deep_only(s) for s in deep.values())
    # 既有标签仍在（规则外置未丢失）
    labels = [t.get("label") for t in normal["system_admin"]["targets"]]
    assert "Windows 更新缓存" in labels
    assert "预读取(Prefetch)" in labels


# ---------------------------------------------------------------------------
# 高级清理过滤
# ---------------------------------------------------------------------------
def test_parse_ext_filter():
    assert _parse_ext_filter(None) is None
    assert _parse_ext_filter(".log,tmp") == [".log", ".tmp"]
    assert _parse_ext_filter("LOG") == [".log"]
    assert _parse_ext_filter(".log,LOG,.log") == [".log"]


def test_filters_from_args():
    args = SimpleNamespace(
        ext=".log,tmp", min_size_mb=5, older_than_days=30, shred_passes=3
    )
    ext, min_bytes, older, passes = _filters_from_args(args)
    assert ext == [".log", ".tmp"]
    assert min_bytes == 5 * 1024 * 1024
    assert older == 30 * 86400
    assert passes == 3


def _mk_file_target(tmp_path, name, size=10, age_days=0):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return Target(
        path=p, kind=TargetKind.FILE, action=TargetAction.DELETE, category="t", size=size
    )


def test_apply_target_filters_ext(tmp_path):
    t_log = _mk_file_target(tmp_path, "a.log")
    t_tmp = _mk_file_target(tmp_path, "b.tmp")
    t_txt = _mk_file_target(tmp_path, "c.txt")
    out = _apply_target_filters([t_log, t_tmp, t_txt], ext_filter=[".log", ".tmp"])
    assert {t.path.name for t in out} == {"a.log", "b.tmp"}


def test_apply_target_filters_min_size(tmp_path):
    small = _mk_file_target(tmp_path, "small", size=5)
    big = _mk_file_target(tmp_path, "big", size=100)
    out = _apply_target_filters([small, big], min_size_bytes=50)
    assert {t.path.name for t in out} == {"big"}


def test_apply_target_filters_older_than(tmp_path):
    new = _mk_file_target(tmp_path, "new", age_days=1)
    old = _mk_file_target(tmp_path, "old", age_days=30)
    out = _apply_target_filters([new, old], older_than_secs=7 * 86400)
    assert {t.path.name for t in out} == {"old"}


def test_apply_target_filters_keeps_dirs(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    dtarget = Target(
        path=d, kind=TargetKind.DIR, action=TargetAction.CLEAR, category="t", size=1
    )
    ftarget = _mk_file_target(tmp_path, "x.txt")
    # 扩展名过滤只影响文件目标：目录目标始终保留
    out = _apply_target_filters([dtarget, ftarget], ext_filter=[".log"])
    assert len(out) == 1
    assert out[0].kind is TargetKind.DIR


# ---------------------------------------------------------------------------
# 多遍安全擦除
# ---------------------------------------------------------------------------
def test_shred_file_multiple_passes(tmp_path):
    f = tmp_path / "s.txt"
    f.write_bytes(b"A" * 2048)
    _shred_file(f, passes=3)
    assert f.read_bytes() != b"A" * 2048
    assert len(f.read_bytes()) == 2048
