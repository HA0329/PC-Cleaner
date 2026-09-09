"""本机运行环境探测（只读、零副作用、逐项容错）。

为了让清理工具真正「适配实际电脑」，而不是盲扫一套固定规则，本模块在
每次交互启动 / ``--checkup`` 时快速探测这台机器：

- 已安装 / 用过的浏览器（Edge / Chrome / Firefox / Brave / Vivaldi / Opera）；
- GPU 厂商（依据 %LOCALAPPDATA% 下的 NVIDIA / AMD 目录判断）；
- 微信布局：4.x（新版 Weixin，roaming ``Tencent\\xwechat``）还是 3.x，
  以及可能存在的**微信数据目录**（如 ``D:\\WeixinShuju`` —— 只提示、绝不清）；
- Steam 本体目录与存在的 steamapps 库；
- pnpm store 实际位置（含盘符根目录的 ``.pnpm-store``）；
- 命令行里可用的开发工具（node / npm / pnpm / pip / go / cargo / java / dotnet / git ...）。

探测结果用于：
- 交互菜单顶部的「本机适配」一行（快速看到这台机器装了哪些东西）；
- ``--checkup`` 的「运行环境适配」小节（详细清单 + 大体积但须在应用内清理的提示）。

安全约定：本模块**只读**，不做任何删除、不写配置、不修改注册表。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _expand(path: str) -> str:
    """展开环境变量与 ~（路径可能不存在，仅字符串展开）。"""
    return os.path.expandvars(os.path.expanduser(path))


def _exists(*parts: str) -> bool:
    """拼接后判断路径是否存在。"""
    if not parts or not parts[0]:
        return False
    return Path(_expand(parts[0])).exists()


def _is_dir(*parts: str) -> bool:
    if not parts or not parts[0]:
        return False
    p = Path(_expand(parts[0]))
    return p.is_dir()


def _drive_roots() -> list[str]:
    """返回本机存在的盘符根路径列表（只探测存在的盘）。"""
    if sys.platform != "win32":
        return []
    out: list[str] = []
    import string

    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive):
                out.append(drive)
        except OSError:
            continue
    return out


def _dir_size_bytes(path: str) -> int | None:
    """递归统计目录字节数；失败返回 None。不跟随符号链接。

    v0.9.3：对 ``st_nlink > 1`` 的文件按 ``(st_dev, st_ino)`` 去重。NTFS 上
    ``WinSxS`` / ``DriverStore`` / ``Windows\\Installer`` 大量使用硬链接，
    逐个累加会把同一份数据重复计数（本机 WinSxS 遍历值 7.52 GB，
    ``DISM /AnalyzeComponentStore`` 报告的真实值 4.55 GB）。
    """
    try:
        total = 0
        seen: set[tuple[int, int]] = set()
        for root, dirs, files in os.walk(_expand(path), followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in files:
                fp = os.path.join(root, name)
                try:
                    if os.path.islink(fp):
                        continue
                    st = os.lstat(fp)
                    if getattr(st, "st_nlink", 1) > 1:
                        key = (getattr(st, "st_dev", 0), getattr(st, "st_ino", 0))
                        if key in seen:
                            continue
                        seen.add(key)
                    total += st.st_size
                except OSError:
                    continue
        return total
    except OSError:
        return None


def _windows_caption() -> str:
    """尽力读取 Windows 版本显示名（读注册表，失败则用 platform）。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            name = winreg.QueryValueEx(key, "ProductName")[0]
            build = winreg.QueryValueEx(key, "CurrentBuildNumber")[0]
            return f"{name} (Build {build})"
    except Exception:  # noqa: BLE001 只读探测，失败用默认
        return platform.platform()


