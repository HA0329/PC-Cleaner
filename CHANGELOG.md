# Changelog

## 0.9.7 (2026-09)

> 主题：**加载指示器 + 扫描实时统计 + 清理实时状态**。
> 0.9.6 解决了"启动没反馈"和"进度行串行乱码"，但环境探测仍是静态一行、
> 扫描进度只有 `12/27`、清理时只显示当前路径——用户看不到"在干活、还要多久、
> 已经找到多少、释放了多少"。本次把这三处都补成实时状态。

### 新增：加载指示器 `Spinner`
- 环境探测（浏览器 / GPU / 微信 / 组件库体积）在慢盘上要几秒，此前只有一行静态
  提示。新增 `ui.Spinner`：stderr 上原地刷新的加载动画（旋转帧 + 已耗时），
  支持 `spinner.update("正在统计组件库与微信缓存体积")` 分阶段换文案。
- 接入点：`--checkup` 的「运行环境适配」小节、交互菜单首屏「本机适配」探测。
- **非 TTY / 管道 / `--json` / MCP 场景完全静默**；重复 `stop()` 幂等。

### 新增：启动横幅
- `ui.print_startup_banner()`：一行显示版本 / 删除模式 / 遍历深度 / 扫描线程
  （`PC Junk Cleaner 0.9.7 · 回收站 · 深度 20 · 4 线程`），一眼看清当前配置。
- 仅在交互式 CLI 启动时输出；`--json` / 重定向 / MCP 下不产生任何字节。

### 增强：扫描进度条（实时状况）
- **实时统计**：进度行同时显示「已找到 N 项 / X MB」，随分类完成滚动累加
  （`scan_all` 新增 `on_category_done` 回调）。
- **剩余时间估算**：按已完成分类的平均耗时推算，显示「剩约 2s」。
- **并行扫描的真实进度（重要修复）**：此前 `workers>1` 时是"按提交顺序 + 全部扫完
  才回放进度"，进度条形同虚设；现在用 `as_completed` **按完成顺序实时刷新**，
  结果顺序仍严格与 `specs` 一致。
- 非 Unicode 终端自动回退 ASCII 图标与加载帧，避免方块 / `UnicodeEncodeError`。

### 新增：清理过程的实时状态
- `ui.CleanProgressDisplay`：清理时显示进度条 + **当前正在处理的目标**
  （路径按终端宽度中间截断）+ 已处理 / 跳过计数 + 已释放（或已入回收站）字节。
- 串联 audit 回调累计字节；`--dry-run` / 非 TTY 自动静默。

### 测试
- **恢复 v0.9.3 安全回归套件** `tests/test_v093_safety.py`（21 例）：junction 拒绝、
  `skip_if_in_use` 链路、`glob_dirs` 缺 pattern、回收站 `recycled`/`freed` 语义、
  危险闸门与退出码、`history` 原子写与损坏备份、`compact_db` 空闲页估算、
  白名单不短路名称保护、`--clean ""` 报错。这些用例在整理测试目录时被删过，
  而被修复的缺陷仍需要它们守着。
- 新增 `tests/test_v097_ui.py`（25 例）：TTY / 非 TTY 静默性、帧刷新与清行、
  实时统计与 ETA、清理计数与字节累计、启动横幅内容。
- 全量：**215 passed**（0.9.6 为 169）。

## 0.9.6 (2026-09)

> 主题：**启动反馈 + 启动提速 + 进度行串行乱码修复**。
> 用户反馈（0.9.5 实测）：双击 `pc_cleaner.bat` 后 cmd 仍然只有光标闪烁
> 一段时间才出进度；且「扫描完成」行与最后一个进度行串在一起，出现
> `耗时 0.0s存(系统账户/下载中断/媒体应用) 27/27 (0.0s)` 的乱码；
> 0.0s 的耗时也让人怀疑扫描是否真的执行了。

### 启动反馈（不再只有光标闪烁）
- **`pc_cleaner.bat` 双击后立刻打印**「正在启动 PC Junk Cleaner ...」——
  bat 顶部新增 `chcp 65001` 与提示行，双击瞬间就有反馈，不再对着
  闪烁的光标干等。
