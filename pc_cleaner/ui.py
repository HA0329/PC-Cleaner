"""共享 UI 工具：终端输出、确认提示、风险徽标与实时进度显示。

从 cli.py 拆分出来的公共小工具，供 cli / menu / commands 三个模块复用：

- ``_echo`` / ``_echo_err``：标准输出 / 错误输出（**进度、加载、实时状态一律走
  stderr**，避免污染 stdout 上的 ``--json`` 输出与 MCP 协议流）；
- ``prompt_yes_no``：交互确认；
- ``_risk_badge`` / ``_admin_tag``：风险与管理员徽标；
- ``is_elevated``：检测当前进程是否通过 UAC 提权运行（基于环境变量）；
- ``Spinner``：加载指示器（v0.9.7），用于耗时的只读探测；
- ``ScanProgressDisplay``：扫描进度与实时统计（v0.9.7 起支持并行扫描的真实进度、
  已找到目标/字节数、剩余时间估算）；
- ``CleanProgressDisplay``：清理过程的实时状态（v0.9.7），进度条 + 当前目标 +
  已处理/跳过计数 + 已释放/已入回收站字节；
- ``print_startup_banner``：一行启动横幅（版本 / 模式 / 深度 / 线程数）。

所有显示组件都遵循同一条铁律：**非 TTY 或 ``enabled=False`` 时完全静默**，
因此 ``--json``、管道、CI、MCP 场景不会多出任何字节。

安全增强（v0.8.1）：新增 ``is_elevated()``，可在菜单中显示 ``[ADMIN]`` 标识。
v0.9.6：进度行追加 ``\\x1b[K`` 清除行尾残留。
v0.9.7：新增 Spinner / CleanProgressDisplay，扫描进度支持并行与实时统计。
"""

from __future__ import annotations

import os  # 用于读取环境变量
import re
import sys
import threading
import time
from typing import Any, TextIO

from .console import (
    _ANSI,
    bold,
    cyan,
    dim,
    display_width,
    get_terminal_width,
    green,
    progress_bar,
    red,
    truncate_path,
    yellow,
)
from .models import format_size

# v0.9.6：行尾清除序列。进度行用 \r 原地刷新，但若新行比上一行短，
# 上一行的尾巴会残留（例如「扫描完成」比最后一个进度行短，出现
# "耗时 0.0s存(系统账户/下载中断/媒体应用) 27/27 (0.0s)" 的串行乱码）。
# 在内容后追加 \x1b[K 可把光标右侧残留一并清掉；ANSI 不可用时为空串。
_ERASE_TO_EOL = "\x1b[K" if _ANSI else ""

#: 剥离 ANSI 转义序列（计算显示宽度时要忽略它们）
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _split_target(msg: str) -> tuple[str, str, str]:
    """把目标描述拆成 ``(动作前缀, 路径主体, 尾注)``。

    engine 的 ``Target.describe()`` 形如 ``[目录-清空] C:\\path (12 个文件, 80.5 MB)``；
    拆开之后可以只截断路径、保留尾注（空间不足时再丢尾注），避免出现
    ``C:\\Use..., 80.5 MB)`` 这种半截括号。
    """
    op_tag = ""
    rest = msg
    if "] " in msg:
        op_tag, rest = msg.split("] ", 1)
        op_tag += "] "
    base, sep, anno = rest.partition(" (")
    return op_tag, base, (f" ({anno}" if sep else "")


def _display_len(text: str) -> int:
    """按显示宽度计算长度（ANSI 转义序列不计入，东亚全角按 2 计）。"""
    return display_width(_ANSI_RE.sub("", text))


# ===========================================================================
# 输出与终端能力
# ===========================================================================
def _echo(*args, **kwargs) -> None:
    """向 stdout 打印信息（常规输出）。"""
    print(*args, **kwargs)


def _echo_err(*args, **kwargs) -> None:
    """向 stderr 打印信息（进度、加载、警告、错误）。"""
    print(*args, file=sys.stderr, **kwargs)


def _is_tty(stream: TextIO) -> bool:
    """流是否为交互式终端（不可判断时按 False 处理）。"""
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 部分流/测试替身没有 isatty
        return False


