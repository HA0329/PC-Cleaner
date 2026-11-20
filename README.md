# 🧹 PC Junk Cleaner

> **Windows-only** 的个人电脑安全垃圾清理工具：先扫描、再确认，默认进回收站，可撤销。
> 当前版本 **0.9.19**（版本记录见 [CHANGELOG.md](CHANGELOG.md)）；内置 **34 个分类、300 条内置规则**
> （34 分类 300 目标），其中 36 条深度规则。

```powershell
python -m pc_cleaner --list      # 只扫描，不删任何东西
python -m pc_cleaner             # 交互式菜单
```

第一次使用：双击 `pc_cleaner.bat` → 输入 `p`（一键推荐）→ 核对分类和合计 → 确认。
推荐流程只清可重建的缓存/临时文件，不碰个人文件、聊天记录和登录数据。

---

## 🚀 快速开始

要求：**Windows + Python 3.12+**。本工具只适配 Windows；源码使用 Python 3.12 的
f-string 语法，3.10/3.11 会直接在解析阶段报错。

```powershell
pip install -e .                          # 安装（send2trash + psutil 为必需依赖）
python -m pc_cleaner --list               # 扫描，不删除
python -m pc_cleaner                      # 菜单；输入 p 一键推荐
python -m pc_cleaner --health             # 只读体检（16 项）
python -m pc_cleaner --checkup            # 清理前准备情况 + 环境探测
python -m pc_cleaner --clean system_temp  # 清理指定分类（先预览再确认）
```

便携运行：设置 `PC_CLEANER_HOME` 可把配置/历史/审计放到其它目录，不写用户目录。

---

## ✨ 主要能力

- **一键推荐（p）**：安全分类 + 游戏/直播/WebView2 缓存；不选高风险、下载、开发缓存和使用痕迹，不碰回收站。
- **风险分级**：🟢 安全 / 🟡 一般 / 🔴 高风险；高风险默认隐藏，`--all` 不选中。
- **默认可恢复**：删除进回收站；`--undo-last` 或菜单 `u` 恢复最近一次。
- **占用保护**：`skip_if_in_use` 目标被占用时跳过；安全缓存支持 `partial_if_in_use`，只清未锁定子项。
- **二次防御**：受保护路径黑名单、删除前再检查、符号链接/junction 跳过、回收站失败默认保留原文件。
- **只读体检**：`--health` 16 项系统健康信号；`--checkup` 清理前准备检查；均不改系统设置。
- **自动化友好**：固定退出码 + JSON 信封；`--mcp` 默认只读，写能力需显式开启。
- **工程质量**：`ruff` + `mypy` 进入 CI；测试见下方。

> 注册表只做 `--registry-scan` 只读报告，**不提供删除入口**：删注册表不释放磁盘空间，误删风险高。
> WinSxS / DriverStore / Windows Installer / 卷影副本只报告体积，不删除。

---

## 🗂 内置清理分类（34 个）

