"""v0.9.19 P2/P3 回归：依赖、shred 语义、配置原子写与保护边界。

对应上一轮审查里仍然成立的低优先级项：

1. ``skip_if_in_use`` 的 psutil 依赖必须默认安装；
2. 默认回收站模式下 ``--shred`` 不能被静默忽略；
3. 配置保存必须原子替换，损坏配置要备份；
4. 受保护清空目录（System32/LogFiles）只允许 CLEAR，DELETE 子项应拒绝；
5. 整个 ``%USERPROFILE%`` 不能作为自定义清理目标；
6. ``database_compact`` 不再以 safe 进入 ``p`` 推荐；
7. MCP 文档不得宣称能清空回收站；
8. 危险确认只接受完整 ``yes``。
"""

from __future__ import annotations

import builtins
import json
import tomllib
from pathlib import Path

import pytest
from pc_cleaner import config, menu
from pc_cleaner.engine import CleanMode, delete_targets
from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind
from pc_cleaner.rules import SYSTEM_CRITICAL_FILES, get_all_category_specs, validate_rules
from pc_cleaner.ui import prompt_yes_no

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_psutil_is_a_default_dependency() -> None:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert any(dep.lower().startswith("psutil") for dep in deps), deps


def test_shred_requires_permanent_mode(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("secret", encoding="utf-8")
    target = Target(
        path=path,
        kind=TargetKind.FILE,
        action=TargetAction.DELETE,
        category="demo",
        size=6,
        file_count=1,
    )
    result = menu._run_clean_flow(
        [CategoryResult(key="demo", label="demo", targets=[target], risk="safe")],
        dry_run=False,
        mode=CleanMode.RECYCLE,
        auto_confirm=True,
        empty_bin=False,
        cfg={"enable_history": False, "preview_lines": 1},
        shred=True,
    )
    assert result.get("needs_confirmation") is True
    assert path.exists(), "shred + recycle 不应静默删除文件"
    assert "permanent" in result["reasons"][0]


def test_config_save_is_atomic_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    path = config.save_config(dict(config.DEFAULTS))
    assert json.loads(path.read_text(encoding="utf-8"))["preview_lines"] == 12
    assert not list(path.parent.glob(".*.tmp"))

    original = path.read_text(encoding="utf-8")

    def fail_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(OSError):
        config.save_config({**config.DEFAULTS, "preview_lines": 3})
    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(".*.tmp"))


def test_corrupt_config_is_backed_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    cfg = config.load_config()
    assert cfg["preview_lines"] == config.DEFAULTS["preview_lines"]
    backups = list(path.parent.glob("config.json.corrupt-*"))
    assert backups, "损坏的 config.json 应留下备份"
    assert not path.exists(), "损坏配置应被移走，避免下次继续解析失败"


def test_clear_under_protected_root_allows_only_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    logfiles = tmp_path / "Windows" / "System32" / "LogFiles"
    wmi = logfiles / "WMI"
    wmi.mkdir(parents=True)
    (wmi / "a.log").write_text("x", encoding="utf-8")

    delete_target = Target(
        path=wmi,
        kind=TargetKind.DIR,
        action=TargetAction.DELETE,
        category="demo",
        size=0,
    )
    denied = delete_targets(
        [delete_target], CleanMode.PERMANENT, on_progress=lambda i, n, m: None
    )
    assert denied["failed"] == 1
    assert denied["deleted"] == 0
    assert wmi.exists(), "受保护清空目录下的子目录不能被 DELETE 删掉"

    clear_target = Target(
        path=logfiles,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="demo",
        size=0,
    )
    cleared = delete_targets(
        [clear_target], CleanMode.PERMANENT, on_progress=lambda i, n, m: None
    )
    assert cleared["deleted"] == 1
    assert logfiles.exists(), "CLEAR 必须保留白名单根目录"
    assert not wmi.exists(), "CLEAR 应清空受保护白名单目录内的普通子项"


def test_safe_categories_have_no_system_admin_targets() -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    by_key = {s["key"]: s for s in specs}
    for key in ("java_rdp_legacy", "crash_telemetry"):
        locations = " ".join(
            str(t.get("path") or t.get("base") or "").lower()
            for t in by_key[key]["targets"]
        )
        assert "%windir%" not in locations, (key, locations)
        assert "%programdata%" not in locations, (key, locations)


def test_database_compact_is_not_recommended(tmp_path: Path) -> None:
    specs = get_all_category_specs(merge_custom=False, deep=True)
    category = next(s for s in specs if s["key"] == "database_compact")
    assert category["risk"] == "moderate"

    db = tmp_path / "History"
    db.write_bytes(b"x")
    target = Target(
        path=db,
        kind=TargetKind.FILE,
        action=TargetAction.COMPACT,
        category="database_compact",
        size=1,
    )
    result = menu.recommended_categories(
        [
            CategoryResult(
                key="database_compact",
                label="浏览器数据库压缩",
                targets=[target],
                risk="moderate",
            )
        ]
    )
    assert result == [], "database_compact 不应进入 p 推荐"


def test_whole_user_profile_target_is_rejected() -> None:
    spec = {
        "key": "bad_profile",
        "label": "bad",
        "risk": "safe",
        "targets": [{"type": "delete_dir", "path": "%USERPROFILE%"}],
    }
    errors = validate_rules([spec])
    assert any("整个用户目录" in err for err in errors), errors


def test_mcp_docs_do_not_claim_recycle_bin() -> None:
    assert "清空回收站" not in (PROJECT_ROOT / "pc_cleaner" / "mcp.py").read_text(
        encoding="utf-8"
    )
    assert "清空回收站" not in (PROJECT_ROOT / "docs" / "mcp.md").read_text(
        encoding="utf-8"
    )


def test_typed_confirmation_rejects_single_y(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "y")
    assert prompt_yes_no("confirm", require_typed=True) is False
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "yes")
    assert prompt_yes_no("confirm", require_typed=True) is True


def test_user_registry_hives_are_critical_files() -> None:
    assert "ntuser.dat" in SYSTEM_CRITICAL_FILES
    assert "usrclass.dat" in SYSTEM_CRITICAL_FILES
