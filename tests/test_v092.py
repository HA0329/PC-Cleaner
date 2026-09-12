"""v0.9.2 增强功能的单元测试。

覆盖本次针对本机实测做的增强与修复：

1. 嵌套目标去重（修复「可释放空间」重复计数）；
2. 受保护子树剪枝（体积统计更准 + 更快）；
3. 新 target 类型 ``empty_dirs`` / ``zero_byte_files``；
4. ``glob_files`` / ``files_by_rule`` 的 ``ext`` 过滤；
5. ``find_dirs`` 的 ``max_depth`` 规则级限制；
6. 引擎实际释放量核算（CLEAR 前后对比）与部分失败上报；
7. 重解析点（符号链接）拒绝删除；
8. 规则校验对新类型/必填字段的检查；
9. ``--clean recycle_bin`` 不再静默无操作；
10. 配置 ``scan_workers`` / ``all_includes_recycle_bin`` 解析。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pc_cleaner import config as cfg_mod
from pc_cleaner.engine import CleanMode, delete_targets
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import (
    get_all_category_specs,
    validate_rules,
)
from pc_cleaner.scanner import (
    _drop_subsumed,
    _is_under,
    is_reparse_point,
    scan_all,
    scan_spec,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _mk_target(path: Path, size: int, kind=TargetKind.DIR, action=TargetAction.CLEAR) -> Target:
    return Target(
        path=path,
        kind=kind,
        action=action,
        category="test",
        size=size,
        file_count=1,
    )


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _leaf(path: Path) -> str:
    """取路径最后一级（v0.9.5：跨平台——Windows 反斜杠在 POSIX 上不是分隔符，
    ``Path(r"C:\\X\\npm-cache").name`` 在 Linux 上会返回整串路径）。"""
    return Path(str(path).replace("\\", os.sep)).name


# ---------------------------------------------------------------------------
# 1. 嵌套目标去重
# ---------------------------------------------------------------------------
def test_is_under_semantics(tmp_path):
    parent = tmp_path / "npm-cache"
    child = parent / "_npx"
    assert _is_under(child, parent) is True
    assert _is_under(parent, parent) is False          # 自身不算包含
    assert _is_under(tmp_path / "other", parent) is False


def test_drop_subsumed_removes_nested_targets():
    parent = Path(r"C:\X\npm-cache")
    npx = parent / "_npx"
    cacache = parent / "_cacache"
    other = Path(r"C:\X\node\corepack")
    kept, dropped = _drop_subsumed(
        [
            _mk_target(parent, 400),
            _mk_target(npx, 200),
            _mk_target(cacache, 150),
            _mk_target(other, 40),
        ]
    )
    names = sorted(_leaf(t.path) for t in kept)
    assert names == ["corepack", "npm-cache"]
    assert dropped == 2


def test_drop_subsumed_is_order_independent():
    parent = Path(r"C:\X\npm-cache")
    npx = parent / "_npx"
    a, b = _mk_target(parent, 400), _mk_target(npx, 200)
    assert [t.path for t in _drop_subsumed([a, b])[0]] == [parent]
    assert [t.path for t in _drop_subsumed([b, a])[0]] == [parent]


def test_drop_subsumed_keeps_compact_targets():
    """COMPACT 目标（数据库压缩）不参与目录覆盖关系，必须保留。"""
    parent = Path(r"C:\X\profile")
    db = parent / "History"
    kept, dropped = _drop_subsumed(
        [
            _mk_target(parent, 100),
            _mk_target(db, 10, kind=TargetKind.FILE, action=TargetAction.COMPACT),
            _mk_target(parent / "Cache" / "data_0", 5, kind=TargetKind.FILE, action=TargetAction.DELETE),
        ]
    )
    kept_names = sorted(_leaf(t.path) for t in kept)
    assert kept_names == ["History", "profile"]   # COMPACT 保留，普通文件被覆盖
    assert dropped == 1


def test_scan_spec_dedups_nested_clear_dirs(tmp_path):
    """同一分类里父目录与子目录都命中时，只保留父目录目标。"""
    parent = tmp_path / "npm-cache"
    _write(parent / "_npx" / "a.bin", 1000)
    _write(parent / "_cacache" / "b.bin", 2000)
    spec = {
        "key": "dev_caches",
        "label": "开发工具缓存",
        "risk": "moderate",
        "targets": [
            {"type": "clear_dir", "path": str(parent), "label": "npm 缓存"},
            {"type": "clear_dir", "path": str(parent / "_npx"), "label": "npx 缓存"},
            {"type": "clear_dir", "path": str(parent / "_cacache"), "label": "cacache"},
        ],
    }
    res = scan_spec(spec)
    assert len(res.targets) == 1
    assert res.targets[0].path == parent
    assert res.liberatable == 3000  # 不重复计数


def test_scan_all_dedups_nested_across_categories(tmp_path):
    """跨分类也要去重：父目录目标覆盖其它分类里的子目录目标。"""
    parent = tmp_path / "cache"
    _write(parent / "sub" / "f.bin", 512)
    specs = [
        {
            "key": "aaa",
            "label": "父",
            "risk": "safe",
            "targets": [{"type": "clear_dir", "path": str(parent)}],
        },
        {
            "key": "bbb",
            "label": "子",
            "risk": "safe",
            "targets": [{"type": "clear_dir", "path": str(parent / "sub")}],
        },
    ]
    results = scan_all(specs, workers=1)
    total_targets = sum(len(r.targets) for r in results)
    assert total_targets == 1
    assert sum(r.liberatable for r in results) == 512


# ---------------------------------------------------------------------------
# 2. 受保护子树剪枝
# ---------------------------------------------------------------------------
def test_dir_size_excludes_protected_subtree(tmp_path):
    """受保护子目录整棵剪枝：既不统计体积，也不计入文件数。"""
    base = tmp_path / "cache"
    _write(base / "normal.bin", 100)
    protected = base / ".git"
    _write(protected / "objects" / "pack.bin", 9999)

    spec = {
        "key": "t",
        "label": "t",
        "risk": "safe",
        "targets": [{"type": "clear_dir", "path": str(base)}],
    }
    res = scan_spec(spec)
    assert len(res.targets) == 1
    assert res.liberatable == 100        # .git 的 9999 字节不计入
    assert res.total_count == 1


# ---------------------------------------------------------------------------
# 3. 新 target 类型
# ---------------------------------------------------------------------------
def test_scan_empty_dirs(tmp_path):
    base = tmp_path / "logs"
    (base / "a" / "b").mkdir(parents=True)
    (base / "c").mkdir()
    _write(base / "keep.bin", 10)

    spec = {
        "key": "t",
        "label": "空目录",
        "risk": "safe",
        "targets": [{"type": "empty_dirs", "base": str(base), "min_age_days": 0}],
    }
    res = scan_spec(spec)
    names = sorted(t.path.name for t in res.targets)
    # 只产出「顶层空目录」a 与 c：a/b 随 a 一起删除，不重复列出
    assert names == ["a", "c"]
    assert all(t.action is TargetAction.DELETE for t in res.targets)
    assert all(t.kind is TargetKind.DIR for t in res.targets)


def test_scan_empty_dirs_respects_min_age(tmp_path):
    import time

    base = tmp_path / "logs"
    (base / "old").mkdir(parents=True)
    (base / "new").mkdir()
    # 把 old 的修改时间设为 10 天前
    old_ts = time.time() - 10 * 86400
    os.utime(base / "old", (old_ts, old_ts))

    spec = {
        "key": "t",
        "label": "空目录",
        "risk": "safe",
        "targets": [{"type": "empty_dirs", "base": str(base), "min_age_days": 5}],
    }
    res = scan_spec(spec)
    assert [t.path.name for t in res.targets] == ["old"]


def test_scan_zero_byte_files(tmp_path):
    base = tmp_path / "downloads"
    _write(base / "empty.tmp", 0)
    _write(base / "real.tmp", 5)
    _write(base / "empty.log", 0)

    spec = {
        "key": "t",
        "label": "0 字节",
        "risk": "safe",
        "targets": [{"type": "zero_byte_files", "base": str(base)}],
    }
    res = scan_spec(spec)
    names = sorted(t.path.name for t in res.targets)
    assert names == ["empty.log", "empty.tmp"]
    assert all(t.kind is TargetKind.FILE for t in res.targets)


def test_scan_zero_byte_files_ext_filter(tmp_path):
    base = tmp_path / "downloads"
    _write(base / "empty.tmp", 0)
    _write(base / "empty.log", 0)
    spec = {
        "key": "t",
        "label": "0 字节",
        "risk": "safe",
        "targets": [{"type": "zero_byte_files", "base": str(base), "ext": [".tmp"]}],
    }
    res = scan_spec(spec)
    assert [t.path.name for t in res.targets] == ["empty.tmp"]


# ---------------------------------------------------------------------------
# 4. ext 过滤
# ---------------------------------------------------------------------------
def test_glob_files_ext_filter(tmp_path):
    base = tmp_path / "d"
    _write(base / "a.log", 10)
    _write(base / "b.tmp", 10)
    spec = {
        "key": "t",
        "label": "t",
        "risk": "safe",
        "targets": [
            {"type": "glob_files", "base": str(base), "pattern": "*", "ext": "log,.tmp"},
        ],
    }
    res = scan_spec(spec)
    assert sorted(t.path.name for t in res.targets) == ["a.log", "b.tmp"]


def test_files_by_rule_pattern_and_ext(tmp_path):
    base = tmp_path / "d"
    _write(base / "a.log", 10)
    _write(base / "b.log", 10)
    _write(base / "c.txt", 10)
    spec = {
        "key": "t",
        "label": "t",
        "risk": "safe",
        "targets": [
            {
                "type": "files_by_rule",
                "base": str(base),
                "pattern": "a*",
                "ext": [".log"],
            }
        ],
    }
    res = scan_spec(spec)
    assert [t.path.name for t in res.targets] == ["a.log"]


# ---------------------------------------------------------------------------
# 5. find_dirs 规则级 max_depth
# ---------------------------------------------------------------------------
def test_find_dirs_rule_max_depth(tmp_path):
    deep = tmp_path / "l1" / "l2" / "l3" / "node_modules"
    deep.mkdir(parents=True)
    _write(deep / "f.bin", 10)
    shallow = tmp_path / "node_modules"
    shallow.mkdir()
    _write(shallow / "f.bin", 10)

    spec = {
        "key": "t",
        "label": "t",
        "risk": "safe",
        "targets": [
            {
                "type": "find_dirs",
                "bases": [str(tmp_path)],
                "names": ["node_modules"],
                "action": "delete",
                "max_depth": 1,
            }
        ],
    }
    res = scan_spec(spec, scan_depth=20)
    assert [t.path for t in res.targets] == [shallow]  # 深层的不再下探


# ---------------------------------------------------------------------------
# 6. 引擎：实际释放量核算 / 部分失败
# ---------------------------------------------------------------------------
def test_delete_targets_clear_reports_real_freed(tmp_path):
    base = tmp_path / "cache"
    _write(base / "a.bin", 1000)
    _write(base / "sub" / "b.bin", 2000)
    t = Target(
        path=base,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="t",
        size=999999,  # 故意给一个夸大的"扫描估计值"
        file_count=2,
    )
    res = delete_targets([t], CleanMode.PERMANENT)
    assert res["deleted"] == 1
    assert res["failed"] == 0
    # 释放量按实际删除前后体积差计算，而不是扫描时的估计值
    assert res["freed"] == 3000
    assert base.exists()  # CLEAR 保留目录本身


def test_delete_targets_audit_receives_freed(tmp_path):
    base = tmp_path / "cache"
    _write(base / "a.bin", 500)
    seen: list[tuple] = []

    def audit(path, size, mode_name, freed):
        seen.append((str(path), size, mode_name, freed))

    t = Target(
        path=base,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="t",
        size=1,
        file_count=1,
    )
    delete_targets([t], CleanMode.PERMANENT, audit=audit)
    assert len(seen) == 1
    assert seen[0][3] == 500  # freed 不再恒为 0


def test_delete_targets_counts_failure_when_nothing_removed(tmp_path, monkeypatch):
    """清空目录时一个子项都没删掉 → 计入 failed，不再假装成功。"""
    base = tmp_path / "cache"
    _write(base / "a.bin", 100)
    t = Target(
        path=base,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="t",
        size=100,
        file_count=1,
    )

    import pc_cleaner.engine as engine

    def _boom(*args, **kwargs):
        raise PermissionError("busy")

    monkeypatch.setattr(engine, "_delete_path", _boom)
    res = delete_targets([t], CleanMode.PERMANENT)
    assert res["deleted"] == 0
    assert res["failed"] == 1
    assert res["freed"] == 0


def test_delete_targets_partial_clear_reports_skipped(tmp_path, monkeypatch):
    base = tmp_path / "cache"
    _write(base / "a.bin", 100)
    _write(base / "b.bin", 100)
    t = Target(
        path=base,
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="t",
        size=200,
        file_count=2,
    )

    import pc_cleaner.engine as engine

    real = engine._delete_path
    calls = {"n": 0}

    def _flaky(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(engine, "_delete_path", _flaky)
    res = delete_targets([t], CleanMode.PERMANENT)
    assert res["deleted"] == 1
    assert res["skipped"] == 1
    assert res["freed"] == 100


# ---------------------------------------------------------------------------
# 7. 重解析点保护
# ---------------------------------------------------------------------------
def test_is_reparse_point_detects_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    assert is_reparse_point(link) is True
    assert is_reparse_point(target) is False


def test_delete_targets_refuses_symlink(tmp_path):
    target = tmp_path / "real"
    _write(target / "data.bin", 10)
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    t = Target(
        path=link,
        kind=TargetKind.DIR,
        action=TargetAction.DELETE,
        category="t",
        size=10,
        file_count=1,
    )
    res = delete_targets([t], CleanMode.PERMANENT)
    assert res["failed"] == 1
    assert link.exists()
    assert target.exists()          # 链接目标绝不能被删除
    assert (target / "data.bin").exists()


# ---------------------------------------------------------------------------
# 8. 规则校验
# ---------------------------------------------------------------------------
def test_validate_rules_rejects_empty_dirs_bad_action():
    errors = validate_rules(
        [
            {
                "key": "t",
                "label": "t",
                "risk": "safe",
                "targets": [
                    {"type": "empty_dirs", "base": "C:/x", "action": "clear"},
                ],
            }
        ]
    )
    assert any("empty_dirs" in e for e in errors)


def test_validate_rules_requires_filter_for_files_by_rule():
    errors = validate_rules(
        [
            {
                "key": "t",
                "label": "t",
                "risk": "safe",
                "targets": [{"type": "files_by_rule", "base": "C:/x"}],
            }
        ]
    )
    assert any("files_by_rule" in e for e in errors)


def test_builtin_rules_valid_and_new_categories_present():
    specs = get_all_category_specs(merge_custom=False, deep=True)
    assert validate_rules(specs) == []
    keys = {s["key"] for s in specs}
    assert {"system_logs", "wechat_cache", "dev_caches"} <= keys
    # 微信分类必须包含本机实测存在的 xplugin / radium 缓存
    wechat = next(s for s in specs if s["key"] == "wechat_cache")
    paths = [t.get("path", "") for t in wechat["targets"]]
    bases = [t.get("base", "") for t in wechat["targets"]]
    assert any("xplugin/plugins" in p for p in paths)
    assert any("radium" in b for b in bases)
    # 绝不能出现微信数据目录（聊天记录）
    assert not any("WeixinShuju" in p or "xwechat_files" in p for p in paths + bases)


def test_builtin_rules_never_target_protected_paths():
    """内置规则不得出现 DriverStore / 微信数据目录等受保护目标。

    例外：``%WINDIR%/WinSxS/Temp``（受保护路径，扫描器不会产出目标，
    引擎也会拒绝），保留该规则仅用于文档说明"需要 DISM 处理"。
    """
    specs = get_all_category_specs(merge_custom=False, deep=True)
    forbidden = ("driverstore", "weixinshuju", "xwechat_files")
    for cat in specs:
        for t in cat["targets"]:
            loc = (t.get("path") or t.get("base") or " ".join(t.get("bases", []))).lower()
            if not loc:
                continue
            assert not any(f in loc for f in forbidden), f"{cat['key']} 命中受保护路径: {loc}"


def test_protected_winsxs_target_never_scanned(tmp_path):
    """即使规则写了 WinSxS 子目录，扫描器也必须产出 0 个目标。"""
    winsxs = Path(os.environ.get("WINDIR", r"C:\Windows")) / "WinSxS" / "Temp"
    spec = {
        "key": "t",
        "label": "t",
        "risk": "moderate",
        "targets": [{"type": "clear_dir", "path": str(winsxs)}],
    }
    res = scan_spec(spec)
    assert res.targets == []


# ---------------------------------------------------------------------------
# 9. CLI：--clean recycle_bin 不再静默无操作
# ---------------------------------------------------------------------------
def _run_cli(monkeypatch, argv, tmp_path, answer="n"):
    """在隔离的 PC_CLEANER_HOME 下运行 cli.main，返回退出码。"""
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *a, **k: answer)
    from pc_cleaner.cli import main

    return main(argv)


def test_clean_recycle_bin_only_actually_prompts(monkeypatch, tmp_path, capsys):
    """--clean recycle_bin 必须真的走到「清空回收站」流程（旧版会静默 return 0）。"""
    rc = _run_cli(monkeypatch, ["--clean", "recycle_bin", "--no-progress"], tmp_path, answer="n")
    out = capsys.readouterr().out
    assert rc == 0
    assert "回收站" in out          # 预览里出现了回收站
    assert "已取消清空回收站" in out  # 回答 n 后确实被取消（说明真的到了确认环节）


def test_clean_recycle_bin_dry_run_reports(monkeypatch, tmp_path, capsys):
    rc = _run_cli(monkeypatch, ["--clean", "recycle_bin", "--dry-run", "--no-progress"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out


def test_clean_unknown_category_errors(monkeypatch, tmp_path, capsys):
    rc = _run_cli(monkeypatch, ["--clean", "no_such_key", "--no-progress"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "无法识别的分类" in out


def test_export_scan_writes_file_even_when_piped(monkeypatch, tmp_path, capsys):
    """--export-scan 不能被「管道自动 JSON」拦截（此前只输出 JSON、文件没生成）。"""
    out_file = tmp_path / "scan.json"
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path / "home"))
    from pc_cleaner.cli import main

    # pytest 环境下 stdout 不是 TTY，会触发管道自动 JSON 分支
    rc = main(["--export-scan", str(out_file), "--no-progress"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out_file.exists()
    assert "已导出" in out
    import json

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "categories" in data


# ---------------------------------------------------------------------------
# 10. 配置
# ---------------------------------------------------------------------------
def test_resolve_workers(monkeypatch):
    assert cfg_mod.resolve_workers({"scan_workers": 1}) == 1
    assert cfg_mod.resolve_workers({"scan_workers": 4}) == 4
    assert cfg_mod.resolve_workers({"scan_workers": 0}) >= 1     # 自动
    assert cfg_mod.resolve_workers({"scan_workers": 999}) == 16  # 上限
    assert cfg_mod.resolve_workers({"scan_workers": "bad"}) >= 1  # 容错


def test_all_includes_recycle_bin_defaults_false():
    assert cfg_mod.DEFAULTS["all_includes_recycle_bin"] is False
    assert cfg_mod.DEFAULTS["scan_workers"] >= 1


def test_all_does_not_empty_recycle_bin_by_default(monkeypatch, tmp_path, capsys):
    """--all 默认不清空回收站（避免误清不可恢复内容）。"""
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    from pc_cleaner.cli import main

    rc = main(["--all", "--dry-run", "--no-progress"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "【回收站】" not in out


# ---------------------------------------------------------------------------
# 11. 回收站 $I 解析与恢复（修复 --undo-last 永远匹配不上）
# ---------------------------------------------------------------------------
def _mk_recycle_info(info_path: Path, original: str, size: int, is_dir: bool) -> None:
    """按 Windows 真实布局写一个 $I 元数据文件。"""
    data = bytearray()
    data += b"\x02\x00\x00\x00\x00\x00\x00\x00"        # 8 字节头
    data += int(size).to_bytes(8, "little")             # 文件大小
    data += (132000000000000000).to_bytes(8, "little")  # 删除时间(FILETIME)
    data += (32768 if is_dir else 0).to_bytes(4, "little")  # 目录记录长度
    data += original.encode("utf-16-le") + b"\x00\x00"
    info_path.write_bytes(bytes(data))


def test_parse_recycle_info_directory_offset(tmp_path):
    """目录记录的路径必须从 offset 28 读取（旧实现从 24 读会多出垃圾字符）。"""
    from pc_cleaner.engine import _parse_recycle_info

    info = tmp_path / "$IABC123"
    orig = r"C:\Users\X\AppData\Roaming\Tencent\xwechat\xplugin\plugins"
    _mk_recycle_info(info, orig, 1234, is_dir=True)
    parsed = _parse_recycle_info(info)
    assert parsed is not None
    assert parsed[0] == orig          # 不带任何前导垃圾字节
    assert parsed[1] == 1234


def test_parse_recycle_info_rejects_garbage(tmp_path):
    from pc_cleaner.engine import _parse_recycle_info

    info = tmp_path / "$Ibad"
    info.write_bytes(b"\x00" * 40)
    assert _parse_recycle_info(info) is None
    short = tmp_path / "$Ishort"
    short.write_bytes(b"\x01\x02")
    assert _parse_recycle_info(short) is None


def test_restore_paths_restores_recycled_children(tmp_path, monkeypatch):
    """目标目录的内容被逐个回收时，--undo-last 应能逐条还原。"""
    import pc_cleaner.engine as engine

    # 回收站恢复是 Windows 专属逻辑；在其它平台伪装 win32，让纯路径/字节
    # 操作真正跑一遍（v0.9.5：此前被平台守卫短路，逻辑从未在 Linux 上执行）
    monkeypatch.setattr(sys, "platform", "win32")

    drive = tmp_path / "drive"
    drive.mkdir()
    sid = drive / "$Recycle.Bin" / "S-1-5-21-0-0-0-1001"
    sid.mkdir(parents=True)

    original_dir = drive / "app" / "plugins"
    original_dir.mkdir(parents=True)
    for name in ("a", "b"):
        orig = str(original_dir / name)
        _mk_recycle_info(sid / f"$I{name}", orig, 10, is_dir=True)
        data_dir = sid / f"$R{name}"
        data_dir.mkdir()
        (data_dir / "f.bin").write_bytes(b"x" * 10)

    # 只扫描测试用的 drive 根
    res = engine.restore_paths([str(original_dir)], drives=[str(drive)])
    assert len(res["restored"]) == 2
    assert (original_dir / "a" / "f.bin").exists()
    assert (original_dir / "b" / "f.bin").exists()
    # $I 元数据被清理，避免孤立记录
    assert not list(sid.glob("$Ia"))


def test_restore_paths_restores_recycled_parent(tmp_path, monkeypatch):
    """父目录被整体回收时，请求子路径也应能恢复（还原父目录）。"""
    import pc_cleaner.engine as engine

    monkeypatch.setattr(sys, "platform", "win32")  # 非 Windows 平台也执行恢复逻辑

    drive = tmp_path / "drive"
    drive.mkdir()
    sid = drive / "$Recycle.Bin" / "S-1-5-21-0-0-0-1001"
    sid.mkdir(parents=True)

    parent = drive / "app" / "plugins"
    _mk_recycle_info(sid / "$IP", str(parent), 10, is_dir=True)
    data_dir = sid / "$RP"
    data_dir.mkdir()
    (data_dir / "f.bin").write_bytes(b"x" * 10)

    res = engine.restore_paths([str(parent / "sub")], drives=[str(drive)])
    assert res["restored"] == [str(parent)]
    assert (parent / "f.bin").exists()


def test_restore_paths_skips_when_original_exists(tmp_path, monkeypatch):
    import pc_cleaner.engine as engine

    monkeypatch.setattr(sys, "platform", "win32")  # 非 Windows 平台也执行恢复逻辑

    drive = tmp_path / "drive"
    drive.mkdir()
    sid = drive / "$Recycle.Bin" / "S-1-5-21-0-0-0-1001"
    sid.mkdir(parents=True)

    original = drive / "file.bin"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"new")
    _mk_recycle_info(sid / "$IF", str(original), 3, is_dir=False)
    (sid / "$RF").write_bytes(b"old")

    res = engine.restore_paths([str(original)], drives=[str(drive)])
    assert res["restored"] == []
    assert any("已有同名文件" in s for s in res["skipped"])
    assert original.read_bytes() == b"new"   # 用户数据不被覆盖


# ---------------------------------------------------------------------------
# 12. 交互式菜单渲染（防回归：曾因缺少 is_elevated 导入在提权时崩溃）
# ---------------------------------------------------------------------------
def _fake_args():
    class _A:
        dry_run = True
        shred = False
        shred_passes = 1
        recycle_fallback = False
        ext = None
        min_size_mb = None
        older_than_days = None

    return _A()


def test_interactive_menu_renders_and_quits(monkeypatch, tmp_path, capsys):
    """菜单主循环必须能正常渲染并响应 q 退出（含管理员/提权分支）。"""
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    monkeypatch.setenv("PC_CLEANER_ELEVATED", "1")   # 覆盖 is_elevated() 分支
    from pc_cleaner.config import load_config
    from pc_cleaner.menu import _interactive
    from pc_cleaner.models import CategoryResult

    results = [
        CategoryResult(key="system_temp", label="系统临时文件", scanned=True),
    ]
    answers = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    rc = _interactive(
        results,
        [],
        CleanMode.RECYCLE,
        _fake_args(),
        load_config(),
        False,
        "size_desc",
        20,
        False,
        deep=False,
        workers=1,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PC Junk Cleaner" in out
    assert "操作:" in out


def test_interactive_menu_detail_tree_sort_paths(monkeypatch, tmp_path, capsys):
    """d / t / s / 区间选择 / 无效输入 都应能正常处理，不抛异常。"""
    monkeypatch.setenv("PC_CLEANER_HOME", str(tmp_path))
    monkeypatch.delenv("PC_CLEANER_ELEVATED", raising=False)
    from pc_cleaner.config import load_config
    from pc_cleaner.menu import _interactive
    from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind

    t = Target(
        path=tmp_path / "cache",
        kind=TargetKind.DIR,
        action=TargetAction.CLEAR,
        category="system_temp",
        size=1024,
        file_count=1,
        label="测试缓存",
    )
    results = [
        CategoryResult(
            key="system_temp",
            label="系统临时文件",
            targets=[t],
            scanned=True,
        )
    ]
    answers = iter(["d 1", "t 1", "s name_asc", "s", "bogus", "1", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr("pc_cleaner.ui.prompt_yes_no", lambda *a, **k: False)
    rc = _interactive(
        results,
        [],
        CleanMode.PERMANENT,
        _fake_args(),
        load_config(),
        False,
        "size_desc",
        20,
        False,
        deep=False,
        workers=1,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "详细列表" in out        # d 1
    assert "树形视图" in out        # t 1
    assert "已切换排序" in out      # s
    assert "无效输入" in out        # bogus
