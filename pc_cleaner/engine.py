"""删除引擎。

职责：
- 把一批 Target 执行删除，支持「进入回收站」或「永久删除」；
- 目录目标按 action 处理：清空内容（保留目录）或删除整个目录；
- 逐项容错：文件被占用/无权限时跳过并继续；
- 删除前做二次防御：拒绝磁盘根路径与受保护路径（即使扫描器漏判）；
- 清空 Windows 回收站（通过 shell32.SHEmptyRecycleBinW）；
- 提供简单的进度回调。

安全增强（v0.8.1）：
- 导入系统关键文件黑名单（SYSTEM_CRITICAL_FILES），拒绝删除。
- 使用 path.resolve() 解析真实路径，防止 .. 和符号链接绕过。
- 删除前重新执行保护检查（TOCTOU 防护）。
- shred 使用 O_SYNC 和 fsync 强制物理写入。
- 非预期异常记录日志。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import logging
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .models import CleanMode, Target, TargetAction, TargetKind, format_size
from .rules import is_within_clear_root, SYSTEM_CRITICAL_FILES  # 安全增强

# ===========================================================================
# 可选依赖：send2trash
# ===========================================================================
try:  # 可选依赖：删除到回收站
    import send2trash  # type: ignore
    HAS_SEND2TRASH = True
except Exception:  # noqa: BLE001
    send2trash = None
    HAS_SEND2TRASH = False


def recycle_available() -> bool:
    """是否支持删除到回收站。"""
    return HAS_SEND2TRASH


# ===========================================================================
# 日志配置
# ===========================================================================
logger = logging.getLogger("pc_cleaner.engine")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)


# 进度回调类型
ProgressCB = Callable[[int, int, str], None]


# ===========================================================================
# 删除前的二次防御（安全增强）
# ===========================================================================
def _guard_path(path: Path, is_protected, action: TargetAction) -> None:
    """删除前的二次防御：拒绝磁盘根路径、系统关键文件、受保护路径。

    安全增强：
    - 首先使用 resolve(strict=True) 解析真实路径，防止 .. 和符号链接绕过。
    - 检查是否系统关键文件黑名单。
    - 检查是否磁盘根。
    - 白名单清空目录（ALLOWED_CLEAR_ROOTS）只允许以 CLEAR 方式处理。
    - 受保护路径匹配使用传入的 is_protected 函数（基于精确前缀匹配）。
    """
    # 1. 解析真实路径（防止 .. 和符号链接）
    try:
        real = path.resolve(strict=True)
    except (OSError, ValueError):
        raise PermissionError(f"无法解析路径: {path}")

    # 2. 拒绝删除磁盘根路径
    if real == Path(real.anchor) or len(real.parts) <= 1:
        raise PermissionError(f"拒绝删除磁盘根路径: {real}")

    # 3. 系统关键文件黑名单检查
    if real.name.lower() in SYSTEM_CRITICAL_FILES:
        raise PermissionError(f"拒绝删除系统关键文件: {real}")

    # 4. 白名单清空例外（只允许 CLEAR）
    if is_within_clear_root(real):
        if action is TargetAction.CLEAR:
            return  # 允许清空内容
        raise PermissionError(f"白名单目录只允许清空内容，拒绝删除整个目录: {real}")

    # 5. 受保护路径检查（精确前缀匹配）
    if is_protected(real):
        raise PermissionError(f"受保护路径，拒绝删除: {real}")


# ===========================================================================
# 文件覆写（shred）增强
# ===========================================================================
def _shred_file(path: Path, passes: int = 1) -> None:
    """多遍随机覆写后删除（增强：强制同步落盘）。

    使用 os.O_SYNC 确保每次写入都立即物理写入磁盘，
    并显式调用 os.fsync 增强可靠性。

    passes：覆写遍数（默认 1，上限 7）。失败时不阻断删除。
    """
    if passes < 1:
        passes = 1
    passes = min(passes, 7)
    try:
        size = path.stat().st_size
        # 以 O_SYNC 模式打开文件，保证写入落盘
        fd = os.open(str(path), os.O_RDWR | os.O_SYNC)
        try:
            for _ in range(passes):
                os.lseek(fd, 0, os.SEEK_SET)
                remaining = size
                chunk = 1024 * 1024  # 1MB 块
                while remaining > 0:
                    n = min(chunk, remaining)
                    os.write(fd, os.urandom(n))
                    remaining -= n
                os.fsync(fd)   # 再次显式同步
        finally:
            os.close(fd)
        # 覆写完成后删除文件
        os.remove(str(path))
    except (OSError, PermissionError):
        # 若覆写失败，尝试普通删除（避免留下文件）
        try:
            os.remove(str(path))
        except OSError:
            pass


# ===========================================================================
# 删除单个文件/目录（TOCTOU 防护）
# ===========================================================================
def _delete_path(
    path: Path,
    mode: CleanMode,
    is_dir: bool,
    recycle_fallback: bool,
    is_protected,          # 安全增强：传入保护检查函数
    shred: bool = False,
    shred_passes: int = 1,
) -> None:
    """删除单个文件或目录。

    安全增强：
    - 在操作前重新解析真实路径，并再次调用 _guard_path 做二次检查（TOCTOU）。
    - 所有删除操作基于 real 路径进行。
    """
    # 重新解析真实路径
    try:
        real = path.resolve(strict=True)
    except (OSError, ValueError):
        raise PermissionError(f"无法解析路径: {path}")

    # 重新执行保护检查（防御窗口期内的篡改）
    # 对于删除子项，一律使用 DELETE 动作（因为 _delete_path 只负责删除自身）
    _guard_path(real, is_protected, TargetAction.DELETE)

    # 执行删除（基于 real 路径）
    if mode is CleanMode.RECYCLE and HAS_SEND2TRASH:
        try:
            send2trash.send2trash(str(real))
            return
        except Exception:
            if not recycle_fallback:
                raise
            # 回退到永久删除
            mode = CleanMode.PERMANENT

    # 永久删除
    if is_dir:
        if shred:
            # 先覆写目录内所有文件（递归）
            for child in real.rglob("*"):
                try:
                    if child.is_file() and not child.is_symlink():
                        _shred_file(child, passes=shred_passes)
                except OSError:
                    continue
        shutil.rmtree(str(real), ignore_errors=False)
    else:
        if shred:
            _shred_file(real, passes=shred_passes)
        else:
            os.remove(str(real))


def _clear_dir_content(
    path: Path,
    mode: CleanMode,
    on_progress,
    recycle_fallback: bool,
    is_protected,
    shred: bool = False,
    shred_passes: int = 1,
) -> None:
    """清空目录内容（保留目录本身）。

    遍历目录下所有子项，对每个子项调用 _delete_path 删除。
    """
    try:
        for child in path.iterdir():
            # 防御：清空时逐个跳过受保护子项（如缓存目录里混入的联接/用户数据）
            if is_protected(child):
                continue
            try:
                child_is_dir = child.is_dir()
            except OSError:
                continue
            try:
                # 删除子项（无论文件还是目录，均为 DELETE 动作）
                _delete_path(
                    child,
                    mode,
                    is_dir=child_is_dir,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                on_progress(child)
            except (PermissionError, OSError):
                # 被占用或无权限，跳过
                continue
    except OSError:
        # 目录本身无法访问，跳过
        pass


# ===========================================================================
# 批量删除入口
# ===========================================================================
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
    audit = audit or (lambda path, size, mode_name: None)
    total = len(targets)
    deleted = 0
    failed = 0
    freed = 0
    for i, t in enumerate(targets, start=1):
        try:
            # 首先调用 _guard_path 进行初步检查（使用原始路径，但内部会 resolve）
            _guard_path(t.path, is_protected, t.action)

            if t.kind is TargetKind.FILE:
                _delete_path(
                    t.path,
                    mode,
                    is_dir=False,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                freed += t.size
                audit(t.path, t.size, mode.value)
            elif t.action is TargetAction.CLEAR:
                # 清空目录内容（保留目录本身）
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
            else:  # DELETE directory
                _delete_path(
                    t.path,
                    mode,
                    is_dir=True,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                freed += t.size
                audit(t.path, t.size, mode.value)
            deleted += 1
            on_progress(i, total, t.describe())
        except (PermissionError, OSError, FileNotFoundError) as exc:
            # 预期的权限/占用/不存在错误，跳过
            failed += 1
            on_progress(i, total, f"[跳过] {t.path} ({exc})")
        except Exception as exc:  # 非预期异常，记录日志
            logger.exception("删除目标 %s 时发生非预期异常: %s", t.path, exc)
            failed += 1
            on_progress(i, total, f"[错误] {t.path} (异常: {exc})")
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


# ===========================================================================
# Windows 回收站操作
# ===========================================================================
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
    except Exception as exc:
        logger.exception("清空回收站失败: %s", exc)
        return {"deleted": 0, "failed": 1, "freed": 0}


# ===========================================================================
# 回收站恢复（undo）
# ===========================================================================
def _parse_recycle_info(info_path: Path) -> tuple[str, int] | None:
    """解析回收站 ``$I<name>`` 元数据文件，返回 (原始路径, 文件大小)。

    格式：8 字节头 + QWORD 文件大小(offset 8) + QWORD 删除时间(offset 16)
    + UTF-16LE 原始完整路径(offset 24 起)。解析失败返回 None。

    兼容性说明：该格式是 Windows 内部实现（Win10/11 实测一致），解析对
    损坏/截断/权限拒绝的数据做容错，返回 None 而不是抛异常。
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
    # 拒绝仅含空字节/控制字符的垃圾数据（真实路径必然包含可打印字符）
    if not orig or not any(ch.isprintable() for ch in orig):
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