"""电脑体检：只读汇总系统健康状态（零依赖、纯标准库、绝不改动系统）。

本模块为 CLI 的 ``--health`` 提供「电脑体检」能力：把散落在注册表、WMI、
事件日志里的健康信号收集成一份**可读**（终端文本 / Markdown）且**可编程**
（``to_dict()``）的报告，方便用户一眼看出「这台电脑现在有什么隐患」。

体检项（每项独立容错，任一失败只让该项变成 ``unknown``）：

- 系统版本（Windows 11 判定：build >= 22000）；
- Windows Update 是否被长期暂停（暂停到期时间在未来 → 警告）；
- 是否有待重启的更新 / 文件重命名；
- Secure Boot、TPM；
- 各盘可用空间；
- 设备管理器中的问题设备（``pnputil /enum-devices /problem``）；
- 近 7 天的异常关机事件（Id 6008 / 41 / 1001）；
- SMB1、来宾登录、防火墙；
- 已注册的杀毒软件、磁盘健康（温度/磨损）、幽灵设备残留；
- 长路径支持、休眠文件。

安全约定（**硬性**）：
- 本模块**只读**：只读注册表、只跑只读查询命令；
- **绝不**写注册表 / 写文件 / 删除任何东西；
- **绝不**导入或调用 :mod:`pc_cleaner.engine` 及任何清理函数；
- 非 Windows 平台所有项返回 ``unknown``（detail=「非 Windows 平台」），不抛异常；
- ``collect()`` 顶层兜底：即使内部异常也返回已收集的项。

用法::

    python -m pc_cleaner.health          # 打印终端文本报告
    python -c "from pc_cleaner.health import collect; print(collect().to_markdown())"
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import json
import locale
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:  # 包内导入（python -m pc_cleaner.health / import pc_cleaner.health）
    from .i18n import t
except ImportError:  # 兜底：直接执行本文件时（python pc_cleaner/health.py）
    from pc_cleaner.i18n import t  # type: ignore[no-redef]

LOGGER = logging.getLogger("pc_cleaner.health")

#: ``to_dict()`` 输出的结构版本号，供外部消费者判断字段兼容性
SCHEMA_VERSION = 1

#: ``HealthItem.status`` 的合法取值
STATUSES: tuple[str, ...] = ("ok", "warn", "error", "unknown")

#: 终端文本报告使用的状态图标
STATUS_ICON: dict[str, str] = {
    "ok": "✅",
    "warn": "⚠️",
    "error": "❌",
    "unknown": "❓",
}

#: PowerShell 子进程最长等待时间（秒）
PS_TIMEOUT = 10.0

#: 慢项超过该耗时后，剩余慢项直接标记为 unknown，保证 collect() 快速返回
SLOW_BUDGET_SECONDS = 11.0

#: 创建子进程时不弹出控制台窗口（非 Windows 下为 0）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 注册表根键
_HKLM = 0x80000002

#: Windows 11 的起始内部版本号（注册表 ProductName 仍写 Windows 10 是微软遗留）
_WIN11_MIN_BUILD = 22000

#: 判定为「异常关机」的事件 Id
_SHUTDOWN_EVENT_IDS = (6008, 41, 1001)


# ---------------------------------------------------------------------------
# 通用小工具（全部只读、全部容错）
# ---------------------------------------------------------------------------
def _is_windows() -> bool:
    """判断当前是否为 Windows 平台。"""
    return sys.platform == "win32"


def _now_text() -> str:
    """返回 ``YYYY-MM-DD HH:MM:SS`` 格式的当前本地时间。"""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _size_text(num: float) -> str:
    """把字节数格式化为人类可读字符串。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return t("health.common.unknown_value")
    if value < 0:
        return t("health.common.unknown_value")
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PB"


def _decode(raw: bytes | None) -> str:
    """把子进程字节输出解码为文本（utf-8 优先，失败回退本地代码页）。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 解码失败回退
        try:
            return raw.decode(locale.getpreferredencoding(False), errors="replace")
        except Exception:  # noqa: BLE001 再失败则丢弃
            return ""


def _run(
    args: list[str],
    timeout: float = PS_TIMEOUT,
) -> tuple[int, str]:
    """执行只读命令并返回 ``(returncode, stdout)``；任何失败返回 ``(-1, "")``。

    统一使用 ``capture_output=True``、``creationflags=CREATE_NO_WINDOW``、
    ``timeout=10``、``encoding="utf-8", errors="replace"``（失败按本地代码页重试一次）。
    绝不使用 ``shell=True``，绝不写入任何文件。
    """
    # 让子进程输出 UTF-8：Windows 自带命令（pnputil 等）默认按控制台代码页
    # 输出，中文系统上是 cp936，直接按 utf-8 解码会乱码。用 cmd 把控制台
    # 代码页切成 65001 再执行（只影响这个临时控制台，不改系统设置）。
    if _is_windows() and args and not args[0].lower().startswith("cmd"):
        args = ["cmd", "/c", "chcp", "65001", ">nul", "&&", *args]

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOGGER.debug("命令超时：%s", args[:2])
        return -1, ""
    except (OSError, ValueError) as exc:  # 命令不存在 / 参数非法
        LOGGER.debug("命令执行失败 %s: %s", args[:2], exc)
        return -1, ""

    text = _decode(proc.stdout)
    if not text.strip() and proc.stdout:
        # utf-8 解出来是空白时，按本机代码页再试一次（中文系统常见 cp936）
        try:
            text = proc.stdout.decode(
                locale.getpreferredencoding(False), errors="replace"
            )
        except Exception:  # noqa: BLE001 回退失败则保留原结果
            LOGGER.debug("本地代码页回退解码失败：%s", args[:2])
    return proc.returncode, text


def _run_powershell(script: str, timeout: float = PS_TIMEOUT) -> str:
    """执行只读 PowerShell 脚本并返回 stdout（失败返回空串）。

    使用 ``-NoProfile -NonInteractive`` 以跳过用户配置、避免任何交互等待；
    脚本开头强制 stdout 用 UTF-8，保证中文输出不乱码。
    """
    args = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script,
    ]
    _code, out = _run(args, timeout=timeout)
    return out


def _json_payload(raw: str) -> dict[str, Any]:
    """从 PowerShell 输出里取最后一个 ``{...}`` 对象并解析为 dict。"""
    if not raw:
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError) as exc:
        LOGGER.debug("JSON 解析失败：%s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """把 PowerShell 的单对象 / 数组统一成 list（``null`` → 空列表）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ps_json(script: str, timeout: float = PS_TIMEOUT) -> dict[str, Any]:
    """执行 PowerShell 并把输出解析成 dict（失败返回空 dict）。"""
    return _json_payload(_run_powershell(script, timeout=timeout))


