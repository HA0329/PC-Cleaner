"""配置读写：保存用户的回收站偏好、额外保护路径与自定义规则。

配置文件位于 ``%APPDATA%\\pc_cleaner\\config.json``（Windows）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    """返回配置目录（跨平台）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser(r"~")
    return Path(base) / "pc_cleaner"


def config_path() -> Path:
    return config_dir() / "config.json"


DEFAULTS: dict[str, Any] = {
    "recycle_by_default": True,      # 默认是否删除到回收站（可恢复）
    "recycle_error_fallback": False,  # 进回收站失败时是否回退为永久删除（默认否，更安全）
    "protected_paths": [],           # 额外保护路径（子串匹配，大小写不敏感）
    "custom_rules": [],              # 自定义清理规则
    "dev_artifact_bases": [],        # find_dirs 的额外基目录（默认含当前工作目录）
    "enabled_categories": [],        # 非空时只扫描这些分类（其余分类隐藏）
    "preview_lines": 12,             # 每个分类预览时最多展示的目标行数
}


def load_config() -> dict[str, Any]:
    """读取配置；文件不存在或损坏时返回默认值。"""
    cfg = dict(DEFAULTS)
    path = config_path()
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    """保存配置，返回保存的路径。"""
    d = config_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # 只保留已知配置字段，避免写入额外噪声
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    path = config_path()
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return path


def update_config(patch: dict[str, Any]) -> dict[str, Any]:
    """合并 patch 到现有配置并保存。"""
    cfg = load_config()
    for k, v in patch.items():
        if k in DEFAULTS:
            cfg[k] = v
    save_config(cfg)
    return cfg
