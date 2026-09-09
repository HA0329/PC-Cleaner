# 🧹 PC Junk Cleaner

> 为**个人电脑**定制的安全垃圾清理工具（Windows 优先）。
> 先预览后确认 · 删除可进回收站 · 二次防御防误删 · 风险分级 · 可撤销 · 审计日志 · 只读体检。

一个以**安全为第一优先级**的垃圾清理程序：它不会直接删东西，而是先扫描、告诉你每个分类
能释放多少空间、列出将删除的文件，**经你确认后**才动手。默认删除到回收站（可恢复），内置
受保护路径黑名单与删除前二次防御。

**v0.9.0 起吸收 BleachBit + Dism++ 的清理能力**：浏览器站点数据（DOM/本地存储、会话、
登录密码、表单历史、站点偏好、同步数据）、浏览器数据库压缩（SQLite `VACUUM`，不删数据）、
以及 Dism++「空间回收」的 Windows 事件日志 / 崩溃内存转储 / .NET 原生映像缓存等。

**v0.9.2 针对本机实测加强**：微信 4.x 插件模块与小程序缓存（本机 +675MB）、系统日志与
诊断残留（新增 `system_logs` 分类）、npm `_npx`/`_cacache` 等开发缓存，并修好
「扫描空间虚高」「释放量不实」「`--undo-last` 无效」等缺陷 —— 本机可清理量 507MB → 1.30GB。

**v0.9.3 安全加固 + 面向自动化**：删除会连带清掉**正在运行**的 `npm-cache` 父规则
（改为只清 `_npx`/`_cacache`/`_logs` 并加 `skip_if_in_use`）、补齐路径规范化与 junction
防护、`--validate-rules` 强制 `pattern`/`max_depth` 并输出警告；新增**只读体检 `--health`**
（16 项）、**退出码契约 + JSON 信封**，并收紧 `--json --yes` 的危险操作闸门。

**v0.9.4 MCP + 契约 + 国际化 + CI**：新增 **MCP 服务端 `--mcp`**（stdio JSON-RPC 2.0，
默认只读、两阶段确认、漂移检测）；JSON 契约升级为**机器可读**（`--json-schema`、
`docs/json-contract.md`，`action.not_executed` 与新增 `status`）；`--health` 报告支持
**中英双语**（`--lang` / `PC_CLEANER_LANG` / 配置 `language`）；新增 GitHub Actions 矩阵 CI。

---

## ✨ 功能特性

- 🗂 **分类清理**：27 个清理分类、272 条内置规则（含 `--deep` 深度规则 50 条），覆盖系统临时
  文件、GPU 着色器缓存、浏览器/网页缓存、微信 4.x 运行缓存与插件模块、游戏平台缓存、开发
  工具缓存、系统日志与诊断残留、下载旧文件、回收站，以及**管理员深度清理**（Windows 更新
  缓存 / Prefetch / 事件日志 / 崩溃转储 / .NET 原生映像等）。
- 🔍 **先预览后确认**：扫描 → 展示体积与文件列表（按体积从大到小）→ 人工确认 → 执行。
- 🚦 **风险分级**：🟢 安全 / 🟡 一般 / 🔴 高风险彩色徽标；高风险分类默认隐藏（`--risky`
  或配置 `show_risky` 开启），`--all` 也不会选中它们。
- ♻️ **可回收**：删除默认进回收站（`send2trash` 已是**必需依赖**，不再静默降级为永久删除）。
- ↩️ **可撤销**：`--undo-last` 把最近一次「进回收站」的清理从回收站恢复回来，支持
  「目录被整体回收」与「目录内容被逐条回收」两种情形。
- 📋 **审计与历史**：每次清理记录 `history.json` + `audit.log`（含**实际释放字节数**），
  `--history` 可查。`history.json` 采用**原子写**（tmp + `os.replace`）、跨进程锁
  `history.lock`，解析失败会改名 `*.corrupt-<时间戳>` 保留证据；**Ctrl+C 中断也会落盘**。
- 🩺 **只读体检 `--health`（v0.9.3）**：16 项系统健康信号（系统版本 / Windows 更新暂停 /
  待重启 / Secure Boot / TPM / 磁盘空间 / 设备问题 / 异常关机 / SMB1 / 来宾登录 / 防火墙 /
  杀毒软件 / 幽灵设备 / 磁盘健康 / 长路径 / 休眠文件），零依赖、纯只读、**绝不修改系统**；
  有 `error` 项时退出码 1，`--health --json` 输出可编程信封。
