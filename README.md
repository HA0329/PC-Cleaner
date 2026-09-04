# 🧹 PC Junk Cleaner

> 为**个人电脑**定制的安全垃圾清理工具（Windows 优先）。
> 先预览后确认 · 删除可进回收站 · 二次防御防误删· 风险分级 · 可撤销 · 审计日志。

一个以**安全为第一优先级**的垃圾清理程序：它不会直接删东西，而是先扫描、告诉你每个分类
能释放多少空间、列出将删除的文件，**经你确认后**才动手。默认删除到回收站（可恢复），内置
受保护路径黑名单与删除前二次防御。

**v0.9.0 起吸收 BleachBit + Dism++ 的清理能力**：浏览器站点数据（DOM/本地存储、会话、
登录密码、表单历史、站点偏好、同步数据）、浏览器数据库压缩（SQLite `VACUUM`，不删数据）、
以及 Dism++「空间回收」的 Windows 事件日志 / 崩溃内存转储 / .NET 原生映像缓存等。

---

## ✨ 功能特性

- 🗂 **分类清理**：23 个清理分类、203 条内置规则（含 `--deep` 深度规则 30 条），覆盖系统临时
  文件、GPU 着色器缓存、浏览器/网页缓存、微信 4.x 运行缓存、游戏平台缓存、开发工具缓存、
  下载旧文件、回收站，以及**管理员深度清理**（Windows 更新缓存 / Prefetch / 事件日志 /
  崩溃转储 / .NET 原生映像等）。
- 🔍 **先预览后确认**：扫描 → 展示体积与文件列表（按体积从大到小）→ 人工确认 → 执行。
- 🚦 **风险分级**：🟢 安全 / 🟡 一般 / 🔴 高风险彩色徽标；高风险分类默认隐藏（`--risky`
  或配置 `show_risky` 开启），`--all` 也不会选中它们。
- ♻️ **可回收**：优先删除到回收站（依赖 `send2trash`），未安装时降级为永久删除并提示。
- ↩️ **可撤销**：`--undo-last` 把最近一次「进回收站」的清理从回收站恢复回来。
- 📋 **审计与历史**：每次清理记录 `history.json` + `audit.log`，`--history` 可查。
- 🛡 **智能安全**：受保护路径黑名单 + 白名单清空例外、删除前**二次防御**、清空时逐项跳过
  受保护子项、跳过符号链接与 junction、逐项捕获权限错误；**进回收站失败时默认保留原文件**，
  绝不静默转永久删除。
- 🧩 **数据库压缩（v0.9.0）**：`compact_db` 目标类型用 SQLite `VACUUM` 对浏览器
  `History` / `Web Data` / `Login Data` / `Cookies` / `places.sqlite` 等库重写以释放碎片，
  **不删除任何数据**，浏览器下次运行自动重建；库被占用/只读时安全跳过。
- 🧭 **本机适配**：启动时只读探测这台机器（浏览器 / GPU / 微信布局 / Steam / pnpm store /
  开发工具），未安装软件的缓存分类自动显示为空。
- 🎛 **交互式菜单**：汇总表即菜单（只给有内容的分类编号）、支持区间选择（`1,3-5`）、
  `r` 连回收站一起清、`d` 详情 / `t` 树形 / `s` 排序 / `x` 切换高风险显示 / `q` 退出。
- ⚙️ **可配置**：`config.json` 可保存回收站偏好、失败回退策略、额外保护路径、自定义规则、
  只扫描指定分类、预览行数、是否显示高风险、是否记录历史等。

---

## 🗂 内置清理分类（23 个）

