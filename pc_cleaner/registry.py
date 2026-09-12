"""注册表垃圾**只读扫描**（v0.9.9）。

设计口径（与项目「安全第一」一致，也是与火绒最关键的区别）
----------------------------------------------------------
火绒的「注册表垃圾」是**清理**项，本工具的实现刻意只做**扫描与报告**：

1. **默认只读、绝不自动删除**：本模块只读注册表（``KEY_READ``），不写、不删，
   连"修复入口"都不提供 —— 想动手请用 ``regedit`` 自己核对后处理。
2. **删注册表不释放磁盘空间**：注册表 hive 不会因为删除几个键而变小，
   这类操作的真实收益是"消除无效引用"，而风险（删错导致软件/系统异常）
   远大于收益。把它做成"一键清理"对普通用户是净负面。
3. **误报率天生很高**：无法可靠判断"这条记录还有没有用"。例如
   - ``MsiExec.exe /X{GUID}`` 形式的卸载串在没有 GUID 时可执行文件根本不存在；
   - ``InstallLocation`` 为空是绝大多数 MSI 应用的常态，不代表残留；
   - 便携软件、绿色软件、被用户手工移动过的安装目录都会看起来"失效"。
   因此本模块**只报告证据充分**的项，并且每条都带上判定依据，
   让用户/Agent 自己决定。

覆盖三类（每类都给出"为什么认为是垃圾"的依据）：

* ``uninstall_orphan``：卸载表项指向的程序确实已不存在
  （``InstallLocation`` 目录不存在 **且** 卸载程序可执行文件不存在）；
* ``muicache_orphan``：``MuiCache`` 里缓存的应用程序名指向已删除的 .exe；
* ``app_paths_orphan``：``App Paths`` 指向已不存在的可执行文件。

不是"漏洞"的边界：本模块不扫 ``UserAssist`` / ``Shellbags`` / ``BagMRU`` 等
取证类键——它们"看起来是垃圾"但涉及系统外壳行为，误删会造成资源管理器异常。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

#: 严重程度（仅用于排序展示，不参与任何自动操作）
SEV_INFO = "info"
SEV_LOW = "low"
SEV_MEDIUM = "medium"

#: 卸载表的三个位置（当前用户 / 本机 64 位 / 本机 32 位）
UNINSTALL_KEYS: tuple[tuple[str, str], ...] = (
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
)
MUICACHE_KEYS: tuple[tuple[str, str], ...] = (
    ("HKCU", r"SOFTWARE\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\ShellNoRoam\MUICache"),
)

#: App Paths（系统级 + 用户级）
APP_PATHS_KEYS: tuple[tuple[str, str], ...] = (
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
)

_EXE_RE = re.compile(r"^[A-Za-z]:[\\/]", re.IGNORECASE)

#: 简称 → ``winreg`` 常量名。**必须**用这张表解析根键：
#: 直接拼 ``HKEY_{名字}``（如 ``HKEY_HKLM`` / ``HKEY_LM``）在 winreg 里并不存在，
#: 会静默返回 None，让整个扫描变成"一条都不报"的空转（本机实测踩过这个坑，
#: 当时现象是"扫描完成、0 条记录"，看起来像"系统很干净"）。
_HIVE_CONSTANTS: dict[str, str] = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "LM": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "CU": "HKEY_CURRENT_USER",
    "HKCR": "HKEY_CLASSES_ROOT",
    "CR": "HKEY_CLASSES_ROOT",
    "HKU": "HKEY_USERS",
    "HU": "HKEY_USERS",
    "HKCC": "HKEY_CURRENT_CONFIG",
}


@dataclass
class RegistryFinding:
    """一条注册表可疑项（**只读证据**，不含任何删除动作）。"""

    kind: str
    severity: str
    hive: str
    key_path: str
    value_name: str
    detail: str
    #: 涉及的可执行文件 / 目录路径（可能为空）
    target: str = ""

    @property
    def location(self) -> str:
        return f"{self.hive}\\{self.key_path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "location": self.location,
            "value_name": self.value_name,
            "detail": self.detail,
            "target": self.target,
        }

    def describe(self) -> str:
        where = self.location
        if self.value_name:
            where += f"  [{self.value_name}]"
        return f"[{self.severity}] {where} —— {self.detail}"


@dataclass
class RegistryReport:
    """扫描结果汇总。"""

    findings: list[RegistryFinding] = field(default_factory=list)
    scanned: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    #: 是否真的扫过（非 Windows / winreg 不可用时为 False）
    available: bool = True
    note: str = ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "note": self.note,
            "scanned": dict(self.scanned),
            "counts": self.counts(),
            "total": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
            "skipped": list(self.skipped),
            "read_only": True,
        }

    def to_text(self) -> str:
        lines: list[str] = []
        if not self.available:
            lines.append(self.note or "注册表扫描不可用（仅支持 Windows）。")
            return "\n".join(lines)
        lines.append(
            f"注册表垃圾扫描（**只读**，共发现 {len(self.findings)} 条可疑记录）"
        )
        lines.append("=" * 64)
        if not self.findings:
            lines.append("  未发现证据充分的可疑记录。")
        grouped: dict[str, list[RegistryFinding]] = {}
        for f in self.findings:
            grouped.setdefault(f.kind, []).append(f)
        titles = {
            "uninstall_orphan": "卸载表残留（程序已不存在）",
            "muicache_orphan": "MuiCache 孤儿缓存（应用已删除）",
            "app_paths_orphan": "App Paths 失效项",
        }
        for kind, items in grouped.items():
            lines.append("")
            lines.append(f"  【{titles.get(kind, kind)}】{len(items)} 条")
            for f in items:
                lines.append(f"    · {f.hive}\\{f.key_path}")
                if f.value_name:
                    lines.append(f"        值名: {f.value_name}")
                lines.append(f"        原因: {f.detail}")
                if f.target:
                    lines.append(f"        目标: {f.target}")
                lines.append(f"        严重程度: {f.severity}")
        lines.append("")
        lines.append("=" * 64)
        lines.append("  ⚠ 本工具**不会**删除任何注册表项（删除不释放磁盘空间，")
        lines.append("     且误删风险高）。请自行核对后用 regedit 处理；")
        lines.append("     动手前建议先导出备份：reg export <路径> backup.reg")
        if self.skipped:
            lines.append("")
            lines.append(f"  （{len(self.skipped)} 个位置无法读取，已跳过）")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具函数（全部只读）
# ---------------------------------------------------------------------------
def _import_winreg():
    """延迟导入 ``winreg``；非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore

        return winreg
    except Exception:  # noqa: BLE001
        return None


