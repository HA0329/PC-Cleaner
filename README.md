# 🧹 PC Junk Cleaner

> 为**个人电脑**定制的安全垃圾清理工具（Windows 优先）。
> 先预览后确认 · 默认进回收站可撤销 · 二次防御防误删 · 风险分级 · 审计留痕 · 只读体检。

以**安全为第一优先级**：不会直接删东西，而是先扫描、告诉你每个分类能释放多少、列出将删除的
文件，**经你确认后**才动手。内置受保护路径黑名单 + 删除前二次防御 + 进程占用保护。

```bash
python -m pc_cleaner --list      # 先看能清多少（不删任何东西）
python -m pc_cleaner             # 进入交互式菜单
```

**当前版本 0.9.10**（版本演进与每处修改的来龙去脉见 [CHANGELOG.md](CHANGELOG.md)）。

---

## ✨ 功能特性

- 🗂 **分类清理**：30 个分类、276 条内置规则（含 51 条 `--deep` 深度规则），覆盖系统临时文件、
  GPU 着色器缓存、浏览器缓存、微信 4.x 运行缓存、游戏平台、开发工具、系统日志、
  **失效快捷方式**、**使用痕迹**，以及管理员深度清理。
- 🔗 **失效快捷方式清理**（v0.9.9）：纯 Python 解析 `.lnk`（不依赖 COM），只清
  "目标所在卷可访问但目标确实不存在"的死链；离线盘、移动盘、网络共享、URL 与
  系统命名空间（文件资源管理器/回收站）一律不动，`Startup` 开机启动目录默认排除。
- 🧹 **使用痕迹清理**（v0.9.9）：最近打开的文档、跳转列表缓存、PowerShell 命令历史
  （不删用户文件，清空后系统自动重建）。
- 🩺 **注册表垃圾只读扫描**（v0.9.9）：`--registry-scan` 列出失效卸载表项 /
  MuiCache 孤儿缓存 / 失效 App Paths，**只报告不删除**（见下方"关于注册表"）。
- 🔍 **先预览后确认**：扫描 → 展示体积与文件列表（按体积降序）→ 人工确认 → 执行。
- ♻️ **默认可回收**：删除进回收站（`send2trash` 为必需依赖，不静默降级为永久删除）；
  `--undo-last` 或菜单 `u` 键可恢复最近一次。
- 🚦 **风险分级**：🟢 安全 / 🟡 一般 / 🔴 高风险；高风险分类默认隐藏，`--all` 也不会选中。
- 🛡 **安全防护**：受保护路径黑名单、删除前二次防御、跳过符号链接/junction、
  路径规范化后再匹配、进回收站失败**默认保留原文件**。
- 🧊 **进程占用保护**：规则声明 `skip_if_in_use` 时，目标被运行中程序占用即跳过。
- 🩺 **只读体检**：`--health` 16 项系统健康信号、`--checkup` 清理前准备情况，均为纯只读。
- 🤖 **面向自动化**：固定退出码契约 + JSON 信封；`--mcp` 暴露 MCP 服务端（**默认只读**）。
- 📊 **实时反馈**：加载指示、扫描进度条（已找到 N 项 / X MB / 剩余时间）、清理实时状态；
  全部走 stderr 且 TTY 限定，`--json`/管道/MCP 场景不多出一个字节。

---

## 🗂 内置清理分类（30 个）

