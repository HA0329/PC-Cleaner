"""清理执行的统一入口、危险操作闸门与退出码契约（v0.9.3）。

设计目标（面向自动化 / AI Agent）：

1. **安全闸门单点**：危险操作（高风险分类 / 永久删除 / 清空回收站）的授权判断
   集中在 :func:`dangerous_reasons`，CLI 的 JSON 分支与交互流程都走它，
   避免"某条路径绕过二次确认"（v0.9.3 修复：``--json --yes`` 曾可直删高风险分类）。
2. **退出码语义固定**：见下方 ``EXIT_*`` 常量；JSON 输出带
   ``schema_version`` / ``ok`` / ``status`` / ``exit_code``，自动化调用可可靠分支。
3. **释放量如实**：进回收站的字节数记在 ``recycled``（要清空回收站才真正释放），
   不再混进 ``freed``。
"""

from __future__ import annotations

from typing import Any, Iterable

# ===========================================================================
# 退出码契约
# ===========================================================================
EXIT_OK = 0
#: 参数 / 配置 / 分类名错误
EXIT_ERROR = 1
#: argparse 用法错误（argparse 直接返回，本模块不产生）
EXIT_USAGE = 2
#: 执行过程中有目标失败（被占用 / 无权限等）
EXIT_DELETE_FAILED = 3
#: 需要确认或已被取消 —— **什么都没删**
EXIT_NEEDS_CONFIRM = 4
#: 用户中断（Ctrl+C）
EXIT_INTERRUPTED = 130

#: JSON 输出契约版本（字段增删时递增）
SCHEMA_VERSION = 1


def dangerous_reasons(
    selected: Iterable[Any] | None = None,
    mode: Any = None,
    keys: Iterable[str] | None = None,
) -> list[str]:
    """返回"需要显式授权"的危险原因列表；空列表表示可以安全执行。

    - ``selected``：``CategoryResult`` 序列（``risk == "risky"`` 计入）；
    - ``mode``：``CleanMode`` 或其字符串值（``permanent`` 计入）；
    - ``keys``：选中的分类键（``recycle_bin`` 计入）。
    """
    reasons: list[str] = []
    risky: list[str] = []
    for res in selected or ():
        try:
            if getattr(res, "risk", "") == "risky":
                risky.append(str(getattr(res, "key", "?")))
        except Exception:  # noqa: BLE001
            continue
    if risky:
        reasons.append("高风险分类: " + ", ".join(sorted(set(risky))))
    mode_value = getattr(mode, "value", mode)
    if mode_value == "permanent":
        reasons.append("永久删除（--permanent，不可恢复）")
    if keys:
        lowered = {str(k).lower() for k in keys}
        if "recycle_bin" in lowered:
            reasons.append("清空回收站（不可恢复）")
    return reasons


def exit_code_for(result: Any) -> int:
    """根据执行结果字典计算退出码（见 ``EXIT_*`` 常量）。"""
    if not isinstance(result, dict):
        return EXIT_OK
    if result.get("needs_confirmation") or result.get("cancelled"):
        return EXIT_NEEDS_CONFIRM
    if result.get("interrupted"):
        return EXIT_INTERRUPTED
    if result.get("failed"):
        return EXIT_DELETE_FAILED
    return EXIT_OK


def status_for(result: Any) -> str:
    """把执行结果映射为稳定的 ``status`` 字符串。"""
    if not isinstance(result, dict):
        return "ok"
    if result.get("needs_confirmation"):
        return "needs_confirmation"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("interrupted"):
        return "interrupted"
    if result.get("dry_run"):
        return "dry_run"
    if result.get("failed"):
        return "partial"
    return "deleted"


def envelope(status: str, exit_code: int, **fields: Any) -> dict[str, Any]:
    """构造带契约版本的标准 JSON 信封（供 ``--json`` 输出）。"""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": exit_code == EXIT_OK,
        "status": status,
        "exit_code": exit_code,
    }
    payload.update(fields)
    return payload


def confirm_required_result(reasons: list[str], **extra: Any) -> dict[str, Any]:
    """构造"需要确认"的结果对象（不执行任何删除）。"""
    result: dict[str, Any] = {
        "needs_confirmation": True,
        "reasons": list(reasons),
    }
    result.update(extra)
    return result


# ===========================================================================
# JSON 契约（机器可读）
# ===========================================================================
#: ``--json`` 顶层信封的 JSON Schema（draft-07 子集，仅用 type/properties/
#: required/additionalProperties/items/enum，便于零依赖校验）。
#: 用 ``python -m pc_cleaner --json-schema`` 输出。
ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "pc-junk-cleaner JSON envelope",
    "type": "object",
    "required": ["schema_version", "ok", "status", "exit_code"],
    "properties": {
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
        "ok": {"type": "boolean"},
        "status": {
            "type": "string",
            "enum": [
                "scan",
                "dry_run",
                "deleted",
                "partial",
                "preview",
                "needs_confirmation",
                "needs_repreview",
                "cancelled",
                "interrupted",
                "restored",
                "failed",
                "ok",
                "error",
            ],
            "description": (
                "ok = 只读操作（如 --health）全部正常；error = 存在 error 级结果或参数错误；"
                "failed = （MCP）undo 一条都没恢复"
            ),
        },
        "exit_code": {"type": "integer", "enum": [0, 1, 2, 3, 4, 130]},
        "version": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "categories": {"type": "array", "items": {"type": "object"}},
        "total_size_bytes": {"type": "integer"},
        "total_targets": {"type": "integer"},
        "recycle_bin_size_bytes": {"type": "integer"},
        "health": {"type": "object"},
        "registry": {
            "type": "object",
            "description": (
                "v0.9.9：注册表垃圾**只读**扫描结果（--registry-scan --json）。"
                "本工具不会删除任何注册表项。"
            ),
            "properties": {
                "available": {"type": "boolean"},
                "note": {"type": "string"},
                "read_only": {"type": "boolean", "const": True},
                "total": {"type": "integer"},
                "counts": {"type": "object"},
                "scanned": {"type": "object"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "skipped": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": True,
        },
        "action": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["recycle", "permanent"]},
                "deleted": {"type": "integer"},
                "failed": {"type": "integer"},
                "skipped": {"type": "integer"},
                "skipped_in_use": {"type": "integer"},
                "vanished": {
                    "type": "integer",
                    "description": (
                        "v0.9.10：扫描后、删除前已不存在、未执行删除的目标数"
                        "（不计入 deleted，也不计入 freed_bytes）"
                    ),
                },
                "freed_bytes": {"type": "integer"},
                "recycled_bytes": {"type": "integer"},
                "selected": {"type": "array", "items": {"type": "string"}},
                "dry_run": {"type": "boolean"},
                "not_executed": {
                    "type": "boolean",
                    "description": "true = 什么都没执行（需确认/缺 --yes/参数错误）",
                },
                "needs_confirmation": {"type": "boolean"},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "dangerous": {"type": "array", "items": {"type": "string"}},
                "error": {"type": "string"},
                "target_count": {"type": "integer"},
                "would_delete": {"type": "array", "items": {"type": "object"}},
                "would_delete_with_yes": {"type": "array", "items": {"type": "object"}},
                "would_empty_recycle_bin": {"type": "boolean"},
                "recycle_bin": {"type": "object"},
            },
            "additionalProperties": True,
        },
    },
    "additionalProperties": True,
}