- 🛡 **智能安全**：受保护路径黑名单 + 白名单清空例外、删除前**二次防御**、清空时逐项跳过
  受保护子项、跳过符号链接与 junction（含删除时拒绝链接自身、**CLEAR 目标本身是链接时也
  拒绝**）、路径词法规范化后再匹配（折叠 `..`、去 `\\?\` 前缀、去结尾空格/点）、逐项捕获
  权限错误；**进回收站失败时默认保留原文件**，绝不静默转永久删除。
- 🧊 **进程占用保护（v0.9.3）**：规则可用 `"skip_if_in_use": true` 声明「目标被运行中进程
  占用时跳过」，避免删到正在运行的程序（如 npm `_npx` 里跑着的 npx / AI 工具）；被跳过的
  目标计入 `skipped_in_use`。
- 🧩 **数据库压缩（v0.9.0）**：`compact_db` 目标类型用 SQLite `VACUUM` 对浏览器
  `History` / `Web Data` / `Login Data` / `Cookies` / `places.sqlite` 等库重写以释放碎片，
  **不删除任何数据**，浏览器下次运行自动重建；库被占用/只读时安全跳过。v0.9.3 起体积按
  **空闲页 × 页大小**估算（不再把整库大小当作可释放），无碎片可回收时不显示该目标。
- 🧭 **本机适配**：启动时只读探测这台机器（浏览器 / GPU / 微信布局与**可清理缓存清单** /
  Steam / pnpm store / 开发工具 / **组件库体积与官方清理命令**），未安装软件的缓存分类
  自动显示为空。
- 🎛 **交互式菜单**：汇总表即菜单（只给有内容的分类编号）、支持区间选择（`1,3-5`）、
  `r` 连回收站一起清、`d` 详情 / `t` 树形 / `s` 排序 / `x` 切换高风险显示 / `q` 退出。
- ⚡ **并行扫描（v0.9.2）**：目录遍历 I/O 密集，`--workers N` / `scan_workers` 多线程扫描，
  结果顺序不变；同一目录只统计一次（含子目录缓存命中）。
- 🤖 **面向自动化（v0.9.3）**：固定**退出码契约**与**JSON 信封**（`schema_version` / `ok` /
  `status` / `exit_code`），危险操作（高风险分类 / 永久删除 / 清空回收站）在非交互环境下
  必须显式 `--risky` 授权，否则返回 `needs_confirmation` + 退出码 4，**什么都不删**。
- 🤖 **MCP 服务端（v0.9.4）**：`--mcp` 以 **stdio JSON-RPC 2.0** 暴露 `scan` / `health` /
  `history` / `preview_delete` 四个**只读**工具；加 `--mcp-allow-delete` 才额外暴露
  `delete` / `undo`，且必须走**两阶段确认**（预览拿 `confirm_token` → 再执行）。详见
  [docs/mcp.md](docs/mcp.md)。
- 🌐 **国际化（v0.9.4）**：`--lang zh_CN|en` / 环境变量 `PC_CLEANER_LANG` / 配置 `language`
  （优先级：`--lang` > 配置 > 环境变量 > `zh_CN`）。当前**完整覆盖 `--health` 体检报告**
  （16 项的 label/detail/advice、标题、汇总、页脚），`--lang en --health` 无中文残留
  （仅杀毒软件产品名来自系统返回值）；交互菜单与清理摘要仍为中文，缺失 key 回退 `zh_CN`。
- 🧾 **机器可读 JSON 契约（v0.9.4）**：`--json-schema` 输出信封的 JSON Schema；
  字段表见 [docs/json-contract.md](docs/json-contract.md)。`action.skipped` 是**整数计数**，
  「什么都没执行」改用 `action.not_executed: true`。
- ⚙️ **可配置**：`config.json` 可保存回收站偏好、失败回退策略、额外保护路径、自定义规则、
  只扫描指定分类、预览行数、是否显示高风险、并行线程数、是否记录历史等。

---

## 🗂 内置清理分类（27 个）

| 分类 key | 名称 | 风险 | 清理内容 |
| --- | --- | --- | --- |
| `system_temp` | 系统临时文件 | 🟢 | 用户/系统 Temp、缩略图/图标缓存、WebCache、错误报告、最近文档/跳转列表、崩溃转储 |
| `gpu_caches` | GPU 着色器缓存 | 🟢 | NVIDIA/AMD DXCache+GLCache、DirectX D3DSCache、NV_Cache（可安全重建） |
| `nvidia_app_cache` | NVIDIA 应用缓存 | 🟢 | NVIDIA App / Overlay 的 CEF 界面缓存与组件缓存 |
| `web_cache` | 浏览器/网页缓存 | 🟢 | Edge/Chrome/Firefox/Brave/Vivaldi/Opera 缓存与 GPU 缓存、Steam htmlcache、INetCache |
| `wechat_cache` | 微信运行缓存 | 🟢 | 微信 4.x：`xplugin/plugins` 插件模块、`radium` 小程序缓存、`net*` 网络缓存、日志/崩溃报告/升级包；3.x 遗留日志与临时文件（**聊天数据目录已从规则中移除，绝不触碰**） |
| `office_caches` | 办公软件缓存 | 🟢 | Office 文档同步缓存（OneDrive/SharePoint）、WPS 缓存 |
| `media_caches` | 多媒体设计软件缓存 | 🟢 | Adobe Premiere Pro / After Effects 媒体缓存 |
| `comm_caches` | 通信工具缓存 | 🟢 | Zoom、Discord、Telegram 缓存 |
| `game_caches` | 游戏平台缓存 | 🟡 | 完美世界更新包、Steam appcache/logs/着色器缓存（规则级深度限制）、Epic/Battle.net/GOG/Riot |
| `game_runtime_cache` | 游戏运行时缓存 | 🟡 | 无畏契约、三角洲行动、Unreal Engine、CS:GO 缓存/日志 |
| `live_stream_cache` | 直播伴侣/电竞平台缓存 | 🟡 | 抖音直播伴侣、完美世界竞技平台（含 `Partitions` 网页分区缓存） |
| `dev_caches` | 开发工具缓存 | 🟡 | pnpm/npm（`_npx` 独立目标 + `skip_if_in_use`、`_cacache`、`_logs`）/pip/uv/yarn/Go/cargo/NuGet/Gradle/Maven/WinGet 缓存、corepack、`__pycache__`、Electron/Docker/VSCode/JetBrains/VS |
| `downloads` | 下载/旧文件 | 🔴 | Downloads 中的大文件/久未使用文件/安装包（默认隐藏） |
| `system_admin` | 系统深度清理 | 🟡 | Windows 更新缓存、Prefetch（清理后首次启动可能变慢）、系统 Temp、`%WINDIR%\SystemTemp`（24H2 新增的系统服务临时目录）、chkdsk 残留(found.*，只清空内容)、更新日志(KB*.log)、备份(*.bak)、事件日志归档、崩溃转储、.NET 原生映像缓存（需管理员） |
| `system_logs` | 系统日志与诊断残留 | 🟢 | DISM/CBS/waasmedic/SIH/NetSetup 日志、`System32\LogFiles`（白名单只清空内容）、`winevt\Logs` 归档、setupapi 安装日志、USOShared/USOPrivate 更新状态、传递优化缓存、Panther、security\logs（需管理员；被服务占用的日志会通过 `skip_if_in_use` 如实跳过） |
| `windows_old` | 旧版 Windows 残留 | 🔴 | `C:\Windows.old`（默认隐藏，需管理员，**不可恢复**；建议优先用「设置 → 系统 → 存储」） |
| `dev_purge` | 项目构建产物 | 🔴 | 散落 node_modules / dist / build / target / .next 等（默认隐藏，深度上限 12 层） |
| `browser_privacy` | 浏览器隐私数据 | 🔴 | Edge/Chrome/Firefox 的 Cookie 与浏览历史（默认隐藏，会退出登录） |
| `browser_data` | 浏览器站点数据 | 🔴 | DOM/本地存储、会话、表单历史、登录密码、站点偏好/权限、同步数据（默认隐藏，会退出登录） |
| `database_compact` | 浏览器数据库压缩 | 🟢 | 对浏览器 SQLite 库执行 `VACUUM` 释放碎片（**不删数据**，体积按空闲页估算） |
| `webview2_caches` | WebView2 嵌入式浏览器缓存 | 🟡 | UWP/系统应用内嵌 WebView2 的图形/着色器/组件缓存 |
| `hidden_installer_backups` | 隐蔽的安装包/升级残留 | 🟡 | `$Windows.~BT`/`~WS`（需管理员；MSI Package Cache / WinSxS 已从规则移除，改由 DISM / 卸载修复处理） |
| `recycle_and_diagnostics` | 诊断日志(ETL) | 🟡 | ETL 诊断日志、WinSAT 性能评估缓存（需管理员；**回收站走 `--clean recycle_bin` 的系统 API**，不再用删文件方式） |
| `cloud_app_hidden` | 云盘与商店应用缓存 | 🟢 | OneDrive 缓存、Windows Store 应用临时文件 |
| `java_rdp_legacy` | Java/远程桌面/字体缓存 | 🟢 | Java 部署缓存、远程桌面位图缓存、系统字体缓存 |
| `crash_telemetry` | 崩溃上报与遥测数据 | 🟢 | WER 错误报告归档/队列、遥测存储 |
| `extreme_stealth` | 变态级隐蔽缓存 | 🟡 | CBS 历史日志、大体积事件日志、Steam/Epic 下载残留、音视频客户端缓存（需管理员；SYSTEM 账户缓存已从规则移除） |

> 🗑 `recycle_bin`（回收站）不在 `rules.json` 中，属于 CLI/引擎特殊处理项：
> 用 `--clean recycle_bin`、`--all`（需配置 `all_includes_recycle_bin`）或菜单里的 `r`。

> 🟢 安全（随便清）· 🟡 一般（清理后按需重建）· 🔴 高风险（默认隐藏，需 `--risky`）。
> 各分类默认启用合理的体积/时间阈值与黑名单，避免误删正在使用的文件。

> 🧊 **`skip_if_in_use`（v0.9.3）**：272 条规则中有 251 条声明了该字段。目标被运行中进程
> 占用（存在打开句柄，或某进程的可执行文件/命令行位于目标之内）时引擎整体跳过并计入
> `skipped_in_use`；`%TEMP%`、`%WINDIR%\Temp`、`Prefetch`、`Downloads` 等**故意不加**该
> 字段（否则「全有全无」的跳过会让它们几乎永远变成空操作），它们仍靠逐项容错处理。

> ⚠️ **非文件 / 系统级项**（BleachBit / Dism++ 中属于注册表或系统工具范畴的清理，如
> MUICache、快速运行/最近查询/Shellbags 历史、系统还原点、被取代的 WinSxS 组件、释放磁盘
> 空闲区域、剪贴板等）本工具**不删除、不伪造路径**，已在 `pc_cleaner/rules.json` 顶部的
> `manual_notes` 中说明，建议用系统自带工具或注册表处理。
>
> 📌 **组件库只报告不删除**：`--checkup` 会列出 WinSxS、DriverStore、`Windows\Installer`、
> 卷影副本的体积与官方清理命令（DISM / pnputil / vssadmin）；这些路径已列入保护黑名单，
> 即使规则误写也不会被删除。
> 体积为**目录遍历值**，包含与系统共享的硬链接（WinSxS 尤其明显），会高于 DISM
> `/AnalyzeComponentStore` 报告的实际占用；统计已对同一遍历内的重复 inode 去重，
> 并在输出中标注该口径。

---

## 🔒 安全模型（重要）

1. **不未经确认就删除**：所有删除都需要人工确认（`--yes` 除外，谨慎使用）；非交互环境
   （管道 / Agent）下 `--yes` 遇到危险操作会被**明确拒绝**（见第 11 条）。
2. **进入回收站优先**：默认删除到回收站，可随时还原；`send2trash` 是必需依赖，
   **不再静默降级为永久删除**。进回收站失败时**默认保留原文件**（可配置
   `recycle_error_fallback` 打开回退永久删除）。
3. **风险分级**：高风险分类（下载文件、构建产物、浏览器隐私/站点数据、Windows.old）
   **默认隐藏**，`--all` 不会选中它们；`--clean` 显式指定时给出警告。
4. **黑名单保护**：绝不触碰 `C:\Windows\System32`、`WinSxS`、`DriverStore`、
   `Windows\Installer`、`$Recycle.Bin`、`System Volume Information`、**微信聊天数据**
   （`WeixinShuju`/`xwechat_files`）、`.git`/`.venv` 等，并支持自定义。匹配采用
   **组件级全等**：相对模式（如 `windows\system32`、`weixinshuju`）命中任意一级路径即
   受保护，不会像子串那样误伤 `windows.old`。
5. **路径规范化后再判定（v0.9.3）**：`expand_path` 先展开环境变量、`absolute()` +
   `normpath()` 折叠 `..`、去掉 `\\?\` 扩展前缀，比较时再去掉结尾空格与点 —— 避免
   `C:\Windows\..\Temp`、`\\?\C:\...`、`Temp.` 这类写法绕过保护或导致「同一目录被当成
   两个路径」的重复计数。
6. **白名单清空例外**：`%WINDIR%\SoftwareDistribution\Download`、`%WINDIR%\Prefetch`、
   `%WINDIR%\Temp` 等位于受保护前缀之下的**明确可重建缓存**，仅允许「清空内容」，
   删除目录本身仍被拒绝；白名单按 `%WINDIR%` 动态解析，与系统盘符解耦。
   v0.9.3 新增 `ALLOWED_CLEAR_UNDER_PROTECTED`：`%WINDIR%\System32\LogFiles` 与
   `%WINDIR%\System32\winevt\Logs` 只允许清空内容（此前它们被 `windows\system32`
   保护模式永久拦下，规则静默空转）。
7. **删除前二次防御**：engine 层再次检查每个目标——磁盘根路径、系统关键文件
   （`pagefile.sys` 等）与受保护路径一律拒绝，即使扫描器漏判也删不掉。
8. **跳过危险结构**：不删除未知/系统目录；不跟随符号链接与 junction，**拒绝删除链接
   自身**；v0.9.3 起**清空（CLEAR）目标本身是链接时也拒绝**（否则会顺着
   `C:\Users\All Users` → `C:\ProgramData` 清空链接目标），体积统计同样不跟随根链接。
9. **进程占用保护（v0.9.3）**：规则声明 `skip_if_in_use` 时，目标被运行中进程占用即跳过，
   避免删到正在运行的程序；跳过数如实计入 `skipped_in_use`。
10. **规则校验（v0.9.3）**：`glob_dirs`/`glob_files` 必须显式给出 `pattern`（此前缺省
    `*` 等于清空整个 `base`）、`action` 大小写不敏感且非法值报错、`find_dirs` 必须给
    `max_depth`；`--validate-rules` 还会打印**警告**（死规则 / 冗余规则 / 风险错配），
    警告不影响退出码。
11. **危险操作闸门（v0.9.3）**：高风险分类、永久删除、清空回收站这三类操作，在
    `--json --yes` 与非交互场景下**必须显式 `--risky` 授权**，否则返回
    `status="needs_confirmation"` + 退出码 4，**什么都不删**（修复了 JSON 分支绕过二次确认）。
12. **逐项容错**：单个文件被占用/无权限时跳过并继续，不中断整个任务；**部分失败会如实
    上报**（`skipped` 计数），一个都没清掉时计为失败。
13. **释放量如实**：永久删除按删除前后体积差核算 `freed`；**进回收站的字节数只计
    `recycled`**（要清空回收站才真正释放），不再混进 `freed` 虚报；清空回收站失败会如实
    报错并计入 `failed`。审计日志同步记录。
14. **审计留痕**：每次清理写入 `history.json`（结构化，原子写 + 跨进程锁）与 `audit.log`
    （人类可读）；**Ctrl+C 中断也会落盘**，可继续用 `--undo-last` 恢复。
15. **MCP 默认只读 + 两阶段确认（v0.9.4）**：`--mcp` 启动的 MCP 服务端默认**只暴露只读工具**
    （`scan` / `health` / `history` / `preview_delete`），`delete` / `undo` 需要显式
    `--mcp-allow-delete`；即使放开，执行也必须先 `preview_delete` 拿到 `confirm_token`
    （600 秒、一次性、绑定清单指纹），并在清单漂移或含危险操作时被拒绝。详见下文
    「🤖 MCP 服务端」小节与 [docs/mcp.md](docs/mcp.md)。

> 📌 微信聊天数据在**数据目录**（如 `D:\WeixinShuju`），本工具只清理
> `%APPDATA%\Tencent\xwechat` 下的**网络运行缓存**，**绝不**触碰聊天记录；若数据目录很大，
> 请用微信「设置 → 存储空间」的官方清理。
>
> 📌 `browser_privacy` / `browser_data` 会删除 Cookie、浏览历史、登录密码与站点数据，
> 导致**退出登录**并重置站点设置，属于高风险分类，默认隐藏，请谨慎开启；清理前先退出浏览器。
>
> 📌 `database_compact` 只执行 SQLite `VACUUM`，**不删除数据**，安全；若浏览器正在运行导致
> 数据库被占用，程序会安全跳过该库。

---

## 🚀 快速开始

要求：Python 3.10+（推荐 3.12）。

> ⚠️ `python -m pc_cleaner` 必须在**项目根目录**（含 `pc_cleaner/` 包、`pyproject.toml`
> 的那一层）运行，不要进入 `pc_cleaner/` 子目录。Windows 用户可直接双击 `pc_cleaner.bat`。

```bash
# 克隆后进入**项目根目录**
python -m pc_cleaner --list           # 只扫描，不删任何东西，查看各分类占用
python -m pc_cleaner --health         # 只读体检（16 项，不改任何系统设置）
python -m pc_cleaner --lang en --health   # 英文体检报告（v0.9.4）
python -m pc_cleaner --json-schema    # 输出 JSON 契约的 JSON Schema（机器可读）
python -m pc_cleaner                  # 进入交互式清理菜单
python -m pc_cleaner --checkup        # 一键体检（管理员状态/磁盘/回收站/可清理量）
python -m pc_cleaner --mcp            # 作为 MCP 服务端（只读）供 AI Agent 调用
```

**便携运行不写系统盘**：设置 `PC_CLEANER_HOME` 把配置/历史/审计日志重定向到其它目录：

```powershell
$env:PC_CLEANER_HOME = "D:\your\workspace\.pc_cleaner_runtime"
python -m pc_cleaner --checkup
```

**Windows 双击运行**：双击 `pc_cleaner.bat`（加 `--no-pause` 可让窗口结束时自动关闭）。
启动器会优先使用 `py -3`（避开 Microsoft Store 的 python 存根）并设置 `PYTHONUTF8=1`，
`--no-pause` 只由启动器消费、不会再被转发给 Python。

或安装为命令行工具（`send2trash` 已随包安装，删除默认进回收站）：

```bash
pip install -e .                      # 推荐：自动带上 send2trash（回收站支持）
pip install -e ".[recycle]"           # 兼容旧写法（该 extra 现为空，等价于上面）
pip install -e ".[dev]"               # 追加 pytest，用于跑测试
```

---

## 🖥 命令行用法

```
python -m pc_cleaner [--list|-l] [--detail|-d] [--tree] [--sort 方式]
                     [--max-depth 深度] [--deep|-D] [--export-scan PATH] [--no-progress]
                     [--workers N]
                     [--clean 分类名] [--all] [--exclude 分类名] [--dry-run]
                     [--recycle|--permanent] [--recycle-fallback] [--yes|-y] [--risky]
                     [--shred] [--shred-passes N]
                     [--ext 扩展名] [--min-size-mb MB] [--older-than-days 天数]
                     [--json] [--json-schema] [--lang LANG] [--checkup] [--health]
                     [--mcp] [--mcp-allow-delete] [--history] [--undo-last] [--admin]
                     [--export-config PATH] [--import-config PATH] [--show-config]
                     [--show-rules] [--validate-rules] [--version]
