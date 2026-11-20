# Changelog

> 本文件维护最近 6 个版本（0.9.12–0.9.17）。更早的 0.2.0–0.9.11 见
> [docs/changelog-archive.md](docs/changelog-archive.md)。

## 0.9.17 (2026-11)

> 主题：**安全修复**（外部代码审查确认的 P0/P1）。

### 快捷方式

- 解析 `CommonNetworkRelativeLink`（NetName + CommonPathSuffix），不再把
  LinkInfo 的 Unicode 偏移字段当作路径；网络共享快捷方式一律 `UNKNOWN`，
  不参与失效判定，避免把有效的共享快捷方式当死链删除。

### 删除引擎

- 用自带的安全遍历替换 `shutil.rmtree`：遇到符号链接 / junction 立即中止，
  不递归、不删除链接本身；`--shred` 的预覆写也显式跳过重解析点，
  防止覆写链接指向的数据。

### MCP 与提权

- `acknowledge_danger` 改为严格布尔判定：字符串 `"false"` 不再被当作授权。
- 提权命令行对 `&|<>^()` 等 cmd 元字符强制加引号，路径中的 `&` 不再被
  解释为命令分隔符。

### 保护名单

- 显式加入 `Windows\Installer` 与 `Windows\System32\DriverStore`，
  与 SECURITY.md 的系统组件承诺一致。

### 验证

- 新增 `tests/test_v0917_security.py`（8 条）；测试总数 410；
- Linux 上 ruff / mypy 全绿；Windows CI 待本次推送验证。

## 0.9.16 (2026-11)

> 主题：**精简文档**（五项改进中的第五项）。本版不改变任何清理规则、交互或删除行为。

### README

- 从 410 行压到 233 行：删除逐版本功能考古、重复的安全说明和冗长命令表；
  保留“快速开始 / 能力 / 34 个分类 / 安全边界 / 常用命令 / JSON / MCP / 配置 / 测试”。
- 修正旧文档里“Windows 之外平台可运行”的错误表述：本工具只适配 Windows。
- 分类表仍覆盖 `rules.json` 的全部 34 个 key；保留 `--deep` / `--max-depth` / 50 层的说明。

### CHANGELOG

- 主文件只保留最近 5 个版本（0.9.12–0.9.16），从 1250 行压到约 150 行；
- 0.2.0–0.9.11 的完整历史移到 [docs/changelog-archive.md](docs/changelog-archive.md)，不丢信息。

### 文档一致性

- `TestDocsMatchImplementation` 9 条继续通过：版本号、测试数 402、分类表、规则数量互相一致；
- `docs/json-contract.md` 版本同步到 0.9.16，`schema_version` 仍为 1；
- 规则仍是 34 分类 / 304 目标 / 38 条 deep，`validate_rules()` 0 error / 0 warning。

### 边界

- 本版只改文档，未在 Windows 实机运行，未打包；
- 文档精简不删除历史：旧版本记录在 archive 中可查。

## 0.9.15 (2026-11)

> 主题：**增强工程质量**（五项改进中的第四项）。本版不改变清理规则与删除范围。

### 质量闸门

- `pyproject.toml` 新增 `ruff`（E4/E7/E9/F/I/B）与 `mypy`（`platform = "win32"`、
  `check_untyped_defs = true`）配置；`dev` extra 同步加入 `ruff` / `mypy`。
- GitHub Actions 在 Windows × Python 3.12 上新增 `ruff check` 与 `mypy` 两个硬闸门，
  与 pytest、规则校验、健康检查一起构成发布前检查。
- 清理 21 处未使用导入、14 处导入顺序、2 处空 f-string；测试代码同步整理。
- 修复类型检查暴露的局部变量复用与异常链问题：`menu` 的删除结果改名
  `delete_result`，`engine` 的回收站条目循环变量改名 `entry`，`raise ... from ...`
  明确链路上文；`rules` 的存活性审计补齐 5 元组类型。

### 健壮性

- `console._reconfigure_utf8_stream` 改为先 `getattr` 再调用：StringIO、已关闭流、
  不支持 `reconfigure` 的嵌入流不再依赖 `AttributeError` 兜底。
- MCP stdio 复用同一个助手，协议流与普通 UI 流的 UTF-8 处理不再两套实现。
- `engine` 中路径解析 / 数据库压缩失败时的 `PermissionError` 补充 `from None` /
  `from exc`，异常链不再含糊。

### 仓库卫生

- 删除被误跟踪的 `pc_junk_cleaner.egg-info/` 构建产物（`.gitignore` 早已忽略该目录）；
- 新增 v0.9.15 质量回归测试：质量闸门配置、UTF-8 流助手、egg-info 跟踪状态。

### 工程

- 新增 3 条测试，测试总数 399 → 402；`validate_rules()` 保持 0 error / 0 warning。
- 版本号同步到 `0.9.15`（`__init__.py`、`pyproject.toml`、`rules.json`、README、JSON 契约）。

### 边界

- 本版只做工程质量与仓库卫生，不新增/删除任何清理规则，不改变用户可见的清理行为。
- 未在 Windows 实机运行验收；未打包 exe（第四项默认不打包，打包留到最终交付）。
- Linux 上只能验证 lint / mypy / 平台无关测试；Windows 路径语义用例仍以 Windows CI 为准。

## 0.9.14 (2026-11)

> 主题：**优化使用体验**（五项改进中的第三项；按要求先不打包）。

### 一键推荐