- **`cli.py` 交互启动时先打印**「正在启动：加载规则并扫描缓存目录 ...」，
  扫描进度条第一帧出现后自动接续到下一行；`--json` / 重定向场景不产生
  多余输出。

### 启动提速（省掉一次 Python 冷启动）
- **`pc_cleaner.bat` 只启动一次 Python**：旧版先 `python -c "版本检查"`
  （输出被 `>nul` 丢弃，屏幕上什么都没显示）再 `python -m pc_cleaner`，
  等于冷启动两次解释器；在装有杀软（360 / 火绒 / Defender 等）的机器上，
  每次冷启动都会被实时扫描拖慢数秒，叠加就是几十秒的「光标闪烁」。
  新增 **`_launcher.py`**：先做版本检查（< 3.10 给出明确报错），再进入
  主程序，全程单进程。

### 进度显示修复
- **行尾清除**：进度行用 `\r` 原地刷新，但新行比上一行短时上一行的尾巴
  会残留——「扫描完成」比最后一个进度行短，于是出现
  `耗时 0.0s存(系统账户/下载中断/媒体应用) 27/27 (0.0s)` 的串行乱码。
  现在每次刷新与完成汇总都在内容后追加 `\x1b[K`（ANSI 不可用时自动为空），
  残留尾巴被清除。
- **亚秒级耗时改毫秒显示**：扫描 = 目录 stat + 文件计数，NVMe / 系统缓存
  下 27 个分类几十毫秒完成是真实且正常的，但「耗时 0.0s」看起来像没干活。
  现在 `耗时 43ms` 更直观可信；≥ 1s 仍显示 `x.xs`。


## 0.9.5 (2026-09)

> 主题：**修复「启动卡顿几十秒」+ 跨平台保护缺陷 + 测试全绿**。
> 用户反馈：双击 `pc_cleaner.bat` 后界面要等几十秒才出现，但扫描早已
> 0 秒完成（进度条瞬间走完）。根因是交互菜单顶部的「本机适配」环境探测
> 在逐盘符做阻塞 I/O，与扫描本身无关。

### 修复：启动卡顿几十秒（主诉 bug）
- **`env.py` 盘符探测改为非阻塞**：旧实现逐个 `os.path.exists("X:\\")` /
  `shutil.disk_usage("X:\\")` 探测 26 个盘符，一旦存在**离线映射网络盘 /
  空读卡器 / 无盘光驱**，每个盘符都会被 SMB / 驱动超时阻塞（单盘 10~30 秒），
  界面因此卡在「本机适配」一行；改用 `GetLogicalDrives` + `GetDriveTypeW`
  （纯读驱动器映射，**不发起盘符 I/O**），且只保留**本地固定盘**
  （`DRIVE_FIXED`）——网络盘 / 可移动盘 / 光驱既不包含可清理缓存，探测又会
  阻塞，全部跳过。`probe_environment` 的磁盘用量统计同步只走固定盘。
- **`scanner._system_drives()` 同样只保留固定盘**：此前虽已用
  `GetLogicalDrives` 避免 26 连探，但仍包含可移动 / 光驱 / 网络盘，对它们
  统计 `$Recycle.Bin` 的 `is_dir` / 遍历仍会阻塞——这会让菜单里的
  「回收站」行与 `--checkup` 的磁盘小节同样卡顿。现在固定盘专属，
  回收站统计既准确又不阻塞。