```

| 参数 | 说明 |
| --- | --- |
| `--list` / `-l` | 仅扫描，列出各分类占用、磁盘可用、回收站占用，不删除 |
| `--detail` / `-d` | 详细展示每个分类下的所有目标目录/文件（不截断） |
| `--tree` | 以树形视图展示扫描结果 |
| `--sort 方式` | `size_desc`(默认) / `size_asc` / `name_asc` / `name_desc` / `count_desc` |
| `--max-depth 深度` | `find_dirs` 遍历深度限制（默认 20） |
| `--deep` / `-D` | 深度扫描：更大遍历深度（50）+ 启用 `deep_only` 高级规则 |
| `--workers N` | 并行扫描线程数（1=串行，0=按 CPU 自动，默认取配置 `scan_workers`） |
| `--export-scan PATH` | 将扫描结果导出为 JSON 文件 |
| `--no-progress` | 不显示扫描进度条 |
| `--clean 分类` | 直接清理指定分类（如 `database_compact,system_temp`，逗号分隔）；**分类名拼错 → 退出码 1** |
| `--all` | 选中所有**非高风险**分类（**默认不含回收站**，见配置 `all_includes_recycle_bin`） |
| `--exclude 分类` | 与 `--all`/`--clean` 联用：排除指定分类；**拼错 → 退出码 1**（此前静默忽略） |
| `--dry-run` | 只预览，不真正删除 |
| `--recycle` | 进回收站（默认方式） |
| `--permanent` | 永久删除（属于危险操作，需二次确认 / `--risky` 授权） |
| `--recycle-fallback` | 进回收站失败时回退为永久删除（默认保留原文件并计入失败） |
| `--yes` / `-y` | 跳过交互确认（谨慎使用）；危险操作仍需 `--risky` |
| `--risky` | 显示高风险分类，并作为危险操作的**显式授权** |
| `--shred` | 永久删除前随机覆写文件内容（隐私增强） |
| `--shred-passes N` | shred 覆写遍数（默认 1，上限 7，需配合 `--shred`） |
| `--ext 扩展名` | 仅清理匹配扩展名的文件（如 `.log,.tmp,.bak`） |
| `--min-size-mb MB` | 全局最小体积过滤 |
| `--older-than-days 天数` | 全局最旧修改时间过滤 |
| `--json` | 以 JSON 输出（带 `schema_version`/`ok`/`status`/`exit_code`）；默认只扫描，删除需 `--yes`，危险操作还需 `--risky` |
| `--json-schema` | 输出 `--json` 信封的 **JSON Schema**（机器可读契约，`docs/json-contract.md`） |
| `--lang LANG` | 界面/报告语言：`zh_CN`（默认）/ `en`；也可用环境变量 `PC_CLEANER_LANG` 或配置 `language` |
| `--checkup` | 一键体检：管理员状态/磁盘/回收站/可清理量/运行环境适配/组件库体积 |
| `--health` | **只读体检报告（16 项）**，零依赖、绝不修改系统；有 `error` 项时退出码 1，可配 `--json`、`--lang` |
| `--mcp` | 以 **MCP 服务端**（stdio JSON-RPC 2.0）运行，**默认只读**：`scan`/`health`/`history`/`preview_delete` |
| `--mcp-allow-delete` | 配合 `--mcp`：额外暴露 `delete`/`undo`（仍需两阶段 `confirm_token` + 危险操作 `acknowledge_danger`） |
| `--history` | 显示清理历史 |
| `--undo-last` | 把最近一次「进回收站」的清理从回收站恢复回来 |
| `--admin` | 以管理员身份重新启动（UAC 提权） |
| `--export-config` / `--import-config` | 配置导入/导出 |
| `--show-config` | 显示当前配置 |
| `--show-rules` | 展示 `rules.json` 内置规则（配合 `--deep` 显示深度规则） |
| `--validate-rules` | 校验 `rules.json`：格式错误 → 退出码 1；另打印警告（死规则/冗余规则/风险错配），警告不影响退出码 |
| `--version` | 显示版本 |

**示例：**

```bash
python -m pc_cleaner --list                                        # 查看能释放多少空间
python -m pc_cleaner --health                                      # 只读体检（16 项）
python -m pc_cleaner --health --json                               # 体检的 JSON 信封（可编程）
python -m pc_cleaner --checkup                                     # 一键体检
python -m pc_cleaner --clean gpu_caches,wechat_cache --recycle     # 清理指定分类，进回收站
python -m pc_cleaner --all --exclude downloads --recycle           # 清理所有非高风险分类（不含回收站）
python -m pc_cleaner --clean recycle_bin                           # 单独清空回收站（会二次确认）
python -m pc_cleaner --clean database_compact                      # 只压缩浏览器数据库（不删数据）
python -m pc_cleaner --clean downloads --recycle --risky           # 高风险分类需显式开启
python -m pc_cleaner --json --clean downloads --yes --risky        # 自动化：显式授权后才执行
python -m pc_cleaner --history                                     # 查看历史
python -m pc_cleaner --undo-last                                   # 从回收站恢复最近一次
python -m pc_cleaner --clean system_logs --recycle --admin         # 系统日志与诊断残留（UAC 提权）
python -m pc_cleaner --clean system_admin --recycle --admin        # 管理员深度清理（UAC 提权）
python -m pc_cleaner --validate-rules --show-rules                 # 校验/查看内置规则
python -m pc_cleaner --list --workers 8                            # 并行扫描（大目录更快）
python -m pc_cleaner --lang en --health                            # 英文体检报告
python -m pc_cleaner --json-schema                                 # 输出 JSON 契约 schema
python -m pc_cleaner --mcp                                         # MCP 只读服务端（stdio）
python -m pc_cleaner --mcp --mcp-allow-delete                      # MCP + 两阶段删除能力
```

---

## 🔢 退出码与 JSON 契约（v0.9.4，面向自动化）

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 参数 / 配置 / 分类名错误（如 `--clean no_such_key`、`--exclude download`） |
| `2` | argparse 用法错误（命令行拼写错误） |
| `3` | 执行过程中有目标失败（被占用 / 无权限 / 清空回收站失败） |
| `4` | 需要确认或已取消 —— **什么都没删**（`needs_confirmation` / `cancelled`） |
| `130` | 用户中断（Ctrl+C） |

`--json` 顶层固定包含契约字段：

```json
{
  "schema_version": 1,
  "ok": true,
  "status": "scan",
  "exit_code": 0,
  "categories": [ ... ],
  "action": { ... }
}
```

- `status` 取值（12 个）：`scan`（仅扫描）/ `dry_run` / `deleted` / `partial`（有失败）/
  `needs_confirmation` / `cancelled` / `interrupted` / `ok`（`--health --json` 正常）/
  `error`（分类名非法等）；**v0.9.4 新增** `preview`（MCP 预览）/
  `needs_repreview`（MCP 清单漂移）/ `restored`（MCP 恢复完成）。
- `action` 字段：`deleted`、`failed`、**`skipped`（整数计数 = 部分清理的目标数）**、
  **`skipped_in_use`**、`freed_bytes`、**`recycled_bytes`**（进回收站的字节数，清空回收站后
  才真正释放）、`selected`，以及清空回收站时的 `recycle_bin` 子结果。
  ⚠️ **语义变更（v0.9.4）**：「什么都没执行」改用 **`action.not_executed: true`**
  （此前文档写的是 `action.skipped = true`）。
- `--health --json` 信封：`{schema_version, ok, status, exit_code, health:{schema_version,
  generated, items[16], counts}}`；体检项的 `advice` 是**数据**（可能含中文建议），
  不属于「混入 stdout 的旁白」。
- 机器可读契约：`--json-schema` 输出信封的 JSON Schema（`service.ENVELOPE_SCHEMA`）；
  完整字段表见 [docs/json-contract.md](docs/json-contract.md)。

**典型自动化流程**：`--json --dry-run` 预览 → 人工/策略确认 → `--json --clean <分类> --yes`
（危险操作再加 `--risky`），据 `exit_code` 与 `status` 分支。

---

## 🤖 MCP 服务端（v0.9.4）

让 AI Agent（Claude Desktop / DSH / 自研 Agent）以**受控的两阶段流程**使用 PC-Cleaner：
新增 [`pc_cleaner/mcp.py`](pc_cleaner/mcp.py)（零依赖 stdio JSON-RPC 2.0，stdout 只承载
协议、日志走 stderr），协议版本支持 `2024-11-05` / `2025-03-26` / `2025-06-18`
（回显客户端请求的版本）。

```bash
python -m pc_cleaner --mcp                       # 只读：scan / health / history / preview_delete
python -m pc_cleaner --mcp --mcp-allow-delete    # 额外暴露 delete / undo（仍需两阶段确认）
```

| 机制 | 说明 |
| --- | --- |
| **默认只读** | 未加 `--mcp-allow-delete` 时 `tools/list` 里**没有** `delete`/`undo`，直接调用也会被拒绝 |
| **两阶段确认** | `preview_delete` 只返回清单 + `confirm_token`（**600 秒**有效、一次性、绑定清单指纹）；执行必须再调 `delete(confirm_token)` |
| **漂移检测（防 TOCTOU）** | `delete` 会重新扫描并比对：清单里**消失**的目标容忍；出现**未预览过的新目标**且体积 > `max(1MB, 5%×清单总量)` → `status="needs_repreview"` 拒绝执行；已有目标体积变化容忍；**永远只删预览过的路径** |
| **危险操作二次授权** | 清单含高风险分类 / 永久删除 / 清空回收站时，`delete` 必须带 `acknowledge_danger=true` |
| **恢复** | `undo` 仅对 `recycle` 会话生效，可按 `session_id` 定位（`history.json` v0.9.4 起带该字段） |

工具返回同时包含 `content[0].text`（JSON 字符串）与 `structuredContent`（对象）。
客户端配置示例、完整工具参数表与一次典型交互见 **[docs/mcp.md](docs/mcp.md)**。

> 建议先用只读模式（不加 `--mcp-allow-delete`）观察 Agent 行为，再按需放开写能力。

---

## 🖥 运行环境自动适配

工具启动时用只读的 [`pc_cleaner/env.py`](pc_cleaner/env.py) **自动探测**这台机器实际安装了
哪些东西（纯只读、零副作用、逐项容错）：

- **浏览器**：Edge / Chrome / Firefox / Brave / Vivaldi / Opera（按用户数据目录判断）；
- **GPU 厂商**：NVIDIA / AMD（按 `%LOCALAPPDATA%` 目录判断，对应着色器缓存可清）；
- **微信布局**：4.x（Weixin，roaming `Tencent\xwechat`）还是 3.x，**可清理的运行缓存清单**
  （`xplugin/plugins`、`radium`、`net*`、日志、升级包的实际路径与体积），以及**微信数据目录**
  （如 `D:\WeixinShuju` —— 只提示"请在微信内清理"，**绝不**触碰聊天数据）；
- **Steam**：安装痕迹与已存在的 steamapps 库目录；
- **pnpm store**：实际位置（含盘符根目录 `.pnpm-store`）；
- **开发工具**：PATH 上可用的 node / npm / pip / go / java / dotnet / git 等；
- **组件库/系统级空间**：WinSxS、DriverStore、`Windows\Installer`、卷影副本的体积与
  **官方清理命令**（DISM / pnputil / vssadmin）——本工具不删除这些内容。

探测结果用于：交互菜单顶部「本机适配」一行，以及 `--checkup` 的「运行环境适配」小节。
**换一台电脑运行时工具会自动重新探测**，未安装软件的缓存分类显示为空，避免"为啥这项是 0"
的疑惑。Windows 之外平台可运行，但分类路径以 Windows 为目标；需管理员的分类与回收站恢复
仅 Windows 有效。

> 🩺 `--health` 与 `--checkup` 的区别：`--checkup` 面向**清理前的准备**（管理员状态、磁盘
> 可清理量、回收站、组件库体积）；`--health` 面向**系统健康排查**（更新/安全启动/设备/磁盘
> 健康等 16 项），两者都是只读的。

---

## ⚙️ 配置文件

程序会在用户目录生成 `pc_cleaner/config.json`（可用 `PC_CLEANER_HOME` 重定向）：

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `recycle_by_default` | 默认是否进回收站 | `true` |
| `recycle_error_fallback` | 进回收站失败是否回退永久删除（false 更安全） | `false` |
| `protected_paths` | 额外加入的黑名单路径 | `[]` |
| `custom_rules` | 自定义清理规则（路径、扩展名、阈值） | `[]` |
| `dev_artifact_bases` | 额外扫描「散落构建产物」的目录 | `[]` |
| `enabled_categories` | 非空时只扫描这些分类（其余隐藏） | `[]` |
| `preview_lines` | 每个分类预览最多展示的行数 | `12` |
| `show_risky` | 交互菜单是否显示高风险分类 | `false` |
| `enable_history` | 是否记录清理历史与审计日志 | `true` |
| `scan_depth` | `find_dirs` 遍历深度限制 | `20` |
| `scan_workers` | 并行扫描线程数（1=串行，0=按 CPU 自动，上限 16） | `4` |
| `all_includes_recycle_bin` | `--all` 是否连带清空回收站（默认否，更安全） | `false` |
| `default_detail` | 默认是否以详细模式显示 | `false` |
| `default_sort` | 默认排序方式 | `size_desc` |
| `show_scan_progress` | 扫描时是否显示进度 | `true` |
| `compact_tree_view` | 是否默认使用树形视图 | `false` |
| `language` | 界面/报告语言：`zh_CN` / `en`（空串 = 按 `PC_CLEANER_LANG` 或默认 `zh_CN`） | `""` |

首次运行后 `--show-config` 查看；多台机器同步用 `--export-config` / `--import-config`。

**自定义规则**支持的目标类型：`clear_dir`、`delete_dir`、`glob_dirs`、`glob_files`、
`files_by_rule`、`find_dirs`、`compact_db`、`empty_dirs`、`zero_byte_files`；
可选字段：`pattern`（`glob_dirs`/`glob_files` 现为**必填**）、`ext`（扩展名过滤）、
`min_size_mb`、`older_than_days`、`min_age_days`（仅 `empty_dirs`）、
`max_depth`（`find_dirs` 现为**必填**）、`action`（`clear`/`delete`，大小写不敏感）、
`deep_only`、**`skip_if_in_use`**（目标被运行中进程占用时跳过）、`label`。
改完用 `--validate-rules` 校验（格式错误退出码 1；警告不影响退出码）。

---

## 🗃 项目结构

```
pc-cleaner/
├── pc_cleaner/
│   ├── __init__.py      # 版本信息（0.9.4）
│   ├── __main__.py      # python -m pc_cleaner 入口（含误用提示）
│   ├── cli.py           # 命令行入口：参数解析、main 编排、--json/--json-schema/--health/--mcp 分发
│   ├── service.py       # 退出码契约 / JSON 信封 / ENVELOPE_SCHEMA / 危险操作闸门判定
│   ├── mcp.py           # MCP 服务端（stdio JSON-RPC 2.0；默认只读 + 两阶段确认 + 漂移检测）
│   ├── i18n.py          # 国际化（扁平词表、零依赖、永不抛出；缺失 key 回退 zh_CN）
│   ├── locales/         # zh_CN.json / en.json（各 167 个 key，当前覆盖 --health 报告）
│   ├── menu.py          # 交互式菜单、预览/汇总展示、清理执行流程（含非交互拒绝与 Ctrl+C 落盘）
│   ├── commands.py      # 管理子命令：history / undo / checkup / 配置 / 规则校验
│   ├── health.py        # --health 只读体检（16 项；零依赖、只读查询、绝不修改系统、双语）
│   ├── proc.py          # 进程占用检测（psutil 可选，无则 PowerShell 回退；60 秒缓存）
│   ├── ui.py            # 共享 UI 工具：输出、确认、扫描进度
│   ├── config.py        # 配置读写（支持 PC_CLEANER_HOME 重定向、并行线程数、language）
│   ├── env.py           # 运行环境探测（只读：浏览器/GPU/微信/Steam/pnpm/组件库；硬链接去重）
│   ├── console.py       # ANSI 颜色 / CJK 对齐 / UTF-8 输出（零依赖）
│   ├── engine.py        # 删除引擎（回收站/永久、二次防御、shred、白名单例外、skip_if_in_use、回收站恢复、数据库压缩）
│   ├── history.py       # 清理历史 / 审计日志（原子写 + history.lock + 损坏改名保留；会话带 session_id）
│   ├── models.py        # 数据结构（Target 含 skip_if_in_use / CategoryResult、CleanMode、TargetAction 含 COMPACT）
│   ├── rules.py         # 分类规则加载/黑名单/白名单（含 ALLOWED_CLEAR_UNDER_PROTECTED）/风险分级/规则校验+警告
│   ├── rules.json       # 内置清理规则（单一数据源，27 分类 / 272 目标，含 deep_only 与 manual_notes）
│   └── scanner.py       # 安全扫描与体积计算（路径规范化、链接拒绝、嵌套去重、受保护剪枝、并行、大小缓存）
├── docs/                # mcp.md（MCP 用法与客户端配置）/ json-contract.md（JSON 字段表）/ donate.jpg
├── tests/               # 单元测试（114 个用例：v092 39 + v093 20 + v094_contract 34 + v094_mcp 21）
├── .github/workflows/ci.yml  # GitHub Actions 矩阵 CI（Windows+Linux × Python 3.10/3.12/3.13）
├── pyproject.toml
├── README.md
├── pc_cleaner.bat       # 一键启动（py -3 优先 / PYTHONUTF8=1 / --no-pause 本地消费）
├── CHANGELOG.md
└── LICENSE
```

---

## 🧪 测试

```bash
pip install -e ".[dev]"
pytest
```

当前测试套件：**114 passed**（`test_v092.py` 39 + `test_v093.py` 20 + `test_v094_contract.py` 34
+ `test_v094_mcp.py` 21）。CI 在 **Windows + Linux × Python 3.10/3.12/3.13** 矩阵上跑同一套
（Linux 上 Windows 专属用例由 `skipif` 跳过，属预期行为）。

- **`test_v092.py`（39）**：核心扫描、保护路径匹配、shred、白名单清空例外、嵌套目标去重、
  新目标类型（空目录 / 0 字节文件）、受保护子树剪枝、规则校验、`compact_db` 数据库压缩、
  回收站 `$I` 解析与恢复（父目录 / 子项两种情形）、引擎实际释放量核算与部分失败上报、
  `--clean recycle_bin` 流程、交互菜单渲染与选择解析等。
- **`test_v093.py`（20）**：路径规范化（折叠 `..` / 去结尾点与空格 / 去 `\\?\` 前缀）与
  保护判定一致性、CLEAR 目标为 junction 时拒绝（`_clear_dir_content` 与扫描器两侧）、
  `_dir_size` 不跟随根链接、`glob_dirs` 必须给 `pattern`、`action` 大小写与非法值、
  `skip_if_in_use` 从规则到 `Target` 的链路与引擎跳过、回收站只计 `recycled`、
  危险操作判定与退出码映射、`--json` 拒绝未授权高风险分类、`--json` 未知分类退出码 1、
  `history.json` 原子写与损坏改名、`compact_db` 空闲页估算、白名单不短路名称级保护。
- **`test_v094_contract.py`（34）**：用真实子进程验证 JSON 契约——stdout 必须是**单个合法
  JSON 对象**（无 ANSI、无旁白文本；`health.items[*].advice` 视为数据）、信封必填字段与
  `ok == (exit_code == 0)`、`status` 落在文档枚举内、退出码映射（0/1/4）、危险闸门与
  `--dry-run` 均不落地删除、`--health` 恰好 16 项且结构完整、stdin 关闭时仍能返回、
  `recycle_bin_size_bytes` 随平台出现、空分类名不崩溃。
- **`test_v094_mcp.py`（21）**：MCP 协议层（initialize / tools/list / 未知方法）、默认只读时
  `delete`/`undo` 不在工具表且调用被拒、两阶段确认（token 缺失/过期/一次性）、漂移检测
  （新增目标超阈值 → `needs_repreview`，消失目标容忍）、危险清单需 `acknowledge_danger`、
  `undo` 走回收站恢复、以及 **stdout 只含 JSON-RPC 帧**（日志不得污染协议流）。

---

## 💖 支持作者

如果这个工具帮你在清理电脑垃圾时省下了时间或磁盘空间，欢迎扫码赞赏，支持持续开发维护：

<p align="center">
  <img src="docs/donate.jpg" alt="赞赏码" width="220" />
</p>

> 图片位于仓库 `docs/donate.jpg`。

---

## 📄 License

[MIT](LICENSE)
