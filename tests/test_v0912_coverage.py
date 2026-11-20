"""v0.9.12 清理覆盖扩展的回归测试。

覆盖：

1. 新分类进入规则库，且 ``all_excluded`` 标记正确；
2. ``require_extracted_sibling`` 只在压缩包旁存在同名解压目录时命中；
3. ``scan_spec`` 把 ``all_excluded`` 传入 ``CategoryResult``；
4. 规则校验保持 0 error / 0 warning。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pc_cleaner.models import CategoryResult
from pc_cleaner.rules import get_all_category_specs, validate_rules
from pc_cleaner.scanner import (
    _has_extracted_sibling_dir,
    _scan_glob_files,
    scan_spec,
)


def test_new_coverage_categories_exist() -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    by_key = {s["key"]: s for s in specs}
    for key in ("downloads_temp", "downloads_installers", "downloads_extracted", "system_wer"):
        assert key in by_key, key
    assert by_key["downloads_installers"]["all_excluded"] is True
    assert by_key["downloads_extracted"]["all_excluded"] is True
    assert by_key["rdp_legacy_cache"]["all_excluded"] is True
    assert by_key["system_wer"]["require_admin"] is True
    # 旧的 downloads 不再重复安装包规则，避免与 downloads_installers 重复计数
    old_types = {t["type"] for t in by_key["downloads"]["targets"]}
    assert old_types == {"files_by_rule"}


def test_counts_are_in_sync() -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    assert len(specs) == 34
    assert sum(len(s["targets"]) for s in specs) == 301


def test_extracted_sibling_helper(tmp_path: Path) -> None:
    archive = tmp_path / "foo.zip"
    archive.write_bytes(b"x")
    (tmp_path / "foo").mkdir()
    assert _has_extracted_sibling_dir(archive) is True

    other = tmp_path / "bar.zip"
    other.write_bytes(b"x")
    assert _has_extracted_sibling_dir(other) is False

    # 多级后缀：foo.tar.gz → foo/
    tar_gz = tmp_path / "foo.tar.gz"
    tar_gz.write_bytes(b"x")
    assert _has_extracted_sibling_dir(tar_gz) is True


def test_glob_files_requires_extracted_sibling(tmp_path: Path) -> None:
    old = time.time() - 40 * 86400
    kept = tmp_path / "kept.zip"
    kept.write_bytes(b"x" * 64)
    os.utime(kept, (old, old))
    (tmp_path / "kept").mkdir()

    lone = tmp_path / "lone.zip"
    lone.write_bytes(b"x" * 64)
    os.utime(lone, (old, old))

    targets = _scan_glob_files(
        {
            "base": str(tmp_path),
            "pattern": "*.zip",
            "min_size_mb": 0,
            "older_than_days": 30,
            "require_extracted_sibling": True,
            "label": "test",
        },
        "test",
        lambda _p: False,
    )
    names = {t.path.name for t in targets}
    assert names == {"kept.zip"}


def test_scan_spec_carries_all_excluded() -> None:
    result = scan_spec(
        {
            "key": "demo_excluded",
            "label": "demo",
            "risk": "moderate",
            "all_excluded": True,
            "targets": [],
        }
    )
    assert isinstance(result, CategoryResult)
    assert result.all_excluded is True


def test_rules_still_validate_clean() -> None:
    report = validate_rules()
    assert list(report) == []
    assert list(report.warnings) == []