# ---------------------------------------------------------------------------
# 单项探测
# ---------------------------------------------------------------------------
def probe_browsers() -> list[dict[str, Any]]:
    """探测各浏览器的用户数据目录是否存在（存在 ≈ 这台机器用过/装着）。"""
    candidates = [
        ("edge", "Microsoft Edge", "%LOCALAPPDATA%/Microsoft/Edge/User Data"),
        ("chrome", "Google Chrome", "%LOCALAPPDATA%/Google/Chrome/User Data"),
        ("firefox", "Mozilla Firefox", "%APPDATA%/Mozilla/Firefox/Profiles"),
        ("brave", "Brave", "%LOCALAPPDATA%/BraveSoftware/Brave-Browser/User Data"),
        ("vivaldi", "Vivaldi", "%LOCALAPPDATA%/Vivaldi/User Data"),
        ("opera", "Opera", "%APPDATA%/Opera Software/Opera Stable"),
    ]
    out: list[dict[str, Any]] = []
    for key, name, loc in candidates:
        user_data = _expand(loc)
        out.append(
            {
                "key": key,
                "name": name,
                "user_data": user_data if os.path.isdir(user_data) else None,
                "installed": os.path.isdir(user_data),
            }
        )
    return out


def probe_gpu() -> list[str]:
    """按 %LOCALAPPDATA% 目录判断 GPU 厂商（只判断是否用过多着色器缓存目录）。"""
    vendors: list[str] = []
    if _is_dir("%LOCALAPPDATA%/NVIDIA") or _is_dir("%LOCALAPPDATA%/NVIDIA Corporation"):
        vendors.append("nvidia")
    if _is_dir("%LOCALAPPDATA%/AMD"):
        vendors.append("amd")
    return vendors


def probe_wechat(measure_size: bool = False) -> dict[str, Any]:
    """探测微信布局（4.x / 3.x）与可能的数据目录。

    数据目录（WeixinShuju / WeChat Files）包含聊天记录等**用户数据**，
    本工具绝不清理，仅用于在菜单 / 体检中给出「请在微信内清理」的提示。

    ``measure_size`` 为 True 时才递归统计数据目录体积（大目录可能较慢，
    菜单默认不开，仅 ``--checkup`` 需要显示体积时开启）。
    """
    roaming4 = _is_dir("%APPDATA%/Tencent/xwechat")
    roaming3 = _is_dir("%APPDATA%/Tencent/WeChat")
    if roaming4:
        layout = "wechat4"
    elif roaming3:
        layout = "wechat3"
    else:
        layout = None

    data_dirs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_candidate(label: str, path: str, marker: str) -> None:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen or not os.path.isdir(path):
            return
        seen.add(norm)
        size = _dir_size_bytes(path) if measure_size else None
        data_dirs.append(
            {
                "label": label,
                "path": path,
                "marker": marker,
                "size_bytes": size,
            }
        )

    # WeChat 4.x 数据目录通常叫 WeixinShuju（内含 xwechat_files）
    for drive in _drive_roots():
        _add_candidate("WeixinShuju(4.x)", os.path.join(drive, "WeixinShuju"), "xwechat_files")
        _add_candidate("WeChat Files(3.x)", os.path.join(drive, "WeChat Files"), "WeChat Files")
    # 3.x 默认在「文档/WeChat Files」
    docs = _expand("%USERPROFILE%/Documents/WeChat Files")
    _add_candidate("WeChat Files(3.x)", docs, "WeChat Files")

    # 可清理的微信运行缓存（4.x）：逐项列出实际存在的目录与体积，
    # 让「微信运行缓存」分类为什么是 0 项 / 有多少可清一目了然。
    cleanable: list[dict[str, Any]] = []
    if roaming4:
        candidates = [
            ("插件模块(按需重下)", "%APPDATA%/Tencent/xwechat/xplugin/plugins"),
            ("小程序容器缓存", "%APPDATA%/Tencent/xwechat/radium"),
            ("网络缓存", "%APPDATA%/Tencent/xwechat/net"),
            ("网络缓存(2)", "%APPDATA%/Tencent/xwechat/net_1"),
            ("升级包缓存", "%APPDATA%/Tencent/xwechat/update"),
            ("运行日志", "%APPDATA%/Tencent/xwechat/log"),
            ("崩溃报告", "%APPDATA%/Tencent/xwechat/crashinfo"),
        ]
        for label, loc in candidates:
            p = _expand(loc)
            if not os.path.isdir(p):
                continue
            size = _dir_size_bytes(p) if measure_size else None
            cleanable.append({"label": label, "path": p, "size_bytes": size})

    return {
        "layout": layout,
        "roaming4": roaming4,
        "roaming3": roaming3,
        "data_dirs": data_dirs,
        "cleanable": cleanable,
    }


