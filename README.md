# 🧹 PC Junk Cleaner

> 为**个人电脑**定制的安全垃圾清理工具（Windows 优先）。
> 交互式菜单 · 先预览后确认 · 删除可进回收站。

一个以**安全为第一优先级**的垃圾清理程序：它不会直接删东西，而是先扫描、
告诉你每个分类能释放多少空间、列出将删除的文件，**经你确认后**才动手。
默认可以删除到回收站（可恢复），并且内置了受保护路径保护。

---

## 📌 本机实测（按实际扫描结果定制）

本项目的分类与路径来自对目标电脑的真实扫描，直接命中真正堆积的缓存：

| 分类 key | 名称 | 本机实测（扫描时） |
| --- | --- | --- |
| `gpu_caches` | GPU 着色器缓存 | **1.31 GB**（NVIDIA DXCache / GLCache） |
| `wechat_cache` | 微信运行缓存 | 959.58 MB（xplugin / radium / log / crashinfo） |
| `web_cache` | 浏览器/网页缓存 | 364.59 MB（Edge、Steam htmlcache） |
| `game_caches` | 游戏平台缓存 | 264.80 MB（完美世界更新包） |
| `dev_caches` | 开发工具缓存 | 53.27 MB（pnpm / pip / 构建产物） |
| `system_temp` | 系统临时文件 | 40.72 MB |
| `downloads` | 下载/旧文件 | 0（可自行配置） |

> 合计约 **2.96 GB** 可安全释放。

---

## ✨ 功能特性

- 🗂 **分类清理**：精准命中 GPU 着色器缓存、微信运行缓存、Edge/Steam 网页缓存、
  游戏平台缓存、pnpm/pip 缓存、系统临时文件、下载旧文件、回收站
- 🔍 **先预览后确认**：扫描 → 展示体积与文件列表 → 人工确认 → 执行
- ♻️ **可回收**：优先删除到回收站（依赖 `send2trash`），未安装时降级为永久删除并提示
- 🛡 **智能安全**：内置受保护路径黑名单、跳过系统关键目录与用户数据、
  不跟随符号链接/目录联接（junction）、逐项捕获权限错误
- 🎛 **交互式菜单**：默认运行即进入菜单，按提示选择要清理的分类
- ⚙️ **可配置**：`config.json` 可保存你的回收站偏好、额外保护路径、自定义规则与目录

---

## 🔒 安全模型（重要）

本工具遵循「先预览、后删除、可恢复、有保护」的原则：

1. **不未经确认就删除**：所有删除都需要人工确认。
2. **进入回收站优先**：默认尝试删除到回收站，可随时还原。
3. **黑名单保护**：内置绝不触碰 `C:\Windows\System32`、`C:\Windows\WinSxS`、
   `$RECYCLE.BIN`、`System Volume Information`、**微信聊天数据
   （`WeixinShuju` / `xwechat_files`）** 等，并支持自定义。
4. **跳过危险结构**：不删除未知/系统目录，不跟随符号链接与 junction，避免误删或被放大请求。
5. **逐项容错**：单个文件被占用/无权限时跳过并继续，不中断整个任务。

> ⚠️ 清理工具天然有风险。请务必先看**预览清单**，不确定的分组不要勾选。
>
> 📌 **关于微信**：本工具只清理 `%APPDATA%\Tencent\xwechat` 下的**运行缓存**
> （xplugin / radium / log / crashinfo），**不会**删除你的聊天记录、图片、视频或文件。
> 若 `D:\WeixinShuju`（微信数据）占用很大（实测约 14 GB），请用微信「设置 →
> 存储空间」的官方清理功能处理，那里才有安全的深度清理选项。

---

## 🖥 运行环境

- **OS**：Microsoft Windows 11 IoT Enterprise LTSC（Build 26100，24H2）
- **CPU / 内存**：AMD Ryzen 5 5600（6 核 12 线程）/ 16 GB
- **磁盘**：C 盘 150 GB（系统）、D 盘 327 GB（软件）
- **Python**：3.12.3（建议 3.10+）

> Windows 之外平台可运行，但分类路径以 Windows 为目标。

---

## 🚀 快速开始

要求：Python 3.10+（推荐 3.12）。

