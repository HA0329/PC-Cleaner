# MCP 服务端（Model Context Protocol）

让 AI Agent（Claude Desktop / DSH / 自研 Agent）以**受控的两阶段流程**使用 PC-Cleaner。

```bash
python -m pc_cleaner --mcp                       # 只读：scan / health / history / preview_delete
python -m pc_cleaner --mcp --mcp-allow-delete    # 额外暴露 delete / undo
```

传输：**stdio**，逐行 JSON-RPC 2.0（`\n` 分隔）。stdout 只承载协议，
所有日志走 stderr；协议版本支持 `2024-11-05` / `2025-03-26` / `2025-06-18`
（回显客户端请求的版本）。

## 安全模型

| 机制 | 说明 |
|---|---|
| **默认只读** | 未加 `--mcp-allow-delete` 时，`tools/list` 里**没有** `delete` / `undo`，直接调用也会被拒绝 |
| **两阶段确认** | `preview_delete` 只返回清单 + `confirm_token`（默认 600 秒有效）；执行必须再调 `delete(confirm_token)` |
| **漂移检测（防 TOCTOU）** | `delete` 会重新扫描并比对清单指纹；目标发生变化 → `status="needs_repreview"`，**不执行** |
| **危险操作二次授权** | 清单含高风险分类 / 永久删除 / 清空回收站时，`delete` 必须带 `acknowledge_danger=true` |
| **token 一次性** | 用过即失效；token 绑定清单内容哈希，不能跨清单复用 |
| **无删除的预览** | `preview_delete` 永远不会调用删除引擎（有单元测试断言） |

## 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `scan` | `deep?: bool` | 各分类体积统计（只读） |
| `health` | — | 16 项只读体检（只读） |
| `history` | `limit?: int` | 最近会话（含 `session_id`） |
| `preview_delete` | `categories: string[]`（必填）、`mode?: "recycle"\|"permanent"`、`deep?`、`min_size_mb?`、`older_than_days?`、`ext?` | `confirm_token`、`expires_in_seconds`、`targets[]`、`total_bytes`、`dangerous[]` |
| `delete` | `confirm_token: string`（必填）、`acknowledge_danger?: bool` | `status`（`deleted` / `partial` / `needs_repreview`）、`result{freed_bytes, recycled_bytes, skipped_in_use…}` |
| `undo` | `session_id?`、`dry_run?` | `restored[]` / `skipped[]`（仅 `recycle` 会话） |

所有工具返回 `content[0].text`（JSON 字符串）**与** `structuredContent`（对象），
便于不同客户端解析。

## 一次典型交互

```jsonc
// → initialize / notifications/initialized（略）

// → tools/call preview_delete
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"preview_delete",
 "arguments":{"categories":["system_temp","gpu_caches"],"mode":"recycle"}}}

// ← structuredContent
{"ok":true,"status":"preview","confirm_token":"3f9c…","expires_in_seconds":600,
 "target_count":33,"total_bytes":26779648,"total_size":"25.54 MB",
 "dangerous":[],"needs_acknowledge_danger":false,
 "targets":[{"path":"…","action":"clear","size_bytes":…,"skip_if_in_use":true}]}

// → tools/call delete
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"delete",
 "arguments":{"confirm_token":"3f9c…"}}}

// ← structuredContent
{"ok":true,"status":"deleted","mode":"recycle",
 "result":{"deleted":33,"failed":0,"skipped_in_use":1,"freed_bytes":0,
           "recycled_bytes":26779648}}
```

## 客户端配置示例（Claude Desktop / 通用 stdio MCP）

```json
{
  "mcpServers": {
    "pc-cleaner": {
      "command": "python",
      "args": ["-m", "pc_cleaner", "--mcp", "--mcp-allow-delete"],
      "cwd": "D:\\path\\to\\PC-Cleaner-main"
    }
  }
}
```

> 建议先用只读模式（去掉 `--mcp-allow-delete`）观察 Agent 行为，再按需放开写能力。