def probe_component_stores(measure_size: bool = False) -> list[dict[str, Any]]:
    """探测「不可用普通删除清理」的组件库/安装包缓存（只读，仅用于提示）。

    - ``WinSxS``：组件库，只能用 ``DISM /Online /Cleanup-Image /StartComponentCleanup``；
    - ``DriverStore``：驱动库，删除会破坏设备驱动；
    - ``Windows\\Installer``：MSI 安装包缓存，删除会影响卸载/修复；
    - ``System Volume Information``：系统还原点（卷影副本）。

    本工具**绝不**删除这些内容（列入保护路径），这里只报告体积并给出官方清理命令。
    """
    items = [
        {
            "key": "winsxs",
            "label": "WinSxS 组件库",
            "path": _expand("%WINDIR%/WinSxS"),
            "advice": "DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase（管理员）",
            # v0.9.3：目录遍历值会把「与系统共享的硬链接」重复计入，
            # DISM /AnalyzeComponentStore 的「实际大小」才是权威值。
            "size_note": "遍历值含与系统共享的硬链接，DISM 报告的实际占用更小",
        },
        {
            "key": "driverstore",
            "label": "驱动库 DriverStore",
            "path": _expand("%WINDIR%/System32/DriverStore/FileRepository"),
            "advice": "请用 pnputil /enum-drivers 逐项评估，勿直接删除",
            "size_note": "遍历值含与系统共享的硬链接",
        },
        {
            "key": "installer",
            "label": "MSI 安装包缓存",
            "path": _expand("%WINDIR%/Installer"),
            "advice": "勿直接删除（影响软件卸载/修复），可用 PatchCleaner 等工具甄别孤立包",
        },
        {
            "key": "vss",
            "label": "系统还原点/卷影副本",
            "path": _expand("%SYSTEMDRIVE%/System Volume Information"),
            "advice": "vssadmin delete shadows /all（管理员，谨慎）",
        },
    ]
    out: list[dict[str, Any]] = []
    for item in items:
        p = item["path"]
        if not os.path.isdir(p):
            continue
        size = _dir_size_bytes(p) if measure_size else None
        out.append({**item, "size_bytes": size})
    return out


def probe_steam() -> dict[str, Any]:
    """探测 Steam：安装痕迹 + 已存在的 steamapps 库目录。"""
    local_dir = _expand("%LOCALAPPDATA%/Steam")
    installed = os.path.isdir(local_dir)

    candidates = [
        r"%LOCALAPPDATA%\Steam",
        r"%PROGRAMFILES(X86)%\Steam",
        r"%PROGRAMFILES%\Steam",
        "D:\\Program Files (x86)\\Steam",
        "D:\\Program Files\\Steam",
        "D:\\Steam",
        "C:\\Steam",
    ]
    library_dirs: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        steamapps = os.path.join(_expand(c), "steamapps")
        norm = os.path.normcase(steamapps)
        if os.path.isdir(steamapps) and norm not in seen:
            seen.add(norm)
            library_dirs.append(steamapps)
    return {"installed": installed, "local_dir": local_dir, "library_dirs": library_dirs}


def probe_pnpm_stores() -> list[str]:
    """探测实际存在的 pnpm store 目录（含盘符根目录 .pnpm-store）。"""
    out: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        norm = os.path.normcase(os.path.abspath(p))
        if os.path.isdir(p) and norm not in seen:
            seen.add(norm)
            out.append(p)

    for drive in _drive_roots():
        _add(os.path.join(drive, ".pnpm-store"))
    _add(_expand("%LOCALAPPDATA%/pnpm/store"))
    _add(_expand("%LOCALAPPDATA%/pnpm-cache"))
    return out