| 分类 | 风险 | 清理内容 |
| --- | --- | --- |
| `system_temp` | 🟢 | 用户/系统 Temp、缩略图/图标缓存、错误报告、转储、最近文档 |
| `gpu_caches` | 🟢 | NVIDIA/AMD/Intel 着色器缓存、D3DSCache（可重建） |
| `nvidia_app_cache` | 🟢 | NVIDIA App / Overlay 界面与组件缓存 |
| `web_cache` | 🟢 | Edge/Chrome/Firefox/Brave/Vivaldi/Opera 等浏览器缓存 |
| `wechat_cache` | 🟢 | 微信 4.x 运行缓存、插件、小程序容器、日志（不碰聊天数据） |
| `office_caches` | 🟢 | Office 同步/智能查找缓存、WPS 缓存 |
| `media_caches` | 🟢 | Adobe Premiere / After Effects 媒体缓存 |
| `comm_caches` | 🟢 | Zoom、Discord、Telegram、Slack、Teams、飞书 |
| `game_caches` | 🟡 | Steam/Epic/Battle.net/GOG/Riot 缓存与日志 |
| `game_runtime_cache` | 🟡 | 无畏契约、三角洲行动、Unreal、CS:GO 运行时缓存 |
| `live_stream_cache` | 🟡 | 抖音直播伴侣、完美世界竞技平台 |
| `dev_caches` | 🟡 | pnpm/npm/pip/uv/yarn/Go/cargo/NuGet/Gradle/Maven、`__pycache__`、IDE 日志 |
| `downloads_temp` | 🟡 | Downloads 根目录 30 天未修改的未完成下载残片（随 `--all`） |
| `downloads_installers` | 🟡 | Downloads 根目录 90/180 天未修改的安装包/镜像/压缩包（需单独选） |
| `downloads_extracted` | 🟡 | 仅当 `foo.zip` 旁存在 `foo/` 时清压缩包（需单独选） |
| `downloads` | 🔴 | Downloads 中 100MB+ 且 180 天未修改的大/旧文件（默认隐藏） |
| `system_admin` | 🟡 | Windows 更新缓存、Prefetch、SystemTemp、chkdsk 残留等（需管理员） |
| `system_wer` | 🟡 | ProgramData WER 归档/队列/Temp（需管理员） |
| `system_logs` | 🟡 | DISM/CBS/waasmedic 日志、`System32\LogFiles`、传递优化缓存（需管理员） |
| `windows_old` | 🔴 | `C:\Windows.old`（默认隐藏，需管理员） |
| `dev_purge` | 🔴 | 散落 node_modules / dist / build / target / .next（默认隐藏） |
| `browser_privacy` | 🔴 | Cookie 与浏览历史（默认隐藏，会退出登录） |
| `browser_data` | 🔴 | 本地存储、会话、表单、登录密码、站点偏好（默认隐藏，会退出登录） |
| `database_compact` | 🟡 | 浏览器 SQLite `VACUUM`（不删数据；浏览器运行时可能失败） |
| `webview2_caches` | 🟡 | UWP/系统应用内嵌 WebView2 的图形/着色器/组件缓存 |
| `hidden_installer_backups` | 🟡 | `$Windows.~BT` / `~WS` 升级残留（需管理员） |
| `recycle_and_diagnostics` | 🟡 | ETL 诊断日志、WinSAT 缓存（需管理员） |
| `cloud_app_hidden` | 🟢 | OneDrive 本地缓存、Windows Store 应用临时文件 |
| `java_rdp_legacy` | 🟢 | Java 部署缓存、远程桌面位图缓存（系统字体缓存见 `system_admin`） |
| `rdp_legacy_cache` | 🟡 | 文档目录下远程桌面旧版剪贴板/位图缓存（不随 `--all`） |
| `crash_telemetry` | 🟢 | 用户级 WER 错误报告、临时转储（系统级见 `system_wer` / `system_admin`） |
| `extreme_stealth` | 🟡 | CBS 历史日志、大体积事件日志、Steam/Epic 下载残留、音视频缓存 |
| `broken_shortcuts` | 🔴 | 开始菜单/桌面/快速启动中的失效快捷方式（离线盘、网络共享、URL 不动） |
| `usage_traces` | 🟡 | 最近文档、跳转列表、PowerShell 命令历史（不删用户文件） |

`recycle_bin` 不在 `rules.json` 中，是引擎特殊项：用 `--clean recycle_bin`、菜单 `r`，
或配置 `all_includes_recycle_bin` 让 `--all` 带上它。

---

## 🔒 安全边界

1. **不确认不删**：默认先预览再确认；`--yes` 仅用于自动化，危险操作仍需显式授权。
2. **回收站优先**：`send2trash` 和 `psutil` 是必需依赖；进回收站失败默认保留原文件，不会静默转永久删除。
3. **高风险闸门**：永久删除、清空回收站、高风险分类在非交互环境必须带 `--risky`；
   否则返回 `needs_confirmation` + 退出码 4，什么都不删。
4. **黑名单 + 二次防御**：`System32`、`WinSxS`、`DriverStore`、`Windows\Installer`、
   `$Recycle.Bin`、微信聊天数据、`.git` / `.venv` 等受保护；引擎删除前会再查一遍。
5. **不碰链接**：不跟随符号链接/junction，也拒绝删除链接自身。
6. **如实上报**：回收站字节只算 `recycled`，不冒充 `freed`；失败/占用/扫描后消失分别计数。
7. **审计留痕**：清理写入 `history.json` 与 `audit.log`，Ctrl+C 也会落盘。

微信聊天数据在数据目录（如 `D:\WeixinShuju`），本工具只清 `%APPDATA%\Tencent\xwechat`
下的运行缓存；数据目录很大时请用微信「设置 → 存储空间」。

---

## 🖥 常用命令

| 命令 | 说明 |
| --- | --- |
| `--list` / `-l` | 只扫描，列出分类占用、磁盘可用、回收站占用 |
| `--deep` / `-D` | 深度扫描：启用 `deep_only` 规则，遍历深度至少 50（除非显式 `--max-depth`） |
| `--clean 分类` | 清理指定分类，逗号分隔；拼错返回退出码 1 |
| `--all` / `--exclude` | 全部非高风险分类 / 排除指定分类 |
| `--dry-run` | 只预览，不删除 |
| `--recycle` / `--permanent` | 回收站（默认）/ 永久删除（危险，需授权） |
| `--risky` | 显示高风险分类，并授权危险操作 |
| `--yes` / `-y` | 跳过确认（谨慎；危险操作仍需 `--risky`） |
| `--ext` / `--min-size-mb` / `--older-than-days` | 按扩展名 / 体积 / 修改时间过滤 |
| `--shred` | 永久删除前随机覆写（必须配合 `--permanent`；`--shred-passes N`） |
| `--health` / `--checkup` | 只读体检 / 清理前准备检查 |
| `--registry-scan` | 注册表垃圾只读扫描，绝不删除 |
| `--history` / `--undo-last` | 查看历史 / 从回收站恢复最近一次 |
| `--json` / `--json-schema` | JSON 输出 / JSON Schema |
| `--mcp` / `--mcp-allow-delete` | MCP 服务端（默认只读）/ 放开写能力 |
| `--show-config` / `--export-config` / `--import-config` | 配置查看与导入导出 |
| `--show-rules` / `--validate-rules` / `--audit-rules` | 展示规则 / 校验 / 本机存活性审计 |
| `--admin` | UAC 提权重启（系统分类需要） |

