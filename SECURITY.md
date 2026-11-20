# 安全策略（Security Policy）

PC-Cleaner 是一个**会删除文件**的工具，安全边界是核心设计目标。本文件说明支持范围、
安全模型、明确边界与漏洞上报方式。

## 支持的版本

| 版本 | 安全更新 |
| --- | --- |
| 0.9.x（当前） | ✅ |
| < 0.9 | ❌（缺少重解析点防护与删除前二次防御，请升级） |

## 安全模型

完整说明见 README 的「安全边界」章节。核心约定：

1. **不确认不删除**：默认先预览、人工确认后才执行；`--dry-run` 永不删除。
2. **默认进回收站**：`send2trash` 是必需依赖；进回收站失败默认保留原文件。
3. **危险操作闸门**：高风险分类 / 永久删除 / 清空回收站，即使 `--yes` 也要显式授权；
   非交互场景直接拒绝（退出码 4）。
4. **删除前二次防御**：引擎重新解析路径并复核黑名单，拒绝磁盘根、系统关键文件与受保护路径。
5. **不跟随链接**：符号链接 / junction 一律不删、不清空（含 CLEAR 目标本身是链接的情况）。
6. **MCP 默认只读**：`--mcp` 不暴露写能力；`delete` 需 `--mcp-allow-delete`、两阶段
   `confirm_token`、清单漂移检测，危险清单还需 `acknowledge_danger=true`。

## 明确的设计边界（不是漏洞）

- **注册表只读**：`--registry-scan` / MCP `registry_scan` 只报告，不提供任何删除入口。
  删除注册表不释放磁盘空间，且“记录是否还有用”无法可靠判定；误删可能导致软件异常。
  需要清理请自行核对后用 `regedit`，并先 `reg export` 备份。
- **失效快捷方式只清“确定失效”**：只处理“目标所在卷可访问、但目标确实不存在”的 `.lnk`；
  离线盘/移动盘/断开的共享、URL、系统命名空间、0 字节 UWP 占位符一律不动；
  开始菜单 `Startup` 默认排除。
- **不删除系统组件**：`WinSxS` / `DriverStore` / `Windows\Installer` / `$Recycle.Bin` /
  `System Volume Information` 均在保护黑名单内，只报告体积与官方清理命令。
- **`--shred` 在 SSD/NVMe 上不保证不可恢复**：磨损均衡与 TRIM 会让覆写不可靠；
  真正销毁数据请使用 BitLocker 或厂商安全擦除。
- **不做遥测、不上传数据**：工具不联网；`--health` 的查询全部是本地命令 / 注册表读取。

## 上报漏洞

请**不要**用公开 Issue 披露未修复的安全问题。优先使用 GitHub 的
**Private vulnerability reporting / Security Advisory**，或通过 Issue 说明“需要私下沟通”。

报告请包含：影响版本与命令、可复现步骤（用临时目录，勿用真实数据）、期望行为 vs 实际行为；
如涉及误删，请说明对象类型与路径形态（是否含 junction / `..` / `\\?\` / 8.3 短名）。

## 已修复的历史安全问题

| 版本 | 问题 |
| --- | --- |
| 0.9.17 | 网络共享 `.lnk` 误判死链；`rmtree`/`rglob` 可能进入 junction；MCP `acknowledge_danger="false"` 被当作授权；提权 cmd 元字符未转义；`Windows\Installer` 未进保护名单 |
| 0.9.9 | MCP `delete` 不写 history/audit；菜单刷新参数错位导致 `find_dirs` 只遍历 1 层；离线盘/命名空间快捷方式误判 |
| 0.9.4 | `--json --clean ""` 静默退化为只扫描；MCP `delete` 重扫未带过滤条件导致漂移误报 |
| 0.9.3 | `--json --yes` 绕过二次确认；CLEAR 目标为 junction 时越界清空；`npm-cache` 父规则误删运行中的 npx |
| 0.9.2 | 撤销失效、释放量虚报、部分失败静默成功 |

更早的完整记录见 [docs/changelog-archive.md](docs/changelog-archive.md)。
