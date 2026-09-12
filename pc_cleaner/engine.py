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
- 白名单清空例外（ALLOWED_CLEAR_ROOTS）：只允许清空*根目录本身*；位于白名单
  根之下的子项视作「正在清空的内容」，允许删除（否则系统 Temp 等永远清不干净）。
- shred 兼容 Windows：os.O_SYNC 在 Windows 上不存在，改为条件启用，并用
  fsync 保证落盘；覆写完成后由调用方删除文件。
- 非预期异常记录日志。

v0.9.2 改进：
- **实际释放量核算**：CLEAR 目标按删除前后体积差计入 ``freed``（此前用的是
  扫描时的估计值，被占用/受保护文件也算进去了，导致"释放了 400MB"实际只有 200MB）；
  ``audit`` 回调新增 ``freed`` 参数，审计日志不再恒为 0。
- **部分失败不再静默**：清空目录时若有子项被占用，返回 ``skipped`` 计数；
  一个子项都没删掉时计为 ``failed``，不再"假装成功"。
- **重解析点保护**：删除前拒绝符号链接 / junction（含链接自身），
  避免越界删到链接目标。
- 回收站恢复成功后清理对应的 ``$I`` 元数据文件，避免残留孤立记录。

v0.9.10 安全修复：
- **回收站不可用时不再静默永久删除**：此前 ``_delete_path`` 的条件写作
  ``mode is RECYCLE and HAS_SEND2TRASH``，条件不成立就直接落到「永久删除」——
  用户以为文件进了回收站（可撤销），实际被永久删除。现在回收站不可用时
  **一律拒绝删除并计入 failed**（``RECYCLE_UNAVAILABLE_REASON``），
  与 README「send2trash 是必需依赖，不再静默降级」的承诺一致。
- **删除计数与释放量如实**：目标在扫描后、删除前被外部删掉时记入 ``vanished``
  （不再算作 ``deleted``，也不再虚报 ``freed``）；``freed`` 统一按**删除前后
  实测体积差**计算，而不是扫描时的估计值。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sqlite3
import sys
import logging
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .models import CleanMode, Target, TargetAction, TargetKind, format_size
from .rules import (
    is_clear_root,
    SYSTEM_CRITICAL_FILES,  # 安全增强
)

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


