"""v0.7.0 修复项的单元测试。

覆盖：白名单与系统盘解耦（%WINDIR% 动态解析）、目录大小缓存（去重后不再
重复遍历）、扫描错误处理细化（权限错误/未知异常）、空目录边界、回收站
$I 元数据容错、规则热重载辅助函数、符号链接跳过、--json 目标预览。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pc_cleaner.rules as rules_mod
import pc_cleaner.scanner as scanner_mod
from pc_cleaner.engine import _parse_recycle_info
from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind
from pc_cleaner.rules import (
    get_enabled_category_specs,
    is_within_clear_root,
)
from pc_cleaner.scanner import _dir_size, _walk_dir, make_protect_check, scan_all, scan_spec


# ---------------------------------------------------------------------------
# 修复 #1：ALLOWED_CLEAR_ROOTS 与系统盘解耦（%WINDIR% 动态解析）
# ---------------------------------------------------------------------------
def test_allowlist_resolves_windir_env(tmp_path, monkeypatch):
    win = tmp_path / "Windows"
    win.mkdir()
    monkeypatch.setenv("WINDIR", str(win))
    # 白名单根（%WINDIR%\Temp 等）在系统盘不是 C: 时也能匹配
    assert is_within_clear_root(win / "Temp") is True
    assert is_within_clear_root(win / "Temp" / "sub" / "x.tmp") is True
    assert is_within_clear_root(win / "Prefetch") is True
    assert is_within_clear_root(win / "SoftwareDistribution" / "Download" / "a") is True
    # 白名单之外不匹配
    assert is_within_clear_root(win / "System32") is False
    assert is_within_clear_root(win) is False
    assert is_within_clear_root(tmp_path / "elsewhere") is False


def test_allowlist_accepts_absolute_paths(tmp_path, monkeypatch):
    """旧式（直接写绝对路径）条目 / 测试注入仍然兼容。"""
    root = tmp_path / "allow"
    root.mkdir()
    monkeypatch.setattr(rules_mod, "ALLOWED_CLEAR_ROOTS", {str(root)})
    assert is_within_clear_root(root) is True
    assert is_within_clear_root(root / "sub") is True
    assert is_within_clear_root(tmp_path / "other") is False


def test_allowlist_real_windir():
    """真实系统上 %WINDIR% 下的白名单目录应命中（字符串匹配，无需存在）。"""
    win = os.environ.get("WINDIR") or r"C:\Windows"
    assert is_within_clear_root(Path(win) / "Temp") is True


# ---------------------------------------------------------------------------
# 修复 #4：目录大小缓存（同一扫描内同一目录只遍历一次）
# ---------------------------------------------------------------------------
def test_dir_size_memo_returns_same_result(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "f1").write_bytes(b"x" * 10)
    memo: dict = {}
    s1, c1 = _dir_size(d, make_protect_check(), memo)
    s2, c2 = _dir_size(d, make_protect_check(), memo)
    assert (s1, c1) == (10, 1)
    assert (s2, c2) == (10, 1)
    assert len(memo) == 1  # 只缓存了一条


def test_dir_size_memo_skips_rewalk(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "f1").write_bytes(b"x" * 10)
    memo: dict = {}
    _dir_size(d, make_protect_check(), memo)
    # 修改目录内容后，同一 memo 命中缓存（扫描期间内容视为不变）
    (d / "f2").write_bytes(b"y" * 20)
    s3, c3 = _dir_size(d, make_protect_check(), memo)
    assert (s3, c3) == (10, 1)
    # 新 memo 重新遍历得到最新值
    s4, c4 = _dir_size(d, make_protect_check(), {})
    assert (s4, c4) == (30, 2)


def test_scan_all_shared_memo_counts_once(tmp_path, monkeypatch):
    """同一目录出现在多个分类时，_dir_size 只真正遍历一次。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "f").write_text("x" * 50)
    spec_a = {
        "key": "a", "label": "A",
        "targets": [{"type": "clear_dir", "path": str(cache), "label": "c"}],
    }
    spec_b = {
        "key": "b", "label": "B",
        "targets": [{"type": "delete_dir", "path": str(cache), "label": "c"}],
    }
    calls = {"n": 0}
    orig = scanner_mod._dir_size

    def counting(path, is_protected, memo=None):
        # 命中缓存的不算真正遍历（与 _dir_size 的 memo 语义一致）
        if memo is not None:
            key = scanner_mod.normalize(path)
            if key in memo:
                return memo[key]
        calls["n"] += 1
        return orig(path, is_protected, memo)

    monkeypatch.setattr(scanner_mod, "_dir_size", counting)
    results = scan_all([spec_a, spec_b])
    assert len(results) == 2
    assert calls["n"] == 1  # 去重后同一目录只统计一次体积


