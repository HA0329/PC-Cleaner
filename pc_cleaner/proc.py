"""进程占用检测（只读）：判断某个路径是否正被进程使用。

用途
----
本工具会清理 ``%LOCALAPPDATA%\\npm-cache\\_npx``、pnpm store、浏览器缓存等目录，
但这些目录里**可能正有程序在运行**：例如 ``node.exe`` 的 cmdline 指向
``...\\npm-cache\\_npx\\<hash>\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js``。
直接删除会破坏正在运行的程序，所以清理前必须先做一次「只读占用检测」，
本模块就是这一步的唯一实现（不删除、不写文件、不改注册表）。

零依赖策略
----------
- 必需依赖：仅标准库（``os`` / ``sys`` / ``json`` / ``time`` / ``shutil`` /
  ``subprocess`` / ``logging``），因此没有 psutil 也能完整工作。
- 可选加速：``psutil`` 存在时用它批量取进程 exe / cmdline，并额外检查
  ``open_files()``（能抓到「文件被打开但命令行里没有该路径」的情况）；
  没有 psutil 时自动回退到一次 ``powershell.exe`` 批量查询
  （``Get-CimInstance Win32_Process`` + ``ConvertTo-Json``）。
- 非 Windows（``sys.platform != "win32"``）：所有函数返回空结果，不抛异常。

缓存策略
--------
进程快照按模块级 cache + 时间戳缓存 **60 秒**（``_CACHE_TTL_SECONDS``），
因此一次扫描内 PowerShell 只会被调用一次；psutil 的 ``open_files()`` 结果
同样缓存 60 秒，避免批量检测时反复遍历句柄。``clear_cache()`` 可手动清空
（测试用）。

失败语义
--------
本模块**绝不抛异常**：没有 powershell、输出编码异常、权限不足、超时、
进程中途退出、psutil 单个进程报错……一律返回空列表 / False / 空字典，
并在 ``logging.getLogger("pc_cleaner.proc")`` 上记 debug 日志。
调用方应把「空结果」理解为「未检测到占用」（保守起见，异常时建议跳过删除）。
"""

from __future__ import annotations

import json
import locale
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable

# ===========================================================================
# 日志（只记 debug：本模块是探测辅助，失败即降级，不应打扰用户）
# ===========================================================================
logger = logging.getLogger("pc_cleaner.proc")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

# ===========================================================================
# 可选依赖：psutil（缺失则走 PowerShell 回退）
# ===========================================================================
_psutil: Any = None
try:  # 可选加速，不是必需依赖
    import psutil as _psutil_module

    _psutil = _psutil_module
except Exception:  # noqa: BLE001  # 没装 / 装了但导入失败，都按没有处理
    _psutil = None

# ===========================================================================
# 常量
# ===========================================================================
_CACHE_TTL_SECONDS = 60.0          # 进程缓存有效期（秒）
_SUBPROCESS_TIMEOUT = 10.0         # PowerShell 单次调用超时（秒）
_REPLACEMENT_CHAR = "\ufffd"       # 解码失败占位符
_REPLACEMENT_MIN_COUNT = 3         # 少于该数量视为个别脏字节，不重试
_REPLACEMENT_RATIO_LIMIT = 0.02    # 替换字符占比超过该比例视为编码猜错

_PS_COMMAND = (
    "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
    "ExecutablePath,CommandLine | ConvertTo-Json -Compress"
)

# ===========================================================================
# 模块级缓存（cache + 时间戳）
# ===========================================================================
_snapshot_cache: list[dict[str, Any]] | None = None
_snapshot_cache_time: float = 0.0
_open_files_cache: list[tuple[int, str, list[str]]] | None = None
_open_files_cache_time: float = 0.0
_powershell_probed: bool = False
_powershell_path: str | None = None


def clear_cache() -> None:
    """清空进程缓存（进程快照 / open_files / powershell 探测，测试用）。"""
    global _snapshot_cache, _snapshot_cache_time
    global _open_files_cache, _open_files_cache_time
    global _powershell_probed, _powershell_path

    _snapshot_cache = None
    _snapshot_cache_time = 0.0
    _open_files_cache = None
    _open_files_cache_time = 0.0
    _powershell_probed = False
    _powershell_path = None
    logger.debug("进程缓存已清空")


