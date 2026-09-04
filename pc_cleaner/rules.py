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
    r"appdata\local\microsoft\windows\explorer\thumbcache",
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


def _resolve_root(root: str) -> str | None:
    """把白名单根路径解析为规范化绝对路径（展开环境变量 / ~）。"""
    try:
        expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
    except (OSError, ValueError):
        return None
    return os.path.normcase(expanded)


def is_within_clear_root(path) -> bool:
    """路径是否位于某个允许清空的白名单目录（含其自身）。

    白名单根路径支持 Windows 环境变量（如 ``%WINDIR%``），每次调用时
    动态展开，因此系统盘不是 ``C:`` 也能正确匹配；也兼容直接写入的
    绝对路径条目（旧式配置 / 测试注入）。
    """
    s = _resolve_root(str(path))
    if s is None:
        return False
    for root in ALLOWED_CLEAR_ROOTS:
        r = _resolve_root(root)
        if r is None:
            continue
        if s == r or s.startswith(r + os.sep):
            return True
    return False


def is_clear_root(path) -> bool:
    """路径是否**恰好等于**某个允许清空的白名单根目录本身。

    用于引擎守卫区分「删除白名单根目录本身」（禁止）与「清空其内容 /
    删除其下的子项」（允许）。
    """
    s = _resolve_root(str(path))
    if s is None:
        return False
    for root in ALLOWED_CLEAR_ROOTS:
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
    "web_cache": {
        "label": "浏览器/网页缓存",
        "description": "Edge、Chrome、Firefox、Brave、Vivaldi、Opera、Steam 网页缓存、INetCache",
        "risk": "safe",
    },
    "wechat_cache": {
        "label": "微信运行缓存",
        "description": "微信 4.x 网络缓存（数据目录中的聊天记录绝不触碰）",
        "risk": "safe",
    },
    "game_caches": {
        "label": "游戏平台缓存",
        "description": "完美世界竞技平台更新包缓存等",
        "risk": "moderate",
    },
    "dev_caches": {
        "label": "开发工具缓存",
        "description": "pnpm/pip/npm/uv/yarn/Go/cargo/NuGet/Gradle 缓存与项目构建产物",
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
        "description": "Windows 更新缓存、系统 Temp、chkdsk 残留、更新日志、备份残留、预读取、事件日志归档、崩溃转储",
        "risk": "moderate",
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
        "description": "DOM/本地存储、会话、站点偏好、登录/表单数据、搜索引擎与同步数据（会退出登录并重置站点设置，默认隐藏）",
        "risk": "risky",
    },
    "database_compact": {
        "label": "浏览器数据库压缩",
        "description": "对浏览器的 History/Web Data/Login Data/Cookies/places.sqlite 等 SQLite 数据库执行 VACUUM 释放碎片（不删除数据，安全）",
        "risk": "safe",
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
    "compact_db",  # 对 SQLite 数据库执行 VACUUM 压缩（不删除，BleachBit「整理优化数据库」）
}
# 合法的目录/文件动作
VALID_ACTIONS: set[str] = {"clear", "delete"}


def validate_rules(specs: list[dict[str, Any]] | None = None) -> list[str]:
    """校验规则列表，返回错误信息列表（空列表表示全部通过）。

    覆盖：分类 key 重复/缺失、非法 risk、缺 label、非法 target 类型、
    各类型必需的字段（path/base/bases/names）、非法 action。
    """
    specs = specs if specs is not None else _builtin_specs(deep=True)
    errors: list[str] = []
    keys_seen: set[str] = set()
    for i, cat in enumerate(specs, start=1):
        if not isinstance(cat, dict):
            errors.append(f"分类 #{i} 不是对象")
            continue
        key = cat.get("key")
        if not isinstance(key, str) or not key:
            errors.append(f"分类 #{i} 缺少字符串 key")
            continue
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
            action = t.get("action")
            if action is not None and action not in VALID_ACTIONS:
                errors.append(f"{loc} 非法 action: {action!r}")
    return errors