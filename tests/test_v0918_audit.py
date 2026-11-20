"""v0.9.18 旧审查回归：嵌套受保护目录、选择语义、统计真实性与规则校验。

覆盖上一版审查报告里仍然成立的阻断项/重要项：

1. CLEAR 目录里的项目目录嵌套 ``.git`` 时，整棵受保护子树必须保留；
2. 回收站失败回退永久删除时，统计必须是 freed / permanent，而不是 recycled；
3. CLEAR 目标删除前消失时计入 vanished，而不是 deleted；
4. ``delete_dir`` 的 ``paths`` 多候选写法必须真的能扫描；
5. ``--validate-rules`` 的底层数据源必须覆盖 custom_rules；
6. 未选中的分类不能抢走选中分类的目标（claim_keys）；
7. ``system_logs`` 不得再以 safe 身份推荐，且不得包含安全审计日志；
8. MCP 的 ``older_than_days`` 不作用到目录目标。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from pc_cleaner import config, engine
from pc_cleaner.engine import CleanMode, delete_targets
from pc_cleaner.mcp import _apply_filters
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import (
    get_all_category_specs,
    validate_rules,
)
from pc_cleaner.scanner import scan_all, scan_spec


def test_clear_keeps_nested_protected_git(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    project = cache / "SomeProject"
    git_dir = project / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("secret", encoding="utf-8")
    (project / "normal.txt").write_text("x", encoding="utf-8")

    target = Target(
        path=cache,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="demo",
        size=0,
        file_count=0,
    )
    result = delete_targets(
        [target], CleanMode.PERMANENT, on_progress=lambda i, total, msg: None
    )
    assert (git_dir / "config").exists(), "嵌套 .git 被 CLEAR 误删"
    assert project.exists(), "包含受保护子树的项目目录不应被部分删除"
    assert result["failed"] == 1
    assert result["deleted"] == 0


def test_recycle_fallback_reports_permanent(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "fallback.bin"
    path.write_bytes(b"x" * 100)

    class BrokenTrash:
        @staticmethod
        def send2trash(_path: str) -> None:
            raise RuntimeError("recycle unavailable")

    monkeypatch.setattr(engine, "HAS_SEND2TRASH", True)
    monkeypatch.setattr(engine, "send2trash", BrokenTrash())
    audits: list[tuple[int, str, int]] = []

    target = Target(
        path=path,
        kind=TargetKind.FILE,
        action=TargetAction.DELETE,
        category="demo",
        size=100,
        file_count=1,
    )
    result = delete_targets(
        [target],
        CleanMode.RECYCLE,
        on_progress=lambda i, total, msg: None,
        recycle_fallback=True,
        audit=lambda p, size, mode, freed: audits.append((size, mode, freed)),
    )
    assert not path.exists()
    assert result["freed"] == 100
    assert result["recycled"] == 0
    assert audits[-1][1] == "permanent"


def test_clear_target_vanished_is_not_deleted(tmp_path: Path) -> None:
    path = tmp_path / "gone"
    path.mkdir()
    (path / "a.txt").write_text("a", encoding="utf-8")
    target = Target(
        path=path,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="demo",
        size=1,
        file_count=1,
    )
    shutil.rmtree(path)
    result = delete_targets(
        [target], CleanMode.PERMANENT, on_progress=lambda i, total, msg: None
    )
    assert result["deleted"] == 0
    assert result["vanished"] == 1


def test_delete_dir_paths_are_scannable(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.bin").write_bytes(b"a")
    (second / "b.bin").write_bytes(b"bb")

    spec = {
        "key": "demo",
        "label": "demo",
        "risk": "safe",
        "targets": [{"type": "delete_dir", "paths": [str(first), str(second)]}],
    }
    result = scan_spec(spec)
    assert len(result.targets) == 2
    assert {t.path for t in result.targets} == {first, second}


def test_validate_rules_includes_custom_rules(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "custom_rules": [
                {
                    "key": "bad_custom",
                    "label": "bad",
                    "risk": "safe",
                    "targets": [{"type": "delete_dir"}],
                }
            ]
        },
    )
    specs = get_all_category_specs(merge_custom=True, deep=True)
    errors = validate_rules(specs)
    assert any("[bad_custom]" in e and "path" in e for e in errors)


def test_unselected_category_does_not_claim_target(tmp_path: Path) -> None:
    parent = tmp_path / "downloads"
    file = parent / "big_old_installer.exe"
    parent.mkdir()
    file.write_bytes(b"x" * 1024)

    specs = [
        {
            "key": "downloads_installers",
            "label": "installers",
            "risk": "moderate",
            "all_excluded": True,
            "targets": [
                {"type": "glob_files", "base": str(parent), "pattern": "*.exe"}
            ],
        },
        {
            "key": "downloads",
            "label": "downloads",
            "risk": "moderate",
            "targets": [
                {"type": "glob_files", "base": str(parent), "pattern": "*.exe"}
            ],
        },
    ]
    results = scan_all(specs, claim_keys={"downloads"})
    by_key = {r.key: r for r in results}
    assert len(by_key["downloads"].targets) == 1
    assert by_key["downloads"].targets[0].path == file


def test_system_logs_is_admin_moderate_without_audit_targets() -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    category = next(s for s in specs if s["key"] == "system_logs")
    assert category["risk"] == "moderate"
    assert category.get("require_admin") is True
    locations = {
        str(t.get("path") or t.get("base") or "").lower()
        for t in category["targets"]
    }
    assert not any("security/logs" in p for p in locations)
    assert not any("winevt/logs/archive" in p for p in locations)
    assert not any("systemdata" in p for p in locations)


def test_mcp_age_filter_does_not_drop_directories(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "f").write_bytes(b"x")
    os.utime(directory, (time.time(), time.time()))

    target = Target(
        path=directory,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="demo",
        size=1,
        file_count=1,
    )
    kept = _apply_filters([target], {"older_than_days": 1})
    assert len(kept) == 1
    assert kept[0] is target
