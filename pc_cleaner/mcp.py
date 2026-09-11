"""MCP（Model Context Protocol）服务端 —— stdio JSON-RPC 2.0（v0.9.4）。

用法::

    python -m pc_cleaner --mcp                        # 只读：scan / health / history / preview_delete
    python -m pc_cleaner --mcp --mcp-allow-delete     # 额外暴露 delete / undo

面向 AI Agent 的安全设计（这是本模块存在的理由）：

1. **默认只读**：不加 ``--mcp-allow-delete`` 时服务端**不暴露**任何写能力
   （``delete`` / ``undo`` 不会出现在 ``tools/list`` 里，被直接调用也会被拒绝）。
2. **两阶段确认**：``preview_delete`` 只返回清单 + ``confirm_token``（默认 10 分钟有效），
   真正执行必须再用 ``delete(confirm_token)``。Agent 无法"一步删掉"。
3. **清单漂移检测（防 TOCTOU）**：``delete`` 会**重新扫描并比对清单哈希**；
   自预览以来目标发生了变化就拒绝执行并返回 ``status="needs_repreview"``。
4. **危险操作二次授权**：清单包含高风险分类 / 永久删除 / 清空回收站时，
   ``delete`` 必须显式带 ``acknowledge_danger=true``（token 只保证"这是你预览过的那批"，
   不代替"你同意承担风险"）。
5. **stdout 只承载协议**：所有人类可读输出走 stderr；本模块**绝不**调用 ``ui._echo``。
6. **token 一次性**：用过即失效，且绑定清单内容哈希。

协议实现是零依赖的（纯标准库），只处理 MCP 的 ``initialize`` / ``ping`` /
``tools/list`` / ``tools/call`` 与通知，未识别的方法返回 ``-32601``。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TextIO

from . import __version__
from .config import load_config
from .engine import CleanMode, delete_targets, restore_paths
from .history import load_history
from .models import Target, TargetAction, TargetKind, format_size
from .rules import get_all_category_specs
from .scanner import scan_all
from .service import dangerous_reasons

#: 本服务端实现的协议版本（客户端请求的版本若在支持列表内则回显）
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_NAME = "pc-cleaner"

#: ``confirm_token`` 有效期（秒）
TOKEN_TTL_SECONDS = 600

#: JSON 契约版本（与 service.SCHEMA_VERSION 保持一致）
SCHEMA_VERSION = 1

_INSTRUCTIONS = (
    "PC-Cleaner 是 Windows 垃圾清理工具。安全流程：先调用 scan / preview_delete 查看清单，"
    "再决定是否用 confirm_token 执行 delete；preview_delete 不会删除任何东西。"
    "若清单含高风险分类/永久删除/清空回收站，delete 还需 acknowledge_danger=true。"
    "English: call scan/preview_delete first (read-only), then delete(confirm_token) to execute. "
    "Destructive manifests require acknowledge_danger=true."
)


class _ToolError(Exception):
    """工具层可预期的错误（返回给调用方，不算服务端异常）。"""


@dataclass
class _Manifest:
    """一次预览的冻结清单（内存中，token 过期即失效）。"""

    token: str
    digest: str
    created: float
    expires: float
    mode: str
    categories: tuple[str, ...]
    deep: bool
    filters: dict[str, Any] = field(default_factory=dict)
    targets: list[Target] = field(default_factory=list)
    total_bytes: int = 0
    dangerous: list[str] = field(default_factory=list)

    def preview_rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = [
            {
                "path": str(t.path),
                "action": t.action.value,
                "size_bytes": t.size,
                "size": format_size(t.size),
                "files": t.file_count,
                "skip_if_in_use": t.skip_if_in_use,
                "label": t.label,
            }
            for t in sorted(self.targets, key=lambda x: x.size, reverse=True)
        ]
        return rows if limit is None else rows[:limit]


#: token -> 清单（进程内）
_MANIFESTS: dict[str, _Manifest] = {}


# ===========================================================================
# 基础工具函数
# ===========================================================================
def _scan_depth() -> int:
    """读取配置里的遍历深度（容错）。"""
    try:
        return int(load_config().get("scan_depth", 20) or 20)
    except (TypeError, ValueError):
        return 20


def _scan_categories(categories: Iterable[str], deep: bool):
    """扫描指定分类，返回 ``(results, 未知分类列表)``。"""
    wanted = {str(c).strip().lower() for c in categories if str(c).strip()}
    specs = get_all_category_specs(deep=deep)
    known = {str(s.get("key", "")).lower() for s in specs}
    missing = sorted(wanted - known)
    picked = [s for s in specs if str(s.get("key", "")).lower() in wanted]
    results = scan_all(picked, scan_depth=_scan_depth(), workers=0)
    return results, missing


def _apply_filters(
    targets: list[Target],
    args: dict[str, Any],
) -> list[Target]:
    """按 ``min_size_mb`` / ``older_than_days`` / ``ext`` 过滤目标（与 CLI 语义一致）。"""
    try:
        min_size = int(args.get("min_size_mb") or 0) * 1024 * 1024
    except (TypeError, ValueError):
        min_size = 0
    try:
        older_days = int(args.get("older_than_days") or 0)
    except (TypeError, ValueError):
        older_days = 0
    ext_raw = args.get("ext")
    exts: set[str] = set()
    if isinstance(ext_raw, str):
        exts = {e.strip().lower() for e in ext_raw.split(",") if e.strip()}
    elif isinstance(ext_raw, (list, tuple, set)):
        exts = {str(e).strip().lower() for e in ext_raw if str(e).strip()}
    exts = {e if e.startswith(".") else "." + e for e in exts}
    cutoff = time.time() - older_days * 86400 if older_days > 0 else None

    out: list[Target] = []
    for t in targets:
        if min_size and t.size < min_size:
            continue
        if exts and t.kind is TargetKind.FILE and t.path.suffix.lower() not in exts:
            continue
        if cutoff is not None:
            try:
                if t.path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
        out.append(t)
    return out


def _digest(mode: str, categories: Iterable[str], deep: bool, targets: Iterable[Target]) -> str:
    """清单指纹：路径 + 体积 + 动作 + 模式 + 分类（大小写不敏感）。"""
    parts = [f"mode={mode}", f"deep={int(bool(deep))}"]
    parts.extend(f"cat={c.lower()}" for c in sorted(str(c).lower() for c in categories))
    parts.extend(
        f"{t.action.value}|{t.size}|{str(t.path).lower()}"
        for t in sorted(targets, key=lambda x: str(x.path).lower())
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _prune_manifests() -> None:
    now = time.time()
    for token in [k for k, v in _MANIFESTS.items() if v.expires < now]:
        _MANIFESTS.pop(token, None)


def _mode_of(name: str) -> CleanMode:
    return CleanMode.PERMANENT if name == "permanent" else CleanMode.RECYCLE


# ===========================================================================
# 工具实现
# ===========================================================================
def _tool_scan(args: dict[str, Any], deep_default: bool) -> dict[str, Any]:
    deep = bool(args.get("deep", deep_default))
    specs = get_all_category_specs(deep=deep)
    results = scan_all(specs, scan_depth=_scan_depth(), workers=0)
    categories = [
        {
            "key": r.key,
            "label": r.label,
            "risk": r.risk,
            "requires_admin": r.requires_admin,
            "admin_blocked": r.admin_blocked,
            "target_count": len(r.targets),
            "file_count": r.total_count,
            "size_bytes": r.liberatable,
            "size": format_size(r.liberatable),
        }
        for r in results
    ]
    total = sum(r.liberatable for r in results)
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "deep": deep,
        "categories": categories,
        "total_size_bytes": total,
        "total_size": format_size(total),
        "total_targets": sum(len(r.targets) for r in results),
    }


def _tool_health(_args: dict[str, Any], _deep_default: bool) -> dict[str, Any]:
    from .health import collect  # 延迟导入：--health 模块较重且与 MCP 无耦合

    report = collect()
    counts = report.counts()
    return {
        "ok": counts.get("error", 0) == 0,
        "schema_version": SCHEMA_VERSION,
        "health": report.to_dict(),
    }


def _tool_history(args: dict[str, Any], _deep_default: bool) -> dict[str, Any]:
    try:
        limit = max(1, min(int(args.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    sessions = load_history()[-limit:]
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "sessions": [
            {
                "session_id": s.get("session_id", ""),
                "ts": s.get("ts", ""),
                "mode": s.get("mode", ""),
                "deleted": s.get("deleted", 0),
                "failed": s.get("failed", 0),
                "freed_bytes": s.get("freed", 0),
                "categories": s.get("categories", []),
                "target_count": len(s.get("targets", []) or []),
            }
            for s in sessions
        ],
    }


def _tool_preview_delete(args: dict[str, Any], deep_default: bool) -> dict[str, Any]:
    raw_categories = args.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise _ToolError("categories 必须是非空字符串数组，例如 [\"system_temp\"]")
    mode_name = str(args.get("mode") or "recycle").lower()
    if mode_name not in ("recycle", "permanent"):
        raise _ToolError("mode 只能是 \"recycle\" 或 \"permanent\"")
    deep = bool(args.get("deep", deep_default))

    results, missing = _scan_categories(raw_categories, deep)
    if missing:
        raise _ToolError(f"无法识别的分类: {', '.join(missing)}")
    targets = _apply_filters([t for r in results for t in r.targets], args)
    dangerous = dangerous_reasons(results, mode_name, [])
    total = sum(t.size for t in targets)

    _prune_manifests()
    manifest = _Manifest(
        token=secrets.token_hex(16),
        digest=_digest(mode_name, raw_categories, deep, targets),
        created=time.time(),
        expires=time.time() + TOKEN_TTL_SECONDS,
        mode=mode_name,
        categories=tuple(str(c) for c in raw_categories),
        deep=deep,
        filters={
            k: args[k]
            for k in ("min_size_mb", "older_than_days", "ext")
            if k in args
        },
        targets=list(targets),
        total_bytes=total,
        dangerous=dangerous,
    )
    _MANIFESTS[manifest.token] = manifest

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "status": "preview",
        "confirm_token": manifest.token,
        "expires_in_seconds": TOKEN_TTL_SECONDS,
        "mode": mode_name,
        "categories": list(manifest.categories),
        "target_count": len(targets),
        "total_bytes": total,
        "total_size": format_size(total),
        "dangerous": dangerous,
        "needs_acknowledge_danger": bool(dangerous),
        "targets": manifest.preview_rows(limit=200),
        "note": (
            "这是只读预览，尚未删除任何东西。"
            "执行需再调用 delete(confirm_token=..., acknowledge_danger=...)。"
        ),
    }


def _rescan(m: _Manifest):
    """按清单记录的分类 + 过滤条件重新扫描（用于漂移检测与执行）。"""
    results, _missing = _scan_categories(m.categories, m.deep)
    return results, _apply_filters([t for r in results for t in r.targets], dict(m.filters))


def _tool_delete(args: dict[str, Any], allow_delete: bool) -> dict[str, Any]:
    if not allow_delete:
        raise _ToolError(
            "服务端未启用写能力：请用 --mcp --mcp-allow-delete 启动后再执行 delete"
        )
    token = str(args.get("confirm_token") or "").strip()
    if not token:
        raise _ToolError("缺少 confirm_token（先调用 preview_delete 获取）")
    _prune_manifests()
    manifest = _MANIFESTS.get(token)
    if manifest is None:
        raise _ToolError("confirm_token 无效、已过期或已被使用；请重新 preview_delete")
    if time.time() > manifest.expires:
        _MANIFESTS.pop(token, None)
        raise _ToolError("confirm_token 已过期（默认 10 分钟）；请重新 preview_delete")
    if manifest.dangerous and not bool(args.get("acknowledge_danger")):
        raise _ToolError(
            "该清单包含危险操作，需 acknowledge_danger=true 才会执行："
            + "；".join(manifest.dangerous)
        )

    # 漂移检测（防 TOCTOU，v0.9.4 语义）：
    # - 清单里的目标**消失**了 → 容忍（它只是不存在了，不会再被删）；
    # - 出现了**用户没预览过的新目标**，且其体积超过 max(1MB, 5% 清单总量)
    #   → 拒绝执行并要求重新预览；
    # - 已有目标体积变化 → 容忍（删除按最新体积统计）；
    # - **永远只删清单里已预览过的路径**，新目标一律不碰。
    _results, fresh_targets = _rescan(manifest)
    before = {str(t.path).lower(): t for t in manifest.targets}
    after = {str(t.path).lower(): t for t in fresh_targets}
    missing = sorted(set(before) - set(after))
    added = [after[p] for p in set(after) - set(before)]
    added_bytes = sum(t.size for t in added)
    threshold = max(1024 * 1024, int(manifest.total_bytes * 0.05))
    if added_bytes > threshold:
        _MANIFESTS.pop(token, None)
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "status": "needs_repreview",
            "reason": (
                "自预览以来出现了未预览过的新目标，已拒绝执行；请重新 preview_delete"
            ),
            "new_targets": [
                {"path": str(t.path), "size_bytes": t.size} for t in added[:50]
            ],
            "new_targets_bytes": added_bytes,
            "target_count_before": len(manifest.targets),
            "target_count_now": len(fresh_targets),
        }

    _MANIFESTS.pop(token, None)  # 一次性 token
    # 只删预览过的路径（大小取最新值）
    to_delete = [after[p] for p in before if p in after]
    mode = _mode_of(manifest.mode)
    res = delete_targets(
        to_delete,
        mode,
        recycle_fallback=bool(load_config().get("recycle_error_fallback", False)),
    )
    return {
        "ok": res["failed"] == 0,
        "schema_version": SCHEMA_VERSION,
        "status": "deleted" if res["failed"] == 0 else "partial",
        "mode": manifest.mode,
        "manifest_digest": manifest.digest,
        "vanished": len(missing),
        "result": {
            "deleted": res["deleted"],
            "failed": res["failed"],
            "skipped": res["skipped"],
            "skipped_in_use": res.get("skipped_in_use", 0),
            "freed_bytes": res["freed"],
            "recycled_bytes": res.get("recycled", 0),
            "freed": format_size(res["freed"]),
        },
    }


def _tool_undo(args: dict[str, Any], allow_delete: bool) -> dict[str, Any]:
    if not allow_delete:
        raise _ToolError(
            "服务端未启用写能力：请用 --mcp --mcp-allow-delete 启动后再执行 undo"
        )
    sessions = load_history()
    if not sessions:
        return {"ok": False, "schema_version": SCHEMA_VERSION, "error": "没有可用的历史会话"}
    sid = args.get("session_id")
    if sid:
        session = next(
            (
                s
                for s in sessions
                if str(s.get("session_id")) == str(sid) or str(s.get("ts")) == str(sid)
            ),
            None,
        )
        if session is None:
            raise _ToolError(f"找不到会话: {sid}")
    else:
        session = sessions[-1]
    if session.get("mode") != CleanMode.RECYCLE.value:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "该会话不是「进回收站」模式，无法恢复",
            "mode": session.get("mode"),
        }
    paths = [str(t.get("path")) for t in (session.get("targets") or []) if t.get("path")]
    if args.get("dry_run"):
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "status": "dry_run",
            "session_id": session.get("session_id", ""),
            "would_restore": paths,
        }
    res = restore_paths(paths)
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "status": "restored",
        "session_id": session.get("session_id", ""),
        "restored": res["restored"],
        "skipped": res["skipped"],
    }


# ===========================================================================
# 工具定义（tools/list）
# ===========================================================================
def _tool_definitions(allow_delete: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "name": "scan",
            "description": (
                "只读扫描所有清理分类并返回体积统计（不删除任何东西）。"
                " Read-only scan of all cleanup categories."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "deep": {
                        "type": "boolean",
                        "description": "启用深度规则（更彻底、更慢）/ enable deep-only rules",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "health",
            "description": (
                "只读体检报告（16 项：更新/安全启动/设备/磁盘/日志等），不修改任何设置。"
                " Read-only system health report."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "history",
            "description": "查看最近的清理历史会话 / recent cleanup sessions (read-only).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "preview_delete",
            "description": (
                "**只读**预览：返回将删除的目标清单、总体积与 confirm_token（10 分钟有效）。"
                "本工具不会删除任何东西；执行需再调用 delete。"
                " Read-only preview returning the target manifest and a confirm_token."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "分类 key 列表，如 [\"system_temp\",\"gpu_caches\"]",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["recycle", "permanent"],
                        "description": "删除方式，默认 recycle（进回收站，可恢复）",
                    },
                    "deep": {"type": "boolean", "description": "启用深度规则"},
                    "min_size_mb": {"type": "integer", "minimum": 0},
                    "older_than_days": {"type": "integer", "minimum": 0},
                    "ext": {"type": "string", "description": "扩展名过滤，如 \".log,.tmp\""},
                },
                "required": ["categories"],
                "additionalProperties": False,
            },
        },
    ]
    if allow_delete:
        tools.append(
            {
                "name": "delete",
                "description": (
                    "执行清理：必须携带 preview_delete 返回的 confirm_token；"
                    "若清单含危险操作还需 acknowledge_danger=true。"
                    "清单自预览以来发生变化时会被拒绝（needs_repreview）。"
                    " Execute a previously previewed manifest."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "confirm_token": {"type": "string"},
                        "acknowledge_danger": {"type": "boolean"},
                    },
                    "required": ["confirm_token"],
                    "additionalProperties": False,
                },
            }
        )
        tools.append(
            {
                "name": "undo",
                "description": (
                    "把最近一次（或指定 session_id）「进回收站」的清理恢复回来。"
                    " Restore a recycle-mode session from the recycle bin."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "dry_run": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            }
        )
    return tools


# ===========================================================================
# JSON-RPC 层
# ===========================================================================
def _rpc_result(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    """把工具返回值包装成 MCP ``tools/call`` 结果（文本 + structuredContent）。"""
    result: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    if is_error:
        result["isError"] = True
    return result


def handle_message(
    msg: dict[str, Any], *, allow_delete: bool = False, deep: bool = False
) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息；通知类返回 ``None``。"""
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    mid = msg.get("id")
    method = msg.get("method")
    if not isinstance(method, str):
        return _rpc_error(mid, -32600, "Invalid Request")

    if mid is None:  # 通知（如 notifications/initialized）
        return None

    if method == "initialize":
        params = msg.get("params") or {}
        client_version = params.get("protocolVersion")
        version = (
            client_version if client_version in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        )
        return _rpc_result(
            mid,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": _INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _rpc_result(mid, {})
    if method == "tools/list":
        return _rpc_result(mid, {"tools": _tool_definitions(allow_delete)})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_result(
                mid,
                _tool_result(
                    {"ok": False, "error": "arguments 必须是对象"}, is_error=True
                ),
            )
        try:
            if name == "scan":
                payload = _tool_scan(args, deep)
            elif name == "health":
                payload = _tool_health(args, deep)
            elif name == "history":
                payload = _tool_history(args, deep)
            elif name == "preview_delete":
                payload = _tool_preview_delete(args, deep)
            elif name == "delete":
                payload = _tool_delete(args, allow_delete)
            elif name == "undo":
                payload = _tool_undo(args, allow_delete)
            else:
                raise _ToolError(f"未知工具: {name}")
        except _ToolError as exc:
            payload = {"ok": False, "schema_version": SCHEMA_VERSION, "error": str(exc)}
            return _rpc_result(mid, _tool_result(payload, is_error=True))
        except Exception as exc:  # noqa: BLE001 工具内部异常不能拖垮服务端
            print(f"[pc-cleaner mcp] tool {name} failed: {exc!r}", file=sys.stderr)
            payload = {
                "ok": False,
                "schema_version": SCHEMA_VERSION,
                "error": f"内部错误: {exc}",
            }
            return _rpc_result(mid, _tool_result(payload, is_error=True))
        return _rpc_result(
            mid,
            _tool_result(payload, is_error=not bool(payload.get("ok", True))),
        )
    return _rpc_error(mid, -32601, f"Method not found: {method}")


def serve(
    *,
    allow_delete: bool = False,
    deep: bool = False,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """stdio 主循环：逐行读取 JSON-RPC，写回单行 JSON。返回退出码。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for stream in (stdin, stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 某些流不支持 reconfigure
            pass
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            response: dict[str, Any] | None = _rpc_error(None, -32700, f"Parse error: {exc}")
        else:
            try:
                response = handle_message(msg, allow_delete=allow_delete, deep=deep)
            except Exception as exc:  # noqa: BLE001
                print(f"[pc-cleaner mcp] handler crashed: {exc!r}", file=sys.stderr)
                response = _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