# ===========================================================================
# 路径规范化与匹配
# ===========================================================================
def _norm_path(path: str | os.PathLike[str]) -> str:
    """规范化路径用于大小写不敏感比较；无法处理时返回空串。

    使用 ``os.path.normcase(os.path.normpath(...))``：Windows 上会统一分隔符
    （``/`` → ``\\``）、折叠 ``..``、并转小写，因此 ``...\\.bin\\..\\x.js``
    与 ``...\\x.js`` 会被视为同一路径。
    """
    try:
        raw = os.fsdecode(os.fspath(path))
    except Exception:  # noqa: BLE001 参数不是路径（如 None / 任意对象）
        return ""
    if not raw:
        return ""
    try:
        norm = os.path.normcase(os.path.normpath(raw))
    except Exception:  # noqa: BLE001
        return ""
    return "" if norm in ("", ".") else norm


def _is_within(candidate: str, target: str) -> bool:
    """candidate 是否落在 target 之下（相等，或以 ``os.sep`` 为前缀）。

    两个入参都必须是 ``_norm_path`` 的产物（已 normcase + normpath）。
    """
    if not candidate or not target:
        return False
    if candidate == target:
        return True
    return candidate.startswith(target + os.sep)


def _split_windows_cmdline(cmdline: str) -> list[str]:
    """按 Windows 引号规则粗解析命令行，支持 ``"a b"`` 与裸 token。

    只做「够用」的解析：双引号成对切换、引号内空白不切分、引号内 ``\\"``
    视为字面引号。不处理连续反斜杠转义等边角情况——对「路径是否被进程使用」
    的判断而言足够。
    """
    if not cmdline:
        return []
    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    started = False
    i = 0
    n = len(cmdline)
    while i < n:
        ch = cmdline[i]
        if ch == "\\" and in_quotes and i + 1 < n and cmdline[i + 1] == '"':
            buf.append('"')
            started = True
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            started = True
        elif ch in " \t" and not in_quotes:
            if started or buf:
                tokens.append("".join(buf))
            buf = []
            started = False
        else:
            buf.append(ch)
            started = True
        i += 1
    if started or buf:
        tokens.append("".join(buf))
    return [t for t in tokens if t]


def _join_cmdline(parts: Any) -> str:
    """把 psutil 的 cmdline 列表还原成可再次解析的命令行字符串。"""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, (list, tuple)):
        return ""
    out: list[str] = []
    for item in parts:
        if item is None:
            continue
        token = str(item)
        if not token:
            continue
        if any(c in token for c in ' \t"'):
            token = '"' + token.replace('"', '\\"') + '"'
        out.append(token)
    return " ".join(out)


def _describe(pid: int, name: str, exe: str) -> str:
    """生成人类可读的进程描述，如 ``node.exe (pid 1234)``。"""
    label = (name or "").strip()
    if not label and exe:
        try:
            label = os.path.basename(exe) or exe
        except Exception:  # noqa: BLE001
            label = exe
    if not label:
        label = "unknown"
    return f"{label} (pid {pid})"


# ===========================================================================
# 进程快照：psutil 优先，PowerShell 回退
# ===========================================================================
def _powershell_executable() -> str | None:
    """返回可用的 powershell.exe 路径（结果缓存）；找不到返回 None。"""
    global _powershell_probed, _powershell_path

    if _powershell_probed:
        return _powershell_path
    _powershell_probed = True
    try:
        _powershell_path = shutil.which("powershell.exe") or shutil.which("powershell")
    except Exception:  # noqa: BLE001
        _powershell_path = None
    if not _powershell_path:
        logger.debug("未找到 powershell.exe，进程占用检测将不可用")
    return _powershell_path


def _looks_like_wrong_encoding(text: str) -> bool:
    """替换字符是否多到「像是用错编码解码」的程度。"""
    if not text:
        return False
    count = text.count(_REPLACEMENT_CHAR)
    if count < _REPLACEMENT_MIN_COUNT:
        return False
    return count / len(text) >= _REPLACEMENT_RATIO_LIMIT


