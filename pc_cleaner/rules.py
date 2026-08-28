"""清理分类规则（针对本机定制）。

这里的分类与路径是基于对实际机器扫描得出的结果设计（详见 README），
集中命中真正堆积的缓存：GPU 着色器缓存、微信运行缓存、Edge/Steam 网页
缓存、游戏平台更新包、pnpm 缓存、系统临时文件等。

安全原则：
- 只清理"明确的缓存/临时"目录；绝不触碰用户数据与正在使用的文件。
- 微信：只清理 ``%APPDATA%\\Tencent\\xwechat`` 下的 xplugin / radium / log /
  crashinfo 等运行缓存；**绝不**触碰 ``D:\\WeixinShuju``（聊天数据，占 14GB），
  那块请用微信自带的存储空间管理来清理。
- GPU 着色器缓存（DXCache/GLCache）可安全清理，程序会自动重建。

路径字符串支持 Windows 环境变量（如 ``%LOCALAPPDATA%``）与 ``~``。
"""

from __future__ import annotations

import json
import os
from typing import Any


# ---------------------------------------------------------------------------
# 受保护路径（黑名单）——匹配即跳过，绝不删除
# ---------------------------------------------------------------------------
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
    r"\.git",
    r"\.venv",
    r"\.idea",
    r"\.vscode",
]

#: 通用遍历时绝不下降进入的目录名（避免误删/放大扫描）
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
    "__pycache__",  # 单独作为目标删除，不作为遍历下降点
    ".idea",
    ".vscode",
    "xwechat_files",  # 微信数据，遍历时跳过
    "weixinshuju",
}

#: 分类 key 的文案
CATEGORY_META: dict[str, dict[str, Any]] = {
    "system_temp": {
        "label": "系统临时文件",
        "description": "用户/系统 Temp、缩略图缓存、窗口缓存、错误报告",
    },
    "gpu_caches": {
        "label": "GPU 着色器缓存",
        "description": "NVIDIA / AMD 着色器缓存，可安全清理并自动重建",
    },
    "web_cache": {
        "label": "浏览器/网页缓存",
        "description": "Edge、Chrome、Firefox、Steam 网页缓存、INetCache",
    },
    "wechat_cache": {
        "label": "微信运行缓存",
        "description": "仅清理 xplugin/radium/log 等缓存，不动聊天数据",
    },
    "game_caches": {
        "label": "游戏平台缓存",
        "description": "完美世界竞技平台更新包缓存等",
    },
    "dev_caches": {
        "label": "开发工具缓存",
        "description": "pnpm/pip/npm 缓存与项目构建产物",
    },
    "downloads": {
        "label": "下载/旧文件",
        "description": "Downloads 中的大文件/久未使用文件",
    },
    "recycle_bin": {
        "label": "回收站",
        "description": "清空回收站",
    },
}


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------
def _user() -> str:
    return os.path.expanduser("~")


def _local() -> str:
    return os.environ.get("LOCALAPPDATA", os.path.join(_user(), "AppData", "Local"))


def _appdata() -> str:
    return os.environ.get("APPDATA", os.path.join(_user(), "AppData", "Roaming"))


def _temp() -> str:
    return os.environ.get("TEMP", os.path.join(_user(), "AppData", "Local", "Temp"))


def _windir() -> str:
    return os.environ.get("WINDIR", r"C:\Windows")


