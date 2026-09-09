"""清理分类规则（针对本机定制）。

这里的分类与路径基于对实际机器的探测与实测设计（详见 README / env.py），
集中命中真正存在的缓存：GPU 着色器缓存（NVIDIA/AMD/DirectX）、微信 4.x
网络缓存、Edge/Steam 网页缓存、游戏平台更新包、pnpm store（本机在
``D:\\.pnpm-store``）、系统临时文件等。

内置规则自 v0.6 起统一维护在随包附带的 ``rules.json`` 中（单一数据源），
本模块负责加载并做安全过滤（deep_only 深度规则、自定义规则合并）。

安全原则：
- 只清理"明确的缓存/临时"目录；绝不触碰用户数据与正在使用的文件。
- 微信 4.x：聊天数据在**数据目录**（如 ``D:\\WeixinShuju``，内含
  ``xwechat_files``），本工具只清理 ``%APPDATA%\\Tencent\\xwechat`` 下的
  网络缓存，**绝不**触碰数据目录（那块请用微信自带的存储空间管理清理）。
- GPU 着色器缓存（DXCache/GLCache/D3DSCache）可安全清理，程序会自动重建。

路径字符串支持 Windows 环境变量（如 ``%LOCALAPPDATA%``）与 ``~``。
本机适配探测见 :mod:`pc_cleaner.env`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ===========================================================================
# 安全增强：系统关键文件黑名单（即使匹配到也禁止删除）
# ===========================================================================
# 这些文件位于系统根目录或关键位置，删除会导致系统无法启动或严重问题。
# 任何扫描规则若匹配到这些文件名，引擎会直接拒绝删除。
SYSTEM_CRITICAL_FILES = {
    "pagefile.sys",    # 虚拟内存页面文件
    "swapfile.sys",    # 交换文件
    "hiberfil.sys",    # 休眠文件
    "bootmgr",         # Windows 启动管理器
    "bootnxt",         # 启动管理器（NXT）
    "ntldr",           # 旧版 NT 加载器
    "ntdetect.com",    # 硬件检测
    "$mft",            # 主文件表（NTFS）
    "$bitmap",         # 位图文件（NTFS）
    "$logfile",        # 日志文件（NTFS）
}

# ===========================================================================
# 受保护路径（黑名单）——匹配即跳过，绝不删除
# ===========================================================================
# 匹配语义（见 scanner.make_protect_check）：
# - 含环境变量 / `~` / 盘符 / 绝对形式的条目 → 展开后做**绝对路径前缀匹配**；
# - 其余相对条目（如 `windows\system32`、`weixinshuju`、`.git`）→ 按**路径组件
#   （目录名序列）全等匹配**，命中任意一层即视为受保护，例如 `windows\system32`
#   可命中 C:/Windows/System32 下的任意子路径，`.git` 可命中任意位置的项目仓库。
#   组件级全等匹配不会像子串匹配那样误伤（如 "windows" 不会误匹配 "windows.old"）。
# - 白名单清空例外（ALLOWED_CLEAR_ROOTS）内的路径不在此受保护（见
#   is_within_clear_root / is_clear_root），仍由引擎对根目录的删除做拦截。
DEFAULT_PROTECTED_PATTERNS: list[str] = [
    r"$recycle.bin",
    r"system volume information",
    r"windows\system32",
    r"windows\system",
    r"windows\syswow64",
    r"windows\systemapps",
    r"windows\winsxs",
    r"windows\temp",
    r"windows\softwaredistribution",
    r"program files\windows nt",
    r"programdata\microsoft\windows defender",
    # 注意（v0.9.3）：曾有一条 appdata\local\microsoft\windows\explorer\thumbcache，
    # 它按「路径组件全等」匹配，只能命中名为 thumbcache 的**目录**，
    # 而真实的缩略图/图标缓存是 Explorer 目录下的 thumbcache_*.db / iconcache_*.db
    # 文件，因此该模式永远匹配不到、属于无效保护（已删除）。
    # 对应文件改为在 rules.json 里用 skip_if_in_use 处理（被 explorer 占用时跳过）。
    # 微信等用户数据，绝不自动删除
    r"weixin",
    r"weixinshuju",
    r"xwechat_files",
    # 开发工具相关
    r"\.git",
    r"\.venv",
    r"\.idea",
    r"\.vscode",
]

# 通用遍历时绝不下降进入的目录名（避免误删/放大扫描）
DEFAULT_SKIP_DIRNAMES: set[str] = {
    "$recycle.bin",
    "system volume information",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",      # 单独作为目标删除，不作为遍历下降点
    ".idea",
    ".vscode",
    "xwechat_files",    # 微信数据，遍历时跳过
    "weixinshuju",
    "windows.old",      # 功能更新残留，体积巨大且需单独处理
    "winsxs",           # 组件库，只允许系统工具（DISM）处理
    "driverstore",      # 驱动库，删除会破坏设备驱动
    "installer",        # MSI 安装包缓存，删除会影响卸载/修复
    "$windows.~bt",
    "$windows.~ws",
}

# ===========================================================================
# 受保护路径下的「白名单清空」例外
# ===========================================================================
# 某些目录位于受保护前缀之下（如 %WINDIR%\SoftwareDistribution 下的
# Download 更新缓存），但它们的*内容*是明确可安全重建的缓存。
# 列入本集合的目录只允许以 CLEAR（清空内容）方式被内置规则清理；
# DELETE（删除目录本身）或任何非白名单路径仍会被二次防御拒绝。
#
# 条目使用环境变量占位符（如 ``%WINDIR%``），在 ``is_within_clear_root``
# 每次调用时动态解析为绝对路径 —— 因此与系统盘符解耦：系统盘不是 C:
# 也能正确匹配（也兼容直接写绝对路径的旧式条目 / 测试注入）。
ALLOWED_CLEAR_ROOTS: set[str] = {
    r"%WINDIR%\SoftwareDistribution\Download",
    r"%WINDIR%\Prefetch",
    # 系统 Temp（%WINDIR%\Temp）：位于受保护前缀 windows\temp 之下，
    # 但内容是可安全重建的临时文件；只允许清空，删除目录本身仍被拒绝。
    r"%WINDIR%\Temp",
}

# ---------------------------------------------------------------------------
# 受保护前缀之下的「只允许清空内容」白名单（v0.9.3 新增）
# ---------------------------------------------------------------------------
# 这些目录本身位于受保护前缀（windows\system32）之下，此前导致
# rules.json 里针对它们的所有规则**永远扫描不到任何目标**（静默空转）：
# System32\LogFiles 实测有 31.9 MB 诊断日志却从未被清理。
#
# 语义与 ALLOWED_CLEAR_ROOTS 的差别：
# - 二者都表示「内容可以清空」；
# - 本集合内的目录**永远不允许被整体删除**（即使规则写 delete_dir），
#   引擎只需放行 CLEAR（见 engine._guard_path / is_clear_root）。
ALLOWED_CLEAR_UNDER_PROTECTED: set[str] = {
    r"%WINDIR%\System32\LogFiles",
    r"%WINDIR%\System32\winevt\Logs",
}


def _clear_root_sets() -> tuple[set[str], set[str]]:
    """返回 (常规白名单, 受保护前缀下的白名单)。"""
    return ALLOWED_CLEAR_ROOTS, ALLOWED_CLEAR_UNDER_PROTECTED


def _resolve_root(root: str) -> str | None:
    """把白名单根路径解析为规范化绝对路径（展开环境变量 / ~）。"""
    try:
        expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
    except (OSError, ValueError):
        return None
    return os.path.normcase(expanded)


def is_within_clear_root(path) -> bool:
    """路径是否位于某个允许清空的白名单目录（含其自身）。

    同时识别两类白名单：``ALLOWED_CLEAR_ROOTS``（常规可清空缓存目录）与
    ``ALLOWED_CLEAR_UNDER_PROTECTED``（受保护前缀之下、只允许清空内容的
    诊断日志目录，如 System32\\LogFiles）。白名单根路径支持 Windows
    环境变量（如 ``%WINDIR%``），每次调用时动态展开，因此系统盘不是 ``C:``
    也能正确匹配；也兼容直接写入的绝对路径条目（旧式配置 / 测试注入）。
    """
    s = _resolve_root(str(path))
    if s is None:
        return False
    normal, under_protected = _clear_root_sets()
    for root in normal | under_protected:
        r = _resolve_root(root)
        if r is None:
            continue
        if s == r or s.startswith(r + os.sep):
            return True
    return False


def is_clear_under_protected(path) -> bool:
    """路径是否位于「受保护前缀之下、只允许清空」的白名单内（含其自身）。

    引擎用它区分：命中本函数 → 只允许 CLEAR（清空内容），
    DELETE（删除目录本身或其子项）应被拒绝。
    """
    s = _resolve_root(str(path))
    if s is None:
        return False
    for root in ALLOWED_CLEAR_UNDER_PROTECTED:
        r = _resolve_root(root)
        if r is None:
            continue
        if s == r or s.startswith(r + os.sep):
            return True
    return False


def is_clear_root(path) -> bool:
    """路径是否**恰好等于**某个允许清空的白名单根目录本身。

    用于引擎守卫区分「删除白名单根目录本身」（禁止）与「清空其内容 /
    删除其下的子项」（允许）。两类白名单的根目录都算，因此
    ``System32\\LogFiles`` 本身永远不会被整体删除。
    """
    s = _resolve_root(str(path))
    if s is None:
        return False
    normal, under_protected = _clear_root_sets()
    for root in normal | under_protected:
        r = _resolve_root(root)
        if r is None:
            continue
        if s == r:
            return True
    return False


# ===========================================================================
# 分类元信息（用于展示和标签）
# ===========================================================================
CATEGORY_META: dict[str, dict[str, Any]] = {
    "system_temp": {
        "label": "系统临时文件",
        "description": "用户/系统 Temp、缩略图/图标缓存、窗口缓存、错误报告、最近文档",
        "risk": "safe",
    },
    "gpu_caches": {
        "label": "GPU 着色器缓存",
        "description": "NVIDIA / AMD / DirectX 着色器缓存，可安全清理并自动重建",
        "risk": "safe",
    },
    "nvidia_app_cache": {
        "label": "NVIDIA 应用缓存",
        "description": "NVIDIA App / Overlay 的 CEF 界面缓存与组件缓存（可重建）",
        "risk": "safe",
    },
    "web_cache": {
        "label": "浏览器/网页缓存",
        "description": "Edge、Chrome、Firefox、Brave、Vivaldi、Opera、Steam 网页缓存、INetCache",
        "risk": "safe",
    },
    "wechat_cache": {
        "label": "微信运行缓存",
        "description": "微信 4.x 网络缓存/日志/升级包与插件模块（数据目录中的聊天记录绝不触碰）",
        "risk": "safe",
    },
    "office_caches": {
        "label": "办公软件缓存",
        "description": "Office 文档同步缓存、智能查找缓存、WPS 缓存（可重建）",
        "risk": "safe",
    },
    "media_caches": {
        "label": "多媒体设计软件缓存",
        "description": "Adobe Premiere Pro / After Effects 媒体缓存（可重建）",
        "risk": "safe",
    },
    "comm_caches": {
        "label": "通信工具缓存",
        "description": "Zoom / Discord / Telegram 等通信应用临时缓存",
        "risk": "safe",
    },
    "game_caches": {
        "label": "游戏平台缓存",
        "description": "Steam / Epic / Battle.net / GOG / Riot / 完美世界竞技平台缓存",
        "risk": "moderate",
    },
    "game_runtime_cache": {
        "label": "游戏运行时缓存",
        "description": "无畏契约、三角洲行动、Unreal Engine、CS:GO 缓存与日志",
        "risk": "moderate",
    },
    "live_stream_cache": {
        "label": "直播伴侣/电竞平台缓存",
        "description": "抖音直播伴侣、完美世界竞技平台网页分区缓存与运行日志",
        "risk": "moderate",
    },
    "dev_caches": {
        "label": "开发工具缓存",
        "description": "pnpm/npm/pip/uv/yarn/Go/cargo/NuGet/Gradle 缓存与散落工具缓存",
        "risk": "moderate",
    },
    "downloads": {
        "label": "下载/旧文件",
        "description": "Downloads 中的大文件/久未使用文件/安装包（高风险，需确认）",
        "risk": "risky",
    },
    "recycle_bin": {
        "label": "回收站",
        "description": "清空回收站（不可恢复）",
        "risk": "moderate",
    },
    "system_admin": {
        "label": "系统深度清理(需管理员)",
        "description": "Windows 更新缓存、系统 Temp、chkdsk 残留、更新日志、预读取、事件日志、崩溃转储",
        "risk": "moderate",
    },
    "system_logs": {
        "label": "系统日志与诊断残留",
        "description": "DISM/CBS/waasmedic 日志、WMI 与安装日志、USB 安装日志、传递优化缓存、USOShared 更新状态",
        "risk": "safe",
    },
    "windows_old": {
        "label": "旧版 Windows 残留",
        "description": "C:\\Windows.old（功能更新残留，占用巨大，删除不可恢复）",
        "risk": "risky",
    },
    "dev_purge": {
        "label": "项目构建产物(高风险)",
        "description": "散落的 node_modules / dist / build / target 等，删除需重建",
        "risk": "risky",
    },
    "browser_privacy": {
        "label": "浏览器隐私数据(高风险)",
        "description": "Cookie 与浏览历史（会退出登录，仅显式开启时清理）",
        "risk": "risky",
    },
    "browser_data": {
        "label": "浏览器站点数据(高风险)",
        "description": "DOM/本地存储、会话、站点偏好、登录/表单数据、搜索引擎与同步数据（会退出登录）",
        "risk": "risky",
    },
    "database_compact": {
        "label": "浏览器数据库压缩",
        "description": "对 History/Web Data/Login Data/Cookies/places.sqlite 执行 VACUUM 释放碎片（不删除数据）",
        "risk": "safe",
    },
    "webview2_caches": {
        "label": "WebView2 嵌入式浏览器缓存",
        "description": "UWP/系统应用内嵌 WebView2 的图形/着色器/组件缓存（可重建）",
        "risk": "moderate",
    },
    "hidden_installer_backups": {
        "label": "隐蔽的安装包/升级残留",
        "description": "$Windows.~BT/~WS、MSI Package Cache、WinSxS 临时目录（需管理员）",
        "risk": "moderate",
    },
    "recycle_and_diagnostics": {
        "label": "回收站与诊断日志(ETL)",
        "description": "各分区回收站、ETL 诊断跟踪日志、WinSAT 性能评估缓存",
        "risk": "moderate",
    },
    "cloud_app_hidden": {
        "label": "云盘与商店应用缓存",
        "description": "OneDrive 缓存/授权缓存、Windows Store 应用临时文件",
        "risk": "safe",
    },
    "java_rdp_legacy": {
        "label": "Java/远程桌面/字体缓存",
        "description": "Java 部署缓存、远程桌面位图缓存、系统字体缓存（可重建）",
        "risk": "safe",
    },
    "crash_telemetry": {
        "label": "崩溃上报与遥测数据",
        "description": "WER 错误报告归档/队列、微软遥测服务存储",
        "risk": "safe",
    },
    "extreme_stealth": {
        "label": "变态级隐蔽缓存(系统账户)",
        "description": "SYSTEM 账户缓存、CBS 历史日志、大体积事件日志、下载残留、音视频客户端缓存",
        "risk": "moderate",
    },
}


# ===========================================================================
# 内置规则加载（rules.json 单一数据源）
# ===========================================================================
def _load_builtin_rules() -> list[dict[str, Any]]:
    """从随包附带的 rules.json 加载内置清理规则（单一数据源）。

    文件缺失或损坏时抛出 RuntimeError，让问题尽早暴露（而非静默降级为
    「无可清理项」，造成"清理工具清不出东西"的假象）。
    """
    path = Path(__file__).with_name("rules.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法加载内置规则文件 {path}: {exc}") from exc
    cats = data.get("categories")
    if not isinstance(cats, list):
        raise RuntimeError(f"内置规则文件 {path} 格式错误：缺少 categories 列表")
    return [c for c in cats if isinstance(c, dict) and c.get("key")]


def _filter_targets(spec: dict[str, Any], deep: bool) -> dict[str, Any]:
    """按 deep 模式过滤目标：非 deep 模式下剔除 ``deep_only`` 规则。"""
    if deep:
        return spec
    targets = [
        t
        for t in spec.get("targets", [])
        if isinstance(t, dict) and not t.get("deep_only")
    ]
    spec = dict(spec)
    spec["targets"] = targets
    return spec


def _builtin_specs(deep: bool = False) -> list[dict[str, Any]]:
    """返回内置分类规格（不含回收站，回收站由 CLI 特殊处理）。

    ``deep=True`` 时额外包含 ``deep_only`` 深度清理规则（更彻底、更慢）。
    """
    cats = _load_builtin_rules()
    out: list[dict[str, Any]] = []
    for cat in cats:
        filtered = _filter_targets(cat, deep)
        # 非 deep 模式下，纯 deep_only 分类（无普通目标）直接跳过
        if not deep and cat.get("deep_only") and not filtered["targets"]:
            continue
        out.append(filtered)
    return out


# ===========================================================================
# 读取自定义规则（用户通过 config.json 添加）
# ===========================================================================
def _load_custom_rules() -> list[dict[str, Any]]:
    from .config import load_config  # 延迟导入，避免循环依赖

    cfg = load_config()
    raw = cfg.get("custom_rules") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    return raw if isinstance(raw, list) else []


def get_all_category_specs(
    merge_custom: bool = True, deep: bool = False
) -> list[dict[str, Any]]:
    """返回内置分类规格，可选合并配置文件中的自定义规则。

    ``deep=True`` 时包含 deep_only 深度清理规则。
    """
    specs = _builtin_specs(deep=deep)
    if merge_custom:
        for custom in _load_custom_rules():
            if not isinstance(custom, dict) or not custom.get("key"):
                continue
            custom = _filter_targets(custom, deep)
            existing = next((s for s in specs if s.get("key") == custom["key"]), None)
            if existing:
                existing.setdefault("targets", []).extend(custom.get("targets", []))
            else:
                custom.setdefault("label", custom.get("key", "自定义分类"))
                specs.append(custom)
    return specs


def get_enabled_category_specs(
    cfg: dict[str, Any] | None = None, deep: bool = False
) -> list[dict[str, Any]]:
    """返回启用的分类规格（内置 + 自定义），并按配置 ``enabled_categories`` 过滤。

    ``cfg`` 为 None 时重新读取配置文件；交互菜单每轮调用本函数即可实现
    rules.json / 配置的热重载（编辑后无需重启程序，下一次扫描即生效）。
    """
    if cfg is None:
        from .config import load_config  # 延迟导入，避免循环依赖

        cfg = load_config()
    specs = get_all_category_specs(merge_custom=True, deep=deep)
    enabled = [k.lower() for k in (cfg.get("enabled_categories") or []) if k]
    if enabled:
        specs = [s for s in specs if s["key"].lower() in enabled]
    return specs


def category_label(key: str) -> str:
    """返回分类的中文名。"""
    meta = CATEGORY_META.get(key)
    if meta:
        return meta["label"]
    return key


def spec_risk(spec: dict[str, Any]) -> str:
    """返回分类规格的风险等级（safe / moderate / risky）。"""
    return str(spec.get("risk") or "safe")


def is_risky_spec(spec: dict[str, Any]) -> bool:
    return spec_risk(spec) == "risky"


def spec_requires_admin(spec: dict[str, Any]) -> bool:
    return bool(spec.get("require_admin", False))


def get_protected_patterns(include_user: bool = True) -> list[str]:
    """返回保护路径列表（内置 + 用户配置）。"""
    patterns = list(DEFAULT_PROTECTED_PATTERNS)
    if include_user:
        from .config import load_config

        user = (load_config().get("protected_paths") or []) or []
        patterns.extend(str(p) for p in user)
    return patterns


# ===========================================================================
# 规则校验（--validate-rules）
# ===========================================================================
# 合法的风险等级
VALID_RISKS: set[str] = {"safe", "moderate", "risky"}
# 合法的 target 类型
VALID_TARGET_TYPES: set[str] = {
    "clear_dir",
    "delete_dir",
    "glob_dirs",
    "glob_files",
    "files_by_rule",
    "find_dirs",
    "compact_db",     # 对 SQLite 数据库执行 VACUUM 压缩（不删除，BleachBit「整理优化数据库」）
    "empty_dirs",     # 删除 base 下的空目录（递归，可配置 min_age_days）
    "zero_byte_files",  # 删除 base 下的 0 字节残留文件
}
# 合法的目录/文件动作
VALID_ACTIONS: set[str] = {"clear", "delete"}

# 用户数据目录标记（用于警告「safe 分类却清理用户数据」）
USER_DATA_MARKERS: set[str] = {
    "documents",
    "desktop",
    "onedrive",
    "wechat files",
    "downloads",
}
# 位于这些组件之下的路径视为「应用缓存」而非用户数据（避免误报）
_APP_DATA_MARKERS = {"appdata", "programdata", "application data"}


class ValidationReport(list):
    """``validate_rules()`` 的返回值：list[str]（错误）+ ``.warnings``（提示）。

    继承 ``list`` 是为了与既有调用方兼容（``if errors: ...`` / ``len(errors)``
    / ``== []`` 行为不变），同时把「只提示、不拦截」的警告放在 ``.warnings``。
    """

    def __init__(self, errors=(), warnings=()) -> None:
        super().__init__(errors)
        self.warnings: list[str] = list(warnings)

    @property
    def ok(self) -> bool:
        """没有错误时为 True（警告不影响）。"""
        return not self


def _expand_for_validation(raw: Any) -> str | None:
    """把规则里的路径字符串展开为规范化绝对路径（仅用于校验，不解析链接）。"""
    try:
        s = os.path.abspath(os.path.expandvars(os.path.expanduser(str(raw))))
    except (OSError, ValueError, TypeError):
        return None
    return os.path.normcase(s)


def _has_clear_root_under(base_norm: str) -> bool:
    """base 之下是否还存在「允许清空」的白名单根（部分覆盖也算有效规则）。"""
    normal, under_protected = _clear_root_sets()
    for root in normal | under_protected:
        r = _resolve_root(root)
        if r and r.startswith(base_norm + os.sep):
            return True
    return False


def _target_locations(t: dict[str, Any]) -> list[str]:
    """取出 target 里可以静态展开的路径（path / base / bases）。"""
    out: list[str] = []
    if isinstance(t.get("path"), str):
        out.append(t["path"])
    if isinstance(t.get("base"), str):
        out.append(t["base"])
    bases = t.get("bases")
    if isinstance(bases, (list, tuple)):
        out.extend(b for b in bases if isinstance(b, str))
    return out


def _warn_protected_targets(specs, warnings: list[str]) -> None:
    """警告「规则展开后命中保护模式」——这类规则永远扫描不到目标（静默空转）。"""
    try:
        from .scanner import make_protect_check  # 延迟导入，避免循环依赖
    except Exception:  # noqa: BLE001 校验不应因扫描器导入失败而中断
        return
    try:
        is_protected = make_protect_check()
    except Exception:  # noqa: BLE001
        return

    for cat in specs:
        if not isinstance(cat, dict):
            continue
        key = cat.get("key") or "?"
        for j, t in enumerate(cat.get("targets", []) or [], start=1):
            if not isinstance(t, dict):
                continue
            ttype = t.get("type")
            loc = f"[{key}] targets[{j}]"
            for raw in _target_locations(t):
                if raw == "<CWD>":
                    continue
                norm = _expand_for_validation(raw)
                if not norm:
                    continue
                if not is_protected(norm):
                    continue
                # base 型规则：只要 base 之下还有白名单清空根，就不算完全空转
                if ttype in ("glob_dirs", "glob_files", "files_by_rule", "compact_db",
                             "empty_dirs", "zero_byte_files", "find_dirs"):
                    if _has_clear_root_under(norm):
                        continue
                warnings.append(
                    f"{loc} 目标落在受保护路径，规则将永远扫描不到任何内容（空转）: {raw}"
                )
                break


def _warn_redundant_targets(specs, warnings: list[str]) -> None:
    """警告「同分类内被父目录目标覆盖」的冗余规则（只统计可静态展开的目录目标）。"""
    for cat in specs:
        if not isinstance(cat, dict):
            continue
        key = cat.get("key") or "?"
        dirs: list[tuple[str, str, int]] = []
        for j, t in enumerate(cat.get("targets", []) or [], start=1):
            if not isinstance(t, dict):
                continue
            if t.get("type") not in ("clear_dir", "delete_dir"):
                continue
            raw = t.get("path")
            norm = _expand_for_validation(raw) if isinstance(raw, str) else None
            if norm:
                dirs.append((norm, str(raw), j))
        for norm, raw, j in dirs:
            for parent, praw, _pj in dirs:
                if parent != norm and norm.startswith(parent + os.sep):
                    warnings.append(
                        f"[{key}] targets[{j}] {raw} 已被同分类父目录目标 {praw} 覆盖"
                        "（体积会被去重，规则冗余）"
                    )
                    break


def _warn_user_data_risk(specs, warnings: list[str]) -> None:
    """警告「base/path 落在用户数据目录却标 safe」的风险错配。"""
    for cat in specs:
        if not isinstance(cat, dict):
            continue
        if str(cat.get("risk") or "safe") != "safe":
            continue
        key = cat.get("key") or "?"
        for j, t in enumerate(cat.get("targets", []) or [], start=1):
            if not isinstance(t, dict):
                continue
            for raw in _target_locations(t):
                if raw == "<CWD>":
                    continue
                norm = _expand_for_validation(raw)
                if not norm:
                    continue
                parts = [p for p in norm.replace("/", os.sep).split(os.sep) if p]
                if any(p in _APP_DATA_MARKERS for p in parts):
                    continue
                if any(p in USER_DATA_MARKERS for p in parts):
                    warnings.append(
                        f"[{key}] targets[{j}] {raw} 指向用户数据目录，"
                        "但分类 risk=safe（建议降级为 risky 或加阈值）"
                    )
                    break


def validate_rules(specs: list[dict[str, Any]] | None = None) -> ValidationReport:
    """校验规则列表，返回 :class:`ValidationReport`（空列表 = 无错误）。

    **错误（errors，导致 --validate-rules 退出码非 0）**覆盖：
    分类 key 重复/缺失、非法 risk、缺 label、非法 target 类型、
    各类型必需的字段（path/base/bases/names）、非法 action；
    v0.9.3 起新增三条强制校验：
    - ``glob_dirs`` / ``glob_files`` 必须显式给出 ``pattern``
      （缺失时扫描器默认 ``*``，等于清理/删除整个 base）；
    - ``action`` 必须是 ``clear`` / ``delete``（大小写不敏感）；
    - ``find_dirs`` 必须显式给出正整数 ``max_depth``。

    **警告（warnings，只提示）**：目标落在受保护路径（规则空转）、
    被同分类父目录目标覆盖（冗余）、base 指向用户数据目录却标 ``safe``。
    """
    specs = specs if specs is not None else _builtin_specs(deep=True)
    errors: list[str] = []
    warnings: list[str] = []
    keys_seen: set[str] = set()
    valid_specs: list[dict[str, Any]] = []
    for i, cat in enumerate(specs, start=1):
        if not isinstance(cat, dict):
            errors.append(f"分类 #{i} 不是对象")
            continue
        key = cat.get("key")
        if not isinstance(key, str) or not key:
            errors.append(f"分类 #{i} 缺少字符串 key")
            continue
        valid_specs.append(cat)
        if key in keys_seen:
            errors.append(f"分类 key 重复: {key}")
        keys_seen.add(key)

        risk = cat.get("risk", "safe")
        if risk not in VALID_RISKS:
            errors.append(f"[{key}] 非法 risk: {risk!r}（应为 safe/moderate/risky）")
        if not cat.get("label"):
            errors.append(f"[{key}] 缺少 label")

        targets = cat.get("targets", [])
        if not isinstance(targets, list) or not targets:
            errors.append(f"[{key}] targets 为空或非列表")
            continue
        for j, t in enumerate(targets, start=1):
            loc = f"[{key}] targets[{j}]"
            if not isinstance(t, dict):
                errors.append(f"{loc} 不是对象")
                continue
            ttype = t.get("type")
            if ttype not in VALID_TARGET_TYPES:
                errors.append(f"{loc} 非法 type: {ttype!r}")
                continue
            if ttype in ("clear_dir", "delete_dir") and not t.get("path"):
                errors.append(f"{loc} 缺少 path")
            if ttype in ("glob_dirs", "glob_files", "files_by_rule") and not t.get("base"):
                errors.append(f"{loc} 缺少 base")
            if ttype in ("glob_dirs", "glob_files"):
                pattern = t.get("pattern")
                if not isinstance(pattern, str) or not pattern.strip():
                    errors.append(
                        f"{loc} {ttype} 必须显式给出 pattern"
                        "（缺失时扫描器默认 '*'，会清空/删除整个 base）"
                    )
            if ttype in ("empty_dirs", "zero_byte_files"):
                if not t.get("base"):
                    errors.append(f"{loc} 缺少 base")
                if ttype == "empty_dirs" and t.get("action") not in (None, "delete"):
                    errors.append(
                        f"{loc} empty_dirs 仅支持 action=delete（得到 {t.get('action')!r}）"
                    )
            if ttype == "files_by_rule" and not t.get("pattern"):
                # files_by_rule 允许只按扩展名/阈值筛选，但至少要有一个筛选条件，
                # 否则等价于「删除整个目录内容」，属于高危误用。
                if not t.get("ext") and not t.get("min_size_mb") and not t.get("older_than_days"):
                    errors.append(
                        f"{loc} files_by_rule 需要 pattern 或 ext/min_size_mb/older_than_days 之一"
                    )
            if ttype == "compact_db":
                if not t.get("base"):
                    errors.append(f"{loc} 缺少 base")
                if not t.get("pattern"):
                    errors.append(f"{loc} 缺少 pattern")
            if ttype == "find_dirs":
                if not t.get("bases"):
                    errors.append(f"{loc} 缺少 bases")
                if not t.get("names"):
                    errors.append(f"{loc} 缺少 names")
                max_depth = t.get("max_depth")
                if max_depth is None:
                    errors.append(
                        f"{loc} find_dirs 缺少 max_depth"
                        "（必须显式限制下探层数，否则会全树遍历）"
                    )
                else:
                    try:
                        if int(max_depth) <= 0:
                            errors.append(f"{loc} find_dirs 的 max_depth 必须为正整数: {max_depth!r}")
                    except (TypeError, ValueError):
                        errors.append(f"{loc} find_dirs 的 max_depth 非法: {max_depth!r}")
            action = t.get("action")
            if action is not None:
                if not isinstance(action, str) or action.lower() not in VALID_ACTIONS:
                    errors.append(f"{loc} 非法 action: {action!r}（应为 clear/delete）")
    # 只在没有结构性错误时统计警告，避免噪声淹没真正的错误
    if not errors:
        _warn_protected_targets(valid_specs, warnings)
        _warn_redundant_targets(valid_specs, warnings)
        _warn_user_data_risk(valid_specs, warnings)
    return ValidationReport(errors, warnings)


def validate_rules_detailed(
    specs: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """返回 ``(errors, warnings)`` 二元组，便于 CLI 分别输出。"""
    report = validate_rules(specs)
    return list(report), list(report.warnings)


def format_rule_warnings(report: Any) -> list[str]:
    """把校验报告里的警告渲染成可直接打印的行（供 --validate-rules 使用）。"""
    return [f"⚠ {w}" for w in (getattr(report, "warnings", None) or [])]