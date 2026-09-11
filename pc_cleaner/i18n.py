"""国际化（i18n）基础设施：零依赖、纯标准库、永不抛出。

设计目标
--------
- **零依赖**：只用标准库（``json`` / ``os``），不引入 gettext、babel 等；
- **扁平词表**：每个语言一个 ``locales/<lang>.json``，内容为
  ``{"health.report.title": "…", "health.os_version.label": "系统版本", …}``
  的**扁平** ``dict[str, str]``（见 :data:`SCHEMA_VERSION`）；
- **永不抛出**：词表文件缺失 / 损坏 / 编码错误 / 占位符不匹配，一律回退，
  调用方（体检报告等）不需要写 ``try``；
- **进程内缓存**：语言文件只读一次，:func:`reload` 可清空缓存（测试用）。

语言来源优先级
--------------
1. :func:`set_language` 的**显式**设置；
2. 环境变量 ``PC_CLEANER_LANG``（如 ``PC_CLEANER_LANG=en``）；
3. :data:`DEFAULT_LANGUAGE`（``zh_CN``）。

``set_language(None)`` / ``set_language("")`` / 空白串表示「清除显式设置」，
于是重新落回「环境变量 → 默认值」这条链——CLI 直接传配置里的 ``language``
（默认空串）即可，无需自己判断。传入**无法识别**的语言名（如 ``"fr"``）时
回退 :data:`DEFAULT_LANGUAGE`。

语言别名容错
------------
大小写、``-``/``_``、编码后缀都会被归一化：``zh`` / ``zh_cn`` / ``zh-hans``
/ ``zh_CN.UTF-8`` → ``zh_CN``；``en`` / ``en_us`` / ``en-gb`` → ``en``。

用法::

    from pc_cleaner import i18n

    i18n.set_language("en")
    i18n.t("health.os_version.label")                    # -> "OS version"
    i18n.t("health.disk_space.detail.drive", drive="C:", free="1 GB", total="2 GB", percent="50.0")

约定（**硬性**）：本模块**只读**语言文件，绝不写文件、绝不联网、绝不改系统。
"""

from __future__ import annotations

import json
import os
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_LANGUAGE",
    "ENV_VAR",
    "available_languages",
    "set_language",
    "current_language",
    "t",
    "load_locale",
    "reload",
]

#: 语言文件的结构版本号：扁平 ``dict[str, str]``（key -> 译文）
SCHEMA_VERSION = 1

#: 默认语言（同时是缺失 key 的回退语言，也是 zh_CN.json 的「原文基准」）
DEFAULT_LANGUAGE = "zh_CN"

#: 读取语言的环境变量名
ENV_VAR = "PC_CLEANER_LANG"

#: 语言目录（与 :mod:`pc_cleaner.health` 同级的 ``locales/``）
LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

#: 语言别名 → 规范语言名（归一化后的小写形式参与匹配）
_ALIASES: dict[str, str] = {
    "zh": "zh_CN",
    "cn": "zh_CN",
    "zh_cn": "zh_CN",
    "zh_hans": "zh_CN",
    "zh_hans_cn": "zh_CN",
    "zh_sg": "zh_CN",
    "chinese": "zh_CN",
    "en": "en",
    "eng": "en",
    "en_us": "en",
    "en_gb": "en",
    "en_ca": "en",
    "en_au": "en",
    "english": "en",
}

#: 语言文件缓存：``{语言名: {key: 译文}}``
_CACHE: dict[str, dict[str, str]] = {}