| 分类 key | 名称 | 风险 | 清理内容 |
| --- | --- | --- | --- |
| `system_temp` | 系统临时文件 | 🟢 | 用户/系统 Temp、缩略图/图标缓存、WebCache、错误报告、最近文档/跳转列表、崩溃转储 |
| `gpu_caches` | GPU 着色器缓存 | 🟢 | NVIDIA/AMD DXCache+GLCache、DirectX D3DSCache、NV_Cache（可安全重建） |
| `web_cache` | 浏览器/网页缓存 | 🟢 | Edge/Chrome/Firefox/Brave/Vivaldi/Opera 缓存与 GPU 缓存、Steam htmlcache、INetCache |
| `wechat_cache` | 微信运行缓存 | 🟢 | 微信 4.x：`%APPDATA%\Tencent\xwechat\net`/`net_1` 网络缓存（聊天数据目录绝对不动） |
| `office_caches` | 办公软件缓存 | 🟢 | Office 文档同步缓存（OneDrive/SharePoint）、WPS 缓存 |
| `media_caches` | 多媒体设计软件缓存 | 🟢 | Adobe Premiere Pro / After Effects 媒体缓存 |
| `comm_caches` | 通信工具缓存 | 🟢 | Zoom、Discord、Telegram 缓存 |
| `game_caches` | 游戏平台缓存 | 🟡 | 完美世界更新包、Steam appcache/logs/着色器缓存、Epic/Battle.net/GOG/Riot |
| `game_runtime_cache` | 游戏运行时缓存 | 🟡 | 无畏契约、三角洲行动、Unreal Engine、CS:GO 缓存/日志 |
| `dev_caches` | 开发工具缓存 | 🟡 | pnpm/pip/npm/uv/yarn/Go/cargo/NuGet/Gradle/WinGet 缓存、`__pycache__`、Electron/Docker/VSCode/JetBrains/VS |
| `downloads` | 下载/旧文件 | 🔴 | Downloads 中的大文件/久未使用文件/安装包（默认隐藏） |
| `recycle_bin` | 回收站 | 🟡 | 清空回收站（单独确认，用 `r` 或 `--clean recycle_bin`） |
| `system_admin` | 系统深度清理 | 🟡 | Windows 更新缓存、Prefetch、系统 Temp、chkdsk 残留(found.*)、更新日志(KB*.log)、备份(*.bak)、事件日志、崩溃转储、.NET 原生映像缓存（需管理员） |
| `windows_old` | 旧版 Windows 残留 | 🔴 | `C:\Windows.old`（默认隐藏，需管理员，不可恢复） |
| `dev_purge` | 项目构建产物 | 🔴 | 散落 node_modules / dist / build / target / .next 等（默认隐藏） |
| `browser_privacy` | 浏览器隐私数据 | 🔴 | Edge/Chrome/Firefox 的 Cookie 与浏览历史（默认隐藏，会退出登录） |
| `browser_data` | 浏览器站点数据 | 🔴 | DOM/本地存储、会话、表单历史、登录密码、站点偏好/权限、同步数据（默认隐藏，会退出登录） |
| `database_compact` | 浏览器数据库压缩 | 🟢 | 对浏览器 SQLite 库执行 `VACUUM` 释放碎片（**不删数据**，安全） |
| `hidden_installer_backups` | 隐蔽的安装包/升级残留 | 🟡 | `$Windows.~BT`/`~WS`、MSI Package Cache、WinSxS 临时目录（需管理员） |
| `recycle_and_diagnostics` | 回收站与诊断日志(ETL) | 🟡 | `$Recycle.Bin`、ETL 诊断日志、WinSAT（需管理员） |
| `cloud_app_hidden` | 云盘与商店应用缓存 | 🟢 | OneDrive 缓存、Windows Store 应用临时文件 |
| `java_rdp_legacy` | Java/远程桌面/字体缓存 | 🟢 | Java 部署缓存、远程桌面位图缓存、系统字体缓存 |
| `crash_telemetry` | 崩溃上报与遥测数据 | 🟢 | WER 错误报告归档/队列、遥测存储 |
| `extreme_stealth` | 变态级隐蔽缓存(系统账户) | 🟡 | SYSTEM 账户缓存、CBS 历史日志、大体积事件日志、Steam/EpiC 下载残留、音视频客户端缓存（需管理员） |

> 🟢 安全（随便清）· 🟡 一般（清理后按需重建）· 🔴 高风险（默认隐藏，需 `--risky`）。
> 各分类默认启用合理的体积/时间阈值与黑名单，避免误删正在使用的文件。

> ⚠️ **非文件 / 系统级项**（BleachBit / Dism++ 中属于注册表或系统工具范畴的清理，如
> MUICache、快速运行/最近查询/Shellbags 历史、系统还原点、被取代的 WinSxS 组件、释放磁盘
> 空闲区域、剪贴板等）本工具**不删除、不伪造路径**，已在 `pc_cleaner/rules.json` 顶部的
> `manual_notes` 中说明，建议用系统自带工具或注册表处理。

---

## 🔒 安全模型（重要）

1. **不未经确认就删除**：所有删除都需要人工确认（`--yes` 除外，谨慎使用）。
2. **进入回收站优先**：默认尝试删除到回收站，可随时还原；进回收站失败时**默认保留原文件**
   （可配置 `recycle_error_fallback` 打开回退永久删除）。
3. **风险分级**：高风险分类（下载文件、构建产物、浏览器隐私/站点数据、Windows.old）
   **默认隐藏**，`--all` 不会选中它们；`--clean` 显式指定时给出警告。