- 菜单新增 `p`：自动选中所有 safe 分类，以及 `game_caches`、`game_runtime_cache`、`live_stream_cache`、`webview2_caches` 四个可重建的 moderate 缓存。
- 推荐流程先展示分类、目标数、合计空间和排除范围，再走一次确认；不再把 30 个分类编号和 12 行路径一次性铺在屏幕上。
- 推荐列表刻意不包含：高风险分类、Downloads 个人文件、开发缓存、使用痕迹、系统升级残留和需管理员分类；不会顺带清空回收站。

### 菜单

- `D`（大写）在菜单内开/关深度模式并重新扫描；不再需要退出程序后自己拼 `--deep`。
- `e` 在菜单内直接发起 UAC 提权重启，并把当前深度状态和原参数转发给新进程。
- 清理完成文案改为“已移入回收站 X（可恢复；清空回收站后才会释放）”或“已释放 X”，不再把两种口径混成一句“释放约 X”。
- 失败、部分完成、占用跳过分开显示，并给出关闭占用程序、重扫、清空回收站等下一步。
- 启动首屏直接提示“输入 p 一键推荐清理”；删除方式、管理员状态和快捷键更醒目。
- `pc_cleaner.bat` 的 Python 提示从 3.10+ 修正为 3.12+。

### 信息设计

- `CategoryResult` 新增 `note`；`rules.json` 的分类说明进入 `d` 详情页、`--detail` 报告、扫描导出 JSON、`--json` 信封和 MCP 扫描结果。
- 推荐页脚与推荐概览会显示“推荐多少、清什么、为什么不包含下载/开发缓存”，不再让用户自己猜。

### 安全边界

- 本版只改交互与文案，不扩大删除范围；`p` 的推荐白名单是显式代码常量，新增 moderate 分类不会自动进入推荐。
- 高风险分类仍需 `x` / `--risky` 显式开启；回收站清空仍需单独 `r` 或 `--clean recycle_bin`；管理员分类仍需 `e` 或 `--admin`。
- 未打包、未在 Windows 实机验收；Linux 上只做静态校验和平台无关测试。`tests/test_v0911_fixes.py` 的 Windows 路径语义用例仍不适用于 Linux。

### 工程

- 新增 8 条 UX 回归测试，测试总数 391 → 399；`validate_rules()` 保持 0 error / 0 warning。
- 版本号同步到 `0.9.14`（`__init__.py`、`pyproject.toml`、`rules.json`、README）。

## 0.9.13 (2026-11)

> 主题：**改革清理收益**（五项改进中的第二项）。

### 默认收益

- `office_caches` 3 条、`media_caches` 2 条与 `wechat_cache` 7 条从 `deep_only` 提升为默认扫描：Office/WPS 缓存、Adobe 媒体缓存、微信小程序/插件/内置浏览器缓存不再需要 `--deep`。
- 默认分类的 deep 规则从 50 条降到 38 条；规则总数仍为 34 分类 / 304 目标。

### 占用时部分清理

- `Target` 新增 `partial_if_in_use`；分类级 `partial_if_in_use` 让 CLEAR 缓存/临时目录在程序运行时只清未锁定子项，而不是整目标跳过。
- 覆盖 `web_cache`、`office_caches`、`media_caches`、`gpu_caches`、`comm_caches`、`system_temp` 等 12 个缓存/临时分类；`wechat_cache` 仍保持整类跳过，避免微信运行时删除正在使用的小程序包。
- `dev_caches`（npm `_npx`）、`system_admin`（Windows 更新缓存）等危险目标仍保持整目标跳过。
- 全部子项都锁定/失败时计入 `skipped_in_use`，不会假装成功。

### 工程

- 新增 7 条收益回归测试；测试总数 391；`validate_rules` 保持 0 error / 0 warning。

## 0.9.12 (2026-11)

> 主题：**完善清理覆盖**（五项改进中的第一项）。

### 清理覆盖

- 新增 `downloads_temp`：Downloads 根目录的 `.crdownload` / `.part` / `.partial` / `.opdownload` / `.download` 未完成下载残片，要求 30 天未修改。
- 新增 `downloads_installers`：Downloads 根目录的 exe/msi/msix/appx/iso/zip/7z/rar/cab，按体积与 90/180 天阈值筛选；`all_excluded`，不随 `--all` 选中。
- 新增 `downloads_extracted`：仅当压缩包旁存在同名解压目录（`foo.zip` 与 `foo/` 并存）时才清理压缩包，避免误删尚未解压的下载。
- 新增 `system_wer`（需管理员）：ProgramData 下的 WER `ReportArchive` / `ReportQueue` / `Temp`，与用户级 `crash_telemetry` 分开。
- 扩展 `system_admin`：`$WinREAgent`、设备元数据缓存、服务账户字体缓存。
- 扩展 `gpu_caches`：Intel ShaderCache、NVIDIA ComputeCache。
- 扩展 `comm_caches`：Slack、Microsoft Teams、飞书/Lark。
- 扩展 `web_cache`：QQ 浏览器、搜狗浏览器。

### 安全与工程

- `CategoryResult` 新增 `all_excluded`；`--all` 与菜单 `all` 不再选中下载安装包/已解压压缩包/RDP 旧缓存，但仍可单独选择。
- `glob_files` 新增 `require_extracted_sibling`，并在 `validate_rules` 中校验类型与适用范围。
- `downloads` 不再重复安装包规则（拆到 `downloads_installers`），避免跨分类重复计数。
- 新增 6 条覆盖回归测试；规则总数 34 分类 / 304 目标，`validate_rules` 保持 0 error / 0 warning。