# ---------------------------------------------------------------------------
# 修复 #9：扫描错误处理细化（边界条件）
# ---------------------------------------------------------------------------
def test_scan_empty_dir_no_target(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    spec = {
        "key": "t", "label": "T",
        "targets": [{"type": "clear_dir", "path": str(empty), "label": "空目录"}],
    }
    res = scan_spec(spec)
    assert res.scanned is True
    assert res.targets == []


def test_scan_spec_permission_error_skips(tmp_path, monkeypatch):
    def boom(path, is_protected, memo=None):
        raise PermissionError("拒绝访问（模拟）")

    monkeypatch.setattr(scanner_mod, "_dir_size", boom)
    cache = tmp_path / "cache"
    cache.mkdir()
    spec = {
        "key": "t", "label": "T",
        "targets": [{"type": "clear_dir", "path": str(cache), "label": "c"}],
    }
    res = scan_spec(spec)
    assert res.scanned is True
    assert res.skipped == 1
    assert res.targets == []


def test_scan_spec_unexpected_error_warns(tmp_path, monkeypatch, capsys):
    def boom(path, is_protected, memo=None):
        raise RuntimeError("未知 bug")

    monkeypatch.setattr(scanner_mod, "_dir_size", boom)
    cache = tmp_path / "cache"
    cache.mkdir()
    spec = {
        "key": "t", "label": "T",
        "targets": [{"type": "clear_dir", "path": str(cache), "label": "c"}],
    }
    res = scan_spec(spec)
    assert res.scanned is True
    assert res.skipped == 1
    err = capsys.readouterr().err
    assert "扫描警告" in err  # 未知异常不再静默吞掉


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 符号链接需要开发者模式/管理员")
def test_walk_skips_symlink(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x" * 10)
    link = tmp_path / "link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("无法创建符号链接")
    names = [p.name for p, _ in _walk_dir(tmp_path, make_protect_check())]
    assert "real.txt" in names
    assert "link.txt" not in names


# ---------------------------------------------------------------------------
# 修复 #5：回收站 $I 元数据容错
# ---------------------------------------------------------------------------
def test_parse_recycle_info_garbage(tmp_path):
    too_short = tmp_path / "$Ishort"
    too_short.write_bytes(b"\x02\x00")
    assert _parse_recycle_info(too_short) is None

    no_path = tmp_path / "$Iempty"
    no_path.write_bytes(b"\x02\x00\x00\x00" + b"\x00" * 20)
    assert _parse_recycle_info(no_path) is None

    broken = tmp_path / "$Ibroken"
    broken.write_bytes(b"\x02\x00\x00\x00" + b"\x00" * 24 + b"\xff\xfe")  # 非法 UTF-16
    assert _parse_recycle_info(broken) is None


# ---------------------------------------------------------------------------
# 热重载辅助：get_enabled_category_specs
# ---------------------------------------------------------------------------
def test_get_enabled_category_specs_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    cfg = {"enabled_categories": ["system_temp", "gpu_caches"]}
    specs = get_enabled_category_specs(cfg)
    assert {s["key"] for s in specs} == {"system_temp", "gpu_caches"}

    # 空配置 / 未限制 → 全部内置分类
    specs_all = get_enabled_category_specs({})
    keys = {s["key"] for s in specs_all}
    assert "system_temp" in keys and "web_cache" in keys and "dev_purge" in keys


# ---------------------------------------------------------------------------
# --json 目标预览（--dry-run 详细输出）
# ---------------------------------------------------------------------------
def test_json_target_preview(tmp_path):
    from pc_cleaner.cli import _json_target_preview

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "f").write_text("x" * 50)
    res = CategoryResult(key="t", label="T", risk="safe")
    res.targets.append(
        Target(path=cache, kind=TargetKind.DIR, action=TargetAction.CLEAR,
               category="t", size=50)
    )
    args = SimpleNamespace(ext=None, min_size_mb=None, older_than_days=None,
                           shred_passes=1)
    preview = _json_target_preview([res], ["t"], args)
    assert len(preview) == 1
    assert preview[0]["path"] == str(cache)
    assert preview[0]["kind"] == "dir"
    assert preview[0]["action"] == "clear"
    assert preview[0]["size_bytes"] == 50