def _supports_unicode(stream: TextIO) -> bool:
    """流能否安全输出 Unicode 符号（Braille 加载帧、█ 进度块）。

    无法判断编码时按"不支持"处理，回退 ASCII，避免在 cp936 控制台抛
    ``UnicodeEncodeError`` 或显示成方块。
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        "⠋█✓".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _format_duration(seconds: float) -> str:
    """把秒数格式化成紧凑字符串（<1s 用毫秒，便于显示"确实干活了"）。"""
    if seconds < 0.001:
        return "<1ms"
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def _refresh(stream: TextIO, line: str, last: str) -> str:
    """原地刷新一行（内容变化时才写，减少闪烁并清掉行尾残留）。"""
    if line == last:
        return last
    stream.write("\r" + line + _ERASE_TO_EOL)
    stream.flush()
    return line


# ===========================================================================
# 交互确认与徽标
# ===========================================================================
def prompt_yes_no(
    question: str,
    default: bool = False,
    require_typed: bool = False,
) -> bool:
    """交互式确认：询问是/否，返回 bool 值。

    Args:
        question: 提示问题
        default: 默认返回值（当用户直接按回车时）
        require_typed: v0.9.8 新增。为 True 时**不接受**单个 ``y``，必须完整输入
            ``yes`` / ``是`` 才视为确认。用于不可恢复的危险操作（永久删除、
            清空回收站、高风险分类），与 ``--yes`` 分支的二次闸门保持同一标准，
            避免用户习惯性连按回车/``y`` 就永久删掉数据。

    Returns:
        bool: True 表示是，False 表示否
    """
    if require_typed:
        while True:
            try:
                raw = input(f"{question} [输入 yes 继续 / 其它取消] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if raw in ("yes", "是"):
                return True
            # 危险操作：除完整 yes 外一律视为取消，且不再循环追问
            # （避免"输入错误→再问一次→用户乱按"反而误确认）
            return False

    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{question} [{hint}] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if raw == "":
            return default
        if raw in ("y", "yes", "是"):
            return True
        if raw in ("n", "no", "否"):
            return False
        _echo("请输入 y 或 n。")


def _risk_badge(risk: str) -> str:
    """返回风险等级的彩色徽标。

    - safe:     绿色 ●
    - moderate: 黄色 ●
    - risky:    红色 ●
    """
    if risk == "safe":
        return green("●")
    if risk == "moderate":
        return yellow("●")
    return red("●")


def _admin_tag(requires_admin: bool) -> str:
    """返回管理员权限标记（如果分类需要管理员权限）。"""
    return yellow(" [需管理员]") if requires_admin else ""


def is_elevated() -> bool:
    """检测当前进程是否通过 UAC 提权运行（基于环境变量）。

    该函数由 commands._relaunch_as_admin 在提权成功后设置
    PC_CLEANER_ELEVATED=1，新进程可通过此函数检测，从而在
    交互菜单中显示 [ADMIN] 标识，提醒用户当前为高权限模式。

    Returns:
        bool: 如果环境变量 PC_CLEANER_ELEVATED 为 "1" 则返回 True，否则 False。
    """
    return os.environ.get("PC_CLEANER_ELEVATED", "0") == "1"


# ===========================================================================
# 启动横幅
# ===========================================================================
def print_startup_banner(
    *,
    version: str,
    mode: str,
    workers: int = 0,
    depth: int = 20,
    deep: bool = False,
    enabled: bool = True,
    stream: TextIO | None = None,
) -> None:
    """打印一行启动横幅：版本 / 删除模式 / 遍历深度 / 扫描线程。

    只在 stderr 是 TTY 时输出（``--json`` / 管道 / MCP 下完全静默）。
    """
    stream = stream if stream is not None else sys.stderr
    if not (enabled and _is_tty(stream)):
        return
    mode_txt = "回收站" if mode == "recycle" else "永久删除"
    workers_txt = f"{workers} 线程" if workers and workers > 1 else "串行"
    deep_txt = " · 深度模式" if deep else ""
    stream.write(
        f"  {bold(f'PC Junk Cleaner {version}')} "
        f"{dim(f'· {mode_txt} · 深度 {depth}{deep_txt} · {workers_txt}')}\n"
    )
    stream.flush()


# ===========================================================================
# 加载指示器（Spinner）
# ===========================================================================
class Spinner:
    """stderr 上的"加载中"指示器（TTY 限定；非 TTY / enabled=False 时完全静默）。

    用于耗时的**只读**探测（运行环境探测、组件库体积统计等），让用户知道程序
    没有卡死。后台线程每 ``interval`` 秒重绘一帧，``stop()`` 时清掉该行并可打印
    一行完成信息。

    用法::

        with Spinner("正在检测运行环境") as sp:
            env = probe_environment()
            sp.update("正在统计组件库体积")

    线程安全：``update()`` 与 ``stop()`` 可从任意线程调用。
    """

    _UNICODE_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _ASCII_FRAMES = "|/-\\"

    def __init__(
        self,
        label: str = "",
        enabled: bool = True,
        stream: TextIO | None = None,
        interval: float = 0.1,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled) and _is_tty(self._stream)
        self._label = label
        self._interval = max(0.05, float(interval))
        self._frames = (
            self._UNICODE_FRAMES
            if _supports_unicode(self._stream)
            else self._ASCII_FRAMES
        )
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time = 0.0
        self._frame_index = 0
        self._last_line = ""
        self._stopped = False

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> "Spinner":
        """启动指示器（重复调用无副作用）。"""
        if not self.enabled or self._thread is not None:
            return self
        self._start_time = time.time()
        self._stop_event.clear()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="pc-cleaner-spinner", daemon=True
        )
        self._thread.start()
        return self

    def update(self, label: str) -> "Spinner":
        """更新提示文案（下一次重绘生效）。"""
        with self._lock:
            self._label = label
        return self

    def stop(self, done: str | None = None) -> None:
        """停止指示器；``done`` 非空时在其位置打印一行完成信息。

        幂等：重复调用不会再写任何字节。
        """
        if not self.enabled or self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        # 清掉当前行，再按需打印完成信息
        self._stream.write("\r" + _ERASE_TO_EOL)
        self._stream.flush()
        self._last_line = ""
        if done:
            self._stream.write(f"  {done}\n")
            self._stream.flush()

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, *_exc: Any) -> bool:
        self.stop()
        return False

    # -- 内部 --------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            with self._lock:
                label = self._label
            frame = self._frames[self._frame_index % len(self._frames)]
            self._frame_index += 1
            elapsed = time.time() - self._start_time
            self._last_line = _refresh(
                self._stream,
                f"  {cyan(frame)} {label} {dim(f'… {_format_duration(elapsed)}')}",
                self._last_line,
            )


# ===========================================================================
# 扫描进度
# ===========================================================================
class ScanProgressDisplay:
    """扫描进度的终端显示控制器（含实时统计与剩余时间估算）。

    用法::

        progress = ScanProgressDisplay(enabled=show_progress)
        results = scan_all(specs, on_progress=progress,
                           on_category_done=progress.category_done)
        progress.finish(results)

    当 ``enabled=False`` 或流不是 TTY 时自动静默，避免污染脚本输出。

    v0.9.7 增强：

    - **实时统计**：已找到的目标数与可释放字节数随分类完成滚动累加；
    - **剩余时间估算**：按已完成分类的平均耗时推算，避免"卡住了吗"的焦虑；
    - **并行扫描真实进度**：配合 ``scan_all(on_category_done=...)``，
      多线程扫描时也能逐个分类刷新（此前要等全部扫完才一次性回放）；
    - **可注入流**：``stream`` 便于测试（默认 stderr）。
    """

    def __init__(
        self,
        enabled: bool = True,
        stream: TextIO | None = None,
        total_categories: int = 0,
    ) -> None:
        """初始化进度显示器。

        Args:
            enabled: 是否启用进度显示（通常根据命令行 --no-progress 决定）
            stream: 输出流，默认 stderr
            total_categories: 分类总数（用于首帧就显示 "0/N"，可省略）
        """
        self._stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled) and _is_tty(self._stream)
        self._unicode = _supports_unicode(self._stream)
        self._start_time: float | None = None
        self._last_update: str = ""
        self._found_targets = 0
        self._found_bytes = 0
        self._total_categories = max(0, int(total_categories))
        self._last_elapsed = 0.0

    # -- 引擎回调 ----------------------------------------------------------
    def __call__(self, category_label: str, current: int, total: int) -> None:
        """更新扫描进度（每开始/完成一个分类时调用）。

        Args:
            category_label: 当前正在扫描的分类名称
            current: 当前分类索引（从1开始）
            total: 分类总数
        """
        if not self.enabled:
            return
        if self._start_time is None:
            self._start_time = time.time()
            self._stream.write("\n")  # 空行开始，避免覆盖已有输出
            self._stream.flush()

        elapsed = time.time() - self._start_time
        self._last_elapsed = elapsed
        bar = progress_bar(current, total, width=25)
        icon = "🔍" if self._unicode else "*"
        parts = [
            f"\r  {dim(f'{icon} 扫描中')} {bar} {cyan(category_label)}",
            dim(f"{current}/{total}"),
        ]
        if self._found_targets:
            parts.append(
                f"{green(str(self._found_targets))} 项 / "
                f"{green(format_size(self._found_bytes))}"
            )
        eta = self._eta(current, total, elapsed)
        if eta:
            parts.append(dim(f"剩约 {eta}"))
        self._last_update = _refresh(
            self._stream, " · ".join(parts), self._last_update
        )

    def category_done(self, result: Any) -> None:
        """某个分类扫描完成时累加实时统计（配合 ``scan_all(on_category_done=)``）。"""
        if not self.enabled:
            return
        try:
            self._found_targets += len(getattr(result, "targets", []) or [])
            self._found_bytes += int(getattr(result, "liberatable", 0) or 0)
        except Exception:  # noqa: BLE001 统计失败不影响扫描
            return

    def _eta(self, current: int, total: int, elapsed: float) -> str:
        """按已完成分类的平均耗时估算剩余时间（样本太少时返回空串）。"""
        if current <= 0 or total <= current or elapsed < 0.05:
            return ""
        per_category = elapsed / current
        remaining = per_category * (total - current)
        if remaining < 0.5:
            return ""
        return _format_duration(remaining)

    def finish(self, results: list) -> None:
        """扫描完成时调用，输出最终汇总信息。

        Args:
            results: CategoryResult 列表（扫描结果）
        """
        if not self.enabled:
            return
        elapsed = time.time() - self._start_time if self._start_time else 0
        total_targets = sum(len(r.targets) for r in results)
        total_size = sum(r.liberatable for r in results)
        mark = "✓" if self._unicode else "+"
        self._stream.write(
            f"\r  {green(mark)} 扫描完成：找到 {bold(str(total_targets))} 个目标，"
            f"可释放 {green(format_size(total_size))}，"
            f"耗时 {dim(_format_duration(elapsed))}{_ERASE_TO_EOL}\n"
        )
        self._stream.flush()


# ===========================================================================
# 清理实时状态
# ===========================================================================
class CleanProgressDisplay:
    """清理过程的实时状态显示（stderr、TTY 限定）。

    - ``__call__(i, total, msg)``：与 engine 的 ``ProgressCB`` 签名兼容，
      显示进度条 + 当前目标 + 已处理/跳过计数；
    - ``record(path, size, mode_name, freed)``：可直接串在 audit 回调上，
      累计"已真正释放"与"已移入回收站"的字节数；
    - ``finish(result)``：输出收尾汇总行（含失败/跳过/占用跳过）。

    非 TTY 或 ``enabled=False`` 时完全静默。
    """

    def __init__(
        self,
        enabled: bool = True,
        stream: TextIO | None = None,
        mode: str = "recycle",
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled) and _is_tty(self._stream)
        self._unicode = _supports_unicode(self._stream)
        self._mode = mode
        self._last_line = ""
        self._start_time = time.time()
        self.deleted = 0
        self.failed = 0
        self.freed_bytes = 0
        self.recycled_bytes = 0
        self._current = 0
        self._total = 0

    # -- 引擎回调 ----------------------------------------------------------
    def __call__(self, i: int, total: int, msg: str) -> None:
        """每个目标处理完调用一次（engine.delete_targets 的 on_progress）。"""
        if not self.enabled:
            return
        self._current = i
        self._total = total
        if msg.startswith("[跳过]") or msg.startswith("[错误]"):
            self.failed += 1
        else:
            self.deleted += 1

        term_w = get_terminal_width()
        # 进度条取 14 列（而非 18）：窄终端下要给"当前目标 + 计数 + 字节数"留空间
        bar = progress_bar(i, total, width=14)
        icon = "🧹" if self._unicode else "-"
        prefix = f"\r  {dim(f'{icon} 清理中')} {bar} "

        counter = f"{self.deleted} 完成"
        if self.failed:
            counter += f"/{self.failed} 跳过"
        counter_txt = f" · {counter}"
        amount_txt = ""
        if self.freed_bytes or self.recycled_bytes:
            amount = self.freed_bytes or self.recycled_bytes
            verb = "已释放" if self.freed_bytes else "已入回收站"
            amount_txt = f" · {verb} {green(format_size(amount))}"

        # 优先级：当前目标 > 字节数 > 计数。逐级尝试塞进终端宽度，绝不换行、
        # 也绝不留下半截路径（窄终端上先丢字节数，再丢当前目标）。
        limit = max(term_w - 1, 40)
        op_tag, base, anno = _split_target(msg)
        target_txt = self._fit_target(op_tag, base, anno, limit, prefix, counter_txt, amount_txt)
        if target_txt:
            if _display_len(prefix) + _display_len(target_txt) + _display_len(counter_txt) + _display_len(amount_txt) <= limit:
                line = f"{prefix}{target_txt}{counter_txt}{amount_txt}"
            else:
                line = f"{prefix}{target_txt}{counter_txt}"
        elif _display_len(prefix) + _display_len(counter_txt) + _display_len(amount_txt) <= limit:
            line = f"{prefix}{counter_txt}{amount_txt}"
        else:
            line = f"{prefix}{counter_txt}"

        self._last_line = _refresh(self._stream, line, self._last_line)

    @staticmethod
    def _fit_target(
        op_tag: str, base: str, anno: str, limit: int, prefix: str, counter_txt: str, amount_txt: str
    ) -> str:
        """在剩余宽度里放"当前目标"；放不下（< 20 列）返回空串。

        优先连字节数一起放；放不下就只放目标 + 计数。
        """
        room_with_amount = (
            limit - _display_len(prefix) - _display_len(counter_txt) - _display_len(amount_txt)
        )
        room_without_amount = limit - _display_len(prefix) - _display_len(counter_txt)
        for room in (room_with_amount, room_without_amount):
            budget = room - _display_len(op_tag)
            if budget < 14:
                continue
            if anno and _display_len(anno) + 12 <= budget:
                return op_tag + truncate_path(base, budget - _display_len(anno)) + anno
            return op_tag + truncate_path(base, budget)
        return ""

    def record(self, path: Any, size: int, mode_name: str, freed: int = 0) -> None:
        """audit 回调：累计已释放/已入回收站的字节数。"""
        if not self.enabled:
            return
        try:
            if mode_name == "recycle":
                self.recycled_bytes += int(size or 0)
            else:
                self.freed_bytes += int(freed or 0)
        except (TypeError, ValueError):
            return

    def finish(self, result: dict[str, Any] | None = None) -> None:
        """收尾：清行并输出一行汇总（result 为 engine 返回的计数字典）。"""
        if not self.enabled:
            return
        self._stream.write("\r" + _ERASE_TO_EOL)
        self._stream.flush()
        self._last_line = ""
        if not result:
            return
        parts = [
            f"完成：删除 {green(str(result.get('deleted', 0)))} 项",
            f"跳过/失败 {yellow(str(result.get('failed', 0)))} 项",
        ]
        if result.get("skipped"):
            parts.append(f"部分清理 {yellow(str(result['skipped']))} 项")
        if result.get("skipped_in_use"):
            parts.append(f"占用跳过 {yellow(str(result['skipped_in_use']))} 项")
        if result.get("freed"):
            parts.append(f"释放 {green(format_size(result['freed']))}")
        if result.get("recycled"):
            parts.append(f"入回收站 {green(format_size(result['recycled']))}")
        elapsed = _format_duration(time.time() - self._start_time)
        self._stream.write(f"  {dim('·')} " + "，".join(parts) + dim(f"（耗时 {elapsed}）") + "\n")
        self._stream.flush()