def _builtin_specs() -> list[dict[str, Any]]:
    """返回内置分类规格（不含回收站，回收站由 CLI 特殊处理）。"""
    return [
        {
            "key": "system_temp",
            "label": CATEGORY_META["system_temp"]["label"],
            "targets": [
                {"type": "clear_dir", "path": _temp(), "label": "用户临时文件"},
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "CrashDumps"),
                    "label": "崩溃转储缓存",
                },
                {
                    "type": "glob_files",
                    "base": os.path.join(_local(), "Microsoft", "Windows", "Explorer"),
                    "pattern": "thumbcache_*.db",
                    "label": "缩略图缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Windows", "WebCache"),
                    "label": "网页窗口缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Windows", "WER"),
                    "label": "Windows 错误报告",
                },
            ],
        },
        {
            "key": "gpu_caches",
            "label": CATEGORY_META["gpu_caches"]["label"],
            "targets": [
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "NVIDIA", "DXCache"),
                    "label": "NVIDIA DXCache",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "NVIDIA", "GLCache"),
                    "label": "NVIDIA GLCache",
                },
                # AMD/Intel 着色器缓存，同样会自行重建
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "AMD", "DXCache"),
                    "label": "AMD DXCache",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "AMD", "GLCache"),
                    "label": "AMD GLCache",
                },
            ],
        },
        {
            "key": "web_cache",
            "label": CATEGORY_META["web_cache"]["label"],
            "targets": [
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Microsoft", "Edge", "User Data"),
                    "pattern": "*/Cache",
                    "action": "clear",
                    "label": "Edge 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Microsoft", "Edge", "User Data"),
                    "pattern": "*/Code Cache",
                    "action": "clear",
                    "label": "Edge 代码缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Steam", "htmlcache"),
                    "label": "Steam 网页缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Windows", "INetCache"),
                    "label": "INetCache",
                },
                # Edge 顶层真正会自行重建的缓存，glob("*/Cache") 覆盖不到这些目录
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Edge", "User Data", "component_crx_cache"),
                    "label": "Edge 组件缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Edge", "User Data", "extensions_crx_cache"),
                    "label": "Edge 扩展缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Edge", "User Data", "GrShaderCache"),
                    "label": "Edge 着色器缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Edge", "User Data", "ShaderCache"),
                    "label": "Edge 常驻着色器缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Microsoft", "Edge", "User Data", "GPUPersistentCache"),
                    "label": "Edge GPU 持久缓存",
                },
                # Edge 的 GPU/WebGPU 着色缓存（glob 覆盖各 Profile 与系统级目录）
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Microsoft", "Edge", "User Data"),
                    "pattern": "*/GPUCache",
                    "action": "clear",
                    "label": "Edge GPU 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Microsoft", "Edge", "User Data"),
                    "pattern": "*/DawnGraphiteCache",
                    "action": "clear",
                    "label": "Edge Dawn 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Microsoft", "Edge", "User Data"),
                    "pattern": "*/DawnWebGPUCache",
                    "action": "clear",
                    "label": "Edge WebGPU 缓存",
                },
                # 其它常见浏览器缓存（跨机器通用，均只清缓存内容、保留目录）
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Google", "Chrome", "User Data"),
                    "pattern": "*/Cache",
                    "action": "clear",
                    "label": "Chrome 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Google", "Chrome", "User Data"),
                    "pattern": "*/Code Cache",
                    "action": "clear",
                    "label": "Chrome 代码缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Google", "Chrome", "User Data"),
                    "pattern": "*/GPUCache",
                    "action": "clear",
                    "label": "Chrome GPU 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Google", "Chrome", "User Data"),
                    "pattern": "*/DawnGraphiteCache",
                    "action": "clear",
                    "label": "Chrome Dawn 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Google", "Chrome", "User Data"),
                    "pattern": "*/DawnWebGPUCache",
                    "action": "clear",
                    "label": "Chrome WebGPU 缓存",
                },
                {
                    "type": "glob_dirs",
                    "base": os.path.join(_local(), "Mozilla", "Firefox", "Profiles"),
                    "pattern": "*/cache2",
                    "action": "clear",
                    "label": "Firefox 缓存",
                },
            ],
        },
        {
            "key": "wechat_cache",
            "label": CATEGORY_META["wechat_cache"]["label"],
            # 只清运行缓存，绝不碰 WeixinShuju 数据
            "targets": [
                {"type": "clear_dir", "path": os.path.join(_appdata(), "Tencent", "xwechat", "xplugin"), "label": "微信 xplugin"},
                {"type": "clear_dir", "path": os.path.join(_appdata(), "Tencent", "xwechat", "radium"), "label": "微信 radium"},
                {"type": "clear_dir", "path": os.path.join(_appdata(), "Tencent", "xwechat", "log"), "label": "微信日志"},
                {"type": "clear_dir", "path": os.path.join(_appdata(), "Tencent", "xwechat", "crashinfo"), "label": "微信崩溃报告"},
            ],
        },
        {
            "key": "game_caches",
            "label": CATEGORY_META["game_caches"]["label"],
            "targets": [
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "perfectworldarena-updater", "pending"),
                    "label": "完美世界更新包缓存",
                },
                # 实测本机更新包缓存的残留不在 pending 子目录，而是根目录的 installer.exe（约 264MB）
                {
                    "type": "glob_files",
                    "base": os.path.join(_local(), "perfectworldarena-updater"),
                    "pattern": "installer*.exe",
                    "label": "完美世界更新安装包残留",
                },
                # Steam 平台自身的缓存/日志（htmlcache 归入 web_cache）
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Steam", "appcache"),
                    "label": "Steam 应用缓存",
                },
                {
                    "type": "clear_dir",
                    "path": os.path.join(_local(), "Steam", "logs"),
                    "label": "Steam 日志",
                },
                {
                    "type": "glob_files",
                    "base": os.path.join(_local(), "Steam", "htmlcache"),
                    "pattern": "*.log",
                    "label": "Steam 网页缓存日志",
                },
            ],
        },
        {
            "key": "dev_caches",
            "label": CATEGORY_META["dev_caches"]["label"],
            "targets": [
                {"type": "clear_dir", "path": os.path.join(_local(), "pnpm-cache"), "label": "pnpm 缓存"},
                {"type": "clear_dir", "path": os.path.join(_local(), "pnpm", "store"), "label": "pnpm store"},
                {"type": "clear_dir", "path": r"D:\.pnpm-store", "label": "D 盘 pnpm store"},
                {"type": "clear_dir", "path": os.path.join(_local(), "pip", "cache"), "label": "pip 缓存"},
                {"type": "clear_dir", "path": os.path.join(_local(), "npm-cache"), "label": "npm 缓存"},
                {"type": "clear_dir", "path": os.path.join(_local(), "uv", "cache"), "label": "uv 缓存"},
                {"type": "clear_dir", "path": os.path.join(_local(), "Yarn", "Cache"), "label": "yarn 缓存"},
                {"type": "clear_dir", "path": os.path.join(_local(), "go-build"), "label": "Go 构建缓存"},
                {"type": "clear_dir", "path": os.path.join(_user(), ".cargo", "registry"), "label": "cargo 依赖缓存"},
                {"type": "clear_dir", "path": os.path.join(_temp(), "WinGet"), "label": "WinGet 临时安装包"},
                {
                    "type": "find_dirs",
                    "bases": ["<CWD>"],
                    # 只删必然可再生的纯缓存目录；不带 dist/build/node_modules 等通用名，避免误删
                    "names": [
                        "__pycache__",
                        ".pytest_cache",
                        ".mypy_cache",
                        ".ruff_cache",
                    ],
                    "action": "delete",
                    "label": "散落工具缓存",
                },
            ],
        },
        {
            "key": "downloads",
            "label": CATEGORY_META["downloads"]["label"],
            "targets": [
                {
                    "type": "files_by_rule",
                    "base": os.path.join(_user(), "Downloads"),
                    "min_size_mb": 100,
                    "older_than_days": 180,
                    "label": "下载的大/旧文件",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# 读取自定义规则
# ---------------------------------------------------------------------------
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


def get_all_category_specs(merge_custom: bool = True) -> list[dict[str, Any]]:
    """返回内置分类规格，可选合并配置文件中的自定义规则。"""
    specs = _builtin_specs()
    if merge_custom:
        for custom in _load_custom_rules():
            if not isinstance(custom, dict) or not custom.get("key"):
                continue
            existing = next((s for s in specs if s.get("key") == custom["key"]), None)
            if existing:
                existing.setdefault("targets", []).extend(custom.get("targets", []))
            else:
                custom.setdefault("label", custom.get("key", "自定义分类"))
                specs.append(custom)
    return specs


def category_label(key: str) -> str:
    """返回分类的中文名。"""
    meta = CATEGORY_META.get(key)
    if meta:
        return meta["label"]
    return key


def get_protected_patterns(include_user: bool = True) -> list[str]:
    """返回保护路径列表（内置 + 用户配置）。"""
    patterns = list(DEFAULT_PROTECTED_PATTERNS)
    if include_user:
        from .config import load_config

        user = (load_config().get("protected_paths") or []) or []
        patterns.extend(str(p) for p in user)
    return patterns
