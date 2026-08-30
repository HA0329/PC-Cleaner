"""删除引擎。

职责：
- 把一批 Target 执行删除，支持「进入回收站」或「永久删除」；
- 目录目标按 action 处理：清空内容（保留目录）或删除整个目录；
- 逐项容错：文件被占用/无权限时跳过并继续；
- 删除前做二次防御：拒绝磁盘根路径与受保护路径（即使扫描器漏判）；
- 清空 Windows 回收站（通过 shell32.SHEmptyRecycleBinW）；
- 提供简单的进度回调。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .models import CleanMode, Target, TargetAction, TargetKind, format_size
from .rules import is_within_clear_root

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


def _guard_path(path: Path, is_protected, action: TargetAction) -> None:
    """删除前的二次防御：拒绝磁盘根路径与受保护路径。

    白名单清空目录（ALLOWED_CLEAR_ROOTS，如
    ``C:\\Windows\\SoftwareDistribution\\Download``）只允许以 CLEAR（清空内容）
    方式处理；DELETE（删除目录本身）或任何非白名单受保护路径一律拒绝。
    """
    p = path.absolute()
    if p == Path(p.anchor) or len(p.parts) <= 1:
        raise PermissionError(f"拒绝删除磁盘根路径: {p}")
    if is_within_clear_root(p):
        if action is TargetAction.CLEAR:
            return  # 白名单清空例外
        raise PermissionError(f"白名单目录只允许清空内容，拒绝删除整个目录: {p}")
    if is_protected(p):
        raise PermissionError(f"受保护路径，拒绝删除: {p}")


def _shred_file(path: Path, passes: int = 1) -> None:
    """多遍随机覆写后删除（隐私：降低被恢复概率）。

    ``passes`` 为覆写遍数（默认 1，上限 7）。失败时不阻断删除
    （擦除只是增强项）；覆盖内容失败也继续删除。
    """
    if passes < 1:
        passes = 1
    passes = min(passes, 7)
    try:
        size = path.stat().st_size
        with path.open("r+b", buffering=0) as f:
            for _ in range(passes):
                f.seek(0)
                remaining = size
                chunk = 1024 * 1024
                while remaining > 0:
                    n = min(chunk, remaining)
                    f.write(os.urandom(n))
                    remaining -= n
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except (OSError, PermissionError):
        pass


def _delete_path(
    path: Path,
    mode: CleanMode,
    is_dir: bool,
    recycle_fallback: bool,
    shred: bool = False,
    shred_passes: int = 1,
) -> None:
    """删除单个文件或目录。"""
    if mode is CleanMode.RECYCLE and HAS_SEND2TRASH:
        try:
            send2trash.send2trash(str(path))
            return
        except Exception:  # noqa: BLE001
            if not recycle_fallback:
                # 默认不回退：进回收站失败就保留原文件，避免误永久删除
                raise
            _delete_path(
                path,
                CleanMode.PERMANENT,
                is_dir,
                recycle_fallback,
                shred=shred,
                shred_passes=shred_passes,
            )
            return
    # 永久删除
    if is_dir:
        if shred:
            for child in path.rglob("*"):
                try:
                    if child.is_file() and not child.is_symlink():
                        _shred_file(child, passes=shred_passes)
                except OSError:
                    continue
        shutil.rmtree(str(path), ignore_errors=False)
    else:
        if shred:
            _shred_file(path, passes=shred_passes)
        os.remove(str(path))


def _clear_dir_content(
    path: Path,
    mode: CleanMode,
    on_progress,
    recycle_fallback: bool,
    is_protected,
    shred: bool = False,
    shred_passes: int = 1,
) -> None:
    """清空目录内容（保留目录本身）。"""
    for child in path.iterdir():
        # 防御：清空时逐个跳过受保护子项（如缓存目录里混入的联接/用户数据）
        if is_protected(child):
            continue
        try:
            child_is_dir = child.is_dir()
        except OSError:
            continue
        try:
            if child_is_dir:
                _delete_path(
                    child,
                    mode,
                    is_dir=True,
                    recycle_fallback=recycle_fallback,
                    shred=shred,
                    shred_passes=shred_passes,
                )
            else:
                _delete_path(
                    child,
                    mode,
                    is_dir=False,
                    recycle_fallback=recycle_fallback,
                    shred=shred,
                    shred_passes=shred_passes,
                )
            on_progress(child)
        except (PermissionError, OSError):
            # 被占用或无权限，跳过
            continue


def delete_targets(
    targets: list[Target],
    mode: CleanMode,
    on_progress: ProgressCB | None = None,
    recycle_fallback: bool | None = None,
    shred: bool = False,
    shred_passes: int = 1,
    audit: Callable[[Path, int, str], None] | None = None,
) -> dict[str, int]:
    """执行删除。

    返回 ``{"deleted": n, "failed": n, "freed": bytes}``。

    ``recycle_fallback``：进回收站失败时是否回退为永久删除。
    ``None`` 时读取配置 ``recycle_error_fallback``（默认 False，即失败就保留）。

    ``shred``：永久删除前先随机覆写（隐私增强，仅对文件内容生效）。

    ``shred_passes``：覆写遍数（默认 1，上限 7），仅 ``shred=True`` 时生效。

    ``audit``：每成功删除一个目标时回调 ``(path, size, mode)``，用于审计日志/历史。
    """
    if recycle_fallback is None:
        recycle_fallback = bool(load_config().get("recycle_error_fallback", False))
    from .scanner import make_protect_check  # 延迟导入，避免循环依赖

    is_protected = make_protect_check()
    on_progress = on_progress or (lambda i, total, msg: None)
    audit = audit or (lambda path, size, mode: None)
    total = len(targets)
    deleted = 0
    failed = 0
    freed = 0
    for i, t in enumerate(targets, start=1):
        try:
            _guard_path(t.path, is_protected, t.action)
            if t.kind is TargetKind.FILE:
                _delete_path(
                    t.path,
                    mode,
                    is_dir=False,
                    recycle_fallback=recycle_fallback,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                freed += t.size
                audit(t.path, t.size, mode.value)
            elif t.action is TargetAction.CLEAR:
                before = _dir_size_now(t.path)
                _clear_dir_content(
                    t.path,
                    mode,
                    lambda p: None,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                after = _dir_size_now(t.path)
                freed += max(before - after, 0)
                audit(t.path, t.size, mode.value)
            else:  # DELETE dir
                _delete_path(
                    t.path,
                    mode,
                    is_dir=True,
                    recycle_fallback=recycle_fallback,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                freed += t.size
                audit(t.path, t.size, mode.value)
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
        shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
        # 第三个参数正是一个字符的垃圾箱根目录路径；传 None 表示所有驱动器
        result = shell32.SHEmptyRecycleBinW(None, None, flags)
        # S_OK = 0 表示成功清空；
        # S_FALSE = 1 / E_UNEXPECTED = 0x8000FFFF 表示回收站本来就空（无操作），不算失败
        if result == 0:  # S_OK
            return {"deleted": 1, "failed": 0, "freed": 0}
        if result in (1, -2147418113):  # S_FALSE, E_UNEXPECTED (0x8000FFFF)
            return {"deleted": 0, "failed": 0, "freed": 0}
        return {"deleted": 0, "failed": 1, "freed": 0}
    except Exception:  # noqa: BLE001
        return {"deleted": 0, "failed": 1, "freed": 0}


# ---------------------------------------------------------------------------
# 回收站恢复（undo）
# ---------------------------------------------------------------------------
def _parse_recycle_info(info_path: Path) -> tuple[str, int] | None:
    """解析回收站 ``$I<name>`` 元数据文件，返回 (原始路径, 文件大小)。

    格式：8 字节头 + QWORD 文件大小(offset 8) + QWORD 删除时间(offset 16)
    + UTF-16LE 原始完整路径(offset 24 起)。解析失败返回 None。
    """
    try:
        data = info_path.read_bytes()
    except OSError:
        return None
    if len(data) < 24:
        return None
    size = int.from_bytes(data[8:16], "little", signed=False)
    raw = data[24:]
    # 原始路径是 UTF-16LE + 2 字节空终结符；去掉终结符后必须是偶数长度
    if raw.endswith(b"\x00\x00"):
        raw = raw[:-2]
    if len(raw) % 2 != 0:
        raw = raw[:-1]
    try:
        orig = raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
    except Exception:  # noqa: BLE001
        return None
    if not orig:
        return None
    return orig, size


def recycle_entries(drives: list[str] | None = None) -> list[dict]:
    """枚举各驱动器回收站中的 (原始路径 -> $R 数据文件) 映射（只读）。"""
    if sys.platform != "win32":
        return []
    from .scanner import _system_drives

    drives = drives or _system_drives()
    entries: list[dict] = []
    for drive in drives:
        root = Path(drive) / "$Recycle.Bin"
        if not root.is_dir():
            continue
        try:
            for sid_dir in root.iterdir():
                if not sid_dir.is_dir():
                    continue
                for info in sid_dir.glob("$I*"):
                    if not info.is_file():
                        continue
                    parsed = _parse_recycle_info(info)
                    if parsed is None:
                        continue
                    orig, size = parsed
                    data = sid_dir / ("$R" + info.name[2:])
                    entries.append(
                        {
                            "original": orig,
                            "size": size,
                            "data": data,
                            "info": info,
                        }
                    )
        except OSError:
            continue
    return entries


def restore_paths(paths: list[str], drives: list[str] | None = None) -> dict[str, Any]:
    """把仍在回收站中的原始路径恢复回原位。

    返回 ``{"restored": [路径...], "skipped": [原因...]}``。
    仅当文件确实还在回收站且原位置不存在同名文件时才恢复。
    """
    if sys.platform != "win32":
        return {"restored": [], "skipped": ["非 Windows 平台"]}
    entries = recycle_entries(drives)
    by_orig: dict[str, dict] = {}
    for e in entries:
        by_orig.setdefault(e["original"].lower(), e)

    restored: list[str] = []
    skipped: list[str] = []
    for raw_path in paths:
        p = Path(raw_path)
        key = str(p).lower()
        e = by_orig.get(key)
        if e is None:
            skipped.append(f"{p}（回收站中已不存在，可能已被手动删除）")
            continue
        if p.exists():
            skipped.append(f"{p}（原位置已有同名文件，已保留回收站副本）")
            continue
        data = e["data"]
        if not data.exists():
            skipped.append(f"{p}（数据文件缺失）")
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(data), str(p))
            restored.append(str(p))
        except (OSError, shutil.Error) as exc:
            skipped.append(f"{p}（恢复失败: {exc}）")
    return {"restored": restored, "skipped": skipped}