| 分类 key | 名称 | 风险 | 清理内容 |
| --- | --- | --- | --- |
| `system_temp` | 系统临时文件 | 🟢 | 用户/系统 Temp、缩略图/图标缓存、WebCache、错误报告、最近文档、崩溃转储 |
| `gpu_caches` | GPU 着色器缓存 | 🟢 | NVIDIA/AMD DXCache、GLCache、D3DSCache（可安全重建） |
| `nvidia_app_cache` | NVIDIA 应用缓存 | 🟢 | NVIDIA App / Overlay 的界面与组件缓存 |
| `web_cache` | 浏览器/网页缓存 | 🟢 | Edge/Chrome/Firefox/Brave/Vivaldi/Opera 缓存、Steam htmlcache、INetCache |
| `wechat_cache` | 微信运行缓存 | 🟢 | 微信 4.x 插件模块、小程序缓存、网络缓存、日志、升级包（**聊天数据绝不触碰**） |
| `office_caches` | 办公软件缓存 | 🟢 | Office 文档同步缓存、WPS 缓存 |
| `media_caches` | 多媒体设计软件缓存 | 🟢 | Adobe Premiere / After Effects 媒体缓存 |
| `comm_caches` | 通信工具缓存 | 🟢 | Zoom、Discord、Telegram |
| `game_caches` | 游戏平台缓存 | 🟡 | Steam appcache/日志/着色器缓存、Epic、Battle.net、GOG、Riot |
| `game_runtime_cache` | 游戏运行时缓存 | 🟡 | 无畏契约、三角洲行动、Unreal Engine、CS:GO |
| `live_stream_cache` | 直播伴侣/电竞平台缓存 | 🟡 | 抖音直播伴侣、完美世界竞技平台 |
| `dev_caches` | 开发工具缓存 | 🟡 | pnpm/npm(`_npx`/`_cacache`)/pip/uv/yarn/Go/cargo/NuGet/Gradle/Maven、`__pycache__`、Electron/Docker/VSCode/JetBrains |
| `downloads` | 下载/旧文件 | 🔴 | Downloads 中的大文件/久未使用文件/安装包（默认隐藏） |
| `system_admin` | 系统深度清理 | 🟡 | Windows 更新缓存、Prefetch、`%WINDIR%\SystemTemp`、chkdsk 残留、.NET 原生映像（需管理员） |
| `system_logs` | 系统日志与诊断残留 | 🟢 | DISM/CBS/waasmedic 日志、`System32\LogFiles`、`winevt\Logs` 归档、传递优化缓存 |
| `windows_old` | 旧版 Windows 残留 | 🔴 | `C:\Windows.old`（默认隐藏，需管理员，**不可恢复**） |
| `dev_purge` | 项目构建产物 | 🔴 | 散落 node_modules / dist / build / target / .next（默认隐藏） |
| `browser_privacy` | 浏览器隐私数据 | 🔴 | Cookie 与浏览历史（默认隐藏，**会退出登录**） |
| `browser_data` | 浏览器站点数据 | 🔴 | 本地存储、会话、表单、登录密码、站点偏好（默认隐藏，**会退出登录**） |
| `database_compact` | 浏览器数据库压缩 | 🟢 | SQLite `VACUUM` 释放碎片（**不删数据**，体积按空闲页估算） |
| `webview2_caches` | WebView2 嵌入式缓存 | 🟡 | UWP/系统应用内嵌 WebView2 的图形/着色器/组件缓存 |
| `hidden_installer_backups` | 隐蔽的安装包残留 | 🟡 | `$Windows.~BT`/`~WS`（需管理员） |
| `recycle_and_diagnostics` | 诊断日志(ETL) | 🟡 | ETL 诊断日志、WinSAT 缓存（需管理员） |
| `cloud_app_hidden` | 云盘与商店应用缓存 | 🟢 | OneDrive 缓存、Windows Store 应用临时文件 |
| `java_rdp_legacy` | Java/远程桌面/字体缓存 | 🟢 | Java 部署缓存、远程桌面位图缓存、系统字体缓存 |
| `rdp_legacy_cache` | 远程桌面旧版缓存(用户文档) | 🟡 | `文档\Remote Desktop\Cache` 里的剪贴板/位图缓存（v0.9.10 从 `java_rdp_legacy` 拆出：可重建，但物理上位于用户文档目录内，故不放进 `--all`） |
| `crash_telemetry` | 崩溃上报与遥测 | 🟢 | WER 错误报告归档/队列、遥测存储 |
| `extreme_stealth` | 变态级隐蔽缓存 | 🟡 | CBS 历史日志、大体积事件日志、Steam/Epic 下载残留、音视频客户端缓存 |
| `broken_shortcuts` | 失效快捷方式 | 🔴 | 开始菜单/桌面/快速启动栏中目标已被卸载的死链（离线盘、移动盘、网络共享、URL、系统命名空间不动，`Startup` 排除） |
| `usage_traces` | 使用痕迹 | 🟡 | 最近打开的文档、跳转列表缓存、PowerShell 命令历史（不删用户文件） |

