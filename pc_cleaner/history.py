"""清理历史 / 审计日志。

借鉴 sifty 的 audit log 与 history 设计：每次真正执行的清理会话都会追加到
``history.json``（结构化）与 ``audit.log``（人类可读），可用 ``--history`` 查看，
并可用 ``--undo-last`` 把最近一次「进回收站」的清理从回收站恢复回来。

历史文件默认放在配置目录下（``%APPDATA%\\pc_cleaner``，可用环境变量
``PC_CLEANER_HOME`` 改到别处，例如工作区）。

v0.9.3 可靠性增强：

- **原子写**：先写 ``*.tmp-<pid>`` 再 ``os.replace`` 替换，避免中途崩溃/断电留下
  半截 JSON（此前 ``load_history`` 会静默当成"没有历史"，撤销能力一次性丢失）；
- **损坏保护**：解析失败时把文件改名 ``*.corrupt-<时间戳>`` 保留证据，
  不再静默丢弃；
- **跨进程互斥**：读-改-写期间用 ``msvcrt.locking`` 锁住 ``history.lock``
  （进程退出由内核自动释放，无死锁风险），避免用户与 Agent 并发写导致会话丢失。

v0.9.11 修复（**删除成功却写不进撤销记录**）：

- 文件名含**孤立代理对**（Windows 允许任意 UTF-16 文件名，未配对的代理码元是
  合法文件名）时，``save_history`` / ``record_deletion_audit`` 的 utf-8 写入会抛
  ``UnicodeEncodeError``。它是 ``ValueError`` 子类而非 ``OSError``，因此穿透了
  原有的 ``except OSError``，导致：文件已删但 ``history.json`` 没写出来
  （``--undo-last`` 永久失效）、``history.json.tmp-<pid>`` 残留、
  且该目标被 ``delete_targets`` 兜底误报为 ``failed``。
  现在统一用 ``errors="backslashreplace"`` 把代理码元写成 JSON 合法的
  ``\\udfff`` 转义（可无损往返），并按"尽力而为"的既有契约放宽异常范围。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from .config import audit_path, history_path

#: 最多保留的会话数（防止文件无限膨胀）
MAX_SESSIONS = 50


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@contextlib.contextmanager
def _history_lock() -> Iterator[None]:
    """跨进程互斥（尽力而为：拿不到锁也继续执行，绝不阻塞清理流程）。

    Windows 用 ``msvcrt.locking``（进程退出由内核自动释放，不会留下死锁）；
    其它平台退化为无锁。锁文件为配置目录下的 ``history.lock``。
    """
    lock_path = history_path().with_name("history.lock")
    fd: int | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                # 已被其它进程持有：不等待，直接无锁继续
                os.close(fd)
                fd = None
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def load_history() -> list[dict[str, Any]]:
    """读取全部历史会话（旧 -> 新）。文件缺失/损坏时返回 ``[]``。

    损坏的文件会被改名保留（``*.corrupt-<时间戳>``）而不是静默丢弃，
    这样"撤销历史丢失"至少留有证据。
    """
    p = history_path()
    try:
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        try:
            p.rename(p.with_name(f"{p.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return []
    except OSError:
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def save_history(sessions: list[dict[str, Any]]) -> None:
    """保存历史会话（原子写 + 只保留最近 ``MAX_SESSIONS`` 条）。

    先写同目录临时文件再 ``os.replace`` 原子替换，避免写入中途失败留下
    无法解析的半截 JSON。

    v0.9.11 修复（**删除成功却写不进撤销记录**）：
    此前 ``tmp.write_text(..., encoding="utf-8")`` 对含**孤立代理对**的文件名会抛
    ``UnicodeEncodeError``。Windows 文件名是任意 UTF-16，未配对的代理码元是**合法
    文件名**（Python 用 ``\\udXXX`` 表示），因此这不是理论边界。实测：清理一个名为
    ``cache\\udfffentry.tmp`` 的文件时，文件**被真的删掉了**，但
    ``history.json`` 根本没写出来 → ``--undo-last`` 永远找不回。
    更糟的是两处叠加：
      1. ``UnicodeEncodeError`` 是 ``ValueError`` 子类，**不是 ``OSError``**，
         所以它穿透了本函数的 ``except OSError``；
      2. 异常抛在 ``os.replace`` **之前**，于是 ``history.json.tmp-<pid>`` 残留下来。
    异常继续上抛到 ``engine.delete_targets`` 的兜底 ``except Exception``，
    该目标被误报成 ``failed``——而它其实已经删了。

    现在两处一起修：写入用 ``errors="backslashreplace"``，把无法编码的代理码元写成
    JSON 合法的 ``\\udfff`` 转义（实测逐字符无损往返，且文件在代理码元区域是纯 ASCII，
    ``load_history`` 的 utf-8 读取不受影响；中文等正常文本仍按 ``ensure_ascii=False``
    原样输出，可读性不变）；失败时清理临时文件，并且本函数按既有契约"尽力而为"——
    持久化失败绝不能中断删除流程或污染删除计数。
    """
    path = history_path()
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(sessions[-MAX_SESSIONS:], ensure_ascii=False, indent=2)
        tmp.write_text(data, encoding="utf-8", errors="backslashreplace")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 尽力而为：任何持久化失败都不得影响删除本身
        with contextlib.suppress(OSError):
            tmp.unlink()  # 不留 .tmp-<pid> 垃圾（此前 UnicodeEncodeError 会留下）


def append_session(session: dict[str, Any]) -> None:
    """追加一次会话并落盘（读-改-写全程持有跨进程锁）。"""
    with _history_lock():
        sessions = load_history()
        sessions.append(session)
        save_history(sessions)


def record_deletion_audit(
    path: Path, size: int, mode: str, freed: int = 0
) -> None:
    """往 audit.log 追加一行人类可读记录（尽力而为，失败不报错）。

    v0.9.11：与 :func:`save_history` 同一处代理对缺陷。此前 docstring 写着
    "尽力而为，失败不报错"，实现却只捕获 ``OSError`` —— 含孤立代理对的文件名
    抛出的 ``UnicodeEncodeError``（``ValueError`` 子类）会直接穿透，把整条删除流程
    炸成"失败"。现在改用 ``errors="backslashreplace"``（代理码元写成 ``\\udfff``
    转义，日志仍是纯文本可读），并按 docstring 的承诺把异常范围放宽。
    """
    try:
        audit_path().parent.mkdir(parents=True, exist_ok=True)
        with audit_path().open("a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(f"[{_now_iso()}] mode={mode} size={size} freed={freed} {path}\n")
    except Exception:  # noqa: BLE001 尽力而为，失败不报错（见 docstring）
        pass


def _new_session_id() -> str:
    """生成会话 id（时间 + pid，足够唯一且可读）。"""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def make_session(
    *,
    mode: str,
    deleted: int,
    failed: int,
    freed: int,
    categories: list[str],
    targets: list[dict[str, Any]],
    note: str = "",
) -> dict[str, Any]:
    """构造一个历史会话对象（v0.9.4 起带稳定 ``session_id``）。

    ``session_id`` 供 ``--undo --session <id>`` 与 MCP 的 ``undo`` 精确定位；
    旧版本写入的会话没有该字段，调用方需按 ``ts`` 回退匹配。
    """
    return {
        "session_id": _new_session_id(),
        "ts": _now_iso(),
        "mode": mode,
        "deleted": deleted,
        "failed": failed,
        "freed": freed,
        "categories": sorted(set(categories)),
        "targets": targets,
        "note": note,
    }


def last_session() -> dict[str, Any] | None:
    """返回最近一次会话（无则 None）。"""
    sessions = load_history()
    return sessions[-1] if sessions else None