4. **黑名单保护**：绝不触碰 `C:\Windows\System32`、`WinSxS`、`$Recycle.Bin`、
   `System Volume Information`、**微信聊天数据**（`WeixinShuju`/`xwechat_files`）、
   `.git`/`.venv` 等，并支持自定义。匹配采用**组件级全等**：相对模式（如 `windows\system32`、
   `weixinshuju`）命中任意一级路径即受保护，不会像子串那样误伤 `windows.old`。
5. **白名单清空例外**：`%WINDIR%\SoftwareDistribution\Download`、`%WINDIR%\Prefetch`、
   `%WINDIR%\Temp` 等位于受保护前缀之下的**明确可重建缓存**，仅允许「清空内容」，
   删除目录本身仍被拒绝；白名单按 `%WINDIR%` 动态解析，与系统盘符解耦。
6. **删除前二次防御**：engine 层再次检查每个目标——磁盘根路径、系统关键文件
   （`pagefile.sys` 等）与受保护路径一律拒绝，即使扫描器漏判也删不掉。
7. **跳过危险结构**：不删除未知/系统目录，不跟随符号链接与 junction。
8. **逐项容错**：单个文件被占用/无权限时跳过并继续，不中断整个任务。
9. **审计留痕**：每次清理写入 `history.json`（结构化）与 `audit.log`（人类可读）。

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
python -m pc_cleaner                  # 进入交互式清理菜单
python -m pc_cleaner --checkup        # 一键体检（管理员状态/磁盘/回收站/可清理量）
```

**便携运行不写系统盘**：设置 `PC_CLEANER_HOME` 把配置/历史/审计日志重定向到其它目录：

```powershell
$env:PC_CLEANER_HOME = "D:\your\workspace\.pc_cleaner_runtime"
python -m pc_cleaner --checkup
```

**Windows 双击运行**：双击 `pc_cleaner.bat`（加 `--no-pause` 可让窗口结束时自动关闭）。

或安装为命令行工具（推荐装 `send2trash`，让删除进回收站）：

```bash
pip install -e .                      # 基础版（标准库）
pip install -e ".[recycle]"           # 推荐：支持删除到回收站
pip install send2trash
```

---

## 🖥 命令行用法

```
python -m pc_cleaner [--list|-l] [--detail|-d] [--tree] [--sort 方式]
                     [--max-depth 深度] [--deep|-D] [--export-scan PATH] [--no-progress]
                     [--clean 分类名] [--all] [--exclude 分类名] [--dry-run]
                     [--recycle|--permanent] [--recycle-fallback] [--yes|-y] [--risky]
                     [--shred] [--shred-passes N]
                     [--ext 扩展名] [--min-size-mb MB] [--older-than-days 天数]
                     [--json] [--checkup] [--history] [--undo-last] [--admin]
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
| `--export-scan PATH` | 将扫描结果导出为 JSON 文件 |
| `--no-progress` | 不显示扫描进度条 |
| `--clean 分类` | 直接清理指定分类（如 `database_compact,system_temp`，逗号分隔） |
| `--all` | 选中所有**非高风险**分类（含回收站）；高风险需另加 `--risky` |
| `--exclude 分类` | 与 `--all`/`--clean` 联用：排除指定分类 |
| `--dry-run` | 只预览，不真正删除 |
| `--recycle` | 进回收站（需安装 send2trash） |
| `--permanent` | 永久删除（会额外确认） |
| `--recycle-fallback` | 进回收站失败时回退为永久删除（默认保留原文件并计入失败） |
| `--yes` / `-y` | 跳过交互确认（谨慎使用） |
| `--risky` | 显示/允许高风险分类 |
| `--shred` | 永久删除前随机覆写文件内容（隐私增强） |
| `--shred-passes N` | shred 覆写遍数（默认 1，上限 7，需配合 `--shred`） |
| `--ext 扩展名` | 仅清理匹配扩展名的文件（如 `.log,.tmp,.bak`） |
| `--min-size-mb MB` | 全局最小体积过滤 |
| `--older-than-days 天数` | 全局最旧修改时间过滤 |
| `--json` | 以 JSON 输出；默认只扫描，删除需配合 `--yes` |
| `--checkup` | 一键体检：管理员状态/磁盘/回收站/可清理量/运行环境适配 |
| `--history` | 显示清理历史 |
| `--undo-last` | 把最近一次「进回收站」的清理从回收站恢复回来 |
| `--admin` | 以管理员身份重新启动（UAC 提权） |
| `--export-config` / `--import-config` | 配置导入/导出 |
| `--show-config` | 显示当前配置 |
| `--show-rules` | 展示 `rules.json` 内置规则（配合 `--deep` 显示深度规则） |
| `--validate-rules` | 校验 `rules.json` 规则格式 |
| `--version` | 显示版本 |

**示例：**

