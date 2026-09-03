"""v0.8.0 新增功能的单元测试。

覆盖：选择解析（编号/区间/all/none/r 回收站）、可选项编号（只给有内容的分类）、
微信数据提示、--sort name_desc 选项、预览与删除同源（过滤后预览）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pc_cleaner.env import wechat_data_summary
from pc_cleaner.menu import (
    _parse_selection,
    _split_choice_tokens,
    selectable_rows,
)
from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind


# ---------------------------------------------------------------------------
# 选择解析（v0.8：区间 / 回收站 / all / none）
# ---------------------------------------------------------------------------
def test_split_tokens_handles_chinese_separators():
    assert _split_choice_tokens("1，2、3；4, 5") == ["1", "2", "3", "4", "5"]
    assert _split_choice_tokens("1,3-5 r") == ["1", "3-5", "r"]


def test_parse_selection_numbers_and_ranges():
    sel = _parse_selection("1,3-5", 7)
    assert sel == {"all": False, "none": False, "recycle": False, "indexes": {1, 3, 4, 5}}


def test_parse_selection_range_reversed_is_clamped():
    sel = _parse_selection("5-2", 7)
    assert sel["indexes"] == {2, 3, 4, 5}


def test_parse_selection_recycle_flag():
    sel = _parse_selection("2-4 r", 7)
    assert sel["recycle"] is True
    assert sel["indexes"] == {2, 3, 4}
    # rb 也是回收站
    assert _parse_selection("rb", 3)["recycle"] is True


def test_parse_selection_out_of_range_ignored():
    sel = _parse_selection("1,99,-1,x", 5)
    assert sel["indexes"] == {1}


def test_parse_selection_all_and_none():
    assert _parse_selection("all", 5)["all"] is True
    assert _parse_selection("a", 5)["all"] is True
    assert _parse_selection("0", 5)["none"] is True
    assert _parse_selection("none", 5)["none"] is True
    assert _parse_selection("", 5)["indexes"] == set()
    # none 优先：即使混入数字 / r 也视为清空
    sel = _parse_selection("0 1 r", 5)
    assert sel["none"] is True and sel["indexes"] == set() and sel["recycle"] is False


def test_parse_selection_empty_input():
    sel = _parse_selection("", 5)
    assert sel["indexes"] == set() and sel["all"] is False and sel["recycle"] is False


# ---------------------------------------------------------------------------
# 可选项编号：只给有内容的分类编号
# ---------------------------------------------------------------------------
def _mk_result(key: str, with_target: bool) -> CategoryResult:
    r = CategoryResult(key=key, label=key, risk="safe")
    if with_target:
        import tempfile

        t = Target(
            path=tempfile.gettempdir(),
            kind=TargetKind.DIR,
            action=TargetAction.CLEAR,
            category=key,
            size=1,
        )
        r.targets.append(t)
    return r


def test_selectable_rows_only_content_rows():
    a = _mk_result("a", True)
    b = _mk_result("b", False)
    c = _mk_result("c", True)
    rows = selectable_rows([a, b, c])
    assert rows == [0, 2]  # b(空) 不占编号
    # 编号 1 → a、编号 2 → c，与菜单展示一致


def test_selectable_rows_all_empty():
    rows = selectable_rows([_mk_result("a", False), _mk_result("b", False)])
    assert rows == []


# ---------------------------------------------------------------------------
# 微信数据目录提示（只提示、绝不清理）
# ---------------------------------------------------------------------------
def test_wechat_data_summary_plain():
    env = {
        "wechat": {
            "data_dirs": [
                {
                    "label": "WeixinShuju(4.x)",
                    "path": r"D:\WeixinShuju",
                    "marker": "xwechat_files",
                    "size_bytes": None,  # 未测量
                }
            ]
        }
    }
    lines = wechat_data_summary(env)
    assert len(lines) == 1
    assert "WeixinShuju" in lines[0]
    assert "微信" in lines[0]  # 提示在微信内清理


# ---------------------------------------------------------------------------
# --sort 支持 name_desc（与菜单一致）
# ---------------------------------------------------------------------------
def test_cli_sort_accepts_name_desc():
    from pc_cleaner.cli import _build_parser

    p = _build_parser()
    args = p.parse_args(["--sort", "name_desc"])
    assert args.sort == "name_desc"
    with pytest.raises(SystemExit):
        p.parse_args(["--sort", "bogus"])


# ---------------------------------------------------------------------------
# 预览与删除同源：过滤函数保持“目录不被 ext 过滤、体积/时间阈值生效”
# ---------------------------------------------------------------------------
def test_apply_target_filters_preview_parity(tmp_path):
    from pc_cleaner.menu import _apply_target_filters

    d = tmp_path / "cache"
    d.mkdir()
    f1 = tmp_path / "a.tmp"
    f1.write_bytes(b"x" * 100)
    dir_target = Target(
        path=d, kind=TargetKind.DIR, action=TargetAction.CLEAR,
        category="t", size=1000,
    )
    file_target = Target(
        path=f1, kind=TargetKind.FILE, action=TargetAction.DELETE,
        category="t", size=100,
    )
    # 扩展名过滤不作用于目录目标
    filtered = _apply_target_filters([dir_target, file_target], ext_filter=[".log"])
    assert filtered == [dir_target]
    # 最小体积作用于目录（按内部总字节）
    filtered = _apply_target_filters([dir_target], min_size_bytes=2000)
    assert filtered == []
