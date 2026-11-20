"""配置读写：保存用户的回收站偏好、额外保护路径与自定义规则。

配置文件位于 ``%APPDATA%\\pc_cleaner\\config.json``（Windows）。

可用环境变量 ``PC_CLEANER_HOME`` 把整个配置/历史/审计目录重定向到其它位置
（例如工作区），方便便携运行且不往系统盘写东西。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("pc_cleaner.config")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())


def config_dir() -> Path:
    """返回配置目录（跨平台）。

    优先使用环境变量 ``PC_CLEANER_HOME``（其下的 pc_cleaner 子目录），
    否则回退到 ``%APPDATA%\\pc_cleaner``。
    """
    base = os.environ.get("PC_CLEANER_HOME") or os.environ.get("APPDATA")
    if not base:
        base = os.path.expanduser(r"~")
    return Path(base) / "pc_cleaner"


def config_path() -> Path:
    return config_dir() / "config.json"


def history_path() -> Path:
    """结构化清理历史（--history / --undo-last 使用）。"""
    return config_dir() / "history.json"


def audit_path() -> Path:
    """人类可读的删除审计日志。"""
    return config_dir() / "audit.log"


DEFAULTS: dict[str, Any] = {
    "recycle_by_default": True,      # 默认是否删除到回收站
    "recycle_error_fallback": False,  # 进回收站失败时是否回退为永久删除（默认否，更安全）
    "protected_paths": [],           # 额外保护路径（组件级全等 + 绝对前缀匹配，大小写不敏感）
    "custom_rules": [],              # 自定义清理规则
    "dev_artifact_bases": [],        # find_dirs 的额外基目录（默认含当前工作目录）
    "enabled_categories": [],        # 非空时只扫描这些分类（其余分类隐藏）
    "preview_lines": 12,             # 每个分类预览时最多展示的目标行数
    "show_risky": False,             # 交互菜单是否显示高风险分类（需 --risky 或设为 true）
    "enable_history": True,          # 是否记录清理历史与审计日志
    "scan_depth": 20,                # find_dirs 遍历深度限制（默认 20 层）
    "scan_workers": 4,               # 并行扫描线程数（1 = 串行；0 表示自动）
    "default_detail": False,         # 默认是否以详细模式显示扫描结果
    "default_sort": "size_desc",     # 默认排序方式：size_desc/size_asc/name_asc/count_desc
    "show_scan_progress": True,      # 扫描时是否显示进度提示
    "compact_tree_view": False,      # 是否默认使用紧凑树形视图
    "all_includes_recycle_bin": False,  # --all 是否连带清空回收站（默认否，更安全）
    # v0.9.4：界面语言（"" = 跟随环境变量 PC_CLEANER_LANG，再回退 zh_CN）
    "language": "",
}


def resolve_workers(cfg: dict[str, Any] | None = None) -> int:
    """解析并行扫描线程数。

    ``scan_workers``：1 表示串行；0 表示按 CPU 核数自动（上限 8）；
    其它值直接使用（上限 16，避免开过多线程反而拖慢）。
    """
    if cfg is None:
        cfg = load_config()
    try:
        raw = int(cfg.get("scan_workers", 4))
    except (TypeError, ValueError):
        raw = 4
    if raw <= 0:
        raw = min(os.cpu_count() or 4, 8)
    return max(1, min(raw, 16))


def _backup_corrupt_config(path: Path) -> Path | None:
    """把损坏的配置移到同目录备份文件；失败返回 None（不阻断启动）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    backup = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}")
    try:
        path.replace(backup)
    except OSError:
        logger.exception("配置损坏且无法备份: %s", path)
        return None
    logger.warning("配置损坏，已备份到 %s 并回退默认值", backup)
    return backup


def load_config() -> dict[str, Any]:
    """读取配置；文件不存在返回默认值，损坏时备份后回退默认值。"""
    cfg = dict(DEFAULTS)
    path = config_path()
    if not path.exists():
        return cfg
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _backup_corrupt_config(path)
        return cfg
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    """原子保存配置，返回保存的路径。

    v0.9.19：先写同目录临时文件并 ``fsync``，再用 ``os.replace`` 原子替换；
    写入失败会抛出 ``OSError``，不再静默返回一个“看起来保存成功”的路径。
    """
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    # 只保留已知配置字段，避免写入额外噪声
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    path = config_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        logger.exception("保存配置失败: %s", path)
        raise
    return path


def update_config(patch: dict[str, Any]) -> dict[str, Any]:
    """合并 patch 到现有配置并保存。"""
    cfg = load_config()
    for k, v in patch.items():
        if k in DEFAULTS:
            cfg[k] = v
    save_config(cfg)
    return cfg
