"""升级版新增行为的单元测试。"""

from __future__ import annotations

import os
import sys
import time

import pytest

from pc_cleaner.config import DEFAULTS, load_config
from pc_cleaner.console import display_width, pad_cjk
from pc_cleaner.engine import CleanMode, delete_targets
from pc_cleaner.models import Target, TargetAction, TargetKind, format_size
from pc_cleaner.scanner import (
    recycle_bin_size,
    scan_all,
    scan_spec,
)

from pc_cleaner.cli import _parse_selection

# ---------------------------------------------------------------------------
# models / console
# ---------------------------------------------------------------------------


def test_format_size_pb():
    assert format_size(1024**5) == "1.00 PB"


def test_format_size_negative():
    assert format_size(-1) == "?"


def test_console_cjk_width():
    assert display_width("系统") == 4
    assert display_width("abc") == 3
    assert pad_cjk("系统", 10) == "系统" + " " * 6


def test_config_new_defaults():
    for key in ("recycle_error_fallback", "enabled_categories", "preview_lines"):
        assert key in DEFAULTS
    cfg = load_config()
    assert cfg["recycle_error_fallback"] is False


def test_parse_selection_fullwidth():
    assert _parse_selection("1，3", 5) == {1, 3}
    assert _parse_selection("all", 5) == "all"
    assert _parse_selection("0", 5) == "none"


# ---------------------------------------------------------------------------
# scanner 升级
# ---------------------------------------------------------------------------


def test_scan_all_returns_list_and_dedupes(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "f").write_text("x" * 50)
    spec_a = {
        "key": "a",
        "label": "A",
        "targets": [{"type": "clear_dir", "path": str(tmp_path / "cache"), "label": "c"}],
    }
    spec_b = {
        "key": "b",
        "label": "B",
        "targets": [{"type": "clear_dir", "path": str(tmp_path / "cache"), "label": "c"}],
    }
    results = scan_all([spec_a, spec_b])
    assert isinstance(results, list)
    assert len(results) == 2
    total = sum(len(r.targets) for r in results)
    assert total == 1  # 全局去重：同一路径只保留一次


def test_glob_files_filters(tmp_path):
    big_old = tmp_path / "big_old.tmp"
    big_old.write_bytes(b"x" * (2 * 1024 * 1024))
    old = time.time() - 40 * 86400
    os.utime(big_old, (old, old))

    big_new = tmp_path / "big_new.tmp"
    big_new.write_bytes(b"x" * (2 * 1024 * 1024))  # 新文件：年龄不达标

    small_old = tmp_path / "small_old.tmp"
    small_old.write_bytes(b"x" * 10)  # 太小：体积不达标
    os.utime(small_old, (old, old))

    spec = {
        "key": "test",
        "label": "测试",
        "targets": [
            {
                "type": "glob_files",
                "base": str(tmp_path),
                "pattern": "*.tmp",
                "min_size_mb": 1,
                "older_than_days": 30,
                "label": "旧大文件",
            }
        ],
    }
    res = scan_spec(spec)
    names = {t.path.name for t in res.targets}
    assert names == {"big_old.tmp"}


def test_find_dirs_action_clear(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "f").write_text("x" * 10)
    spec = {
        "key": "dev",
        "label": "构建",
        "targets": [
            {
                "type": "find_dirs",
                "bases": [str(tmp_path)],
                "names": ["cache"],
                "action": "clear",
                "label": "散落缓存",
            }
        ],
    }
    res = scan_spec(spec)
    assert len(res.targets) == 1
    assert res.targets[0].action is TargetAction.CLEAR


def test_recycle_bin_size_returns_int():
    size = recycle_bin_size()
    assert isinstance(size, int)
    assert size >= 0


# ---------------------------------------------------------------------------
# engine 安全加固
# ---------------------------------------------------------------------------


def _mk_target(path, kind=TargetKind.FILE, action=TargetAction.DELETE, size=1):
    return Target(path=path, kind=kind, action=action, category="test", size=size)


def test_engine_refuses_drive_root(tmp_path):
    from pathlib import Path

    # 磁盘根路径直接拒绝（跨平台：len(parts)<=1 即命中）
    res = delete_targets(
        [_mk_target(Path(tmp_path.anchor))], CleanMode.PERMANENT, recycle_fallback=False
    )
    assert res["failed"] == 1


def test_engine_refuses_protected_path(tmp_path):
    # 路径含 ".git" 命中内置保护，删除前二次防御拦截
    p = tmp_path / "proj" / ".git" / "objects"
    res = delete_targets([_mk_target(p)], CleanMode.PERMANENT, recycle_fallback=False)
    assert res["failed"] == 1


def test_engine_clear_skips_protected_child(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "ok.txt").write_text("x")
    protected = cache / "xwechat_files"  # 命中微信数据保护
    protected.mkdir()
    (protected / "keep.txt").write_text("x")

    res = delete_targets(
        [_mk_target(cache, kind=TargetKind.DIR, action=TargetAction.CLEAR, size=1)],
        CleanMode.PERMANENT,
        recycle_fallback=False,
    )
    assert res["failed"] == 0
    assert not (cache / "ok.txt").exists()
    assert protected.exists()
    assert (protected / "keep.txt").exists()


def test_engine_no_silent_fallback_on_recycle_error(tmp_path, monkeypatch):
    import pc_cleaner.engine as engine

    if not engine.HAS_SEND2TRASH:
        pytest.skip("send2trash 未安装，跳过")

    f = tmp_path / "f.txt"
    f.write_text("x")

    def boom(path):
        raise OSError("模拟回收站失败")

    monkeypatch.setattr(engine.send2trash, "send2trash", boom)

    # 默认不回退：失败保留原文件
    res = delete_targets(
        [_mk_target(f, size=1)], CleanMode.RECYCLE, recycle_fallback=False
    )
    assert res["failed"] == 1
    assert f.exists()

    # 显式回退：永久删除
    res2 = delete_targets(
        [_mk_target(f, size=1)], CleanMode.RECYCLE, recycle_fallback=True
    )
    assert res2["deleted"] == 1
    assert not f.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 可建 junction")
def test_walk_skips_junction(tmp_path):
    from pc_cleaner.scanner import _walk_dir, make_protect_check

    real = tmp_path / "real"
    real.mkdir()
    (real / "data.txt").write_text("x" * 10)
    junc = tmp_path / "junc"
    # mklink /J 创建目录联接（无需管理员）
    code = os.system(f'mklink /J "{junc}" "{real}"')
    if code != 0:
        pytest.skip("无法创建 junction")
    try:
        check = make_protect_check()
        names = [p.name for p, is_dir in _walk_dir(tmp_path, check)]
        # junction 本身被跳过（不进 names），内部文件只能通过 real 目录进入一次
        assert "junc" not in names
        assert names.count("data.txt") == 1
    finally:
        try:
            junc.rmdir()
        except OSError:
            pass
