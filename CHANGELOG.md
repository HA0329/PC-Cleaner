# Changelog

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