#: 请求了「进回收站」但 send2trash 不可用时的统一文案。
#: v0.9.10 安全修复：此前这种情况会**静默降级为永久删除**（见 _delete_path），
#: 与 README「send2trash 是必需依赖，不再静默降级为永久删除」的承诺矛盾：
#: 用户以为进回收站（可撤销），实际文件被永久删除且再也找不回来。
RECYCLE_UNAVAILABLE_REASON = (
    "请求「进回收站」但 send2trash 不可用（未安装或导入失败）："
    "已保留原文件，未做任何删除。请先 pip install send2trash，"
    "或用 --permanent 显式确认要永久删除。"
)


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
    - 首先使用 resolve() 解析真实路径（不要求存在），防止 .. 和符号链接绕过。
    - 检查是否系统关键文件黑名单。
    - 检查是否磁盘根。
    - 白名单清空目录（ALLOWED_CLEAR_ROOTS）：
        * CLEAR 动作：允许清空内容；
        * DELETE 动作：仅当路径**恰好等于**某个白名单根目录时拒绝删除目录本身；
        * 白名单根之下的子项 = 「正在被清空的内容」，放行交给后续删除
          （否则 C:\\Windows\\Temp 等白名单目录会永远清不干净）。
    - 受保护路径匹配使用传入的 is_protected 函数。
    """
    # 1. 解析真实路径（防止 .. 和符号链接）；不要求路径存在，避免误伤
    try:
        real = path.resolve(strict=False)
    except (OSError, ValueError):
        raise PermissionError(f"无法解析路径: {path}")

    # 2. 拒绝删除磁盘根路径
    if real == Path(real.anchor) or len(real.parts) <= 1:
        raise PermissionError(f"拒绝删除磁盘根路径: {real}")

    # 3. 系统关键文件黑名单检查
    if real.name.lower() in SYSTEM_CRITICAL_FILES:
        raise PermissionError(f"拒绝删除系统关键文件: {real}")

    # 4. 白名单清空例外（只允许清空内容；禁止删除白名单根目录本身）
    #
    # v0.9.8 安全修复：此处原先在 `is_clear_root(real)` 判断之后有一句无条件的
    # `return`，使白名单根**之下的子项**直接放行，第 5 步的 is_protected 永远
    # 不会被调用。结果 `C:\Windows\Temp\xwechat_files`、`C:\Windows\Temp\.git`
    # 这类"混在可清空缓存目录里的受保护名"会被真的删掉——正是 scanner.py
    # make_protect_check() 在 v0.9.3 专门修掉的那种「白名单整体短路」，
    # 引擎侧当时漏改了。
    #
    # 现在不再提前 return，一律落到第 5 步由 is_protected 判定：
    # make_protect_check() 对白名单根内的**普通缓存内容**返回 False（放行），
    # 只对真正混入的受保护名返回 True（拒绝），因此清空功能不受影响。
    if action is not TargetAction.CLEAR and is_clear_root(real):
        raise PermissionError(f"白名单目录只允许清空内容，拒绝删除整个目录: {real}")

    # 5. 受保护路径检查
    if is_protected(real):
        raise PermissionError(f"受保护路径，拒绝删除: {real}")


# ===========================================================================
# 文件覆写（shred）
# ===========================================================================
def _shred_file(path: Path, passes: int = 1) -> None:
    """多遍随机覆写文件内容（不删除文件，删除由调用方负责）。

    Windows 兼容：``os.O_SYNC`` 在 Windows 上不存在，因此仅在平台提供时
    启用，并统一调用 ``os.fsync`` 强制物理落盘，保证覆写可靠性。

    passes：覆写遍数（默认 1，上限 7）。覆写失败抛 OSError/PermissionError，
    由调用方决定是否仍然删除文件。
    """
    if passes < 1:
        passes = 1
    passes = min(passes, 7)
    size = path.stat().st_size
    # Windows 上 os.open 必须显式加 O_BINARY，否则随机字节里的 \n 会被翻译成
    # \r\n 导致文件变长；POSIX 无此标志。
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_SYNC"):  # POSIX 可用；Windows 无此标志
        flags |= os.O_SYNC
    fd = os.open(str(path), flags)
    try:
        for _ in range(passes):
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = size
            chunk = 1024 * 1024  # 1MB 块
            while remaining > 0:
                n = min(chunk, remaining)
                os.write(fd, os.urandom(n))
                remaining -= n
            try:
                os.fsync(fd)
            except OSError:
                pass
    finally:
        os.close(fd)


# ===========================================================================
# 删除单个文件/目录（TOCTOU 防护）
# ===========================================================================
def _rmtree(path: Path) -> None:
    """删除整棵目录，Windows 只读属性导致失败时先清属性再重试（v0.9.3）。

    ``shutil.rmtree(ignore_errors=False)`` 在遇到只读文件时直接抛
    ``PermissionError``；临时目录/缓存里只读文件很常见，先 ``chmod`` 再删。
    Python 3.12 起 ``onerror`` 被 ``onexc`` 取代，这里按版本选择。
    """

    def _retry(func, target, exc):  # noqa: ANN001
        try:
            os.chmod(target, 0o700)
        except OSError:
            raise exc
        try:
            func(target)
        except OSError:
            raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(str(path), ignore_errors=False, onexc=_retry)
    else:  # pragma: no cover - Python 3.10/3.11
        shutil.rmtree(str(path), ignore_errors=False, onerror=_retry)


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
    - 拒绝删除重解析点（符号链接 / junction）本身：删除链接目标会越界，
      而删除链接自身容易让上层目录结构失效，一律跳过更安全。
    """
    # 重新解析真实路径
    try:
        real = path.resolve(strict=False)
    except (OSError, ValueError):
        raise PermissionError(f"无法解析路径: {path}")

    # 重解析点保护：不跟随、也不删除链接本身
    from .scanner import is_reparse_point  # 延迟导入，避免循环依赖

    if is_reparse_point(path) or is_reparse_point(real):
        raise PermissionError(f"拒绝删除符号链接/联接（junction）: {path}")

    # 重新执行保护检查（防御窗口期内的篡改）
    # 对于删除子项，一律使用 DELETE 动作（因为 _delete_path 只负责删除自身）
    _guard_path(real, is_protected, TargetAction.DELETE)

    # v0.9.10 安全修复：请求回收站但 send2trash 不可用时**拒绝删除**。
    # 此前该条件写作 `mode is CleanMode.RECYCLE and HAS_SEND2TRASH`，条件不成立时
    # 直接落到下面的「永久删除」，即：回收站不可用时静默永久删除用户数据，
    # 却仍按用户的理解"进回收站可恢复"。现在改为不删、抛错、由调用方计入 failed。
    if mode is CleanMode.RECYCLE and not HAS_SEND2TRASH:
        raise PermissionError(RECYCLE_UNAVAILABLE_REASON)

    # 执行删除（基于 real 路径）
    if mode is CleanMode.RECYCLE:
        try:
            send2trash.send2trash(str(real))
            return
        except Exception:
            if not recycle_fallback:
                raise
            # 显式配置 recycle_error_fallback=true：回退到永久删除
            mode = CleanMode.PERMANENT

    # 永久删除
    if is_dir:
        if shred:
            # 先覆写目录内所有文件（递归）
            for child in real.rglob("*"):
                try:
                    if child.is_file() and not is_reparse_point(child):
                        _shred_file(child, passes=shred_passes)
                except OSError:
                    continue
        _rmtree(real)
    else:
        if shred:
            # 先覆写内容，再删除文件（覆写失败仍尝试删除，避免留下文件）
            try:
                _shred_file(real, passes=shred_passes)
            except OSError:
                pass
            os.remove(str(real))
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
) -> tuple[int, int]:
    """清空目录内容（保留目录本身），返回 (成功删除的子项数, 失败子项数)。

    逐项容错：单个子项被占用/无权限时计入失败并继续，不中断整个清空。
    重解析点（符号链接 / junction）直接跳过（既不计成功也不计失败），
    避免越界删除链接目标。
    """
    from .scanner import is_reparse_point  # 延迟导入，避免循环依赖

    # v0.9.10 安全修复：与 _delete_path 同一道闸门。清空目录若按「回收站」执行，
    # 而 send2trash 不可用，则**一个子项都不能删**（否则等于永久删除缓存内容）。
    if mode is CleanMode.RECYCLE and not HAS_SEND2TRASH:
        logger.warning("拒绝清空目录（回收站不可用，不降级为永久删除）: %s", path)
        return (0, 1)

    if is_reparse_point(path):
        # v0.9.3 安全增强：CLEAR 根自身是符号链接 / junction 时拒绝。
        # 此前只对子项判重解析点，path.iterdir() 会跟随链接，
        # 于是「清空 C:\Users\All Users」会真的清空 C:\ProgramData。
        logger.warning("拒绝清空符号链接/junction 目录（不跟随链接）: %s", path)
        return (0, 0)

    deleted = 0
    failed = 0
    try:
        children = list(path.iterdir())
    except OSError:
        return (0, 0)
    for child in children:
        try:
            if is_reparse_point(child):
                continue
        except OSError:
            continue
        # 防御：清空时逐个跳过受保护子项（如缓存目录里混入的用户数据 /
        # .git / 微信数据目录 / junction）。
        #
        # v0.9.8 安全修复：此处原先的条件是
        #     `if is_protected(child) and not is_within_clear_root(child)`
        # 但本函数的每一个 child 都必然位于白名单清空根**之内**，所以
        # `is_within_clear_root(child)` 恒为 True、`not ...` 恒为 False，
        # 这个跳过分支从未生效过 —— 白名单根内混入的受保护名照删不误。
        # 现在只依据 is_protected 判定：make_protect_check() 对普通缓存
        # 内容返回 False（放行），只对真正混入的受保护名返回 True（跳过）。
        if is_protected(child):
            continue
        try:
            child_is_dir = child.is_dir()
        except OSError:
            failed += 1
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
            deleted += 1
            on_progress(child)
        except (PermissionError, OSError):
            # 被占用或无权限，跳过
            failed += 1
            continue
    return (deleted, failed)