```bash
python -m pc_cleaner --list                                        # 查看能释放多少空间
python -m pc_cleaner --checkup                                     # 一键体检
python -m pc_cleaner --clean gpu_caches,wechat_cache --recycle     # 清理指定分类，进回收站
python -m pc_cleaner --all --exclude downloads,recycle_bin --recycle
python -m pc_cleaner --clean database_compact                      # 只压缩浏览器数据库（不删数据）
python -m pc_cleaner --clean downloads --recycle --risky           # 高风险分类需显式开启
python -m pc_cleaner --history                                     # 查看历史
python -m pc_cleaner --undo-last                                   # 从回收站恢复最近一次
python -m pc_cleaner --clean system_admin --recycle --admin        # 管理员深度清理（UAC 提权）
python -m pc_cleaner --validate-rules --show-rules                 # 校验/查看内置规则
```

---

## 🖥 运行环境自动适配

工具启动时用只读的 [`pc_cleaner/env.py`](pc_cleaner/env.py) **自动探测**这台机器实际安装了
哪些东西（纯只读、零副作用、逐项容错）：

- **浏览器**：Edge / Chrome / Firefox / Brave / Vivaldi / Opera（按用户数据目录判断）；
- **GPU 厂商**：NVIDIA / AMD（按 `%LOCALAPPDATA%` 目录判断，对应着色器缓存可清）；
- **微信布局**：4.x（Weixin，roaming `Tencent\xwechat`）还是 3.x，以及**微信数据目录**
  （如 `D:\WeixinShuju` —— 只提示"请在微信内清理"，**绝不**触碰聊天数据）；
- **Steam**：安装痕迹与已存在的 steamapps 库目录；
- **pnpm store**：实际位置（含盘符根目录 `.pnpm-store`）；
- **开发工具**：PATH 上可用的 node / npm / pip / go / java / dotnet / git 等。

探测结果用于：交互菜单顶部「本机适配」一行，以及 `--checkup` 的「运行环境适配」小节。
**换一台电脑运行时工具会自动重新探测**，未安装软件的缓存分类显示为空，避免"为啥这项是 0"
的疑惑。Windows 之外平台可运行，但分类路径以 Windows 为目标；需管理员的分类与回收站恢复
仅 Windows 有效。

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
| `default_detail` | 默认是否以详细模式显示 | `false` |
| `default_sort` | 默认排序方式 | `size_desc` |
| `show_scan_progress` | 扫描时是否显示进度 | `true` |
| `compact_tree_view` | 是否默认使用树形视图 | `false` |

首次运行后 `--show-config` 查看；多台机器同步用 `--export-config` / `--import-config`。

---

## 🗃 项目结构

```
pc-cleaner/
├── pc_cleaner/
│   ├── __init__.py      # 版本信息
│   ├── __main__.py      # python -m pc_cleaner 入口（含误用提示）
│   ├── cli.py           # 命令行入口：参数解析、main 编排、--json 输出
│   ├── menu.py          # 交互式菜单、预览/汇总展示、清理执行流程
│   ├── commands.py      # 管理子命令：history / undo / checkup / 配置 / 规则 / 提权
│   ├── ui.py            # 共享 UI 工具：输出、确认、扫描进度
│   ├── config.py        # 配置读写（支持 PC_CLEANER_HOME 重定向）
│   ├── env.py           # 运行环境探测（只读：浏览器/GPU/微信/Steam/pnpm/开发工具）
│   ├── console.py       # ANSI 颜色 / CJK 对齐 / UTF-8 输出（零依赖）
│   ├── engine.py        # 删除引擎（回收站/永久、二次防御、shred、白名单例外、回收站恢复、数据库压缩）
│   ├── history.py       # 清理历史 / 审计日志（--history / --undo-last）
│   ├── models.py        # 数据结构（Target / CategoryResult、CleanMode、TargetAction 含 COMPACT）
│   ├── rules.py         # 分类规则加载/黑名单/白名单/风险分级/规则校验
│   ├── rules.json       # 内置清理规则（单一数据源，23 分类 / 203 目标，含 deep_only 与 manual_notes）
│   └── scanner.py       # 安全扫描与体积计算（管理员感知、大小缓存、保护路径匹配）
├── tests/               # 单元测试（75 个用例）
├── pyproject.toml
├── README.md
├── pc_cleaner.bat       # 一键启动
├── CHANGELOG.md
└── LICENSE
```

---

## 🧪 测试

```bash
pip install -e ".[dev]"
pytest
```

当前测试套件：**75 passed**，覆盖核心扫描、保护路径匹配、shred、白名单清空例外、回收站
`$I` 恢复、规则校验、以及 v0.9.0 的 `compact_db` 数据库压缩。

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