def _to_int(value: Any) -> int | None:
    """尽力把任意值转成 int；失败返回 ``None``。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _query_hklm(
    subkey: str, name: str
) -> tuple[bool, int | None, Any]:
    """只读查询 ``HKLM\\<subkey>`` 下的一个值。

    返回 ``(键是否存在, 值类型, 值)``：值不存在时值为 ``None``；
    任何异常都被吞掉并记录 debug 日志（只读探测，绝不抛出）。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_READ) as key:
            try:
                value, vtype = winreg.QueryValueEx(key, name)
                return True, vtype, value
            except FileNotFoundError:
                return True, None, None
    except FileNotFoundError:
        return False, None, None
    except OSError as exc:
        LOGGER.debug("读取注册表失败 %s\\%s: %s", subkey, name, exc)
        return False, None, None
    except Exception as exc:  # noqa: BLE001 只读探测，任何异常都不外抛
        LOGGER.debug("读取注册表异常 %s\\%s: %s", subkey, name, exc)
        return False, None, None


def _hklm_exists(subkey: str) -> bool:
    """只读判断注册表子键是否存在。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_READ):
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        LOGGER.debug("打开注册表键失败 %s: %s", subkey, exc)
        return False
    except Exception as exc:  # noqa: BLE001 只读探测，任何异常都不外抛
        LOGGER.debug("打开注册表键异常 %s: %s", subkey, exc)
        return False


def _parse_iso(text: Any) -> _dt.datetime | None:
    """解析注册表里的 UTC 时间串（形如 ``2041-01-05T16:03:09Z``）。"""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _humanize_delta(delta: _dt.timedelta) -> str:
    """把时间差转成「约 X 天 / 约 X 个月 / 约 X 年」的中文描述。"""
    days = int(delta.total_seconds() // 86400)
    if days < 1:
        hours = int(delta.total_seconds() // 3600)
        return t("health.common.duration.hours", hours=max(hours, 0))
    if days < 60:
        return t("health.common.duration.days", days=days)
    if days < 730:
        return t("health.common.duration.months", months=f"{days / 30.44:.1f}")
    return t("health.common.duration.years", years=f"{days / 365.25:.1f}")


def _fixed_drive_roots() -> list[str]:
    """用 ``kernel32.GetLogicalDrives()`` 找出所有**固定盘**的根路径。

    位图第 0 位是 A、第 1 位是 B……只保留 ``GetDriveTypeW`` 返回
    ``DRIVE_FIXED(3)`` 的盘，避免对光驱 / U 盘 / 网络盘做无意义统计。
    """
    roots: list[str] = []
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (OSError, AttributeError) as exc:
        LOGGER.debug("加载 kernel32 失败：%s", exc)
        return roots

    try:
        mask = int(kernel32.GetLogicalDrives())
    except Exception as exc:  # noqa: BLE001 只读探测
        LOGGER.debug("GetLogicalDrives 失败：%s", exc)
        return roots

    try:
        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
    except Exception as exc:  # noqa: BLE001 仅影响类型声明
        LOGGER.debug("声明 GetDriveTypeW 签名失败：%s", exc)
        get_drive_type = None

    for index in range(26):
        if not (mask >> index) & 1:
            continue
        root = f"{chr(ord('A') + index)}:\\"
        if get_drive_type is not None:
            try:
                if int(get_drive_type(root)) != 3:  # 3 == DRIVE_FIXED
                    continue
            except Exception as exc:  # noqa: BLE001 单盘探测失败则跳过
                LOGGER.debug("GetDriveTypeW(%s) 失败：%s", root, exc)
                continue
        roots.append(root)
    return roots


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class HealthItem:
    """一个体检项的结果。"""

    key: str
    label: str
    status: str
    detail: str
    advice: str = ""

    def to_dict(self) -> dict[str, str]:
        """转成 JSON 可序列化的字典。"""
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "advice": self.advice,
        }


@dataclass
class HealthReport:
    """一次完整体检的结果。"""

    items: list[HealthItem] = field(default_factory=list)
    generated: str = field(default_factory=_now_text)

    def counts(self) -> dict[str, int]:
        """按状态统计各体检项数量。"""
        result = {status: 0 for status in STATUSES}
        for item in self.items:
            if item.status in result:
                result[item.status] += 1
            else:  # 兜底：未知状态归入 unknown
                result["unknown"] += 1
        return result

    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 可序列化的字典（含 ``schema_version``）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "generated": self.generated,
            "counts": self.counts(),
            "items": [item.to_dict() for item in self.items],
        }

    def to_markdown(self) -> str:
        """生成 Markdown 表格报告。"""
        counts = self.counts()
        lines = [
            t("health.report.markdown.title"),
            "",
            t("health.report.markdown.generated", time=self.generated),
            t(
                "health.report.markdown.summary",
                ok=counts["ok"],
                warn=counts["warn"],
                error=counts["error"],
                unknown=counts["unknown"],
            ),
            "",
            t("health.report.markdown.header"),
            t("health.report.markdown.separator"),
        ]
        for item in self.items:
            icon = STATUS_ICON.get(item.status, "❓")
            detail = item.detail.replace("|", "\\|").replace("\n", " ")
            advice = (
                item.advice or t("health.report.markdown.empty_advice")
            ).replace("|", "\\|").replace("\n", " ")
            lines.append(
                t(
                    "health.report.markdown.row",
                    icon=icon,
                    label=item.label,
                    detail=detail,
                    advice=advice,
                )
            )
        lines.append("")
        lines.append(t("health.report.markdown.footer"))
        return "\n".join(lines)

    def to_text(self) -> str:
        """生成终端文本报告（带 ✅⚠️❌❓ 图标）。"""
        counts = self.counts()
        lines = [
            t("health.report.title"),
            t("health.report.generated", time=self.generated),
            t(
                "health.report.summary",
                ok=counts["ok"],
                warn=counts["warn"],
                error=counts["error"],
                unknown=counts["unknown"],
            ),
            "-" * 60,
        ]
        for item in self.items:
            icon = STATUS_ICON.get(item.status, "❓")
            lines.append(f"{icon} {item.label}")
            lines.append(f"    {item.detail}")
            if item.advice:
                lines.append(f"    💡 {item.advice}")
        lines.append("-" * 60)
        lines.append(t("health.report.footer"))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 体检项实现（每项独立容错，绝不抛出）
# ---------------------------------------------------------------------------
def _check_os_version() -> HealthItem:
    """系统版本：读注册表 CurrentVersion，build>=22000 显示为 Windows 11。"""
    key = "os_version"
    label = t("health.os_version.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        sub = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        product = _query_hklm(sub, "ProductName")[2]
        display = _query_hklm(sub, "DisplayVersion")[2]
        build_raw = _query_hklm(sub, "CurrentBuildNumber")[2]
        ubr = _query_hklm(sub, "UBR")[2]
        edition = _query_hklm(sub, "EditionID")[2]

        build = _to_int(build_raw)
        if build is None:
            # 少数机器 CurrentBuildNumber 缺失，用 CurrentBuild 兜底
            build = _to_int(_query_hklm(sub, "CurrentBuild")[2])

        if build is None and not product:
            return HealthItem(
                key,
                label,
                "unknown",
                t("health.os_version.detail.registry_failed"),
            )

        # 微软遗留：Windows 11 的 ProductName 依然写着 Windows 10
        family = "Windows 11" if (build is not None and build >= _WIN11_MIN_BUILD) else "Windows 10"
        name = str(product) if product else family
        if build is not None and build >= _WIN11_MIN_BUILD and "Windows 10" in name:
            name = name.replace("Windows 10", "Windows 11")

        parts: list[str] = []
        if display:
            parts.append(t("health.os_version.detail.display", version=display))
        if build is not None:
            build_text = t("health.os_version.detail.build", build=build)
            if _to_int(ubr) is not None:
                build_text += f".{_to_int(ubr)}"
            parts.append(build_text)
        if edition:
            parts.append(t("health.os_version.detail.edition", edition=edition))

        detail = name
        if parts:
            detail = t(
                "health.os_version.detail.paren",
                name=name,
                parts=t("health.sep.comma").join(parts),
            )
        advice = ""
        if build is not None and build < _WIN11_MIN_BUILD:
            advice = t("health.os_version.advice.old_windows")
        return HealthItem(key, label, "ok", detail, advice)
    except Exception as exc:  # noqa: BLE001 单项失败不影响其它项
        LOGGER.debug("os_version 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.os_version.detail.failed"))


def _check_windows_update() -> HealthItem:
    """Windows Update：暂停到期时间仍在未来 → 警告。"""
    key = "windows_update"
    label = t("health.windows_update.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        sub = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
        expiry = _parse_iso(_query_hklm(sub, "PauseUpdatesExpiryTime")[2])
        feature_start = _parse_iso(_query_hklm(sub, "PauseFeatureUpdatesStartTime")[2])
        quality_start = _parse_iso(_query_hklm(sub, "PauseQualityUpdatesStartTime")[2])

        now = _dt.datetime.now(_dt.timezone.utc)
        if expiry is None:
            if feature_start is None and quality_start is None:
                return HealthItem(
                    key, label, "ok", t("health.windows_update.detail.ok")
                )
            return HealthItem(
                key,
                label,
                "warn",
                t("health.windows_update.detail.missing_expiry"),
                t("health.windows_update.advice.resume"),
            )

        if expiry <= now:
            return HealthItem(
                key,
                label,
                "ok",
                t(
                    "health.windows_update.detail.expired",
                    expiry=expiry.astimezone().strftime("%Y-%m-%d %H:%M"),
                ),
            )

        paused_since = min(
            [t for t in (feature_start, quality_start) if t is not None],
            default=None,
        )
        detail = t(
            "health.windows_update.detail.paused",
            expiry=expiry.astimezone().strftime("%Y-%m-%d %H:%M"),
        )
        if paused_since is not None:
            detail += t(
                "health.windows_update.detail.paused_since",
                since=paused_since.astimezone().strftime("%Y-%m-%d"),
                delta=_humanize_delta(now - paused_since),
            )
        else:
            detail += t(
                "health.windows_update.detail.until_expiry",
                delta=_humanize_delta(expiry - now),
            )
        return HealthItem(
            key,
            label,
            "warn",
            detail,
            t("health.windows_update.advice.long_pause"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("windows_update 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.windows_update.detail.failed"))


def _check_pending_reboot() -> HealthItem:
    """待重启：CBS / WU 重启标记或 PendingFileRenameOperations 任一存在 → 警告。"""
    key = "pending_reboot"
    label = t("health.pending_reboot.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        hits: list[str] = []
        if _hklm_exists(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"
        ):
            hits.append(t("health.pending_reboot.detail.cbs"))
        if _hklm_exists(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
        ):
            hits.append(t("health.pending_reboot.detail.windows_update"))
        exists, _vtype, value = _query_hklm(
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
            "PendingFileRenameOperations",
        )
        if exists and value:
            hits.append(t("health.pending_reboot.detail.file_rename"))

        if not hits:
            return HealthItem(key, label, "ok", t("health.pending_reboot.detail.ok"))
        return HealthItem(
            key,
            label,
            "warn",
            t(
                "health.pending_reboot.detail.pending",
                items=t("health.sep.list").join(hits),
            ),
            t("health.pending_reboot.advice.reboot"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("pending_reboot 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.pending_reboot.detail.failed"))


def _check_secure_boot() -> HealthItem:
    """Secure Boot：注册表 State\\UEFISecureBootEnabled 为 0 或不存在 → 警告。"""
    key = "secure_boot"
    label = t("health.secure_boot.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        exists, _vtype, value = _query_hklm(
            r"SYSTEM\CurrentControlSet\Control\SecureBoot\State",
            "UEFISecureBootEnabled",
        )
        if not exists:
            return HealthItem(
                key,
                label,
                "warn",
                t("health.secure_boot.detail.missing"),
                t("health.secure_boot.advice.enable"),
            )
        code = _to_int(value)
        if code == 1:
            return HealthItem(key, label, "ok", t("health.secure_boot.detail.enabled"))
        return HealthItem(
            key,
            label,
            "warn",
            t("health.secure_boot.detail.disabled", value=value),
            t("health.secure_boot.advice.bios"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("secure_boot 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.secure_boot.detail.failed"))


def _check_tpm() -> HealthItem:
    """TPM：注册表服务键存在即视为存在，并用 Get-Tpm 补充版本/状态。"""
    key = "tpm"
    label = t("health.tpm.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        exists = _hklm_exists(r"SYSTEM\CurrentControlSet\Services\TPM")
        if not exists:
            return HealthItem(
                key,
                label,
                "warn",
                t("health.tpm.detail.no_service"),
                t("health.tpm.advice.enable"),
            )

        extra = ""
        raw = _run_powershell(
            "try { $t = Get-Tpm -ErrorAction Stop; "
            "'tpm={' + [int][bool]$t.TpmPresent + ';ready=' + [int][bool]$t.TpmReady + ';enabled=' "
            "+ [int][bool]$t.TpmEnabled + '}' } catch { }",
            timeout=PS_TIMEOUT,
        )
        present = ready = enabled = None
        if "tpm=" in raw:
            body = raw.split("tpm=", 1)[1]
            body = body.split("}", 1)[0]
            for chunk in body.split(";"):
                if "=" not in chunk:
                    continue
                name, _, val = chunk.partition("=")
                num = _to_int(val.strip())
                if name.strip() == "ready":
                    ready = num
                elif name.strip() == "enabled":
                    enabled = num
                elif name.strip() == "present":
                    present = num

        if present == 0:
            return HealthItem(
                key,
                label,
                "warn",
                t("health.tpm.detail.no_chip"),
                t("health.tpm.advice.firmware"),
            )
        flags: list[str] = []
        if ready == 1:
            flags.append(t("health.tpm.detail.ready"))
        if enabled == 1:
            flags.append(t("health.tpm.detail.enabled_flag"))
        extra = (
            t("health.tpm.detail.paren", flags=t("health.sep.comma").join(flags))
            if flags
            else ""
        )
        return HealthItem(
            key, label, "ok", t("health.tpm.detail.present", extra=extra)
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("tpm 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.tpm.detail.failed"))


def _check_disk_space() -> HealthItem:
    """磁盘空间：遍历固定盘，可用率 <10% 警告、<5% 异常。"""
    key = "disk_space"
    label = t("health.disk_space.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        roots = _fixed_drive_roots()
        if not roots:
            return HealthItem(key, label, "unknown", t("health.disk_space.detail.no_drives"))

        parts: list[str] = []
        worst_pct = 100.0
        worst_drive = ""
        error_drives: list[str] = []
        for root in roots:
            try:
                usage = shutil.disk_usage(root)
            except OSError as exc:
                LOGGER.debug("disk_usage(%s) 失败：%s", root, exc)
                continue
            if usage.total <= 0:
                continue
            free_pct = usage.free / usage.total * 100.0
            parts.append(
                t(
                    "health.disk_space.detail.drive",
                    drive=root,
                    free=_size_text(usage.free),
                    total=_size_text(usage.total),
                    percent=f"{free_pct:.1f}",
                )
            )
            if free_pct < worst_pct:
                worst_pct, worst_drive = free_pct, root
            if free_pct < 5.0:
                error_drives.append(root)

        if not parts:
            return HealthItem(
                key, label, "unknown", t("health.disk_space.detail.unreadable")
            )

        detail = t("health.sep.detail").join(parts)
        if error_drives:
            return HealthItem(
                key,
                label,
                "error",
                detail
                + t(
                    "health.disk_space.detail.critical",
                    drives=t("health.sep.list").join(error_drives),
                ),
                t("health.disk_space.advice.clean"),
            )
        if worst_pct < 10.0:
            return HealthItem(
                key,
                label,
                "warn",
                detail
                + t(
                    "health.disk_space.detail.low",
                    drive=worst_drive,
                    percent=f"{worst_pct:.1f}",
                ),
                t("health.disk_space.advice.low"),
            )
        return HealthItem(key, label, "ok", detail)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("disk_space 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.disk_space.detail.failed"))


#: pnputil 输出里的实例 ID 行（中英文界面都覆盖；v0.9.4 起不再依赖中文字面量）
_INSTANCE_ID_RE = re.compile(r"^(?:instance\s*id|实例\s*id)\s*[:：]", re.IGNORECASE)


def _check_devices() -> HealthItem:
    """设备问题：``pnputil /enum-devices /problem`` 解析出有问题的设备。

    v0.9.4：改为**与界面语言无关**的解析——只看 ``Instance ID`` / ``实例 ID`` 行
    （中英文都覆盖，正则匹配）。此前依赖中文字面量（"未找到设备"等），
    在英文/其它语言的 Windows 上会误判。
    """
    key = "devices"
    label = t("health.devices.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        # pnputil 是 Windows 自带命令；退出码可能非 0（无问题设备时也常见），
        # 因此只依据输出内容判定。
        _code, out = _run(["pnputil.exe", "/enum-devices", "/problem"], timeout=PS_TIMEOUT)
        if not out.strip():
            return HealthItem(key, label, "unknown", t("health.devices.detail.no_output"))

        devices: list[str] = []
        for line in out.splitlines():
            text = line.strip()
            match = _INSTANCE_ID_RE.match(text)
            if match:
                value = text[match.end():].strip()
                if value:
                    devices.append(value)

        if not devices:
            # 没有任何 Instance ID 行 → 没有列出问题设备（与系统语言无关）
            return HealthItem(key, label, "ok", t("health.devices.detail.ok"))

        head = t("health.sep.list").join(devices[:3])
        more = (
            t("health.devices.detail.more", count=len(devices))
            if len(devices) > 3
            else ""
        )
        return HealthItem(
            key,
            label,
            "warn",
            t(
                "health.devices.detail.problem",
                count=len(devices),
                devices=head + more,
            ),
            t("health.devices.advice.reinstall"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("devices 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.devices.detail.failed"))


def _check_unexpected_shutdown() -> HealthItem:
    """异常关机：System 日志近 7 天 Id 6008/41/1001。"""
    key = "unexpected_shutdown"
    label = t("health.unexpected_shutdown.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        script = (
            "$r = [ordered]@{}; "
            "try { $r.events = @(Get-WinEvent -FilterHashtable "
            "@{LogName='System'; Id=6008,41,1001; StartTime=(Get-Date).AddDays(-7)} "
            "-ErrorAction SilentlyContinue -MaxEvents 20 | ForEach-Object { "
            "[pscustomobject]@{ t = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'); "
            "id = [int]$_.Id; p = [string]$_.ProviderName } }) } catch { $r.events = @() }; "
            "$r | ConvertTo-Json -Compress -Depth 4"
        )
        payload = _ps_json(script, timeout=PS_TIMEOUT)
        if not payload:
            return HealthItem(
                key, label, "unknown", t("health.unexpected_shutdown.detail.no_log")
            )

        events = _as_list(payload.get("events"))
        if not events:
            return HealthItem(
                key, label, "ok", t("health.unexpected_shutdown.detail.ok")
            )

        listed = []
        for ev in events[:5]:
            if not isinstance(ev, dict):
                continue
            listed.append(
                t(
                    "health.unexpected_shutdown.detail.event",
                    time=ev.get("t", "?"),
                    id=ev.get("id", "?"),
                )
            )
        detail = t("health.unexpected_shutdown.detail.count", count=len(events))
        if listed:
            detail = t(
                "health.unexpected_shutdown.detail.with_events",
                detail=detail,
                events=t("health.sep.list").join(listed),
            )
        return HealthItem(
            key,
            label,
            "warn",
            detail,
            t("health.unexpected_shutdown.advice.check"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("unexpected_shutdown 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.unexpected_shutdown.detail.failed"))


def _check_smb1() -> HealthItem:
    """SMB1：mrxsmb10 / srv 服务的 Start 值（4=已禁用）。"""
    key = "smb1"
    label = t("health.smb1.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        findings: list[str] = []
        checked: list[str] = []
        for service in ("mrxsmb10", "srv"):
            sub = rf"SYSTEM\CurrentControlSet\Services\{service}"
            exists, _vtype, value = _query_hklm(sub, "Start")
            if not exists:
                continue
            start = _to_int(value)
            if start is None:
                checked.append(t("health.smb1.detail.unknown_start", service=service))
                continue
            checked.append(t("health.smb1.detail.start", service=service, start=start))
            if start != 4:
                findings.append(
                    t("health.smb1.detail.not_disabled", service=service, start=start)
                )

        if not checked:
            return HealthItem(key, label, "ok", t("health.smb1.detail.no_config"))
        if findings:
            return HealthItem(
                key,
                label,
                "warn",
                t(
                    "health.smb1.detail.enabled",
                    findings=t("health.sep.list").join(findings),
                ),
                t("health.smb1.advice.disable"),
            )
        return HealthItem(
            key,
            label,
            "ok",
            t(
                "health.smb1.detail.disabled",
                services=t("health.sep.comma").join(checked),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("smb1 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.smb1.detail.failed"))


def _check_insecure_guest() -> HealthItem:
    """来宾登录：AllowInsecureGuestAuth 为 1 → 警告。"""
    key = "insecure_guest"
    label = t("health.insecure_guest.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        exists, _vtype, value = _query_hklm(
            r"SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters",
            "AllowInsecureGuestAuth",
        )
        if not exists or value is None:
            return HealthItem(
                key, label, "ok", t("health.insecure_guest.detail.not_set")
            )
        if _to_int(value) == 1:
            return HealthItem(
                key,
                label,
                "warn",
                t("health.insecure_guest.detail.enabled"),
                t("health.insecure_guest.advice.disable"),
            )
        return HealthItem(
            key,
            label,
            "ok",
            t("health.insecure_guest.detail.disabled", value=value),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("insecure_guest 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.insecure_guest.detail.failed"))


def _check_firewall() -> HealthItem:
    """防火墙：三个配置档 EnableFirewall 全为 1 → 正常，否则警告。"""
    key = "firewall"
    label = t("health.firewall.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        profiles = (
            ("Domain", t("health.firewall.profile.domain")),
            ("Standard", t("health.firewall.profile.private")),
            ("Public", t("health.firewall.profile.public")),
        )
        enabled: list[str] = []
        disabled: list[str] = []
        missing: list[str] = []
        for name, cn in profiles:
            sub = (
                r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters"
                rf"\FirewallPolicy\{name}Profile"
            )
            exists, _vtype, value = _query_hklm(sub, "EnableFirewall")
            if not exists:
                missing.append(cn)
                continue
            if _to_int(value) == 1:
                enabled.append(cn)
            else:
                disabled.append(cn)

        if not enabled and not disabled:
            return HealthItem(
                key, label, "unknown", t("health.firewall.detail.unreadable")
            )

        none_text = t("health.common.none")
        detail = t(
            "health.firewall.detail.summary",
            enabled=t("health.sep.list").join(enabled) or none_text,
            disabled=t("health.sep.list").join(disabled) or none_text,
        )
        if missing:
            detail += t(
                "health.firewall.detail.missing",
                missing=t("health.sep.list").join(missing),
            )
        if disabled:
            return HealthItem(
                key,
                label,
                "warn",
                detail,
                t("health.firewall.advice.enable_all"),
            )
        return HealthItem(key, label, "ok", detail)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("firewall 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.firewall.detail.failed"))


def _collect_ps_bundle() -> dict[str, Any]:
    """一次 PowerShell 调用取回杀毒软件 / 磁盘健康 / 幽灵设备三组数据。

    合并调用是为了把 ``collect()`` 的耗时压到 15 秒内。
    """
    script = (
        "$r = [ordered]@{}; "
        "try { $r.av = @(Get-CimInstance -Namespace root\\SecurityCenter2 "
        "-ClassName AntiVirusProduct -ErrorAction SilentlyContinue | ForEach-Object { "
        "[pscustomobject]@{ name = [string]$_.displayName; "
        "state = [string]$_.productState } }) } catch { $r.av = @() }; "
        "try { $r.disks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | ForEach-Object { "
        "$d = $_; try { $c = $d | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue } "
        "catch { $c = $null }; [pscustomobject]@{ name = [string]$d.FriendlyName; "
        "temp = $(if ($c) { [int]$c.Temperature } else { -1 }); "
        "wear = $(if ($c) { [int]$c.Wear } else { -1 }) } }) } catch { $r.disks = @() }; "
        "try { $r.phantom = @(Get-PnpDevice -PresentOnly:$false "
        "-ErrorAction SilentlyContinue).Count } catch { $r.phantom = -1 }; "
        "$r | ConvertTo-Json -Compress -Depth 4"
    )
    return _ps_json(script, timeout=PS_TIMEOUT)


def _av_state_text(code: int | None) -> str:
    """把 SecurityCenter2 的 ``productState`` 翻译成状态文案。

    WSC 的 ``productState`` 位布局（v0.9.4 按实测校准）：

    - bit 16-23：产品类型（``0x04`` = 防病毒）；
    - **bit 12-15：扫描引擎状态**（0=关闭 / 1=开启 / 2=暂停 / 3=过期）；
    - bit 4-7：病毒库状态（0=最新 / 1=需要更新）。

    实例：``0x041000`` = 防病毒 + 开启 + 库最新；``0x041010`` = 开启但库需更新；
    ``0x042000`` = 防病毒 + **已暂停**（本机火绒实测出现过该值）。
    """
    if code is None:
        return t("health.antivirus.state.unknown")
    scanner = (code >> 12) & 0x0F
    signatures = (code >> 4) & 0x0F
    if scanner == 0x01:
        if signatures == 0x00:
            return t("health.antivirus.state.up_to_date")
        if signatures == 0x01:
            return t("health.antivirus.state.outdated")
        return t("health.antivirus.state.enabled")
    if scanner == 0x00:
        return t("health.antivirus.state.realtime_off", code=code)
    if scanner == 0x02:
        return t("health.antivirus.state.snoozed", code=code)
    if scanner == 0x03:
        return t("health.antivirus.state.expired", code=code)
    return t("health.antivirus.state.unknown_code", code=code)


def _av_is_active(code: int | None) -> bool:
    """判断 ``productState`` 是否表示实时保护已开启（bit 12-15 == 1）。"""
    return code is not None and (code >> 12) & 0x0F == 0x01


def _check_antivirus(bundle: dict[str, Any]) -> HealthItem:
    """杀毒软件：SecurityCenter2 注册的防病毒产品。"""
    key = "antivirus"
    label = t("health.antivirus.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        if "av" not in bundle:
            return HealthItem(
                key, label, "unknown", t("health.antivirus.detail.no_security_center")
            )

        products = _as_list(bundle.get("av"))
        if not products:
            return HealthItem(
                key,
                label,
                "warn",
                t("health.antivirus.detail.none_registered"),
                t("health.antivirus.advice.install"),
            )

        parts: list[str] = []
        inactive = False
        for product in products:
            if not isinstance(product, dict):
                continue
            name = str(
                product.get("name") or t("health.antivirus.detail.unknown_product")
            )
            code = _to_int(product.get("state"))
            state = _av_state_text(code)
            if not _av_is_active(code):
                inactive = True
            parts.append(t("health.antivirus.detail.product", name=name, state=state))
        if not parts:
            return HealthItem(
                key, label, "unknown", t("health.antivirus.detail.unparsable")
            )

        detail = t("health.sep.list").join(parts)
        if inactive:
            return HealthItem(
                key,
                label,
                "warn",
                detail,
                t("health.antivirus.advice.enable_realtime"),
            )
        return HealthItem(key, label, "ok", detail)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("antivirus 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.antivirus.detail.failed"))


def _check_phantom_devices(bundle: dict[str, Any]) -> HealthItem:
    """幽灵设备：曾经接入过、当前不存在的设备残留（属正常现象）。"""
    key = "phantom_devices"
    label = t("health.phantom_devices.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        count = _to_int(bundle.get("phantom"))
        if count is None or count < 0:
            _code, out = _run(["pnputil.exe", "/enum-devices"], timeout=PS_TIMEOUT)
            count = len(
                [
                    line
                    for line in out.splitlines()
                    if _INSTANCE_ID_RE.match(line.strip())
                ]
            )
            if count == 0:
                return HealthItem(
                    key,
                    label,
                    "unknown",
                    t("health.phantom_devices.detail.unable"),
                )
        if count > 0:
            return HealthItem(
                key,
                label,
                "ok",
                t("health.phantom_devices.detail.count", count=count),
                t("health.phantom_devices.advice.ignore"),
            )
        return HealthItem(key, label, "ok", t("health.phantom_devices.detail.none"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("phantom_devices 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.phantom_devices.detail.failed"))


def _check_disk_health(bundle: dict[str, Any]) -> HealthItem:
    """磁盘健康：Get-PhysicalDisk 的温度与磨损；温度 > 60℃ → 警告。"""
    key = "disk_health"
    label = t("health.disk_health.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        if "disks" not in bundle:
            return HealthItem(
                key,
                label,
                "unknown",
                t("health.disk_health.detail.no_storage_module"),
            )

        disks = _as_list(bundle.get("disks"))
        if not disks:
            return HealthItem(
                key, label, "unknown", t("health.disk_health.detail.no_disks")
            )

        parts: list[str] = []
        hot: list[str] = []
        for disk in disks:
            if not isinstance(disk, dict):
                continue
            name = str(
                disk.get("name") or t("health.disk_health.detail.unknown_disk")
            )
            temp = _to_int(disk.get("temp"))
            wear = _to_int(disk.get("wear"))
            piece = name
            if temp is not None and temp >= 0:
                piece += t("health.disk_health.detail.temp", temp=temp)
                if temp > 60:
                    hot.append(t("health.disk_health.detail.hot", name=name, temp=temp))
            else:
                piece += t("health.disk_health.detail.temp_unknown")
            if wear is not None and wear >= 0:
                piece += t("health.disk_health.detail.wear", wear=wear)
            parts.append(piece)

        if not parts:
            return HealthItem(
                key, label, "unknown", t("health.disk_health.detail.unparsable")
            )

        detail = t("health.sep.detail").join(parts)
        if hot:
            return HealthItem(
                key,
                label,
                "warn",
                detail,
                t(
                    "health.disk_health.advice.hot",
                    disks=t("health.sep.list").join(hot),
                ),
            )
        return HealthItem(key, label, "ok", detail)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("disk_health 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.disk_health.detail.failed"))


def _check_long_paths() -> HealthItem:
    """长路径支持：LongPathsEnabled 为 0 → 警告。"""
    key = "long_paths"
    label = t("health.long_paths.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        exists, _vtype, value = _query_hklm(
            r"SYSTEM\CurrentControlSet\Control\FileSystem", "LongPathsEnabled"
        )
        if not exists or value is None:
            return HealthItem(
                key,
                label,
                "warn",
                t("health.long_paths.detail.not_set"),
                t("health.long_paths.advice.enable"),
            )
        if _to_int(value) == 1:
            return HealthItem(
                key, label, "ok", t("health.long_paths.detail.enabled")
            )
        return HealthItem(
            key,
            label,
            "warn",
            t("health.long_paths.detail.disabled", value=value),
            t("health.long_paths.advice.enable_short"),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("long_paths 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.long_paths.detail.failed"))


def _check_hibernation() -> HealthItem:
    """休眠：``C:\\hiberfil.sys`` 是否存在（信息项）。"""
    key = "hibernation"
    label = t("health.hibernation.label")
    try:
        if not _is_windows():
            return HealthItem(key, label, "unknown", t("health.common.not_windows"))

        drive = os.environ.get("SystemDrive") or "C:"
        path = os.path.join(drive + os.sep, "hiberfil.sys")
        if os.path.exists(path):
            size = None
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                LOGGER.debug("读取 hiberfil.sys 大小失败：%s", exc)
            detail = t("health.hibernation.detail.exists", path=path)
            if size:
                detail += t("health.hibernation.detail.size", size=_size_text(size))
            detail += t("health.hibernation.detail.available")
            return HealthItem(
                key,
                label,
                "ok",
                detail,
                t("health.hibernation.advice.off"),
            )
        return HealthItem(
            key,
            label,
            "ok",
            t("health.hibernation.detail.absent", path=path),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("hibernation 检查失败：%s", exc)
        return HealthItem(key, label, "unknown", t("health.hibernation.detail.failed"))


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def collect(fast: bool = False) -> HealthReport:
    """执行全部体检项并返回报告。

    ``fast=True`` 时跳过慢项（设备问题枚举、事件日志查询、体积/设备统计）。

    任何单项失败只影响该项（``status="unknown"``，详情写入 debug 日志）；
    顶层同样有兜底，绝不会抛出异常。
    """
    items: list[HealthItem] = []
    started = time.monotonic()

    if not _is_windows():
        # 非 Windows 平台：所有项统一返回 unknown，不做任何探测
        for builder in (
            ("os_version", t("health.os_version.label")),
            ("windows_update", t("health.windows_update.label")),
            ("pending_reboot", t("health.pending_reboot.label")),
            ("secure_boot", t("health.secure_boot.label")),
            ("tpm", t("health.tpm.label")),
            ("disk_space", t("health.disk_space.label")),
            ("devices", t("health.devices.label")),
            ("unexpected_shutdown", t("health.unexpected_shutdown.label")),
            ("smb1", t("health.smb1.label")),
            ("insecure_guest", t("health.insecure_guest.label")),
            ("firewall", t("health.firewall.label")),
            ("antivirus", t("health.antivirus.label")),
            ("phantom_devices", t("health.phantom_devices.label")),
            ("disk_health", t("health.disk_health.label")),
            ("long_paths", t("health.long_paths.label")),
            ("hibernation", t("health.hibernation.label")),
        ):
            items.append(
                HealthItem(
                    builder[0], builder[1], "unknown", t("health.common.not_windows")
                )
            )
        return HealthReport(items=items, generated=_now_text())

    #: 快项：纯注册表读取 / 轻量磁盘统计，顺序执行
    quick_checks = (
        _check_os_version,
        _check_windows_update,
        _check_pending_reboot,
        _check_secure_boot,
        _check_tpm,
        _check_disk_space,
        _check_smb1,
        _check_insecure_guest,
        _check_firewall,
        _check_long_paths,
        _check_hibernation,
    )

    for check in quick_checks:
        try:
            item = check()
        except Exception as exc:  # noqa: BLE001 单项兜底
            LOGGER.debug("%s 检查异常：%s", getattr(check, "__name__", check), exc)
            item = HealthItem(
                getattr(check, "__name__", "unknown"),
                t("health.common.item_placeholder"),
                "unknown",
                t("health.common.check_failed"),
            )
        items.append(item)

    # 慢项：合并成一次 PowerShell 调用 + 两个独立查询
    bundle: dict[str, Any] = {}
    if not fast:
        try:
            bundle = _collect_ps_bundle()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("PowerShell 合并查询失败：%s", exc)
            bundle = {}

        slow_checks: tuple[Any, ...] = (
            _check_devices,
            _check_unexpected_shutdown,
            lambda: _check_antivirus(bundle),
            lambda: _check_phantom_devices(bundle),
            lambda: _check_disk_health(bundle),
        )
    else:
        # fast 模式：慢项不执行，但保留条目并标注「已跳过」，避免误读为正常
        slow_checks = tuple(
            (
                lambda k=key, lab=label: HealthItem(
                    k,
                    lab,
                    "unknown",
                    t("health.common.fast_skipped.detail"),
                    t("health.common.fast_skipped.advice"),
                )
            )
            for key, label in (
                ("devices", t("health.devices.label")),
                ("unexpected_shutdown", t("health.unexpected_shutdown.label")),
                ("antivirus", t("health.antivirus.label")),
                ("phantom_devices", t("health.phantom_devices.label")),
                ("disk_health", t("health.disk_health.label")),
            )
        )

    for check in slow_checks:
        if time.monotonic() - started > SLOW_BUDGET_SECONDS:
            items.append(
                HealthItem(
                    "slow_skipped",
                    t("health.slow_skipped.label"),
                    "unknown",
                    t("health.slow_skipped.detail"),
                )
            )
            break
        try:
            item = check()
        except Exception as exc:  # noqa: BLE001 单项兜底
            LOGGER.debug("慢项检查异常：%s", exc)
            item = HealthItem(
                "unknown",
                t("health.common.item_placeholder"),
                "unknown",
                t("health.common.check_failed"),
            )
        items.append(item)

    # 顺序整理：按体检项顺序稳定输出（上面的顺序已固定，这里仅保证 key 唯一）
    seen: set[str] = set()
    unique: list[HealthItem] = []
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
    return HealthReport(items=unique, generated=_now_text())


if __name__ == "__main__":
    print(collect().to_text())
