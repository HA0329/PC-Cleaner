"""v0.9.7 启动显示 / 加载指示 / 实时状态的单元测试。

全部使用**假的 TTY 流**（``io.StringIO`` 子类覆写 ``isatty``），因此不依赖真实终端，
也绝不会往 stdout 写东西。重点验证三件事：

1. 非 TTY / ``enabled=False`` 时**完全静默**（自动化场景不能多出任何字节）；
2. 启用时能正确渲染进度条、实时统计与剩余时间；
3. 收尾时清行，不残留半截内容。
"""

from __future__ import annotations

import io
import re
import time

import pytest

from pc_cleaner import ui
from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind
from pc_cleaner.ui import (
    CleanProgressDisplay,
    ScanProgressDisplay,
    Spinner,
    _format_duration,
    print_startup_banner,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class FakeTTY(io.StringIO):
    """最小 TTY 替身：isatty() 为真、带 encoding。"""

    encoding = "utf-8"

    def isatty(self) -> bool:  # noqa: D102
        return True


class FakePipe(io.StringIO):
    """非 TTY 替身（管道 / 重定向）。"""

    encoding = "utf-8"

    def isatty(self) -> bool:  # noqa: D102
        return False


def _plain(stream: io.StringIO) -> str:
    """去掉 ANSI 与 \r，便于断言内容。"""
    return ANSI_RE.sub("", stream.getvalue()).replace("\r", "")


def _category(key: str = "system_temp", n_targets: int = 2, size: int = 1024) -> CategoryResult:
    return CategoryResult(
        key=key,
        label=f"分类-{key}",
        risk="safe",
        scanned=True,
        targets=[
            Target(
                path=__import__("pathlib").Path(f"C:/tmp/{key}/{i}.bin"),
                kind=TargetKind.FILE,
                action=TargetAction.DELETE,
                category=key,
                size=size,
            )
            for i in range(n_targets)
        ],
    )


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0005, "<1ms"),
        (0.043, "43ms"),
        (0.5, "500ms"),
        (1.25, "1.2s"),
        (9.94, "9.9s"),
        (61.0, "1m01s"),
    ],
)
def test_format_duration(seconds, expected):
    assert _format_duration(seconds) == expected


def test_is_tty_and_unicode_detection():
    assert ui._is_tty(FakeTTY()) is True
    assert ui._is_tty(FakePipe()) is False
    assert ui._is_tty(io.StringIO()) is False  # 没有 isatty 的替身
    assert ui._supports_unicode(FakeTTY()) is True
    assert ui._supports_unicode(io.StringIO()) is False


# ---------------------------------------------------------------------------
# Spinner（加载指示器）
# ---------------------------------------------------------------------------
def test_spinner_silent_when_disabled():
    out = FakeTTY()
    sp = Spinner("测试", enabled=False, stream=out, interval=0.05)
    sp.start()
    time.sleep(0.15)
    sp.stop("完成")
    assert out.getvalue() == ""


def test_spinner_silent_on_non_tty():
    out = FakePipe()
    with Spinner("测试", stream=out, interval=0.05):
        time.sleep(0.15)
    assert out.getvalue() == ""


def test_spinner_renders_frames_and_clears():
    out = FakeTTY()
    with Spinner("正在探测", stream=out, interval=0.05) as sp:
        time.sleep(0.2)
        sp.update("正在统计体积")
        time.sleep(0.15)
    text = out.getvalue()
    assert "\r" in text            # 原地刷新
    assert not text.endswith("\n")  # 收尾清行，不留换行
    plain = _plain(out)
    assert "正在探测" in plain      # 启动文案
    assert "正在统计体积" in plain  # update() 生效


def test_spinner_stop_prints_done_line():
    out = FakeTTY()
    sp = Spinner("x", stream=out, interval=0.05).start()
    time.sleep(0.1)
    sp.stop("环境探测完成")
    assert "环境探测完成" in _plain(out)
    assert out.getvalue().rstrip().endswith("环境探测完成")


def test_spinner_stop_is_idempotent():
    out = FakeTTY()
    sp = Spinner("x", stream=out, interval=0.05).start()
    sp.stop()
    before = out.getvalue()
    sp.stop()
    assert out.getvalue() == before


# ---------------------------------------------------------------------------
# 扫描进度
# ---------------------------------------------------------------------------
def test_scan_progress_silent_when_disabled():
    out = FakeTTY()
    p = ScanProgressDisplay(enabled=False, stream=out)
    p("系统临时文件", 1, 3)
    p.category_done(_category())
    p.finish([_category()])
    assert out.getvalue() == ""


def test_scan_progress_silent_on_non_tty():
    out = FakePipe()
    p = ScanProgressDisplay(stream=out)
    p("系统临时文件", 1, 3)
    p.finish([_category()])
    assert out.getvalue() == ""


def test_scan_progress_renders_live_stats_and_eta():
    out = FakeTTY()
    p = ScanProgressDisplay(stream=out)
    p.category_done(_category(n_targets=3, size=2048))  # 先累加一些统计
    p("系统临时文件", 1, 8)  # 首帧启动计时
    time.sleep(0.3)  # 让 ETA 有足够样本（elapsed/current*(total-current) > 0.5s）
    p("系统日志", 2, 8)
    text = _plain(out)
    assert "扫描中" in text
    assert "2/8" in text
    assert "3 项" in text                 # 实时目标数
    assert "6.00 KB" in text              # 实时体积（3 × 2048 B）
    assert "剩约" in text                  # ETA


