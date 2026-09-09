# JSON 契约（v0.9.4 / schema_version = 1）

面向脚本与 AI Agent 的稳定输出契约。**字段只增不减**；破坏性变更必须提升
`schema_version`。机器可读版本：`python -m pc_cleaner --json-schema`。

---

## 1. 顶层信封（`--json`）

```json
{
  "schema_version": 1,
  "ok": false,
  "status": "needs_confirmation",
  "exit_code": 4,
  "version": "0.9.4",
  "recycle_available": true,
  "admin": true,
  "elevated": false,
  "dry_run": false,
  "categories": [ { "key": "...", "label": "...", "risk": "safe|moderate|risky",
                    "requires_admin": false, "admin_blocked": false,
                    "count": 12, "target_count": 3,
                    "size_bytes": 123, "size": "123 B" } ],
  "total_size_bytes": 0,
  "total_targets": 0,
  "recycle_bin_size_bytes": 0,
  "action": { }
}
```

必填字段：`schema_version`、`ok`、`status`、`exit_code`；且恒有 `ok == (exit_code == 0)`。

### `status` 取值

| 值 | 含义 |
|---|---|
| `scan` | 只扫描，未执行任何操作 |
| `dry_run` | 预览模式，未执行 |
| `deleted` | 已执行且全部成功 |
| `partial` | 已执行但存在失败/跳过 |
| `preview` | （MCP）`preview_delete` 的只读预览 |
| `needs_confirmation` | 危险操作未获显式授权，**什么都没做** |
| `needs_repreview` | （MCP）清单自预览以来已变化，拒绝执行 |
| `cancelled` | 用户取消 |
| `interrupted` | Ctrl+C 中断（已落盘历史） |
| `restored` | （MCP）回收站恢复完成 |
| `error` | 参数/分类/内部错误 |

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 参数 / 配置 / 分类名错误 |
| `2` | argparse 用法错误（由 argparse 直接返回） |
| `3` | 执行过程中有目标失败 |
| `4` | 需要确认或已取消 —— **什么都没删** |
| `130` | 用户中断 |

---

## 2. `action` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | `recycle` \| `permanent` | 删除方式 |
| `selected` | string[] | 实际选中的分类 key |
| `deleted` / `failed` / `skipped` | int | 成功目标数 / 失败目标数 / 部分清理的目标数 |
| `skipped_in_use` | int | 因被运行中进程占用而跳过的目标数（规则声明 `skip_if_in_use`） |
| `freed_bytes` | int | **真正释放**的字节（永久删除 / 清空 / 压缩） |
| `recycled_bytes` | int | **移入回收站**的字节（清空回收站后才真正释放） |
| `recycle_bin` | object | `empty_recycle_bin()` 的结果（`deleted`/`failed`/`freed`） |
| `not_executed` | bool | `true` = 什么都没执行（需确认 / 缺 `--yes` / 参数错误） |
| `needs_confirmation` | bool | 危险操作被闸门拦下 |
| `reasons` / `dangerous` | string[] | 被拦下的原因（高风险分类 / 永久删除 / 清空回收站） |
| `dry_run` | bool | 预览模式 |
| `target_count` | int | 预览目标数 |
| `would_delete` / `would_delete_with_yes` | object[] | 预览清单（`path`/`action`/`size_bytes`/`files`/`label`） |
| `would_empty_recycle_bin` | bool | 预览是否包含清空回收站 |
| `error` | string | `status="error"` 时的原因 |

> `skipped` 恒为**整数计数**；"什么都没做"用 `not_executed`（v0.9.4 起与布尔语义分离）。

---

## 3. 危险操作闸门（自动化友好）

高风险分类、永久删除（`--permanent`）、清空回收站属于危险操作：

- 交互模式：即使 `--yes` 也要二次确认；
- 非交互（管道 / Agent）：**直接拒绝**，返回 `status="needs_confirmation"` + 退出码 4；
- `--json --clean <高风险分类> --yes` 必须再加 `--risky` 才会执行。

推荐调用序列（两阶段）：

```bash
python -m pc_cleaner --json --all --dry-run          # 1) 只读预览
python -m pc_cleaner --json --clean system_temp --yes --dry-run   # 2) 复核目标
python -m pc_cleaner --json --clean system_temp --yes             # 3) 执行（安全分类）
```

---

## 4. `--health --json`

```json
{
  "schema_version": 1, "ok": true, "status": "ok", "exit_code": 0,
  "health": {
    "schema_version": 1, "generated": "YYYY-MM-DD HH:MM:SS",
    "counts": { "ok": 12, "warn": 4, "error": 0, "unknown": 0 },
    "items": [ { "key": "os_version", "label": "...", "status": "ok",
                 "detail": "...", "advice": "" } ]
  }
}
```

`status` 为 `error`（存在 `error` 级体检项，退出码 1）或 `ok`；`warn` 不影响退出码。

---

## 5. 校验示例（零依赖）

```python
import json, subprocess, sys

def run(*args):
    out = subprocess.run([sys.executable, "-m", "pc_cleaner", "--json", *args],
                         capture_output=True, text=True, encoding="utf-8", check=False)
    payload = json.loads(out.stdout)          # 必须是单个 JSON 对象
    assert payload["ok"] == (payload["exit_code"] == 0)
    return payload, out.returncode
```
