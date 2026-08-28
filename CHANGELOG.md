# Changelog

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