def test_scan_progress_finish_summary():
    out = FakeTTY()
    p = ScanProgressDisplay(stream=out)
    p("a", 1, 2)
    p.finish([_category(n_targets=2, size=1024), _category("gpu_caches", 1, 512)])
    text = _plain(out)
    assert "扫描完成" in text
    assert "3 个目标" in text
    assert "2.50 KB" in text   # 2×1024 + 1×512
    assert "耗时" in text


def test_scan_progress_no_eta_when_too_fast():
    out = FakeTTY()
    p = ScanProgressDisplay(stream=out)
    p("a", 1, 10)  # 立即调用，elapsed < 0.05s
    assert "剩约" not in _plain(out)


# ---------------------------------------------------------------------------
# 清理实时状态
# ---------------------------------------------------------------------------
def test_clean_progress_silent_when_disabled():
    out = FakeTTY()
    c = CleanProgressDisplay(enabled=False, stream=out)
    c(1, 2, "C:/tmp/a.bin")
    c.record("C:/tmp/a.bin", 10, "permanent", 10)
    c.finish({"deleted": 1})
    assert out.getvalue() == ""


def test_clean_progress_counts_and_renders(monkeypatch):
    monkeypatch.setattr(ui, "get_terminal_width", lambda: 140)  # 宽终端：目标与字节数都能放
    out = FakeTTY()
    c = CleanProgressDisplay(stream=out, mode="permanent")
    c(1, 3, "[目录-清空] C:/tmp/cache (2 个文件, 1.00 KB)")
    c(2, 3, "[跳过] C:/tmp/locked.bin (被占用)")
    c.record("C:/tmp/cache", 1024, "permanent", 1024)
    c(3, 3, "[文件] C:/tmp/x.bin (512 B)")
    text = _plain(out)
    assert "清理中" in text
    assert "2 完成" in text          # 两次成功
    assert "1 跳过" in text          # 一次跳过
    assert "已释放 1.00 KB" in text  # record 累计
    assert "C:/tmp/locked.bin" in text  # 当前目标（含尾注）
    assert c.deleted == 2 and c.failed == 1 and c.freed_bytes == 1024


def test_clean_progress_recycle_uses_recycled_label(monkeypatch):
    monkeypatch.setattr(ui, "get_terminal_width", lambda: 140)
    out = FakeTTY()
    c = CleanProgressDisplay(stream=out, mode="recycle")
    c(1, 2, "C:/tmp/a.bin")
    c.record("C:/tmp/a.bin", 2048, "recycle", 0)
    c(2, 2, "C:/tmp/b.bin")  # 下一次渲染带上累计字节
    assert "已入回收站 2.00 KB" in _plain(out)
    assert c.recycled_bytes == 2048 and c.freed_bytes == 0


def test_clean_progress_narrow_terminal_keeps_target_within_width(monkeypatch):
    """窄终端（80 列）下不换行、不出现半截路径；当前目标优先于字节数。"""
    monkeypatch.setattr(ui, "get_terminal_width", lambda: 80)
    out = FakeTTY()
    c = CleanProgressDisplay(stream=out, mode="recycle")
    long_path = "C:/Users/Administrator.DESKTOP-B166QN2/AppData/Local/Temp/xwechat/radium/users/a/b/c.bin"
    c(1, 3, f"[文件] {long_path} (12 个文件, 80.50 MB)")
    c.record(long_path, 80 * 1024 * 1024, "recycle", 0)
    c(2, 3, f"[文件] {long_path} (12 个文件, 80.50 MB)")
    raw = ANSI_RE.sub("", out.getvalue())
    frames = [f for f in raw.split("\r") if "清理中" in f]
    assert frames
    for frame in frames:
        assert len(frame) <= 80, frame          # 不换行
    assert "C:/Users" in frames[-1]             # 目标优先保留
    assert "2 完成" in frames[-1]


def test_clean_progress_finish_summary_and_clear():
    out = FakeTTY()
    c = CleanProgressDisplay(stream=out)
    c(1, 1, "C:/tmp/a.bin")
    c.finish(
        {"deleted": 1, "failed": 2, "skipped": 3, "skipped_in_use": 4, "freed": 1024, "recycled": 0}
    )
    text = _plain(out)
    assert "完成：删除 1 项" in text
    assert "跳过/失败 2 项" in text
    assert "部分清理 3 项" in text
    assert "占用跳过 4 项" in text
    assert "释放 1.00 KB" in text


def test_clean_progress_record_ignores_bad_values():
    out = FakeTTY()
    c = CleanProgressDisplay(stream=out)
    c.record("x", None, "permanent", None)  # 不应抛异常
    assert c.freed_bytes == 0


# ---------------------------------------------------------------------------
# 启动横幅
# ---------------------------------------------------------------------------
def test_startup_banner_content():
    out = FakeTTY()
    print_startup_banner(
        version="0.9.7", mode="recycle", workers=4, depth=20, deep=False, stream=out
    )
    text = _plain(out)
    assert "PC Junk Cleaner 0.9.7" in text
    assert "回收站" in text and "4 线程" in text and "深度 20" in text
    assert out.getvalue().endswith("\n")


def test_startup_banner_silent_when_disabled_or_pipe():
    tty_out = FakeTTY()
    print_startup_banner(version="0.9.7", mode="permanent", enabled=False, stream=tty_out)
    assert tty_out.getvalue() == ""
    pipe_out = FakePipe()
    print_startup_banner(version="0.9.7", mode="permanent", stream=pipe_out)
    assert pipe_out.getvalue() == ""


def test_startup_banner_deep_and_serial():
    out = FakeTTY()
    print_startup_banner(
        version="0.9.7", mode="permanent", workers=1, depth=50, deep=True, stream=out
    )
    text = _plain(out)
    assert "永久删除" in text and "串行" in text and "深度模式" in text