#: 显式设置的语言（``None`` 表示未显式设置，走环境变量 / 默认值）
_explicit: str | None = None


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _normalize(raw: Any) -> str:
    """把语言名归一化成小写下划线形式（去掉编码 / 修饰后缀）。

    ``"ZH-CN"`` -> ``"zh_cn"``；``"en_US.UTF-8"`` -> ``"en_us"``；
    ``"zh_CN@euro"`` -> ``"zh_cn"``；非字符串 / 空白 -> ``""``。
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip().lower().replace("-", "_")
    for sep in (".", "@"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text.strip()


def _resolve(raw: Any) -> str | None:
    """把任意语言名解析成**实际可用**的规范语言名；无法识别返回 ``None``。

    - 精确匹配 ``locales/`` 里已有的语言文件（大小写不敏感）；
    - 否则查别名表（``zh`` / ``zh-hans`` → ``zh_CN``，``en_us`` → ``en``）；
    - 否则按语言主标签前缀匹配（装了 ``de.json`` 时 ``de_AT`` → ``de``）；
    - ``locales/`` 目录不存在时仍返回别名表里的规范名（此时 :func:`t`
      会整体回退 key，保证非 Windows 平台不崩溃）。
    """
    norm = _normalize(raw)
    if not norm:
        return None

    available = available_languages()
    lowered = {name.lower(): name for name in available}
    if norm in lowered:
        return lowered[norm]

    alias = _ALIASES.get(norm)
    if alias is None:
        alias = _ALIASES.get(norm.split("_", 1)[0])
    if alias is not None:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
        if not available:
            # 没有语言目录：仍返回规范名，t() 会回退到 key（绝不崩溃）
            return alias
        return None

    base = norm.split("_", 1)[0]
    for name in available:
        if name.split("_", 1)[0].lower() == base:
            return name
    return None


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def available_languages() -> list[str]:
    """扫描 ``locales/`` 目录，返回可用语言名（如 ``["en", "zh_CN"]``）。

    目录不存在 / 不可读 / 无 ``*.json`` 时返回空列表，绝不抛出。
    """
    try:
        names = [
            entry.name[: -len(".json")]
            for entry in os.scandir(LOCALES_DIR)
            if entry.is_file() and entry.name.endswith(".json")
        ]
    except OSError:
        return []
    except Exception:  # noqa: BLE001 目录异常不应影响体检
        return []
    return sorted(name for name in names if name)


def load_locale(lang: str) -> dict[str, str]:
    """读取并返回某语言的词表（只含字符串值）；无法识别 / 读取失败返回空 dict。

    结果按语言名缓存（进程内只读一次文件）；返回的是**副本**，调用方
    修改不会污染缓存。损坏的 JSON / 非 dict 顶层 / 非字符串值都会安全跳过。
    """
    resolved = _resolve(lang)
    if resolved is None:
        return {}
    cached = _CACHE.get(resolved)
    if cached is None:
        cached = _read_locale_file(resolved)
        _CACHE[resolved] = cached
    return dict(cached)


def _read_locale_file(lang: str) -> dict[str, str]:
    """从磁盘读取语言文件并过滤出 ``str -> str`` 条目（任何异常返回 ``{}``）。"""
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data: Any = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return {}
    except Exception:  # noqa: BLE001 只读加载，任何异常都不得外抛
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def set_language(lang: str | None) -> str:
    """设置当前语言，返回**实际生效**的语言名。

    - ``None`` / ``""`` / 空白 → 清除显式设置，回落到
      ``PC_CLEANER_LANG`` 环境变量，再回落到 :data:`DEFAULT_LANGUAGE`；
    - 可识别的语言名 / 别名 → 生效（``zh`` / ``zh-hans`` → ``zh_CN``，
      ``en_us`` → ``en``）；
    - 无法识别的语言名 → 回退 :data:`DEFAULT_LANGUAGE`。

    任何输入都不会抛出异常。
    """
    global _explicit
    if lang is None or not _normalize(lang):
        _explicit = None
        return current_language()
    resolved = _resolve(lang)
    if resolved is None:
        resolved = DEFAULT_LANGUAGE
    _explicit = resolved
    return _explicit


def current_language() -> str:
    """返回当前生效的语言名（显式设置 > ``PC_CLEANER_LANG`` > 默认值）。"""
    if _explicit is not None:
        return _explicit
    try:
        env_value = os.environ.get(ENV_VAR)
    except Exception:  # noqa: BLE001 极端环境下 os.environ 也可能不可用
        return DEFAULT_LANGUAGE
    resolved = _resolve(env_value)
    return resolved or DEFAULT_LANGUAGE


def t(key: str, /, **kwargs) -> str:
    """查表取译文，支持 ``str.format`` 命名占位符。

    回退顺序：当前语言 → :data:`DEFAULT_LANGUAGE` → ``key`` 本身。
    占位符不匹配 / 文件损坏等任何异常都不抛出：优先返回可用的模板串
    （未格式化），最差返回 ``key``。
    """
    try:
        template = _lookup(key)
    except Exception:  # noqa: BLE001 查表异常回退 key
        return str(key)
    if not isinstance(template, str):
        return str(key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception:  # noqa: BLE001 占位符不匹配：返回未格式化的模板
        return template


def _lookup(key: str) -> str:
    """在当前语言 / 默认语言里找 ``key``，都找不到就返回 ``key``。"""
    if not isinstance(key, str):
        return str(key)
    lang = current_language()
    value = load_locale(lang).get(key)
    if isinstance(value, str) and value != "":
        return value
    if lang != DEFAULT_LANGUAGE:
        fallback = load_locale(DEFAULT_LANGUAGE).get(key)
        if isinstance(fallback, str) and fallback != "":
            return fallback
    return key


def reload() -> None:
    """清空语言文件缓存（测试用；显式设置的语言保持不变）。"""
    _CACHE.clear()