> 🔗 `broken_shortcuts` 的判定分四档：`ok`（目标存在）/ `broken`（卷可访问但目标不存在，
> **只有这一档会被清理**）/ `unavailable`（离线盘、未插移动盘、断开的共享 —— 插上盘还能用）/
> `unknown` 与 `invalid`（URL、系统命名空间、0 字节 UWP 占位符）。判定细节见
> [SECURITY.md 的设计边界](SECURITY.md#明确的设计边界不是漏洞)。
>
> 🧹 `usage_traces` 属隐私与桌面整洁范畴，**允许**打进 `--all`；`browser_privacy` /
> `browser_data` 才是会退出登录的高风险项。

> 🗑 `recycle_bin`（回收站）不在 `rules.json` 中，属引擎特殊处理项：
> 用 `--clean recycle_bin`、菜单 `r`，或配置 `all_includes_recycle_bin` 让 `--all` 带上它。

---

## 🔒 安全保证

1. **不确认不删**：所有删除都要人工确认（`--yes` 除外，谨慎使用）；非交互环境下遇到危险操作会被**明确拒绝**。
2. **回收站优先**：默认进回收站；`send2trash` 是必需依赖，**不再静默降级为永久删除**。
   进回收站失败时**默认保留原文件**（`recycle_error_fallback` 可改为回退永久删除，默认关闭）。
   回收站不可用时（未安装/导入失败）**整批拒绝执行并报错，一个文件都不删** ——
   `--recycle` 会在启动阶段就返回退出码 1，引擎层同样拒绝，绝不会"以为进了回收站、其实永久删除"。
3. **风险分级**：高风险分类默认隐藏，`--all` 不选中；永久删除、清空回收站、含高风险分类的
   操作在非交互场景下**必须显式 `--risky` 授权**，否则返回 `needs_confirmation` + 退出码 4，**什么都不删**。
   交互式确认要求完整输入 `yes`（单个 `y` 无效）。
4. **黑名单保护**：绝不触碰 `System32`、`WinSxS`、`DriverStore`、`Windows\Installer`、
   `$Recycle.Bin`、`System Volume Information`、**微信聊天数据**（`WeixinShuju`/`xwechat_files`）、
   `.git`/`.venv` 等，支持自定义。匹配为**组件级全等**（不会像子串那样误伤 `windows.old`）。
5. **路径规范化后再判定**：折叠 `..`、去掉 `\\?\` 前缀、去结尾空格与点，
   避免 `C:\Windows\..\Temp` 这类写法绕过保护。
6. **白名单清空例外**：`%WINDIR%\Prefetch`、`%WINDIR%\Temp`、`System32\LogFiles` 等
   受保护前缀之下**明确可重建**的缓存，只允许「清空内容」，删除目录本身仍被拒绝。
7. **删除前二次防御**：引擎层对每个目标**再查一次**磁盘根、系统关键文件（`pagefile.sys` 等）
   与受保护路径，即使扫描器漏判也删不掉。
8. **跳过危险结构**：不跟随符号链接与 junction，拒绝删除链接自身，清空目标本身是链接时也拒绝。
9. **逐项容错 + 如实上报**：单个文件被占用/无权限时跳过并继续；部分失败计入 `skipped`，一个都没清掉算失败。
   扫描之后、删除之前就已被外部删掉的目标计入 `vanished`（**不算** `deleted`，也不谎报释放量）。
10. **释放量如实**：永久删除按**删除前后实测体积差**算 `freed`；进回收站的字节只计 `recycled`
    （清空回收站才真正释放），不混进 `freed` 虚报。
11. **审计留痕**：每次清理写入 `history.json`（原子写 + 跨进程锁）与 `audit.log`；**Ctrl+C 也会落盘**。

> 📌 微信聊天数据在**数据目录**（如 `D:\WeixinShuju`），本工具只清 `%APPDATA%\Tencent\xwechat`
> 下的运行缓存，**绝不**触碰聊天记录；数据目录很大时请用微信「设置 → 存储空间」。
>
> 📌 `browser_privacy` / `browser_data` 会删除 Cookie、登录密码与站点数据导致**退出登录**，
> 属高风险，默认隐藏，清理前请先退出浏览器。
>
> 📌 组件库（WinSxS / DriverStore / `Windows\Installer` / 卷影副本）**只报告不删除**，
> `--checkup` 会给出官方清理命令（DISM / pnputil / vssadmin）。这些路径已在保护黑名单中。
>
> 📌 **关于注册表**：火绒一类工具提供"注册表垃圾清理"，本工具**只做只读扫描**
> （`--registry-scan`），**不提供任何删除入口**。原因：删注册表项**不释放磁盘空间**，
> 而"这条记录还有没有用"无法可靠判定，误删会让软件甚至系统出问题。实测也印证了这点 ——
> 本项目实现该扫描时，最初用"压缩产品码匹配"判定 MSI 残留，结果把 41 条 MSI 表项
> **全部误报**（里面是仍在使用的 VC++/Node.js/Java 运行时）；改为调用 Windows Installer
> API（`MsiQueryProductStateW`）做权威判定后，拿不到结论就**不报**。要动手请自行用
> `regedit` 核对，并先 `reg export <路径> backup.reg` 备份。

---

## 🚀 快速开始

要求：Python 3.10+（推荐 3.12）。

> ⚠️ `python -m pc_cleaner` 必须在**项目根目录**（含 `pc_cleaner/`、`pyproject.toml` 的那层）运行，
> 不要进入 `pc_cleaner/` 子目录。测试同理：在项目根目录执行 `pytest`（在别的目录跑会因
> 找不到 `pc_cleaner` 包而整批 collection error）。

```bash
pip install -e .                          # send2trash 随包安装，删除默认进回收站
python -m pc_cleaner --list               # 只扫描，不删任何东西
python -m pc_cleaner                      # 交互式菜单
python -m pc_cleaner --health             # 只读体检（16 项，不改任何系统设置）
python -m pc_cleaner --checkup            # 一键体检（管理员/磁盘/回收站/可清理量）
python -m pc_cleaner --clean system_temp  # 清理指定分类（会先预览再确认）
```

**Windows 双击**：`pc_cleaner.bat`（加 `--no-pause` 可让窗口结束时自动关闭；
带空格的参数请用引号，例如 `pc_cleaner.bat --export-scan "D:\My Dir\scan.json"`）。

**便携运行，不写用户目录**：用 `PC_CLEANER_HOME` 把配置/历史/审计重定向到别处：

```powershell
$env:PC_CLEANER_HOME = "D:\your\workspace\.pc_cleaner_runtime"
python -m pc_cleaner --checkup
```

---

## 🖥 常用命令

| 参数 | 说明 |
| --- | --- |
| `--list` / `-l` | 仅扫描，列出各分类占用、磁盘可用、回收站占用 |
| `--detail` / `-d`、`--tree` | 详细 / 树形展示扫描结果 |
| `--deep` / `-D` | 深度扫描：启用 `deep_only` 规则，并把遍历深度提升到至少 50 层（除非显式给了 `--max-depth`） |
| `--workers N` | 并行扫描线程数（1=串行，0=按 CPU 自动，默认 4） |
| `--clean 分类` | 清理指定分类（逗号分隔）；**分类名拼错 → 退出码 1** |
| `--all` / `--exclude 分类` | 选中全部非高风险分类 / 排除某些分类 |
| `--dry-run` | 只预览，不真正删除 |
| `--recycle` / `--permanent` | 进回收站（默认）/ 永久删除（危险，需授权） |
| `--risky` | 显示高风险分类，并作为危险操作的**显式授权** |
| `--yes` / `-y` | 跳过交互确认（谨慎）；危险操作仍需 `--risky` |
| `--ext` / `--min-size-mb` / `--older-than-days` | 按扩展名 / 最小体积 / 最旧修改时间过滤 |
| `--shred` | 永久删除前随机覆写内容（隐私增强，`--shred-passes N` 控制遍数） |
| `--health` | **只读体检（16 项）**；有 `error` 项时退出码 1，可配 `--json`、`--lang` |
| `--registry-scan` | 注册表垃圾**只读扫描**（失效卸载表项 / MuiCache 孤儿 / 失效 App Paths）；**绝不删除注册表项** |
| `--checkup` | 清理前准备情况 + 本机环境适配 + 组件库体积 |
| `--history` / `--undo-last` | 查看清理历史 / 从回收站恢复最近一次 |
| `--json` / `--json-schema` | JSON 输出（带退出码契约）/ 输出契约的 JSON Schema |
| `--mcp` / `--mcp-allow-delete` | MCP 服务端（默认只读）/ 额外放开删除能力 |
| `--show-config`、`--export-config`、`--import-config` | 配置查看与导入导出 |
| `--show-rules` / `--validate-rules` | 展示内置规则 / 校验规则格式与告警 |
| `--audit-rules` | 配合 `--validate-rules`：列出**在本机匹配不到任何内容**的规则 |
| `--admin` | UAC 提权重启（系统深度清理需要） |
完整参数（`--sort`、`--max-depth`、`--export-scan`、`--no-progress`、`--lang`、
`--recycle-fallback`、`--shred-passes` 等）见 `python -m pc_cleaner --help`。

**常见用法**

```bash
python -m pc_cleaner --list --deep                                  # 深度扫描看能清多少
python -m pc_cleaner --clean gpu_caches,wechat_cache --recycle       # 清指定分类，进回收站
python -m pc_cleaner --all --exclude downloads --recycle             # 清所有非高风险分类
python -m pc_cleaner --clean recycle_bin                             # 清空回收站（会二次确认）
python -m pc_cleaner --clean database_compact                        # 压缩浏览器数据库（不删数据）
python -m pc_cleaner --clean system_logs --recycle --admin           # 系统日志（UAC 提权）
python -m pc_cleaner --clean broken_shortcuts --risky --recycle      # 清失效快捷方式（高风险，需显式选择）
python -m pc_cleaner --clean usage_traces --recycle                  # 清使用痕迹（最近文档/跳转列表/命令历史）
python -m pc_cleaner --registry-scan                                 # 注册表垃圾只读扫描（不删任何东西）
python -m pc_cleaner --json --clean downloads --yes --risky          # 自动化：显式授权才执行
python -m pc_cleaner --undo-last                                     # 恢复最近一次清理
python -m pc_cleaner --validate-rules --audit-rules                  # 查规则在本机是否还有效
```

**菜单快捷键**：编号（支持 `1,3-5` 区间）选择 · `all` 全选 · `r` 回收站 · `0` 清空选择 ·
`d` 详情 · `t` 树形 · `s` 排序 · `x` 切换高风险显示 · `f` 重新扫描 · `u` 撤销上次 ·
`h` 帮助 · `q` 退出。

---

## 🔢 退出码与 JSON 契约

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 参数 / 配置 / 分类名错误 |
| `2` | argparse 用法错误 |
| `3` | 执行中有目标失败（被占用 / 无权限） |
| `4` | 需要确认或已取消 —— **什么都没删** |
| `130` | 用户中断（Ctrl+C） |

`--json` 顶层固定为 `{schema_version, ok, status, exit_code, categories, action}`。
`action` 含 `deleted`、`failed`、`skipped`（部分清理的目标数）、`skipped_in_use`、
`vanished`（扫描后已不存在、未执行删除的目标数）、`freed_bytes`、
**`recycled_bytes`**（进回收站的字节，清空回收站后才真正释放）。
「什么都没执行」用 **`action.not_executed: true`**。

**典型自动化流程**：`--json --dry-run` 预览 → 确认 → `--json --clean <分类> --yes`
（危险操作再加 `--risky`），据 `exit_code` 与 `status` 分支。
完整字段表与 `status` 取值见 [docs/json-contract.md](docs/json-contract.md)，
`--json-schema` 可直接取到 JSON Schema。

---

## 🤖 MCP 服务端

让 AI Agent（Claude Desktop / DSH / 自研）以受控流程使用本工具，零依赖 stdio JSON-RPC 2.0，
stdout 只承载协议、日志走 stderr。

```bash
python -m pc_cleaner --mcp                       # 只读：scan / health / history / preview_delete
python -m pc_cleaner --mcp --mcp-allow-delete    # 额外暴露 delete / undo（仍需两阶段确认）
```

| 机制 | 说明 |
| --- | --- |
| **默认只读** | 未加 `--mcp-allow-delete` 时工具表里没有 `delete`/`undo`，直接调用也会被拒绝 |
| **遵守本机配置** | 配置 `enabled_categories` 里被关掉的分类，Agent 既不能预览也不能删除（v0.9.10） |
| **两阶段确认** | `preview_delete` 返回清单 + `confirm_token`（600 秒、一次性、绑定清单指纹） |
| **漂移检测** | `delete` 会重扫比对，出现未预览过的新目标且超阈值 → 拒绝执行；**永远只删预览过的路径** |
| **危险二次授权** | 清单含高风险分类 / 永久删除 / 清空回收站时须带 `acknowledge_danger=true` |
| **留痕可撤销** | `delete` 写 `history.json` + `audit.log`，删掉的东西可用 `undo` 找回；`undo` 会**如实上报**：一条都没恢复时返回 `ok:false / status:"failed"`，部分恢复返回 `status:"partial"`（v0.9.10） |
| **注册表只读** | `registry_scan` 只报告不修改；MCP 层不存在任何注册表清理工具 |

客户端配置示例与完整工具参数见 [docs/mcp.md](docs/mcp.md)。
建议先用只读模式观察 Agent 行为，再按需放开写能力。

---

## 🖥 运行环境自动适配

启动时**只读探测**本机实际装了什么（纯只读、零副作用、逐项容错），未安装软件的缓存分类
自动显示为空，避免"为啥这项是 0"的疑惑：

- **浏览器**：Edge / Chrome / Firefox / Brave / Vivaldi / Opera
- **GPU**：NVIDIA / AMD（对应着色器缓存可清）
- **微信**：4.x 还是 3.x、可清理的运行缓存清单与实际体积、数据目录位置（只提示不触碰）
- **Steam**：安装痕迹与各 steamapps 库目录；**pnpm store** 实际位置
- **开发工具**：PATH 上可用的 node / npm / pip / go / java / dotnet / git
- **组件库**：WinSxS / DriverStore / `Windows\Installer` / 卷影副本的体积与官方清理命令

Windows 之外平台可运行，但分类路径以 Windows 为目标；需管理员的分类与回收站恢复仅 Windows 有效。

> 🩺 `--checkup` 面向**清理前的准备**（管理员、磁盘、可清理量、组件库）；
> `--health` 面向**系统健康排查**（更新 / 安全启动 / 设备 / 磁盘健康等 16 项）。两者都只读。

---

## ⚙️ 配置文件

程序在用户目录生成 `pc_cleaner/config.json`（可用 `PC_CLEANER_HOME` 重定向），
`--show-config` 查看、`--export-config` / `--import-config` 跨机同步。主要字段：

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `recycle_by_default` | 默认是否进回收站 | `true` |
| `recycle_error_fallback` | 进回收站失败是否回退永久删除（false 更安全） | `false` |
| `protected_paths` | 额外加入的黑名单路径 | `[]` |
| `custom_rules` | 自定义清理规则 | `[]` |
| `enabled_categories` | 非空时只扫描这些分类 | `[]` |
| `preview_lines` | 每个分类预览展示的行数 | `12` |
| `show_risky` | 菜单是否显示高风险分类 | `false` |
| `enable_history` | 是否记录历史与审计日志 | `true` |
| `scan_depth` / `scan_workers` | 遍历深度 / 并行线程数（上限 16） | `20` / `4` |
| `all_includes_recycle_bin` | `--all` 是否连带清空回收站 | `false` |
| `language` | `zh_CN` / `en`（空串 = 按 `PC_CLEANER_LANG` 或默认） | `""` |

其余字段（`default_sort`、`show_scan_progress`、`dev_artifact_bases` 等）见 `--show-config` 输出。

**自定义规则**支持 `clear_dir`、`delete_dir`、`glob_dirs`、`glob_files`、`files_by_rule`、
`find_dirs`、`compact_db`、`empty_dirs`、`zero_byte_files`；可选字段 `pattern`（`glob_*` 必填）、
`ext`、`min_size_mb`、`older_than_days`、`max_depth`（`find_dirs` 必填）、`action`、
`deep_only`、`skip_if_in_use`、`label`。改完用 `--validate-rules` 校验。

**多候选路径（兼容软件不同版本的目录布局）**：同一软件改过目录名时，把新旧路径都列出来，
**存在哪个就清哪个**：

```jsonc
// clear_dir / delete_dir：用 paths 代替 path
{ "type": "clear_dir", "paths": ["%APPDATA%/Tencent/xwechat/log",
                                 "%APPDATA%/Tencent/xwechat/logs"],
  "label": "微信日志", "skip_if_in_use": true }

// glob_dirs：用 bases 代替 base
{ "type": "glob_dirs", "bases": ["%APPDATA%/Tencent/xwechat/radium",
                                 "%APPDATA%/Tencent/xwechat/radium/cache"],
  "pattern": "web", "action": "clear", "label": "小程序容器网页缓存" }
```

单数 `path` / `base` 的老写法行为不变，两种可同时出现，结果按路径去重。
用 `--validate-rules --audit-rules` 可查出**本机哪些规则已经扫不到东西**
（识别"软件升级后规则路径过时"，而不是继续静默空转）。

---

## 🗃 项目结构

```
pc_cleaner/
├── cli.py / __main__.py   # 命令行入口与参数解析
├── menu.py                # 交互式菜单、预览与清理流程
├── scanner.py             # 安全扫描与体积计算（并行、去重、剪枝）
├── engine.py              # 删除引擎（回收站/永久、二次防御、shred、恢复、VACUUM）
├── rules.py / rules.json  # 规则加载、黑名单/白名单、风险分级、校验（30 分类 276 目标）
├── lnk.py                 # Windows 快捷方式(.lnk)解析 —— 纯 Python，无 COM（v0.9.9）
├── registry.py            # 注册表垃圾**只读**扫描（绝不删除，v0.9.9）
├── health.py / env.py     # 只读体检（16 项）/ 运行环境探测
├── mcp.py / service.py    # MCP 服务端 / 退出码契约与 JSON 信封
├── history.py             # 清理历史与审计日志（原子写 + 跨进程锁）
├── i18n.py / locales/     # 国际化（覆盖 --health 报告）
├── ui.py / console.py     # 输出、确认、进度 / ANSI 颜色与 CJK 对齐
├── proc.py / config.py    # 进程占用检测 / 配置读写
docs/                      # mcp.md、json-contract.md
tests/                     # 347 个单元测试
```

---

## 🧪 测试

```bash
pip install -e ".[dev]"
pytest        # 347 passed（须在项目根目录运行，见上方提示）
```

CI 在 **Windows + Linux × Python 3.10/3.12/3.13** 矩阵上跑同一套
（Linux 上 Windows 专属用例由 `skipif` 跳过）。各测试文件的覆盖点见 [CHANGELOG.md](CHANGELOG.md)。

---

## 💖 支持作者

如果这个工具帮你省下了时间或磁盘空间，欢迎扫码赞赏：

<p align="center">
  <img src="docs/donate.jpg" alt="赞赏码" width="220" />
</p>

## 📄 License

[MIT](LICENSE)