### 修复：跨平台保护匹配缺陷（Linux / macOS 安全）
- **`scanner._path_components()` 统一分隔符**：此前只在 Windows 上生效
  （`os.sep == "\\"`），规则里的 `\.git` / `windows\system32` 等模式在
  POSIX 上会变成**带反斜杠的单个组件**，永远匹配不到真实目录名，导致
  「受保护目录不被保护、体积统计不剪枝」（实测 `.git` 子树被计入可释放）。
  现在先把 `\` 统一成 `os.sep` 再拆分，Windows 行为不变。
- **`rules._resolve_root()` / `scanner.expand_path()` 跨平台展开 `%VAR%`**：
  POSIX 的 `os.path.expandvars` 不识别 Windows 风格 `%WINDIR%` 占位符，
  导致白名单 / 保护路径在 Linux / macOS 上全部失配；新增
  `_expand_vars_cross_platform` 统一展开，并在 POSIX 上把反斜杠归一化为
  分隔符。Windows 行为完全不变。

### 测试（165 passed，4 skipped，CI 双平台全绿）
- 修复过期测试：`test_parse_selection_fullwidth`（`_parse_selection` 早已
  改为返回结构化 dict，断言仍是旧 API）；`test_parse_recycle_info`
  （测试辅助构造的 `$I` 文件缺 4 字节「目录记录长度」字段，与 v0.9.2 起
  解析器按 offset 28 读路径的真实格式不符）；两个「预读取(Prefetch)」
  标签断言改为前缀匹配（规则标签已带提示后缀）。
- 修复跨平台测试假设：`test_protect_check` 注入可移植 `WINDIR` 环境变量；
  `test_restore_paths_*` 在非 Windows 平台伪装 `sys.platform="win32"`，
  让回收站恢复逻辑真正跑一遍（此前被平台守卫短路，逻辑从未在 Linux 上
  执行）；`test_drop_subsumed_*` 用跨平台方式取路径末级
  （`Path(r"C:\X\npm-cache").name` 在 POSIX 上返回整串路径）；
  `test_allowlist_real_windir` 标为 Windows 专属跳过。
- 版本统一到 **0.9.5**（`__init__.py` / `pyproject.toml`）。


## 0.9.4 (2026-09)

> 主题：**MCP 服务端 + 机器可读 JSON 契约 + 国际化 + CI**。
> 让 AI Agent 能安全地（默认只读、两阶段确认、漂移检测）使用本工具，并给自动化调用
> 提供固定契约与 JSON Schema。

### MCP 服务端
- **新增 [`pc_cleaner/mcp.py`](pc_cleaner/mcp.py)**：零依赖 **stdio JSON-RPC 2.0** 服务端
  （`python -m pc_cleaner --mcp`），stdout 只承载协议、日志全部走 stderr；协议版本支持
  `2024-11-05` / `2025-03-26` / `2025-06-18`，**回显客户端请求的版本**。
- **默认只读**：未加 `--mcp-allow-delete` 时 `tools/list` 只有 `scan` / `health` / `history` /
  `preview_delete`，`delete` / `undo` 既不出现在工具表、直接调用也会被拒绝。
- **两阶段确认**：`preview_delete` 只返回清单 + `confirm_token`（**600 秒**有效、一次性、
  绑定清单指纹），执行必须再调 `delete(confirm_token)`——Agent 无法「一步删掉」。
- **漂移检测（防 TOCTOU）**：`delete` 重新扫描后比对——清单里**消失**的目标容忍（不再删）；
  出现**未预览过的新目标**且体积 > `max(1MB, 5% × 清单总量)` → `status="needs_repreview"`
  拒绝执行；已有目标体积变化容忍；**永远只删预览过的路径**。
- **危险操作二次授权**：清单含高风险分类 / 永久删除 / 清空回收站时，`delete` 必须带
  `acknowledge_danger=true`（token 只证明「这是你预览过的那批」，不代替授权）。
- 每个工具同时返回 `content[0].text`（JSON 字符串）与 `structuredContent`（对象）；
  `undo` 仅对 `recycle` 会话生效，可按 `session_id` 定位。
- 新增 [`docs/mcp.md`](docs/mcp.md)：安全模型、工具参数表、一次典型交互、Claude Desktop /
  通用 stdio MCP 客户端配置示例。

### JSON 契约
- **新增 `--json-schema`**：输出 `service.ENVELOPE_SCHEMA`（draft-07 子集，零依赖校验），
  字段表与调用示例见新增的 [`docs/json-contract.md`](docs/json-contract.md)。
- **⚠️ 语义变更**：`action.skipped` 现在是**整数计数**（部分清理的目标数）；
  「什么都没执行」改用 **`action.not_executed: true`**（0.9.3 文档写的 `action.skipped=true`
  已作废）。
- `status` 新增三个取值：`preview`（MCP 预览）、`needs_repreview`（MCP 清单漂移）、
  `restored`（MCP 回收站恢复完成）；并把 `--health --json` 实际返回的 `ok` 正式纳入枚举，
  文档枚举同步为 12 个。
- 契约承诺：字段**只增不减**，破坏性变更必须提升 `schema_version`（当前 `1`）。

### 国际化
- **新增 [`pc_cleaner/i18n.py`](pc_cleaner/i18n.py) + `pc_cleaner/locales/{zh_CN,en}.json`**
  （各 167 个 key，扁平 `key → 译文`）：零依赖、只读语言文件、**永不抛出**（文件缺失/损坏/
  占位符不匹配一律回退）。
- 用法：`--lang zh_CN|en`、环境变量 `PC_CLEANER_LANG`、配置项 `language`；
  优先级 **`--lang` > 配置 > 环境变量 > `zh_CN`**；语言别名归一化
  （`zh` / `zh_cn` / `zh-hans` / `zh_CN.UTF-8` → `zh_CN`；`en` / `en_us` / `en-gb` → `en`）。
- **当前覆盖范围**：`--health` 体检报告已完整双语（16 项的 label/detail/advice、标题、
  汇总行、页脚）。实测 `--lang en --health` 除杀毒软件产品名（`火绒安全软件`，来自系统
  返回值）外无中文残留。
- **诚实标注限制**：交互式菜单、清理摘要与部分 CLI 提示**仍为中文**（尚未覆盖）；
  locale key 缺失时回退 `zh_CN`，再缺失回退 key 本身。
- 配置文件新增 `language`（默认 `""` = 按环境变量或默认 `zh_CN`）。

### CI 与测试
- **新增 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)**：矩阵
  `windows-latest` + `ubuntu-latest` × Python `3.10` / `3.12` / `3.13`，`fail-fast: false`；
  步骤 `pip install -e ".[dev]"` → `compileall` → `pytest -q` → `--version` →
  `--validate-rules` → `--health --json`（重定向到文件后断言是合法 JSON、信封字段齐全、
  `health.items` 非空）；仅使用 `actions/checkout@v4` 与 `actions/setup-python@v5`
  两个官方 action，并带 `concurrency`（同分支旧运行取消）与 `permissions: contents: read`；
  统一 `bash` + `PYTHONUTF8/PYTHONIOENCODING=utf-8`，避免 Windows 运行器写出 UTF-16/BOM。
- **新增 `tests/test_v094_contract.py`（34）**：真实子进程验证 JSON 契约——stdout 必须是
  **单个合法 JSON 对象**（无 ANSI、无旁白文本；`health.items[*].advice` 视为数据）、
  信封必填字段与 `ok == (exit_code == 0)`、`status` 落在文档枚举内、退出码映射（0/1/4）、
  危险闸门与 `--dry-run` 均不落地删除、`--health` 恰好 16 项且结构完整、stdin 关闭时仍能
  正常返回、`recycle_bin_size_bytes` 随平台出现、空分类名不崩溃。
- **新增 `tests/test_v094_mcp.py`（21）**：协议层（initialize / tools/list / 未知方法）、
  默认只读时 `delete`/`undo` 不在工具表且调用被拒、两阶段确认（token 缺失/过期/一次性）、
  漂移检测（新增目标超阈值 → `needs_repreview`；消失目标容忍）、危险清单需
  `acknowledge_danger`、`undo` 走回收站恢复、**stdout 只含 JSON-RPC 帧**。
- 全套测试 **114 passed**（`test_v092.py` 39 + `test_v093.py` 20 +
  `test_v094_contract.py` 34 + `test_v094_mcp.py` 21）。Linux 上部分 Windows 专属用例
  由 `skipif` 跳过，属预期行为。

### 其它
- 版本统一到 **0.9.4**（`__init__.py` / `pyproject.toml`）。
- `history.json` 的会话新增 **`session_id`**（供 MCP `undo` 与将来的 `--undo --session`
  精确定位；**旧会话无此字段**，读取时容错）。
- `rules.json` 与 `--checkup` 的增量（新增 `%WINDIR%\SystemTemp` 规则 → 272 个目标；
  WinSxS / DriverStore 体积改为标注硬链接口径并按 inode 去重）随本版合入，
  已在 0.9.3 小节末尾同步记录。
- 未做的事（明确说明，避免误解）：**没有** Trash Vault（回收站保险库）、
  **没有** `--undo --session`（仅预留 `session_id`）、**没有** PyInstaller 打包。

## 0.9.3 (2026-09)

> 主题：**安全加固 + 面向自动化（AI Agent / 管道调用）的接口契约**。
> 修掉一批「会删到正在运行的程序 / 顺着链接越界清空 / 自动化绕过二次确认」类缺陷，
> 新增只读体检 `--health`（16 项）与固定退出码 / JSON 信封。

### 安全修复
- **`--json --yes` 绕过二次确认（严重）**：JSON 分支此前不看风险等级，`--json --clean
  downloads --yes` 可直接删高风险分类。现在高风险分类 / 永久删除 / 清空回收站三类危险操作
  必须显式加 `--risky`，否则返回 `status="needs_confirmation"` + **退出码 4**，**什么都不删**。
- **非交互环境不再"静默取消"**：管道 / Agent 场景下 `--yes` 遇到危险操作会被**明确拒绝**
  （`needs_confirmation`），而不是靠 stdin EOF 取消却返回退出码 0。
- **CLEAR 目标本身是符号链接 / junction 时拒绝**：此前会顺着链接清空**链接目标**的目录内容
  （例如 `C:\Users\All Users` → `C:\ProgramData`）；同时 `_dir_size` 不再跟随根链接，
  体积统计不再虚高。删除（DELETE）目标拒绝链接自身的行为保持。
- **路径词法规范化**：`expand_path` 统一 `absolute() + normpath()` 折叠 `..`、去掉 `\\?\`
  扩展前缀；`normalize()` 额外去掉结尾空格与点。修复「同一目录因写法不同被当成两个路径」
  导致的重复计数，以及 `\\?\` / 尾随点写法让绝对前缀式保护失效的问题。
- **分类名拼错不再静默忽略**：`--clean` / `--exclude` 里的未知分类名现在报错并返回
  **退出码 1**（此前 `--exclude download` 会被忽略、照常清理 `downloads`）。
- **规则校验强制必填字段**：`glob_dirs` / `glob_files` 必须显式给出 `pattern`（此前缺省
  `*` 等于清空整个 `base`）；`action` 大小写不敏感、非法值报错；`find_dirs` 必须给出正整数
  `max_depth`。`--validate-rules` 另输出**警告**（死规则 / 冗余规则 / 风险错配），
  警告不影响退出码。
- **回收站释放量不再虚报**：进回收站的字节数只计入 `recycled`（清空回收站后才真正释放），
  `freed` 只统计永久删除与清空回收站的实际减少量；**清空回收站失败会如实报错并计入
  `failed`**（此前被忽略）。
- **`history.json` 不再因写入中断而整体丢失**：原子写（`*.tmp-<pid>` + `os.replace`）、
  解析失败改名 `*.corrupt-<时间戳>` 保留证据、读-改-写期间用 `history.lock` 做跨进程互斥；
  **Ctrl+C 中断也会落盘**（已进回收站的文件仍可 `--undo-last` 找回），退出码 130。
- **删除正在运行的程序的根因修复（规则侧）**：删除 `%LOCALAPPDATA%/npm-cache` 父规则
  （它会被去重逻辑保留并 rmtree 掉整个 `npm-cache`，而其中 ~100% 是 `_npx`，实测包含
  正在运行的 npx / AI 工具），改为只清 `_npx` / `_cacache` / `_logs` 并加 `skip_if_in_use`。

### 新增能力
- **`--health` 只读体检（16 项）**：新增 [`pc_cleaner/health.py`](pc_cleaner/health.py)，
  零第三方依赖、纯只读（只读注册表 / 只跑只读查询命令，**绝不修改系统**）。覆盖系统版本、
  Windows 更新暂停、待重启、Secure Boot、TPM、磁盘空间、设备问题、异常关机、SMB1、
  不安全来宾登录、防火墙、杀毒软件、幽灵设备、磁盘健康、长路径、休眠文件；每项独立容错，
  失败只变 `unknown`。存在 `error` 项时退出码 1；`--health --json` 输出
  `{schema_version, ok, status, exit_code, health:{...}}`。
- **进程占用检测 `skip_if_in_use`**：新增 [`pc_cleaner/proc.py`](pc_cleaner/proc.py)，
  psutil 可选（存在时批量取 exe/cmdline 与打开句柄），缺失时回退一次
  `powershell.exe Get-CimInstance Win32_Process`；进程快照与句柄结果均缓存 **60 秒**。
  规则声明 `"skip_if_in_use": true` 后，目标被运行中进程占用时引擎整体跳过并计入
  `skipped_in_use`（审计日志记为 `mode=skipped_in_use`）。
- **`compact_db` 体积口径修正**：改为「空闲页 × 页大小」估算可回收量，不再把**整库大小**
  当作可释放；无碎片可回收时不显示该目标。

### 接口契约（面向自动化）
- **退出码固定**：`0` 成功 / `1` 参数·配置·分类错误 / `2` argparse 用法 / `3` 删除失败 /
  `4` 需要确认或已取消（什么都没删）/ `130` 用户中断。
- **JSON 信封**：`--json` 顶层新增 `schema_version`(=1)、`ok`、`status`
  （`scan`/`dry_run`/`deleted`/`partial`/`needs_confirmation`/`cancelled`/`interrupted`/
  `error`）、`exit_code`；`action` 新增 `recycled_bytes`、`skipped_in_use`。
- **`send2trash` 改为必需依赖**：`pip install -e .` 即带回收站支持，不再静默降级为永久
  删除（与「安全第一 / 可撤销」的定位一致）；`.[recycle]` 保留为**空 extra** 兼容旧写法。
- **`pc_cleaner.bat`**：`--no-pause` 改为启动器本地消费（此前被转发给 Python，导致
  `unrecognized arguments` + 退出码 2）；优先 `py -3` 避开 Microsoft Store 的 python 存根；
  设置 `PYTHONUTF8=1` 避免 cp936 控制台 UnicodeEncodeError。

### 规则调整（`rules.json` 283 → 272 个目标）
- **删除 12 条**：`%LOCALAPPDATA%/npm-cache` 父规则、微信 3.x **数据目录**
  `%USERPROFILE%/Documents/WeChat Files`、`%PROGRAMDATA%/Package Cache`、
  `%WINDIR%/WinSxS/Temp|Backup`、`$Recycle.Bin` 三条、`System32/config/systemprofile`
  三条（受保护路径 / 不可逆 / 非受支持方式），以及被 `*` 覆盖的冗余
  `System32/LogFiles pattern "*/*"`。
- **`_npx` 改为独立目标**并加警示 label「正在运行的 npx/AI 工具会被破坏，占用时自动跳过」，
  配 `skip_if_in_use`；共 **251/272** 条规则带该字段（`%TEMP%`、`%WINDIR%\Temp`、`Prefetch`、
  `Downloads`、`dev_purge`、`compact_db`、`windows_old` 等故意不加，避免「全有全无」的跳过
  让它们变成空操作）。
- **新增白名单 `ALLOWED_CLEAR_UNDER_PROTECTED`**：`%WINDIR%\System32\LogFiles` 与
  `%WINDIR%\System32\winevt\Logs` 位于受保护前缀之下，此前所有针对它们的规则**永久空转**
  （实测 `System32\LogFiles` 有 31.9 MB 日志却从未被清理）。现在**只允许清空内容**，
  根目录本身仍不可删除。
- `chkdsk 残留(found.*)` 由 `delete` 改为 `clear`（只清空内容，不删目录本身）；
  `%WINDIR%/Prefetch` label 标注「清理后首次启动/常用程序可能变慢」；
  `winevt\Logs` 的 `*.evtx` 规则改为 `deep_only` + `older_than_days: 30`；
  `USOPrivate/UpdateStore`、`Edge|Chrome Sync Data`、`OneDrive/FileCoAuth` 等状态数据规则
  改为 `deep_only` 并在 label 写明后果；两条 `find_dirs` 显式给出 `max_depth`（16 / 12）。
- 同步修正各分类 `note`：微信去掉不可复现的「本机约 636MB」、`hidden_installer_backups`
  说明改用 DISM、`recycle_and_diagnostics` 说明回收站改走系统 API、`system_logs` 说明新白名单、
  `database_compact` 说明体积口径。
- **新增 1 条**：`%WINDIR%/SystemTemp`（Windows 11 24H2 新增的系统服务临时目录，
  此前未被任何规则覆盖）。
- **组件库体积口径**（`--checkup` / 运行环境适配）：`WinSxS` / `DriverStore` 的目录遍历值
  含「与系统共享的硬链接」，会高于实际占用（本机 WinSxS 遍历 7.49 GB，DISM
  `/AnalyzeComponentStore` 报告实际 4.55 GB），现已在输出中明确标注；
  统计函数对同一遍历内出现的重复 inode 做去重（`st_nlink > 1` 时按 `(st_dev, st_ino)`）。

### 文档与测试
- README 同步：特性列表、分类表、安全模型（新增路径规范化 / junction / `skip_if_in_use` /
  规则校验 / 危险操作闸门）、参数表（新增 `--health`）、**新增「退出码与 JSON 契约」小节**、
  项目结构（新增 `health.py` / `proc.py` / `service.py` / `tests/test_v093.py`）、安装说明
  （`send2trash` 必需）、测试数字（43 → **59**）。
- 新增 `tests/test_v093.py`（20 个用例）：路径规范化与保护判定一致性、CLEAR 目标为 junction
  时拒绝、`_dir_size` 不跟随根链接、`glob_dirs` 必填 `pattern`、`action` 大小写与非法值、
  `skip_if_in_use` 从规则到 `Target` 的链路与引擎跳过、回收站只计 `recycled`、危险操作判定
  与退出码映射、`--json` 拒绝未授权高风险分类、`--json` 未知分类退出码 1、`history.json`
  原子写与损坏改名、`compact_db` 空闲页估算、白名单不短路名称级保护。
- 全套测试 **59 passed**（`test_v092.py` 39 + `test_v093.py` 20）。

## 0.9.2 (2026-09)

> 主题：**针对本机实测加强清理能力 + 修复一批真实缺陷**。
> 本机（Windows 10 IoT LTSC 2024 / Edge + NVIDIA + 微信 4.x + Steam + npm/pnpm 开发环境）
> 可清理量从 **507 MB → 1.30 GB**，并修好了「扫描空间虚高、清理量不实、撤销无效」等硬伤。

### 清理能力增强（本机实测校准）
- **微信运行缓存大幅补强（本机 +675MB）**：新增 `xplugin/plugins` 插件模块
  （实测 636MB，按需重下）与 `radium` 小程序容器缓存（`web` 39MB + deep 模式下的
  `users/*/applet|xworker|udr`）；网络缓存改为 `net_*` 通配（覆盖 `net_1` 等实例）；
  补 3.x 遗留 `WeChat/Logs`、`WeChat/Temp`。**聊天数据目录（WeixinShuju /
  xwechat_files）依然绝不触碰**。
- **新增 `system_logs` 分类（本机 +41MB）**：DISM / CBS / waasmedic / SIH / NetSetup
  日志、`System32\LogFiles`（WMI、Scm 等）、`setupapi.*.log` / `setupact.log` /
  `setuperr.log` 驱动安装日志、`WindowsUpdate.log` / `PFRO.log` / `DtcInstall.log`、
  USOShared/USOPrivate 更新编排状态（本机 36MB）、传递优化 P2P 缓存与跟踪日志、
  Panther 安装日志、`security\logs`、`debug`；deep 模式补 `SettingSync`、`SystemData`、
  事件日志归档。
- **开发缓存补强（本机 +450MB）**：npm `_npx`（实测 212MB）/ `_cacache` / `_logs`、
  corepack 缓存、各盘根 `.pnpm-store` 通配、`~/.m2/repository`、VS Code 日志与缓存、
  JetBrains 日志、VS 组件模型缓存。
- **直播伴侣/电竞平台**：补 `perfectworldarena/Partitions`（本机 34.7MB）、
  `webcast_mate` 组件缓存与日志。
- **Steam 着色器缓存**加规则级 `max_depth: 3`：不再为了找 `shadercache`
  遍历上百 GB 的 steamapps（本机单条规则 1.6s → 0.03s）。

### 新增能力
- **新 target 类型**：`empty_dirs`（空目录，支持 `min_age_days`）、
  `zero_byte_files`（0 字节残留文件）。
- **`ext` 扩展名过滤**：`glob_files` / `files_by_rule` / `zero_byte_files` 可写
  `"ext": [".log", ".tmp"]`，比 `pattern` 更直观。
- **`find_dirs` 规则级 `max_depth`**：单条规则即可限制下探层数。
- **并行扫描**：`scan_all(workers=N)` + 配置 `scan_workers` / `--workers N`
  （0=按 CPU 自动，1=串行）；结果顺序与规则顺序保持一致。
- **`--checkup` 新增「组件库/系统级空间」小节**：报告 WinSxS（本机 7.51GB）、
  DriverStore（3.34GB）、`Windows\Installer`（397MB）、卷影副本的体积与**官方清理
  命令**（DISM / pnputil / vssadmin）——这些内容本工具不删除，只如实告知。
- **`--checkup` 微信小节**新增「可清理运行缓存」清单（逐项路径 + 体积）。

### 修复
- **「可释放空间」虚高（重复计数）**：`npm-cache` 与 `npm-cache/_npx` 同时命中时，
  子目录体积被算两次（本机 413MB 被重复计入）。现在父目录目标覆盖其子目录目标
  （`_drop_subsumed`），分类内与跨分类都生效；COMPACT（数据库压缩）目标不参与覆盖。
- **`--clean recycle_bin` 静默无操作**：旧版只选回收站时因 `selected` 为空直接
  `return 0`，什么也没发生（菜单里的 `r` 是好的，命令行是坏的）。现在会正常预览
  回收站体积并进入确认流程。
- **`--undo-last` 永远匹配不上（恢复 0 项）**：`$I` 元数据解析从 offset 24 读路径，
  对**目录**记录会把目录长度字段的低字节（`b'd\x00'`）当成路径首字符，导致原路径
  比对失败。改为按真实布局从 offset 28 读取；`restore_paths` 同时支持
  「父目录被整体回收」与「目录内容被逐条回收」两种情形，恢复后清理 `$I` 元数据。
  实测：微信插件 15 个子项全部成功恢复。
- **「释放约 X」不实**：CLEAR 目标此前把扫描时的估计值当作释放量（含被占用/受保护
  文件），现在按删除前后体积差核算；`audit.log` 的 `freed` 不再恒为 0。
- **部分失败被静默吞掉**：清空目录时子项被占用会返回 `skipped` 计数；一个都没删掉
  时计为 `failed`，不再"假装成功"。实测：`waasmedic` / `USOShared\Logs` 被运行中的
  系统服务占用，工具会如实报告「跳过」而不是虚报释放了 40MB。
- **重解析点（符号链接 / junction）保护**：删除前拒绝链接自身与链接目标
  （此前只跳过目录遍历），避免越界删除。
- **`--all` 不再隐式清空回收站**：回收站不可恢复，改为需显式
  `--clean recycle_bin`（或配置 `all_includes_recycle_bin=true`）；README/帮助同步更正。
- **`--json` 补 `recycle_bin` 处理**：`--clean recycle_bin --yes` 现在会真正清空并
  返回结果，`--dry-run` 返回 `would_empty_recycle_bin`。
- **`--export-scan` 在管道/重定向下也能写文件**：此前会被「管道自动 JSON」提前拦截，
  只输出 JSON、文件根本没生成；现在导出优先处理并打印结果摘要。
- **版本号与文档一致**：`__init__.py` / `pyproject.toml` / `rules.json` 统一到 0.9.2；
  README 的分类数（27）、规则数（283）、测试数同步更新。
- **`CATEGORY_META` 补全**：13 个分类此前没有中文元信息（只有 rules.json 里有），
  现在与规则文件一一对应。

### 其它
- `_dir_size` 对**受保护子树整棵剪枝**：体积统计更准（受保护内容本就不会被删），
  同时避免统计注定要跳过的巨型目录。
- `_dir_size` 递归写入每个子目录的 memo：父目录测一次后子目录规则直接命中缓存。
- 空目录/0 字节目标不再占用预览行；`find_dirs` 命中的空目录不再产出 0 字节目标。
- 新增 `tests/test_v092.py`（39 个用例，含交互菜单渲染回归测试），全套 **43 passed**。

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
