"""删除引擎。

职责：
- 把一批 Target 执行删除，支持「进入回收站」或「永久删除」；
- 目录目标按 action 处理：清空内容（保留目录）或删除整个目录；
- 逐项容错：文件被占用/无权限时跳过并继续；
- 清空 Windows 回收站（通过 shell32.SHEmptyRecycleBinW）；
- 提供简单的进度回调。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

from .models import CleanMode, Target, TargetAction, TargetKind, format_size

try:  # 可选依赖：删除到回收站
    import send2trash  # type: ignore

    HAS_SEND2TRASH = True
except Exception:  # noqa: BLE001
    send2trash = None
    HAS_SEND2TRASH = False


def recycle_available() -> bool:
    """是否支持删除到回收站。"""
    return HAS_SEND2TRASH


ProgressCB = Callable[[int, int, str], None]


def _delete_path(path: Path, mode: CleanMode, is_dir: bool) -> None:
    """删除单个文件或目录。"""
    if mode is CleanMode.RECYCLE and HAS_SEND2TRASH:
        try:
            send2trash.send2trash(str(path))
            return
        except Exception:  # noqa: BLE001
            # 进回收站失败则回退到永久删除（并告知调用方）
            _delete_path(path, CleanMode.PERMANENT, is_dir)
            return
    # 永久删除
    if is_dir:
        shutil.rmtree(str(path), ignore_errors=False)
    else:
        os.remove(str(path))


def _clear_dir_content(path: Path, mode: CleanMode, on_progress) -> None:
    """清空目录内容（保留目录本身）。"""
    for child in path.iterdir():
        try:
            child_is_dir = child.is_dir()
        except OSError:
            continue
        try:
            if child_is_dir:
                _delete_path(child, mode, is_dir=True)
            else:
                _delete_path(child, mode, is_dir=False)
            on_progress(child)
        except (PermissionError, OSError):
            # 被占用或无权限，跳过
            continue


def delete_targets(
    targets: list[Target],
    mode: CleanMode,
    on_progress: ProgressCB | None = None,
) -> dict[str, int]:
    """执行删除。

    返回 ``{"deleted": n, "failed": n, "freed": bytes}``。
    """
    on_progress = on_progress or (lambda i, total, msg: None)
    total = len(targets)
    deleted = 0
    failed = 0
    freed = 0
    for i, t in enumerate(targets, start=1):
        try:
            if t.kind is TargetKind.FILE:
                _delete_path(t.path, mode, is_dir=False)
                freed += t.size
            elif t.action is TargetAction.CLEAR:
                before = _dir_size_now(t.path)
                _clear_dir_content(t.path, mode, lambda p: None)
                after = _dir_size_now(t.path)
                freed += max(before - after, 0)
            else:  # DELETE dir
                _delete_path(t.path, mode, is_dir=True)
                freed += t.size
            deleted += 1
            on_progress(i, total, t.describe())
        except (PermissionError, OSError, FileNotFoundError):
            failed += 1
            on_progress(i, total, f"[跳过] {t.path}")
    return {"deleted": deleted, "failed": failed, "freed": freed}


def _dir_size_now(path: Path) -> int:
    """快速计算目录当前大小（用于 CLEAR 模式的前后对比）。"""
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# Windows 回收站
# ---------------------------------------------------------------------------
def empty_recycle_bin() -> dict[str, int]:
    """清空 Windows 回收站。返回 ``{"deleted": n, "failed": n, "freed": bytes}``。"""
    # 第一个参数：当有多个用户时是否跳过其他用户的回收站（True 表示跳过）
    # 第二个参数：要清空的驱动器（None 表示全部）
    # 第三个参数：标志（SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND）
    if sys.platform != "win32":
        return {"deleted": 0, "failed": 0, "freed": 0}
    try:
        SHERB_NOCONFIRMATION = 0x00000001
        SHERB_NOPROGRESSUI = 0x00000002
        SHERB_NOSOUND = 0x00000004
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        shell32 = ctypes.windll.shell32
        # 第三个参数正是一个字符的垃圾箱根目录路径；传 None 表示所有驱动器
        result = shell32.SHEmptyRecycleBinW(None, None, flags)
        if result == 0:  # S_OK
            return {"deleted": 1, "failed": 0, "freed": 0}
        return {"deleted": 0, "failed": 1, "freed": 0}
    except Exception:  # noqa: BLE001
        return {"deleted": 0, "failed": 1, "freed": 0}
