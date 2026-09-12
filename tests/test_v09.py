"""v0.9.0 新增功能的单元测试：吸收 BleachBit/Dism++。

覆盖：
- compact_db（BleachBit「整理优化数据库」）：对 SQLite 数据库执行 VACUUM
  压缩释放碎片，**不删除数据**；
- 引擎 delete_targets 对 COMPACT 目标的处理（压缩而非删除）；
- 新分类 browser_data（浏览器站点数据）与 database_compact 的规则加载与校验。
"""

from __future__ import annotations

import sqlite3

import pytest

from pc_cleaner.engine import CleanMode, compact_database, delete_targets
from pc_cleaner.models import Target, TargetAction, TargetKind
from pc_cleaner.rules import get_all_category_specs, validate_rules
from pc_cleaner.scanner import scan_spec


def _mk_db(path, insert=3000, keep=200):
    """建一个带大量空闲页的 SQLite 库，返回 (原始字节数)。"""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload TEXT)")
        con.executemany(
            "INSERT INTO t (payload) VALUES (?)",
            [("x" * 100,) for _ in range(insert)],
        )
        con.commit()
        con.execute("DELETE FROM t WHERE id > ?", (keep,))
        con.commit()
    finally:
        con.close()
    return path.stat().st_size


def test_compact_database_keeps_data_and_shrinks(tmp_path):
    db = tmp_path / "History"
    before = _mk_db(db)
    freed = compact_database(db)
    # 压缩不删除文件，且文件仍可读
    assert db.exists()
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    con.close()
    assert rows == 200  # 数据仍在
    assert freed >= 0
    assert db.stat().st_size <= before


def test_engine_delete_targets_compacts_not_deletes(tmp_path):
    db = tmp_path / "Web Data"
    before = _mk_db(db)
    res = delete_targets(
        [
            Target(
                path=db,
                kind=TargetKind.FILE,
                action=TargetAction.COMPACT,
                category="t",
                size=before,
            )
        ],
        # 即使指定 recycle 模式，COMPACT 也不该把文件删除/进回收站
        CleanMode.RECYCLE,
        recycle_fallback=False,
    )
    assert res["failed"] == 0
    assert res["deleted"] == 1
    assert db.exists()  # 未被删除，只是压缩


def test_scan_compact_db_produces_compact_target(tmp_path):
    db = tmp_path / "Login Data"
    _mk_db(db)
    (tmp_path / "not_index.txt").write_text("x")
    spec = {
        "key": "database_compact",
        "label": "压缩",
        "risk": "safe",
        "targets": [
            {
                "type": "compact_db",
                "base": str(tmp_path),
                "pattern": "Login Data",
                "label": "登录库压缩",
            },
            {
                "type": "compact_db",
                "base": str(tmp_path),
                "pattern": "*.sqlite",
                "label": "sqlite 库压缩",
            },
        ],
    }
    res = scan_spec(spec)
    targets = [t for t in res.targets if t.action is TargetAction.COMPACT]
    assert targets
    assert all(t.kind is TargetKind.FILE for t in targets)
    assert all(t.action is TargetAction.COMPACT for t in targets)
    # 非匹配文件（not_index.txt）不进入
    assert all(t.path.name != "not_index.txt" for t in targets)


def test_new_categories_present_and_valid(tmp_path):
    specs = {s["key"]: s for s in get_all_category_specs(merge_custom=False, deep=True)}
    assert "browser_data" in specs
    assert "database_compact" in specs
    errors = validate_rules(get_all_category_specs(merge_custom=False, deep=True))
    assert errors == []