> ⚠️ `python -m pc_cleaner` 必须在**项目根目录**（含 `pc_cleaner/` 包、`pyproject.toml`
> 的那一层）运行，不要进入 `pc_cleaner/` 子目录。Windows 用户可直接双击
> `pc_cleaner.bat`（已自动切到根目录），或先 `pip install -e .` 后再从任意目录运行。

```bash
# 克隆后进入**项目根目录**
python -m pc_cleaner --list           # 只扫描，不删任何东西，查看各分类占用
python -m pc_cleaner                  # 进入交互式清理菜单
```

**Windows 双击运行**：双击项目根目录下的 `pc_cleaner.bat`。

或安装为命令行工具：

```bash
pip install -e .                      # 基础版（标准库）
pip install -e ".[recycle]"           # 推荐：支持删除到回收站
```

推荐先安装 `send2trash`（让删除进回收站）：

```bash
pip install send2trash
```

---

## 🖥 命令行用法

```
python -m pc_cleaner [--list] [--clean 分类名] [--all] [--dry-run]
                     [--recycle|--permanent] [--json] [--yes]
```

| 参数 | 说明 |
| --- | --- |
| `--list` | 仅扫描，列出各分类的占用与可清理体积，不删除 |
| `--clean 分类` | 直接清理指定分类（如 `web_cache,system_temp`，用逗号分隔多个） |
| `--all` | 选中所有分类 |
| `--dry-run` | 只预览，不真正删除（默认即预览确认） |
| `--recycle` | 删除进回收站（若已安装 send2trash） |
| `--permanent` | 永久删除（会先确认，默认需额外确认） |
| `--json` | 输出 JSON 结果，便于脚本/自动化使用 |
| `--yes` | 跳过交互确认（谨慎使用，会合并 --permanent 语义） |

**示例：**

```bash
# 查看能释放多少空间
python -m pc_cleaner --list

# 只清理 GPU 着色器缓存和微信运行缓存，进回收站
python -m pc_cleaner --clean gpu_caches,wechat_cache --recycle

# 以 JSON 输出扫描结果
python -m pc_cleaner --list --json
```

---

## 🗂 内置清理分类

| 分类 key | 名称 | 清理内容 |
| --- | --- | --- |
| `system_temp` | 系统临时文件 | 用户/系统 Temp、缩略图缓存、WebCache、错误报告 |
| `gpu_caches` | GPU 着色器缓存 | NVIDIA DXCache / GLCache（可安全清理并重建） |
| `web_cache` | 浏览器/网页缓存 | Edge 缓存、Steam htmlcache、INetCache |
| `wechat_cache` | 微信运行缓存 | xplugin / radium / log / crashinfo（不动聊天数据） |
| `game_caches` | 游戏平台缓存 | 完美世界竞技平台更新包缓存 |
| `dev_caches` | 开发工具缓存 | pnpm/pip/npm 缓存、`__pycache__`、构建产物 |
| `downloads` | 下载/旧文件 | Downloads 中的大文件/近期未动文件（可按体积/时间筛选） |
| `recycle_bin` | 回收站 | 清空回收站（单独确认） |

> 各分类默认启用合理的体积/时间阈值与黑名单，避免误删正在使用的文件。

---

## ⚙️ 配置文件

程序会在用户目录生成 `pc_cleaner/config.json`：
- `recycle_by_default`：默认是否进回收站
- `protected_paths`：额外加入的黑名单路径
- `custom_rules`：自定义清理规则（路径、扩展名、阈值）
- `dev_artifact_bases`：额外扫描「散落构建产物」的目录（默认含当前工作目录）

首次运行后用 `--show-config` 查看，或手动编辑该文件。

---

## 🗃 项目结构

```
pc-cleaner/
├── pc_cleaner/
│   ├── __init__.py      # 版本信息
│   ├── __main__.py      # python -m pc_cleaner 入口
│   ├── cli.py           # 命令行与交互菜单
│   ├── config.py        # 配置读写
│   ├── engine.py        # 删除引擎（回收站/永久、进度、回收站清空）
│   ├── models.py        # 数据结构
│   ├── rules.py         # 分类规则/黑名单/白名单（本机定制）
│   └── scanner.py       # 安全扫描与体积计算
├── tests/               # 单元测试
├── pyproject.toml
├── README.md
└── LICENSE
```

---

## 🧪 测试

```bash
pip install -e ".[dev]"
pytest
```

---

## 📄 License

[MIT](LICENSE)