# ===========================================================================
# 批量删除入口
# ===========================================================================
def _in_use_reason(path: Path) -> str:
    """目标是否被运行中进程占用；是则返回原因描述，否则空串（v0.9.3）。

    依赖 :mod:`pc_cleaner.proc`（零必需依赖，psutil 可选，PowerShell 回退）；
    该模块不可用或检测失败时返回空串（不阻断删除，保持原有行为）。
    """
    try:
        from .proc import any_process_uses
    except Exception:  # noqa: BLE001
        return ""
    try:
        users = any_process_uses(path)
    except Exception:  # noqa: BLE001
        return ""
    if not users:
        return ""
    return "、".join(str(u) for u in list(users)[:3])


def delete_targets(
    targets: list[Target],
    mode: CleanMode,
    on_progress: ProgressCB | None = None,
    recycle_fallback: bool | None = None,
    shred: bool = False,
    shred_passes: int = 1,
    audit: Callable[[Path, int, str, int], None] | None = None,
) -> dict[str, int]:
    """执行删除。

    返回 ``{"deleted": n, "failed": n, "freed": bytes, "recycled": bytes,
    "skipped": n, "skipped_in_use": n, "vanished": n}``。

    - ``deleted``：**确实被删除/清空**的目标数（目标在删除前就已消失、或什么都没能
      删掉的 CLEAR 目标不计入——v0.9.10 修正，此前扫描后被外部删掉的目标也会算作
      "删除成功"，让自动化脚本误以为清理生效了）；
    - ``failed``：抛错/完全没能清理的目标数；
    - ``freed``：**实际释放**的字节数（永久删除按删除前后体积差；压缩按 VACUUM
      前后差）；
    - ``recycled``：**进回收站**的字节数（v0.9.3 新增——进回收站不释放空间，
      要清空回收站才释放，因此不再混进 ``freed`` 虚报）；
    - ``skipped``：部分成功（清空目录时有子项被占用）的目标数；
    - ``skipped_in_use``：因目标被运行中进程占用而跳过的目标数（规则声明了
      ``skip_if_in_use``，如 npm ``_npx``）；
    - ``vanished``：扫描到删除之间就已不存在、因而"无事可做"的目标数（v0.9.10）。

    ``recycle_fallback``：进回收站失败时是否回退为永久删除。
    ``None`` 时读取配置 ``recycle_error_fallback``（默认 False，即失败就保留）。

    ``shred``：永久删除前先随机覆写（隐私增强，仅对文件内容生效）。

    ``shred_passes``：覆写遍数（默认 1，上限 7），仅 ``shred=True`` 时生效。

    ``audit``：每成功处理一个目标时回调 ``(path, size, mode, freed)``，
    用于审计日志/历史（``freed`` 为该目标实际释放的字节数）。

    安全（v0.9.10）：``mode=RECYCLE`` 而 send2trash 不可用时，**整批拒绝执行**
    （全部计入 ``failed``、不删任何文件），绝不静默降级为永久删除。
    """
    if recycle_fallback is None:
        recycle_fallback = bool(load_config().get("recycle_error_fallback", False))
    from .scanner import make_protect_check  # 延迟导入，避免循环依赖

    is_protected = make_protect_check()
    on_progress = on_progress or (lambda i, total, msg: None)
    audit = audit or (lambda path, size, mode_name, freed: None)
    total = len(targets)
    deleted = 0
    failed = 0
    skipped = 0
    skipped_in_use = 0
    vanished = 0
    freed = 0
    recycled = 0
    # v0.9.3：进回收站时空间并未真正释放（要清空回收站才释放），分开统计。
    # v0.9.10：recycle_mode 只在真的可用时才为 True，且下面会提前拒绝不可用的情况。
    recycle_mode = mode is CleanMode.RECYCLE
    if recycle_mode and not HAS_SEND2TRASH:
        for i, t in enumerate(targets, start=1):
            failed += 1
            audit(t.path, 0, "recycle_unavailable", 0)
            on_progress(i, total, f"[跳过] {t.path}（{RECYCLE_UNAVAILABLE_REASON}）")
        logger.error("拒绝执行：%s", RECYCLE_UNAVAILABLE_REASON)
        return {
            "deleted": 0,
            "failed": failed,
            "freed": 0,
            "recycled": 0,
            "skipped": 0,
            "skipped_in_use": 0,
            "vanished": 0,
            "recycle_unavailable": True,
        }
    for i, t in enumerate(targets, start=1):
        try:
            # 首先调用 _guard_path 进行初步检查（使用原始路径，但内部会 resolve）
            _guard_path(t.path, is_protected, t.action)

            # v0.9.3：规则声明 skip_if_in_use 时，目标被运行中进程占用就跳过。
            # 典型场景：npm _npx 里正跑着 npx 安装的 CLI / AI 工具，
            # 清掉会让它们"当场不报错、下次启动才崩"。
            if t.skip_if_in_use:
                reason = _in_use_reason(t.path)
                if reason:
                    skipped_in_use += 1
                    audit(t.path, 0, "skipped_in_use", 0)
                    on_progress(i, total, f"[跳过] {t.path}（正被进程占用: {reason}）")
                    continue

            if t.action is TargetAction.COMPACT:
                # 数据库压缩：VACUUM 重写文件，不删除数据
                freed_here = compact_database(t.path)
                freed += freed_here
                audit(t.path, t.size, "compact", freed_here)
            elif t.kind is TargetKind.FILE:
                # v0.9.10：以**删除前后实测差**计入 freed；目标若在扫描后已消失，
                # 记 vanished 而不是"删除成功 + 释放 t.size"。
                before = _path_size_now(t.path)
                if before == 0 and not t.path.exists():
                    vanished += 1
                    audit(t.path, 0, "vanished", 0)
                    on_progress(i, total, f"[跳过] {t.path}（目标已不存在，无需清理）")
                    continue
                _delete_path(
                    t.path,
                    mode,
                    is_dir=False,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                if recycle_mode:
                    recycled += before
                    audit(t.path, before, mode.value, 0)
                else:
                    freed_here = max(before - _path_size_now(t.path), 0)
                    freed += freed_here
                    audit(t.path, before, mode.value, freed_here)
            elif t.action is TargetAction.CLEAR:
                # 清空目录内容（保留目录本身）
                before = _dir_size_now(t.path)
                _cleared, clear_failed = _clear_dir_content(
                    t.path,
                    mode,
                    on_progress=lambda p: None,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                after = _dir_size_now(t.path)
                freed_here = max(before - after, 0)
                if recycle_mode:
                    recycled += freed_here
                    audit(t.path, t.size, mode.value, 0)
                else:
                    freed += freed_here
                    audit(t.path, t.size, mode.value, freed_here)
                if clear_failed and freed_here == 0:
                    # 一个都没删掉：计入失败，避免"假装成功"
                    failed += 1
                    on_progress(i, total, f"[跳过] {t.path}（{clear_failed} 个子项被占用）")
                    continue
                if clear_failed:
                    skipped += 1
            else:  # DELETE directory
                before = _dir_size_now(t.path)
                _delete_path(
                    t.path,
                    mode,
                    is_dir=True,
                    recycle_fallback=recycle_fallback,
                    is_protected=is_protected,
                    shred=shred,
                    shred_passes=shred_passes,
                )
                if recycle_mode:
                    recycled += before
                    audit(t.path, before, mode.value, 0)
                else:
                    freed_here = max(before - _dir_size_now(t.path), 0)
                    freed += freed_here
                    audit(t.path, before, mode.value, freed_here)
            deleted += 1
            on_progress(i, total, t.describe())
        except FileNotFoundError:
            # 目标在扫描之后、删除之前消失了：无事可做，不算删除成功
            vanished += 1
            audit(t.path, 0, "vanished", 0)
            on_progress(i, total, f"[跳过] {t.path}（目标已不存在，无需清理）")
        except (PermissionError, OSError) as exc:
            # 预期的权限/占用错误，跳过
            failed += 1
            on_progress(i, total, f"[跳过] {t.path} ({exc})")
        except Exception as exc:  # 非预期异常，记录日志
            logger.exception("删除目标 %s 时发生非预期异常: %s", t.path, exc)
            failed += 1
            on_progress(i, total, f"[错误] {t.path} (异常: {exc})")
    return {
        "deleted": deleted,
        "failed": failed,
        "freed": freed,
        "recycled": recycled,
        "skipped": skipped,
        "skipped_in_use": skipped_in_use,
        "vanished": vanished,
    }


def _path_size_now(path: Path) -> int:
    """单个路径当前占用的字节数（文件取 st_size，目录取递归总和，读不到返回 0）。

    v0.9.10：用于把 ``freed`` 从"扫描时的估计值"改为"删除前后实测差"，
    并识别"扫描后已被外部删掉"的目标（见 ``delete_targets``）。
    """
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return _dir_size_now(path)
    except OSError:
        return 0
    return 0


def _dir_size_now(path: Path) -> int:
    """快速计算目录当前大小（用于 CLEAR 模式的前后对比）。不跟随重解析点。"""
    from .scanner import is_reparse_point  # 延迟导入，避免循环依赖

    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not is_reparse_point(child):
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# ===========================================================================
# SQLite 数据库压缩（BleachBit「整理优化数据库」）
# ===========================================================================
def compact_database(path: Path) -> int:
    """对 SQLite 数据库执行 VACUUM 以释放碎片，返回释放的字节数。

    - **不删除数据**，只重写文件去掉空闲页，浏览器会在下次运行时重建索引；
    - 数据库被占用 / 非 SQLite / 只读时抛 PermissionError，安全跳过；
    - 使用 autocommit（isolation_level=None），避免 VACUUM 被隐式事务包裹而失败。
    """
    try:
        before = path.stat().st_size
    except OSError:
        raise PermissionError(f"无法访问数据库: {path}")
    # v0.9.3：VACUUM 需要约 1 倍库大小的临时空间，空间不足时直接拒绝，
    # 避免白耗时间/写满磁盘（SQLite 会回滚，但用户看到的是"压缩失败"）。
    try:
        free = shutil.disk_usage(str(path.parent)).free
    except OSError:
        free = None
    if free is not None and free < before:
        raise PermissionError(
            f"磁盘可用空间不足（VACUUM 约需 {format_size(before)}）: {path}"
        )
    try:
        con = sqlite3.connect(str(path), isolation_level=None)
        try:
            con.execute("VACUUM")
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise PermissionError(f"数据库压缩失败（可能被占用或非 SQLite）: {exc}")
    except OSError as exc:
        raise PermissionError(f"数据库压缩失败: {exc}")
    try:
        after = path.stat().st_size
    except OSError:
        after = before
    return max(before - after, 0)


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

    格式（Win10/11 实测，v0.9.2 修正）::

        offset 0  : 8 字节头（$I 版本标识）
        offset 8  : QWORD 文件大小
        offset 16 : QWORD 删除时间（FILETIME）
        offset 24 : DWORD 目录记录长度（**仅当该记录是目录时非 0**）
        offset 28 : UTF-16LE 原始完整路径 + 2 字节空终结符

    旧实现从 offset 24 读路径，对**目录**记录会把长度字段的低字节
    （如 ``b'd\\x00'``）当成第一个字符，导致 ``--undo-last`` 永远匹配不上
    原路径（本机实测：恢复全部跳过）。现在固定从 offset 28 读，
    并兼容长度字段恰好落在路径起始处的畸形数据。

    解析失败返回 None（对损坏/截断/权限拒绝做容错，不抛异常）。
    """
    try:
        data = info_path.read_bytes()
    except OSError:
        return None
    if len(data) < 28:
        return None
    size = int.from_bytes(data[8:16], "little", signed=False)
    raw = data[28:]
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

    匹配规则（v0.9.2 增强）：
    - 回收站里存的是**目标本身**（``$I`` 记录的路径 == 目标路径）→ 直接还原；
    - 回收站里存的是目标的**某个父目录**（send2trash 删除目录时会把该目录整体
      移入回收站，``$I`` 只记录父目录）→ 把该父目录整体还原，并报告已还原的
      父目录路径（子路径随之恢复）。
    仅当原位置不存在同名文件时才恢复，避免覆盖用户新数据。
    """
    if sys.platform != "win32":
        return {"restored": [], "skipped": ["非 Windows 平台"]}
    entries = recycle_entries(drives)

    def _canon(p) -> str:
        """把路径规范成"回收站里记录的那种"写法（小写 + 解析后的绝对路径）。

        v0.9.8 修复 ``--undo-last`` 对 ``%TEMP%`` 完全失效：

        删除时引擎用的是 ``path.resolve()``（如
        ``C:\\Users\\Administrator.DESKTOP-B166QN2\\AppData\\Local\\Temp``），
        由 shell 写进回收站的 ``$I``；而历史记录里存的是**扫描时的原始写法**。
        由于 ``%TEMP%`` 展开后是 8.3 短名
        （``C:\\Users\\ADMINI~1.DES\\AppData\\Local\\Temp``），两者字符串永远
        不相等，于是恢复查找全部落空，还会反过来告诉用户"回收站中已不存在，
        可能已被手动删除"——诱导用户去清空回收站，把本可恢复的数据真删掉。

        这里统一做 resolve（目标已不存在时，其**已存在的父级**仍会被解析，
        足以把短名折叠成长名），使两侧写法可比。
        """
        try:
            return str(Path(p).resolve(strict=False)).lower()
        except (OSError, ValueError):
            return str(p).lower()

    by_orig: dict[str, dict] = {}
    for e in entries:
        raw = str(e["original"]).lower()
        by_orig.setdefault(raw, e)
        # 同时登记解析后的写法，兼容历史记录里存的是长名、回收站存的是短名
        # （或反之）的情况。
        canon = _canon(e["original"])
        if canon != raw:
            by_orig.setdefault(canon, e)

    def _restore_one(e: dict, target: Path, requested: Path) -> None:
        """把一条回收站记录还原到 ``target``（记录内已确认 target 不存在）。"""
        data = e["data"]
        if not data.exists():
            skipped.append(f"{target}（数据文件缺失）")
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(data), str(target))
            restored.append(str(target))
            # 清理对应的 $I 元数据，避免回收站里留下指向不存在数据的孤立记录
            try:
                info = e.get("info")
                if info is not None and Path(info).exists():
                    Path(info).unlink()
            except OSError:
                pass
        except (OSError, shutil.Error) as exc:
            skipped.append(f"{target}（恢复失败: {exc}）")

    restored: list[str] = []
    skipped: list[str] = []
    for raw_path in paths:
        p = Path(raw_path)
        # v0.9.8：用规范化写法查找，兼容 8.3 短名 / 长名混用（见 _canon 说明）；
        # 恢复本身仍使用历史记录里的原始路径，保证还原到用户原本的位置。
        key = _canon(raw_path)
        e = by_orig.get(key)
        if e is not None:
            if p.exists():
                skipped.append(f"{p}（原位置已有同名文件，已保留回收站副本）")
            else:
                _restore_one(e, p, p)
            continue

        # 回退 1：目标本身不在，但它的某个**父目录**被整体回收（send2trash
        # 删除目录时 $I 记录的是父目录）→ 还原该父目录，子路径随之恢复。
        parent_hit: dict | None = None
        parent_path = p
        cur = p.parent
        while len(cur.parts) > 1:
            parent_hit = by_orig.get(_canon(cur))
            if parent_hit is not None:
                parent_path = cur
                break
            cur = cur.parent
        if parent_hit is not None:
            if parent_path.exists():
                skipped.append(f"{parent_path}（原位置已有同名文件，已保留回收站副本）")
            else:
                _restore_one(parent_hit, parent_path, p)
            continue

        # 回退 2：目标目录的**内容**被逐个回收（send2trash 逐子项删除时
        # $I 记录的是子项）→ 把所有位于该目录下的记录逐条还原。
        prefix = key.rstrip("\\/") + os.sep
        # by_orig 为兼容短名/长名会为同一条 $I 登记多个键，按 data 去重避免重复还原。
        seen_data: set[str] = set()
        children: list[dict] = []
        for k, e in by_orig.items():
            if not k.startswith(prefix):
                continue
            dk = str(e.get("data", "")).lower()
            if dk in seen_data:
                continue
            seen_data.add(dk)
            children.append(e)
        if children:
            done = 0
            for child in children:
                child_target = Path(child["original"])
                if child_target.exists():
                    skipped.append(f"{child_target}（原位置已有同名文件，已保留回收站副本）")
                    continue
                before = len(restored)
                _restore_one(child, child_target, p)
                if len(restored) > before:
                    done += 1
            if done == 0:
                skipped.append(f"{p}（回收站中的子项均无法恢复）")
            continue

        skipped.append(f"{p}（回收站中没有对应记录）")
    return {"restored": restored, "skipped": skipped}