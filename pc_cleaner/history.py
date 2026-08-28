"""清理历史 / 审计日志。

借鉴 sifty 的 audit log 与 history 设计：每次真正执行的清理会话都会追加到
``history.json``（结构化）与 ``audit.log``（人类可读），可用 ``--history`` 查看，
并可用 ``--undo-last`` 把最近一次「进回收站」的清理从回收站恢复回来。

历史文件默认放在配置目录下（``%APPDATA%\\pc_cleaner``，可用环境变量
``PC_CLEANER_HOME`` 改到别处，例如工作区）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import audit_path, history_path

#: 最多保留的会话数（防止文件无限膨胀）
MAX_SESSIONS = 50


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_history() -> list[dict[str, Any]]:
    """读取全部历史会话（旧 -> 新）。文件缺失/损坏时返回 []。"""
    p = history_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_history(sessions: list[dict[str, Any]]) -> None:
    """保存历史会话（只保留最近 MAX_SESSIONS 条）。"""
    try:
        history_path().parent.mkdir(parents=True, exist_ok=True)
        history_path().write_text(
            json.dumps(sessions[-MAX_SESSIONS:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def append_session(session: dict[str, Any]) -> None:
    """追加一次会话并落盘。"""
    sessions = load_history()
    sessions.append(session)
    save_history(sessions)


def record_deletion_audit(
    path: Path, size: int, mode: str, freed: int = 0
) -> None:
    """往 audit.log 追加一行人类可读记录（尽力而为，失败不报错）。"""
    try:
        audit_path().parent.mkdir(parents=True, exist_ok=True)
        with audit_path().open("a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] mode={mode} size={size} freed={freed} {path}\n")
    except OSError:
        pass


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
    """构造一个历史会话对象。"""
    return {
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