def _run_powershell(encoding: str) -> str:
    """执行一次进程查询命令并返回 stdout（失败返回空串，绝不抛异常）。"""
    exe = _powershell_executable()
    if not exe:
        return ""
    argv = [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _PS_COMMAND,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 固定 argv，无 shell 注入面
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            encoding=encoding,
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("PowerShell 进程查询超时（%.0fs）", _SUBPROCESS_TIMEOUT)
        return ""
    except FileNotFoundError:
        logger.debug("PowerShell 不存在：%s", exe)
        return ""
    except Exception as exc:  # noqa: BLE001 权限 / OSError / 其它
        logger.debug("PowerShell 进程查询失败：%r", exc)
        return ""
    stdout = completed.stdout or ""
    if completed.returncode != 0 and not stdout.strip():
        logger.debug("PowerShell 返回码 %s，stderr=%s", completed.returncode, (completed.stderr or "")[:200])
    return stdout


def _parse_process_json(text: str) -> list[dict[str, Any]]:
    """把 ``ConvertTo-Json`` 的输出解析成进程字典列表（失败返回 []）。"""
    payload = text.lstrip("\ufeff").strip()
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001 输出被截断 / 混入告警
        logger.debug("进程 JSON 解析失败：%r", exc)
        return []
    if isinstance(data, dict):  # 只有一个进程时 ConvertTo-Json 返回对象而非数组
        data = [data]
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("ProcessId") or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        name = item.get("Name") or ""
        exe = item.get("ExecutablePath") or ""
        cmdline = item.get("CommandLine") or ""
        out.append(
            {
                "pid": pid,
                "name": str(name),
                "exe": str(exe),
                "cmdline": str(cmdline),
            }
        )
    return out


def _fallback_encoding() -> str | None:
    """返回 UTF-8 解码失败时应重试的编码；没有可用的回退编码时返回 None。

    首选 ``locale.getpreferredencoding(False)``；但在 UTF-8 模式
    （``python -X utf8`` / ``PYTHONUTF8=1``）下它会返回 "utf-8"，从而掩盖
    Windows 真实代码页，此时改用 ``locale.getencoding()``（3.11+，不受
    UTF-8 模式影响，Windows 上即 ANSI 代码页，如 cp936）。
    """
    candidates: list[str] = []
    try:
        candidates.append(locale.getpreferredencoding(False) or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        candidates.append(locale.getencoding() or "")
    except Exception:  # noqa: BLE001 3.11 以下没有该函数
        pass
    for enc in candidates:
        if enc and enc.strip().lower().replace("_", "-") not in ("utf-8", "utf8", "cp65001"):
            return enc
    return None


def _snapshot_via_powershell() -> list[dict[str, Any]]:
    """用一次 PowerShell 调用批量取全部进程信息（编码猜错时换编码重试一次）。"""
    text = _run_powershell("utf-8")
    if _looks_like_wrong_encoding(text):
        fallback = _fallback_encoding()
        if fallback:
            logger.debug("UTF-8 解码出现大量替换字符，改用 %s 重试一次", fallback)
            text = _run_powershell(fallback)
        else:
            logger.debug("UTF-8 解码出现大量替换字符，但无可用的回退编码")
    return _parse_process_json(text)


def _snapshot_via_psutil() -> list[dict[str, Any]]:
    """用 psutil 取全部进程信息（单个进程失败逐个吞掉）。"""
    if _psutil is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        iterator = _psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil.process_iter 失败：%r", exc)
        return []
    for proc in iterator:
        try:
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
            name = info.get("name") or ""
            exe = info.get("exe") or ""
            out.append(
                {
                    "pid": pid,
                    "name": str(name),
                    "exe": str(exe),
                    "cmdline": _join_cmdline(info.get("cmdline")),
                }
            )
        except Exception as exc:  # noqa: BLE001 进程可能已退出 / 无权限
            logger.debug("读取进程信息失败：%r", exc)
            continue
    return out


def process_snapshot() -> list[dict[str, Any]]:
    """返回 ``[{"pid": int, "name": str, "exe": str, "cmdline": str}]``；失败返回 []。

    带 60 秒模块级缓存，因此一次扫描内 PowerShell 只会被调用一次。
    非 Windows 直接返回 []。
    """
    global _snapshot_cache, _snapshot_cache_time

    if sys.platform != "win32":
        return []

    now = time.monotonic()
    if _snapshot_cache is not None and (now - _snapshot_cache_time) < _CACHE_TTL_SECONDS:
        return [dict(item) for item in _snapshot_cache]

    try:
        snapshot = _snapshot_via_psutil() if _psutil is not None else []
        if not snapshot:
            snapshot = _snapshot_via_powershell()
    except Exception as exc:  # noqa: BLE001 兜底：本模块绝不向上抛异常
        logger.debug("进程快照失败：%r", exc)
        snapshot = []

    _snapshot_cache = snapshot
    _snapshot_cache_time = time.monotonic()
    logger.debug("进程快照完成：%d 个进程（psutil=%s）", len(snapshot), _psutil is not None)
    return [dict(item) for item in _snapshot_cache]


# ===========================================================================
# psutil open_files 补充检测
# ===========================================================================
def _open_file_owners() -> list[tuple[int, str, list[str]]]:
    """返回 ``[(pid, name, [已规范化打开文件路径, ...]), ...]``，带 60 秒缓存。

    仅在 psutil 可用时有意义；逐进程异常全部吞掉（``open_files()`` 在 Windows
    上对系统进程常报 AccessDenied）。
    """
    global _open_files_cache, _open_files_cache_time

    if sys.platform != "win32" or _psutil is None:
        return []

    now = time.monotonic()
    if _open_files_cache is not None and (now - _open_files_cache_time) < _CACHE_TTL_SECONDS:
        return [(pid, name, list(paths)) for pid, name, paths in _open_files_cache]

    owners: list[tuple[int, str, list[str]]] = []
    try:
        iterator = _psutil.process_iter(["pid", "name"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil.process_iter 失败（open_files）：%r", exc)
        return []

    for proc in iterator:
        try:
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
            name = str(info.get("name") or "")
            paths: list[str] = []
            for handle in proc.open_files():
                norm = _norm_path(getattr(handle, "path", "") or "")
                if norm:
                    paths.append(norm)
            if paths:
                owners.append((pid, name, paths))
        except Exception as exc:  # noqa: BLE001 进程退出 / 无权限，逐个吞掉
            logger.debug("读取 open_files 失败：%r", exc)
            continue

    _open_files_cache = owners
    _open_files_cache_time = time.monotonic()
    return [(pid, name, list(paths)) for pid, name, paths in owners]


# ===========================================================================
# 占用判断
# ===========================================================================
def _process_uses(proc: dict[str, Any], target: str) -> bool:
    """单个进程的 exe 或 cmdline 任一 token 是否落在 target 之下。"""
    exe = _norm_path(proc.get("exe") or "")
    if _is_within(exe, target):
        return True
    for token in _split_windows_cmdline(proc.get("cmdline") or ""):
        if _is_within(_norm_path(token), target):
            return True
    return False


def is_available() -> bool:
    """能否进行进程占用检测（psutil 可用或 PowerShell 回退可用）。"""
    if sys.platform != "win32":
        return False
    if _psutil is not None:
        return True
    return _powershell_executable() is not None


def any_process_uses(path: str | os.PathLike[str]) -> list[str]:
    """返回正在使用该路径的进程描述列表（如 ``['node.exe (pid 1234)']``）；无则 []。

    判定语义：进程的 exe 路径，或 cmdline 解析出的任一 token，规范化后
    **等于**目标路径或**以 目标路径 + os.sep 为前缀**（大小写不敏感）。
    若 psutil 可用，还会检查 ``open_files()``，以覆盖「文件被打开但命令行里
    没有该路径」的情况。任何失败都返回 []。
    """
    if sys.platform != "win32":
        return []

    target = _norm_path(path)
    if not target:
        return []

    found: dict[int, str] = {}
    try:
        for proc in process_snapshot():
            try:
                if not _process_uses(proc, target):
                    continue
                pid = int(proc.get("pid") or 0)
                if pid in found:
                    continue
                found[pid] = _describe(pid, str(proc.get("name") or ""), str(proc.get("exe") or ""))
            except Exception as exc:  # noqa: BLE001 单个进程失败不影响其它
                logger.debug("进程匹配失败：%r", exc)
                continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("进程快照获取失败：%r", exc)

    try:
        for pid, name, paths in _open_file_owners():
            if pid in found:
                continue
            if any(_is_within(open_path, target) for open_path in paths):
                found[pid] = _describe(pid, name, "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("open_files 匹配失败：%r", exc)

    return list(found.values())


def paths_in_use(paths: Iterable[str | os.PathLike[str]]) -> dict[str, list[str]]:
    """批量检测：``{原样路径字符串: [进程描述...]}``，只返回有占用的项。"""
    out: dict[str, list[str]] = {}
    if sys.platform != "win32":
        return out
    try:
        items = list(paths)
    except Exception as exc:  # noqa: BLE001 传入的不是可迭代对象
        logger.debug("paths_in_use 入参不可迭代：%r", exc)
        return out
    for item in items:
        try:
            key = item if isinstance(item, str) else str(item)
        except Exception:  # noqa: BLE001
            continue
        if key in out:
            continue
        users = any_process_uses(item)
        if users:
            out[key] = users
    return out


def is_path_in_use(path: str | os.PathLike[str]) -> bool:
    """便捷判断：该路径是否正被任一进程使用。"""
    try:
        return bool(any_process_uses(path))
    except Exception:  # noqa: BLE001 理论上不会发生，兜底保持「绝不抛异常」
        return False