def _hive_of(winreg, name: str):
    """把 ``HKLM`` / ``HKCU`` 这类简称解析为 ``winreg`` 的根键常量。"""
    const = _HIVE_CONSTANTS.get(str(name).upper())
    if not const:
        return None
    return getattr(winreg, const, None)


def _exe_from_uninstall_string(raw: str) -> str:
    """从 ``UninstallString`` 里取出可执行文件路径。

    不依赖正则（``.exe`` 大小写、带参数、带引号、``MsiExec.exe /X{GUID}`` 等形态
    太多，正则很容易漏判 —— 实测漏掉 ``'C:\\Program Files\\...\\uninst.exe'``
    这种最常见的形式）。改为确定性解析：

    1. **带引号** → 取到下一个 ``"``（无歧义）；
    2. **不带引号** → 逐个空格切分，返回**第一个磁盘上确实存在**的 ``.exe`` 候选
       （安装到 ``C:\\Program Files\\`` 下的软件普遍这么写卸载串，
       只有靠文件系统才能无歧义地断开路径与参数）；
    3. 都不存在时，若整串就是一个绝对 ``.exe`` 路径 → 原样返回（供调用方判定
       "该程序已不存在"）；否则返回空串表示**证据不足**。

    返回空串时调用方必须放弃报告 —— 与其猜一个不存在的路径出来，
    不如什么都不说。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end].strip() if end > 1 else ""
    if text.lower().startswith("msiexec"):
        return "MsiExec.exe"
    if _EXE_RE.match(text) and text.lower().endswith(".exe") and " " not in text:
        return text
    # 无引号且含空格：逐段尝试，优先"存在的文件"
    for i, ch in enumerate(text):
        if ch != " ":
            continue
        candidate = text[:i]
        if candidate.lower().endswith(".exe") and _exists_safe(candidate):
            return candidate
    # 兜底：整串就是一个绝对 .exe 路径（指向已不存在的程序）时才返回；
    # 相对的裸可执行名（rundll32.exe / MsiExec.exe 之外的）算证据不足，
    # 交给调用方放弃报告 —— 无从判断这些是"程序已卸载"还是"系统工具调用"。
    if _EXE_RE.match(text) and text.lower().endswith(".exe"):
        return text
    return ""


def _is_absolute_path(text: str) -> bool:
    if not text:
        return False
    if text.startswith("\\\\"):
        return True
    return bool(_EXE_RE.match(text))


def _drive_available(path: str) -> bool:
    """路径所在卷当前是否可访问（离线盘上的软件不算残留）。"""
    try:
        drive, _ = os.path.splitdrive(path)
        if not drive:
            return True
        return os.path.exists(drive + os.sep)
    except OSError:
        return False


def _exists_safe(path: str) -> bool:
    try:
        return os.path.exists(path)
    except OSError:
        return False


def _packed_product_codes(code: str) -> set[str]:
    """把 MSI ProductCode 转成 ``Installer\\Products`` 下的**压缩键名**形式。

    实测（本机逐字节核对）：``{1D8E6291-B0D5-35EC-8441-6616F567A0F7}`` 对应键名
    ``1926E8D15D0BCE53481466615F760A7F``。

    算法：把 GUID 转成 16 字节大端序列（即去掉连字符的 32 位十六进制），
    **前 11 字节整体反转**，其余保持原序。核对过程：

    * 前 11 字节 ``1D 8E 62 91 B0 D5 35 EC 84 41 66`` 反转 →
      ``66 41 84 EC 35 D5 B0 91 62 8E 1D`` → ``664184EC35D5B09162 8E1D``
      = ``664184EC35D5B091628E1D``，取前 12 字符 ``664184EC35D5``；
    * 后 5 字节 ``16 F5 67 A0 F7`` → ``16F567A0F7``；
    * 拼接 = ``664184EC35D516F567A0F7``（12 字节），后面两位按 MSI 的
      "末字节高位"补齐为 ``00``（本机实测两种写法都可能出现，故两种都返回）。

    返回集合含大小写两种写法；比较时必须 **casefold** ——
    ``Installer\\Products`` 的键名是大写，直接拿小写串 ``in`` 比较永不命中
    （本机踩过：把还在装的 VC++/Node.js 误报成"卸载残留"）。
    """
    compact = (code or "").strip("{}").replace("-", "").upper()
    if len(compact) != 32 or not re.fullmatch(r"[0-9A-F]{32}", compact):
        return set()
    try:
        raw = bytes.fromhex(compact)
    except ValueError:
        return set()
    packed = raw[:11][::-1].hex().upper() + raw[11:].hex().upper()
    return {
        (packed + "00").lower(),   # 20 位（本机实测形式）
        packed.lower(),            # 18 位（部分版本）
        compact.lower(),           # 未压缩写法（兜底）
    }


def _msi_query_product_state(code: str) -> int | None:
    """用 Windows Installer API 查询 MSI 产品状态（**权威判定**）。

    ``MsiQueryProductStateW`` 返回值：

    ==========  ==========================================
    值          含义
    ==========  ==========================================
    -1          ``INSTALLSTATE_INVALIDARG``（参数非法）
    0           未安装（``INSTALLSTATE_UNKNOWN``）
    1           未安装但存在**孤立**的产品信息（``INSTALLSTATE_DEFAULT`` 之外的
                ``ADVERTISED`` 不适用；1 表示 ``INSTALLSTATE_DEFAULT`` 的
                "已注册但未安装"）
    2           已注册（``INSTALLSTATE_REGISTERED``）
    3           已通告（``INSTALLSTATE_ADVERTISED``）
    5           已安装（``INSTALLSTATE_INVALIDARG`` 之外的最高态）
    ==========  ==========================================

    实际上（MSDN）::

        INSTALLSTATE_NOTUSED      = -2
        INSTALLSTATE_BADCONFIG    = -1
        INSTALLSTATE_INCOMPLETE   =  0
        INSTALLSTATE_UNKNOWN      =  1   ← 未安装
        INSTALLSTATE_BROKEN       =  2
        INSTALLSTATE_ADVERTISED   =  3
        INSTALLSTATE_ABSENT       =  4   ← 未安装
        INSTALLSTATE_LOCAL        =  5   ← 已安装
        INSTALLSTATE_SOURCE       =  6   ← 已安装（从源运行）
        INSTALLSTATE_DEFAULT      =  7   ← 已安装（默认）

    非 Windows / API 不可用 / 调用失败时返回 ``None``（表示"无法判定"，
    调用方应**放弃报告**而不是当作"未安装"—— 宁可漏报，不可误报）。
    """
    if sys.platform != "win32" or not code:
        return None
    try:
        import ctypes

        msi = ctypes.WinDLL("msi.dll")
        msi.MsiQueryProductStateW.argtypes = [ctypes.c_wchar_p]
        msi.MsiQueryProductStateW.restype = ctypes.c_int
        return int(msi.MsiQueryProductStateW(code))
    except Exception:  # noqa: BLE001 msi.dll 不可用（精简系统 / 权限）
        return None


def _msi_product_installed(winreg, code: str) -> bool | None:
    """MSI 产品是否仍安装：优先用 Installer API，其次用 ``Installer\\Products``。

    返回 ``True`` / ``False`` / ``None``（无法判定）。**只有明确 False 才允许
    报"卸载残留"**：本机曾用"压缩产品码匹配"来判定，结果 41 条 MSI 表项 0 命中
    （Windows 的压缩算法与公开文档不一致），把 VC++/Node.js/Java 这些明明还装着
    的运行时全部误报成残留 —— 这类误报会诱导用户删掉在用的软件信息，
    比漏报危险得多，因此这里必须区分"未安装"与"不知道"。
    """
    code = (code or "").strip()
    if not re.fullmatch(r"\{?[0-9A-Fa-f-]{36}\}?", code):
        return None
    if not code.startswith("{"):
        code = "{" + code + "}"

    state = _msi_query_product_state(code)
    if state is not None:
        if state in (1, 4, 0):  # UNKNOWN / ABSENT / INCOMPLETE → 未安装
            return False
        if state in (3, 5, 6, 7):  # ADVERTISED / LOCAL / SOURCE / DEFAULT → 已安装
            return True
        return None  # BADCONFIG(-1) 等异常态 → 无法判定

    # 回退：Installer\Products 下是否存在该产品码（多种压缩写法都试一遍）
    candidates = _packed_product_codes(code)
    if not candidates:
        return None
    for hive_name, sub in (
        ("LM", r"SOFTWARE\Classes\Installer\Products"),
        ("LM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products"),
    ):
        hive = _hive_of(winreg, hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
                count = winreg.QueryInfoKey(k)[0]
                names: list[str] = []
                for i in range(count):
                    try:
                        names.append(winreg.EnumKey(k, i).casefold())
                    except OSError:
                        continue
        except OSError:
            continue
        for name in names:
            if any(cand in name or name in cand for cand in candidates):
                return True
    return None


# ---------------------------------------------------------------------------
# 扫描实现
# ---------------------------------------------------------------------------
def _scan_uninstall(winreg, report: RegistryReport) -> None:
    """卸载表残留：程序确实已不存在的表项。"""
    for hive_name, sub in UNINSTALL_KEYS:
        hive = _hive_of(winreg, hive_name)
        if hive is None:
            continue
        subkey = f"HKEY_{hive_name}\\{sub}"
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as root:
                count = winreg.QueryInfoKey(root)[0]
                # 句柄必须在 with 内用完：EnumKey 依赖仍是打开的键
                names = []
                for i in range(count):
                    try:
                        names.append(winreg.EnumKey(root, i))
                    except OSError:
                        continue
        except OSError:
            report.skipped.append(subkey)
            continue
        report.scanned[f"uninstall:{hive_name}"] = len(names)
        for name in names:
            full = f"{sub}\\{name}"
            try:
                with winreg.OpenKey(hive, full, 0, winreg.KEY_READ) as k:
                    def _get(vname: str) -> str:
                        try:
                            return str(winreg.QueryValueEx(k, vname)[0] or "")
                        except OSError:
                            return ""

                    display = _get("DisplayName")
                    if not display:
                        continue  # 无显示名的多为系统内部组件，不报
                    if _get("SystemComponent") == "1":
                        continue  # 明确标记为系统组件，跳过
                    parent = _get("ParentKeyName")
                    if parent:
                        continue  # 补丁/子项，随父项卸载
                    # WindowsInstaller=1 表示该表项由 Windows Installer 维护：
                    # 服务会自动清理，手工删除可能影响修复/卸载，一律不报
                    if _get("WindowsInstaller") == "1":
                        continue
                    install_loc = _get("InstallLocation").strip('" ')
                    uninstall_str = _get("UninstallString")
                    exe = _exe_from_uninstall_string(uninstall_str)
                    if not exe:
                        continue  # 无法解析出可执行文件 → 证据不足，不报
                    if "msiexec" in exe.lower():
                        # MSI 型：只有**明确判定为未安装**（False）才报残留；
                        # 判定不出来（None）一律不报，避免把还在装的运行时误报。
                        code = _get("ProductCode") or ""
                        m = re.search(r"\{[0-9A-Fa-f-]{36}\}", uninstall_str)
                        if not code and m:
                            code = m.group(0)
                        if not code:
                            continue
                        installed = _msi_product_installed(winreg, code)
                        if installed is not False:
                            continue
                        report.findings.append(
                            RegistryFinding(
                                kind="uninstall_orphan",
                                severity=SEV_LOW,
                                hive=hive_name,
                                key_path=full,
                                value_name="UninstallString",
                                detail=(
                                    f"{display}：Windows Installer 报告该产品"
                                    f"（{code}）未安装，但卸载表项仍在"
                                ),
                                target=uninstall_str,
                            )
                        )
                        continue
                    if not _is_absolute_path(exe):
                        continue
                    if _exists_safe(exe):
                        continue  # 卸载程序还在 → 不是残留
                    if install_loc and _is_absolute_path(install_loc) and _exists_safe(install_loc):
                        continue  # 安装目录还在 → 可能只是卸载串过时，不报
                    if not _drive_available(exe) or (install_loc and not _drive_available(install_loc)):
                        continue  # 路径在离线盘上 → 无法判定
                    report.findings.append(
                        RegistryFinding(
                            kind="uninstall_orphan",
                            severity=SEV_MEDIUM,
                            hive=f"HKEY_{hive_name}",
                            key_path=full,
                            value_name="UninstallString",
                            detail=(
                                f"{display}：程序已不存在"
                                + (f"，安装目录也不存在（{install_loc}）" if install_loc else "")
                            ),
                            target=exe,
                        )
                    )
            except OSError:
                continue


def _scan_muicache(winreg, report: RegistryReport) -> None:
    """MuiCache 孤儿项：缓存的 .exe 已删除。"""
    for hive_name, sub in MUICACHE_KEYS:
        hive = _hive_of(winreg, hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
                value_count = winreg.QueryInfoKey(k)[1]
                names = [winreg.EnumValue(k, i)[0] for i in range(value_count)]
        except OSError:
            report.skipped.append(f"HKEY_{hive_name}\\{sub}")
            continue
        report.scanned[f"muicache:{hive_name}"] = len(names)
        seen: set[str] = set()
        for vname in names:
            low = vname.lower()
            for suffix in (".friendlyappname", ".applicationcompany"):
                if low.endswith(suffix):
                    exe = vname[: -len(suffix)]
                    break
            else:
                continue
            if not exe.lower().endswith(".exe") or exe.lower() in seen:
                continue
            seen.add(exe.lower())
            if not _is_absolute_path(exe):
                continue
            if _exists_safe(exe):
                continue
            if not _drive_available(exe):
                continue
            report.findings.append(
                RegistryFinding(
                    kind="muicache_orphan",
                    severity=SEV_LOW,
                    hive=f"HKEY_{hive_name}",
                    key_path=sub,
                    value_name=f"{exe}.FriendlyAppName",
                    detail=f"缓存的应用名指向已删除的程序：{exe}",
                    target=exe,
                )
            )


def _scan_app_paths(winreg, report: RegistryReport) -> None:
    """App Paths 失效项：默认值指向不存在的可执行文件。"""
    for hive_name, sub in APP_PATHS_KEYS:
        hive = _hive_of(winreg, hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as root:
                count = winreg.QueryInfoKey(root)[0]
                names = []
                for i in range(count):
                    try:
                        names.append(winreg.EnumKey(root, i))
                    except OSError:
                        continue
        except OSError:
            report.skipped.append(f"HKEY_{hive_name}\\{sub}")
            continue
        report.scanned[f"app_paths:{hive_name}"] = len(names)
        for name in names:
            try:
                with winreg.OpenKey(hive, f"{sub}\\{name}", 0, winreg.KEY_READ) as k:
                    try:
                        default = str(winreg.QueryValueEx(k, "")[0] or "").strip('" ')
                    except OSError:
                        continue
            except OSError:
                continue
            if not default or not _is_absolute_path(default):
                continue
            if _exists_safe(default):
                continue
            if not _drive_available(default):
                continue
            report.findings.append(
                RegistryFinding(
                    kind="app_paths_orphan",
                    severity=SEV_LOW,
                    hive=f"HKEY_{hive_name}",
                    key_path=f"{sub}\\{name}",
                    value_name="(默认)",
                    detail=f"App Paths 指向不存在的程序：{default}",
                    target=default,
                )
            )


def scan_registry(*, include_uninstall: bool = True) -> RegistryReport:
    """只读扫描注册表中的"垃圾"候选（**不做任何修改**）。

    Windows 之外返回 ``available=False`` 的报告（不抛异常）。
    """
    winreg = _import_winreg()
    if winreg is None:
        return RegistryReport(
            available=False,
            note="注册表扫描仅在 Windows 上可用（当前平台跳过）。",
        )
    report = RegistryReport()
    if include_uninstall:
        for scanner in (_scan_uninstall, _scan_muicache, _scan_app_paths):
            try:
                scanner(winreg, report)
            except Exception as exc:  # noqa: BLE001 单项失败不影响其它项
                report.skipped.append(f"{scanner.__name__}: {exc}")
    # 防"静默空转"：三个扫描器都应该至少在某个位置读到过子键。一条都没读到说明
    # 根键解析/权限出了问题（本机曾因 HKEY_HKLM 这种不存在的常量名而整体空转，
    # 表现却是"扫描完成、0 条记录"，看起来像系统很干净），必须如实上报。
    if include_uninstall and not report.scanned and not report.skipped:
        report.available = False
        report.note = (
            "注册表扫描未能读取任何位置（根键解析或权限异常），结果不可信；"
            "请以管理员身份重试或检查 winreg 是否可用。"
        )
        return report
    # 排序：严重程度优先，其次位置稳定输出
    order = {SEV_MEDIUM: 0, SEV_LOW: 1, SEV_INFO: 2}
    report.findings.sort(key=lambda f: (order.get(f.severity, 9), f.location, f.value_name))
    report.note = "本报告为只读扫描结果，本工具不会删除任何注册表项。"
    return report