**菜单键**：`p` 推荐 · 编号（支持 `1,3-5`）· `all` · `r` 回收站 · `d` 详情 · `t` 树形 ·
`s` 排序 · `x` 高风险 · `D` 深度 · `e` 提权 · `f` 重扫 · `u` 撤销 · `h` 帮助 · `q` 退出。

常见用法：

```powershell
python -m pc_cleaner --deep --list
python -m pc_cleaner --clean gpu_caches,wechat_cache --recycle
python -m pc_cleaner --all --exclude downloads --recycle
python -m pc_cleaner --clean recycle_bin
python -m pc_cleaner --clean database_compact
python -m pc_cleaner --clean broken_shortcuts --risky --recycle
python -m pc_cleaner --json --dry-run
```

---

## 🔢 退出码与 JSON

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 参数/配置/分类名错误 |
| `2` | argparse 用法错误 |
| `3` | 执行中有目标失败 |
| `4` | 需要确认或已取消，什么都没删 |
| `130` | 用户中断（Ctrl+C） |

`--json` 顶层固定为 `{schema_version, ok, status, exit_code, categories, action}`。
`action` 区分 `freed_bytes` 与 `recycled_bytes`；未执行时 `action.not_executed: true`。
完整字段与 `status` 见 [docs/json-contract.md](docs/json-contract.md)。

---

## 🤖 MCP

```powershell
python -m pc_cleaner --mcp                       # 默认只读
python -m pc_cleaner --mcp --mcp-allow-delete    # 额外暴露 delete/undo
```

默认只读；`preview_delete` 返回清单 + 一次性 `confirm_token`；`delete` 重扫比对，
只删预览过的路径；高风险/永久删除/清空回收站需 `acknowledge_danger=true`。
客户端配置见 [docs/mcp.md](docs/mcp.md)。

---

## ⚙️ 配置

配置在 `pc_cleaner/config.json`（`PC_CLEANER_HOME` 可重定向），`--show-config` 查看。
常用字段：`recycle_by_default`（true）、`recycle_error_fallback`（false）、
`protected_paths`、`custom_rules`、`enabled_categories`、`preview_lines`（12）、
`show_risky`（false）、`enable_history`（true）、`scan_depth`（20）、`scan_workers`（4）、
`all_includes_recycle_bin`（false）、`language`。

自定义规则支持 `clear_dir`、`delete_dir`、`glob_dirs`、`glob_files`、`files_by_rule`、
`find_dirs`、`compact_db`、`empty_dirs`、`zero_byte_files`；常用字段
`pattern`、`ext`、`min_size_mb`、`older_than_days`、`max_depth`、`action`、`deep_only`、
`skip_if_in_use`、`partial_if_in_use`、`all_excluded`、`label`。
多候选路径可用 `paths` / `bases` 兼容软件不同版本的目录布局；改完用 `--validate-rules` 校验。

---

## 🗃 项目结构

```
pc_cleaner/   # cli、menu、scanner、engine、rules、lnk、registry、health、mcp、history...
docs/         # json-contract.md、mcp.md、changelog-archive.md
tests/                     # 429 个单元测试
```

---

## 🧪 测试与质量

```bash
pip install -e ".[dev]"
python -m ruff check pc_cleaner tests   # 未使用导入 / 导入顺序 / 常见 bug
python -m mypy                         # win32 平台，含未注解函数
pytest        # 429 passed（须在项目根目录运行，见上方提示）
```

CI 在 Windows × Python 3.12 上运行 pytest、`ruff`、`mypy`、版本/规则校验和 JSON 健康检查；版本或规则校验失败也会让任务变红。
Linux 只用于静态检查和平台无关测试；Windows 路径语义用例以 Windows CI 为准。

UI 输出全部走 stderr 的进度组件，避免污染 `--json` / MCP stdout；历史与审计使用原子写 +
跨进程锁。

---

## 💖 支持作者

<p align="center">
  <img src="docs/donate.jpg" alt="赞赏码" width="220" />
</p>

## 📄 License

[MIT](LICENSE)
