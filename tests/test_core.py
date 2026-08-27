"""核心单元测试：聚焦扫描、保护判断和体积计算。"""

from __future__ import annotations

import os

import pytest

from pc_cleaner.models import TargetAction, TargetKind, format_size
from pc_cleaner.scanner import (
    _dir_size,
    _scan_find_dirs,
    _scan_files_by_rule,
    expand_path,
    make_protect_check,
    scan_spec,
)


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.00 KB"
    assert format_size(1024 * 1024) == "1.00 MB"


def test_expand_path_env(tmp_path):
    # 相对/绝对路径与 ~ 展开
    p = expand_path("~")
    assert p.is_absolute()


def test_protect_check(tmp_path):
    check = make_protect_check()
    # 内置保护
    assert check(expand_path(r"%WINDIR%\System32\foo.exe")) is True
    assert check(expand_path(r"C:\Windows\System32\x.dll")) is True
    # 普通路径不受保护
    assert check(tmp_path / "data") is False
    # 用户自定义保护
    user_check = make_protect_check([str(tmp_path / "keep")])
    assert user_check(tmp_path / "keep" / "x") is True


def test_dir_size_counts_files(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1.txt").write_text("x" * 10)
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "2.txt").write_text("x" * 20)
    size, count = _dir_size(tmp_path / "a", make_protect_check())
    assert count == 2
    assert size == 30


def test_scan_clear_dir_category(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "f1").write_text("y" * 100)
    spec = {
        "key": "test",
        "label": "测试",
        "targets": [
            {"type": "clear_dir", "path": str(tmp_path / "cache"), "label": "缓存"}
        ],
    }
    res = scan_spec(spec)
    assert res.scanned is True
    assert len(res.targets) == 1
    t = res.targets[0]
    assert t.kind is TargetKind.DIR
    assert t.action is TargetAction.CLEAR
    assert t.size == 100
    assert t.file_count == 1


def test_files_by_rule_size_filter(tmp_path):
    # 大文件匹配，小文件不匹配
    (tmp_path / "big.bin").write_bytes(b"z" * (50 * 1024 * 1024))
    (tmp_path / "small.txt").write_text("hi")
    # 用 25MB 阈值
    spec = {
        "key": "downloads",
        "label": "下载",
        "targets": [
            {
                "type": "files_by_rule",
                "base": str(tmp_path),
                "min_size_mb": 25,
                "older_than_days": 0,
                "label": "大文件",
            }
        ],
    }
    res = scan_spec(spec)
    # big.bin 50MB >= 25MB -> 命中；small.txt 2B < 25MB -> 不命中
    assert len(res.targets) == 1
    assert res.targets[0].path.name == "big.bin"


def test_find_dirs_detects_names(tmp_path):
    for dname in ("__pycache__", ".pytest_cache", "build", "other"):
        (tmp_path / dname).mkdir()
        (tmp_path / dname / "x.txt").write_text("x")
    spec = {
        "key": "dev",
        "label": "构建",
        "targets": [
            {
                "type": "find_dirs",
                "bases": [str(tmp_path)],
                "names": ["__pycache__", ".pytest_cache", "build"],
                "action": "delete",
                "label": "散落构建",
            }
        ],
    }
    res = scan_spec(spec)
    names = {t.path.name for t in res.targets}
    assert names == {"__pycache__", ".pytest_cache", "build"}


def test_skips_protected_in_clear(tmp_path):
    # 目标路径本身受保护时应返回空
    spec = {
        "key": "test",
        "label": "测试",
        "targets": [
            {"type": "clear_dir", "path": r"%WINDIR%\System32", "label": "系统"}
        ],
    }
    res = scan_spec(spec)
    assert res.targets == []