def probe_dev_tools() -> dict[str, str | None]:
    """探测 PATH 上可用的开发工具。"""
    names = [
        "node", "npm", "pnpm", "yarn", "corepack",
        "pip", "pip3", "uv", "poetry", "conda",
        "go", "cargo", "rustc",
        "java", "gradle", "mvn", "dotnet", "git",
        "code", "winget",
    ]
    out: dict[str, str | None] = {}
    for n in names:
        w = shutil.which(n)
        out[n] = w
    return out


# ---------------------------------------------------------------------------
# 汇总探测
# ---------------------------------------------------------------------------
def probe_environment(measure_wechat_size: bool = False) -> dict[str, Any]:
    """一次性汇总本机环境（浏览器 / GPU / 微信 / Steam / pnpm / 工具 / 磁盘）。

    全部只读；任何单项失败都不影响其它项。
    ``measure_wechat_size``：是否统计微信数据目录体积（较大时可能耗时）。
    """
    import string as _string

    drives: list[dict[str, Any]] = []
    for letter in _string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            usage = shutil.disk_usage(drive)
        except OSError:
            continue
        used_pct = usage.used / usage.total * 100 if usage.total > 0 else 0
        drives.append(
            {
                "drive": drive,
                "free": usage.free,
                "total": usage.total,
                "used_pct": used_pct,
            }
        )

    return {
        "os_caption": _windows_caption(),
        "arch": platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "python": platform.python_version(),
        "admin": _is_admin(),
        "drives": drives,
        "browsers": probe_browsers(),
        "gpu": probe_gpu(),
        "wechat": probe_wechat(measure_size=measure_wechat_size),
        "steam": probe_steam(),
        "pnpm_stores": probe_pnpm_stores(),
        "dev_tools": probe_dev_tools(),
        "component_stores": probe_component_stores(measure_size=measure_wechat_size),
    }


def _is_admin() -> bool:
    """当前进程是否有管理员权限。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 展示辅助（供交互菜单 / --checkup 复用）
# ---------------------------------------------------------------------------
def browser_label(b: dict[str, Any]) -> str:
    """浏览器探测条目 → "Edge ✓ / Chrome ✗" 风格文本。"""
    mark = "✓" if b.get("installed") else "✗"
    return f"{b['name']} {mark}"


def wechat_data_summary(probe: dict[str, Any]) -> list[str]:
    """返回微信数据目录的人类可读提示行（仅提示，绝不清理）。"""
    from .models import format_size

    we = probe.get("wechat") or {}
    lines: list[str] = []
    for d in we.get("data_dirs") or []:
        size = d.get("size_bytes")
        size_txt = format_size(size) if size is not None else "?"
        lines.append(
            f"{d['label']}: {d['path']}（占用 {size_txt}，含聊天记录，"
            "请在微信「设置 → 存储空间」内清理）"
        )
    return lines


def wechat_cleanable_summary(probe: dict[str, Any]) -> list[str]:
    """返回微信**可清理运行缓存**的清单行（用于体检报告，与分类规则对齐）。"""
    from .models import format_size

    we = probe.get("wechat") or {}
    lines: list[str] = []
    for d in we.get("cleanable") or []:
        size = d.get("size_bytes")
        size_txt = format_size(size) if size is not None else "?"
        lines.append(f"{d['label']}: {d['path']}（约 {size_txt}）")
    return lines


def component_store_summary(probe: dict[str, Any]) -> list[str]:
    """返回「需系统工具清理」的组件库提示行（本工具不删除这些内容）。"""
    from .models import format_size

    lines: list[str] = []
    for item in probe.get("component_stores") or []:
        size = item.get("size_bytes")
        size_txt = format_size(size) if size is not None else "?"
        note = item.get("size_note")
        note_txt = f"，{note}" if note else ""
        lines.append(
            f"{item['label']}: {item['path']}（约 {size_txt}{note_txt}）→ {item['advice']}"
        )
    return lines
