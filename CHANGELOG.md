# Changelog

## 0.9.0 (2026-09)

### 吸收 BleachBit + Dism++（借鉴两者能力）
- **新增 `browser_data`（高风险，默认隐藏）**：BleachBit 的 Edge/Chrome/Firefox
  站点数据 —— DOM/本地存储与会话存储、会话、表单历史、登录密码、默认搜索引擎、
  站点偏好/权限、同步数据。删除会退出登录并重置站点设置，清理前请先退出浏览器。
- **新增 `database_compact`（安全）**：BleachBit「整理优化数据库」—— 新增
  `compact_db` 目标类型，用 SQLite `VACUUM` 对浏览器 History / Web Data /
  Login Data / Cookies / places.sqlite 等库重写以释放碎片，**不删除数据**；
  引入新引擎函数 `engine.compact_database()`（autocommit 下 VACUUM，库被占用/
  只读时安全跳过，跨分类去重时 COMPACT 目标独立保留，避免被删除类分类抢占）。
- **扩展 `system_admin`**（Dism++「空间回收」补充）：Windows 事件日志
  `%WINDIR%\System32\winevt\Logs\*.evtx`、崩溃内存转储 `%WINDIR%\MEMORY.DMP`、
  `.NET 原生映像缓存 `%WINDIR%\assembly\NativeImages_*`（deep_only，可重建）。
- **扩展 `browser_privacy`**：补充 Edge/Chrome 新版 Cookie 路径 `*/Network/Cookies`
  与 Firefox 历史图标 `favicons.sqlite`。
- **非文件 / 系统级项**（MUICache、Run 历史、Shellbags、系统还原点、被取代的
  WinSxS、释放磁盘空闲区域等）已在 `rules.json` 顶部 `manual_notes` 说明，
  由系统自带工具/注册表处理，本工具不伪造路径。
- 版本号统一为 0.9.0；新增 `tests/test_v09.py`（4 个用例覆盖 compact_db）。

## 0.8.0 (2026-09)

### 本机实测适配（结合实际电脑重新校准）
- **新增 `env.py` 运行环境探测**（只读、零副作用）：一键识别这台机器实际安装了
  哪些浏览器（Edge / Chrome / Firefox / Brave / Vivaldi / Opera）、GPU 厂商、
  微信布局（3.x / 4.x）与**微信数据目录**、Steam 及其 steamapps 库、pnpm store
  实际位置（含盘符根目录 `.pnpm-store`）、PATH 上的开发工具。
- **`--checkup` 新增「运行环境适配」小节**：列出检测到的软件与缓存对应关系；
  微信数据目录（如本机 `D:\WeixinShuju`，13.7GB）会以 ⚠ 提示"含聊天记录，
  请在微信「设置 → 存储空间」内清理"，本工具绝不触碰。
- **交互菜单顶部新增「本机适配」一行**：浏览器 / GPU / 微信 / Steam / pnpm store
  一眼可见；未检测到的浏览器（Chrome/Firefox 等）会注明对应缓存分类将为空。
- **`rules.json` 按本机实测校准**：
  - `wechat_cache`：微信 4.x 改为清理 `%APPDATA%\Tencent\xwechat\net` /
    `net_1`（4.x 实际存在的网络缓存目录），删除原 3.x 时期已不存在的
    xplugin/radium/log/crashinfo 死路径；
  - `gpu_caches`：补充 DirectX 着色器缓存 `%LOCALAPPDATA%\D3DSCache`；
  - 微信分类与 README 的说明同步为"只清 4.x roaming 网络缓存、数据目录不动"。

### 交互更好
- **汇总表即菜单**：只给「有内容」的分类编号（0 项分类从可选项移除，改由页脚
  小结说明），编号与 `d`/`t` 查看命令一致，不再出现"选中 0 项分类"的困惑；
- **回收站可直接选择**：输入 `r`（或 `rb`）即可连同所选分类一起清空回收站，
  修复了菜单上印着 `r. 回收站` 却无法输入的断头热键；
- **支持区间与混合选择**：`1,3-5`、`2-4 r`、`all` 等；排序 `s` 循环补齐
  `name_desc`（体积↓/↑、名称↓/↑、文件数↓），`--sort` 同步支持；
- **预览与删除同源**：清理预览改为展示"经过 `--ext/--min-size-mb/
  --older-than-days` 过滤后真正会删的内容"，不再出现预览与删除不一致。

### 其它
- 版本号统一为 0.8.0；新增 `tests/test_v080_env_menu.py` 覆盖选择解析、区间
  展开、可选项编号与微信数据提示等。

## 0.7.0 (2026-08)

### 安全与可靠性
- **白名单与系统盘解耦**：`ALLOWED_CLEAR_ROOTS` 从硬编码 `C:\Windows\...` 改为
  `%WINDIR%` 环境变量占位符，每次调用动态解析——系统盘不是 `C:` 时白名单清空
  例外也能正确生效（`is_within_clear_root` 兼容直接写绝对路径的旧式条目）。
- **GBK 控制台崩溃修复**：中文 Windows 管道/重定向下打印 `✓ ● 🔍` 等非 GBK 字符
  会抛 `UnicodeEncodeError` 直接崩溃；现在强制 stdout/stderr 为 UTF-8 输出
  （`errors=replace` 兜底），`--validate-rules` 等命令在管道下不再中断。
- **回收站 `$I` 元数据容错**：`_parse_recycle_info` 拒绝仅含空字节/控制字符的
  垃圾数据（此前可能解析出 `'\x00\x00\ufeff'` 之类的假路径）。
- **扫描错误处理细化**：`scan_spec` 区分「预期的 OSError/PermissionError/ValueError」
  （静默跳过）与「未知异常」（记录 `[扫描警告]` 到 stderr，不再静默吞掉，便于调试）。

### 性能
- **目录大小缓存**：同一扫描内共享 `_dir_size` memo（按规范化路径），同一目录被
  多个规则命中（或跨分类重复）时只递归遍历一次；`scan_all` 全局共享、`scan_spec`
  单独调用时自动新建，行为向后兼容。

### 模块拆分（cli.py 瘦身）
- 新增 `ui.py`（共享 UI 工具：输出/确认/进度）、`menu.py`（交互菜单、预览、清理
  执行流程）、`commands.py`（history / undo / checkup / 配置导入导出 / 规则展示
  校验 / 提权重启 等子命令）；`cli.py` 只保留参数解析、`main` 编排与 `--json`
  输出，并向后兼容地再导出 `_parse_selection` / `_apply_target_filters` 等测试用名。

### 新功能
- **交互菜单热重载**：菜单每轮循环重新读取 `rules.json` 与 `config.json`
  （`get_enabled_category_specs`），编辑后无需重启程序，下一次扫描即生效。
- **`--json --dry-run` 详细预览**：`--clean/--all` 配合 `--dry-run`（或未给
  `--yes`）时，`action` 字段返回 `would_delete` 目标预览清单
  （路径/类型/动作/体积），自动化脚本可先预览再决定。

### 其它
- 版本号统一为 0.7.0；新增 `tests/test_fixes.py`（14 个用例覆盖上述修复）。

## 0.6.0 (2026-08)

### 清理规则外置（单一数据源）
- **`rules.json`**：内置清理规则从 `rules.py` 硬编码迁移到随包附带的
  `pc_cleaner/rules.json`，路径统一用环境变量占位符（`%TEMP%` / `%LOCALAPPDATA%` /
  `%APPDATA%` / `%WINDIR%` / `%USERPROFILE%` / `%SYSTEMDRIVE%`、`~`、`<CWD>`），
  不再写死 Python 函数，便于直接编辑、审阅与替换。
- 规则文件缺失/损坏时抛出清晰错误（而非静默"清不出东西"）。
- `pyproject.toml` 新增 `[tool.setuptools.package-data]` 保证 `rules.json` 随包分发。

### 高级清理模式
- **`--deep` / `-D` 深度扫描**：遍历深度提升至 50（可被 `--max-depth` 覆盖），
  并启用 `rules.json` 中标记 `deep_only` 的附加缓存规则（Service Worker 缓存、
  DawnCache、Electron / Discord / Telegram 缓存、Windows 图标/字体缓存等）。
- **`--ext EXT[,EXT...]`**：仅清理匹配扩展名的文件目标（目录目标不受影响），
  例如 `--ext .log,.tmp,.bak`。
- **`--min-size-mb MB`**：全局最小体积过滤，只清理 >= 指定 MB 的目标。
- **`--older-than-days DAYS`**：全局最旧修改时间过滤，只清理 >= 指定天数的文件。
- **`--shred-passes N`**：安全擦除遍数（默认 1，上限 7），配合 `--shred` 使用；
  `_shred_file` 支持多遍随机覆写。

### 规则查看与校验
- **`--show-rules`**：可视化展示 `rules.json` 内置规则（分类、目标类型、路径、阈值、
  风险、deep 标记）；配合 `--deep` 一并显示 deep_only 深度规则。
- **`--validate-rules`**：校验 `rules.json` 格式（重复/缺失 key、非法 risk/type/action、
  缺少必填字段），通过返回 0、有问题返回 1 并逐条列出。
- **`--checkup` 接入 `--deep`**：体检报告头部显示扫描模式（标准/深度）与遍历深度，
  深度模式下体检会包含 deep_only 规则并以更大深度扫描。

### 其它
- 版本号统一为 0.6.0；`get_all_category_specs` 新增 `deep` 参数（默认 False，向后兼容）。

## 0.5.0 (2026-08)

### 核心改进：目录显示增强
- **`--detail` / `-d` 详细展示模式**：完整展示每个分类下的所有目标目录/文件，不再截断；
  交互菜单中可按 `d <编号>` 查看单个分类详情，或按 `d` 查看全部详情。
- **`--tree` 树形视图**：以树形结构展示扫描结果，直观呈现目录层级关系。
- **`--sort` 排序方式**：支持按体积（size_desc/size_asc）、名称（name_asc）、文件数（count_desc）排序。
- **交互式菜单增强**：新增 `d`（详细）、`t`（树形）、`s`（切换排序）命令，
  可在菜单中灵活切换查看方式，不再局限于摘要视图。
- **汇总表格式优化**：交互式菜单的汇总信息改为表格展示，更清晰直观。

### 扫描增强
- **可配置扫描深度**：新增 `--max-depth` 参数和 `scan_depth` 配置项，
  `find_dirs` 遍历深度从固定 12 层提升至默认 20 层，可按需调整。
- **扫描进度提示**：扫描时实时显示进度条和当前分类，可通过 `--no-progress` 关闭。
- **扫描耗时统计**：每个分类记录扫描耗时，方便性能分析。

### 新功能
- **`--export-scan PATH`**：将扫描结果导出为 JSON 文件，便于离线分析或存档。
- **扫描进度显示类 `ScanProgressDisplay`**：自动检测 TTY，非终端环境自动静默。
- **Target 新增 `label` 字段**：每个目标可携带规则标签，详细展示时显示更多信息。

### 控制台增强（console.py）
- 新增 `magenta`、`white`、`bg_red`、`bg_green`、`bg_yellow` 颜色函数。
- 新增 `get_terminal_width()`、`reset_terminal_width()` 终端宽度检测。
- 新增 `truncate_path()` 长路径智能截断（保留首尾）。
- 新增 `progress_bar()` 文本进度条。
- 新增 `separator()`、`box_header()`、`box_footer()` 格式化工具。
- 新增 `format_table_row()` 表格行格式化。

### 配置新增字段
- `scan_depth`：find_dirs 遍历深度限制（默认 20）
- `default_detail`：默认是否以详细模式显示（默认 false）
- `default_sort`：默认排序方式（默认 size_desc）
- `show_scan_progress`：扫描时是否显示进度（默认 true）
- `compact_tree_view`：是否默认使用树形视图（默认 false）

## 0.4.0 (2026-04)

### 新功能（借鉴系统自带的经典 `clean.bat` 垃圾清理脚本）
- **系统 Temp**：新增清理 `C:\Windows\Temp`（此前仅清用户 Temp）；通过
  `ALLOWED_CLEAR_ROOTS` 白名单清空例外实现——只清空内容、保留目录本身，
  删除目录仍被二次防御拒绝。
- **最近文档/跳转列表**：`system_temp` 新增清空
  `%APPDATA%\Microsoft\Windows\Recent`（对应 clean.bat 的
  `del %userprofile%\recent\*.*`，使用现代路径，不影响文件本身）。
- **chkdsk 残留**：`system_admin` 新增删除卷根目录 `found.*`（found.000 等
  磁盘扫描碎片目录）；只删目录本身，不做 clean.bat 式的全盘递归 `*.chk`。
- **更新日志与备份残留**：`system_admin` 新增清理 `%WINDIR%\KB*.log`、
  `%WINDIR%` 顶层 `*.bak`、`%WINDIR%\Logs\WindowsUpdate` 诊断日志。
- 全部沿用既有安全模型：需管理员、先预览后确认、逐项容错、受保护路径拦截。

### 其它
- 项目结构整理：确认 `pc-cleaner/pc-cleaner/` 整份重复副本目录已不在磁盘上，
  项目为单一干净结构。

## 0.3.0 (2026-02)

### 新功能（借鉴 GitHub 开源清理工具）
- **风险分级**（借鉴 windows-cleaner-cli）：分类标记 🟢 安全 / 🟡 一般 / 🔴 高风险；
  高风险分类（downloads / dev_purge / browser_privacy / windows_old）默认隐藏，
  需 `--risky` 或配置 `show_risky`；`--all` 不会包含它们，`--clean` 显式指定时警告。
- **审计日志 + 清理历史**（借鉴 sifty）：每次清理写入 `history.json` 与 `audit.log`；
  新增 `--history` 查看历史、`--undo-last` 从回收站恢复最近一次清理
  （解析 `$Recycle.Bin` 的 `$I`/`$R` 映射）。
- **管理员深度清理分类 `system_admin`**（借鉴 sifty / WinPurge）：
  Windows 更新缓存、Prefetch、事件日志归档、系统崩溃转储；
  未提权自动跳过并提示，新增 `--admin` 一键 UAC 提权重启。
- **白名单清空例外**：`ALLOWED_CLEAR_ROOTS` 允许内置规则清空受保护前缀下
  明确可重建的缓存（如 `C:\Windows\SoftwareDistribution\Download`），
  删除目录本身仍被二次防御拒绝。
- **安全擦除 `--shred`**（借鉴 BleachBit / KCleaner）：永久删除前随机覆写一遍。
- **一键体检 `--checkup`**（借鉴 sifty）：只读汇总管理员状态、磁盘可用、
  回收站占用、可清理分类与上次清理记录。
- **配置导入导出**（借鉴 Win11Debloat）：`--export-config` / `--import-config`。
- **管道自动 JSON**（借鉴 sifty）：stdout 非终端且为只读扫描时自动输出 JSON。
- **`PC_CLEANER_HOME` 环境变量**：把配置/历史/审计日志重定向到任意目录
  （便携运行不写系统盘）。
- **更多清理细节**：Brave / Vivaldi / Opera 缓存与 GPU 缓存、Explorer 图标缓存、
  NuGet / Gradle / Go 模块缓存、下载目录 `.exe`/`.msi` 安装包、系统崩溃转储。

### 安全加固
- 删除守卫区分动作：白名单目录仅允许 CLEAR（清空内容），DELETE 目录本身仍拒绝。
- `--json` 输出新增 `risk` / `requires_admin` / `admin_blocked` / `admin` 字段。

## 0.2.0 (2025-01)

### 安全加固
- **删除前二次防御**：即使扫描器漏判，engine 也会拒绝磁盘根路径与任何受保护路径（黑名单子串匹配），杜绝误删。
- **进回收站失败不再静默永久删除**：默认失败即保留原文件并计入"跳过/失败"；如需旧行为可在配置里打开
  `recycle_error_fallback`，或命令行加 `--recycle-fallback`。
- **清空缓存目录时逐项跳过受保护子项**：即使缓存目录里混入用户数据/联接也不会被清掉。
- **遍历跳过 junction**：Python 3.12 下用 `DirEntry.is_junction()` 显式识别目录联接，避免越界放大扫描。
- **`--json` 模式默认只扫描**：要真正删除必须同时给 `--yes`（且非 `--dry-run`），避免自动化静默误删。

### 新功能
- `--exclude KEY[,KEY...]`：与 `--all`/`--clean` 联用排除指定分类（如 `--all --exclude downloads,recycle_bin`）。
- 交互菜单支持**循环清理**：一次清理完可继续扫描、再次选择。
- 预览**按体积从大到小排序**；展示行数可由配置 `preview_lines` 调整。
- 显示**磁盘可用空间**（清理前后）与**回收站占用**（仅 Windows，只读估算）。
- Windows 控制台 **ANSI 彩色输出**（自动降级，无第三方依赖），中文按显示宽度对齐。
- 新增缓存规则：uv / yarn / Go / cargo / WinGet 缓存；Steam appcache 与日志；
  Edge/Chrome 的 GPU/Dawn/WebGPU 缓存。
- `glob_files` 规则支持 `min_size_mb` / `older_than_days` 过滤（向后兼容）。
- `find_dirs` 规则支持 `action: "clear"`（清空而非删除目录）。
- 配置新增：`recycle_error_fallback`、`enabled_categories`（只扫描指定分类）、`preview_lines`。

### 修复
- `prompt_yes_no` 空输入判断 bug（重复空串元组）。
- `recycle_by_default=false` 时仍进回收站的问题：现在尊重配置，改为永久删除。
- `_scan_find_dirs` 里 action 恒为 delete 的死代码：现在真正支持 clear。
- `format_size` 支持 PB 与负数兜底。

### 其它
- `pc_cleaner.bat`：支持 `py -3` 回退、Python 版本检查、`--no-pause`。
- `python pc_cleaner/__main__.py` 直接运行给出友好提示而非 traceback。
- 删除项目内误提交的整份重复副本目录。
