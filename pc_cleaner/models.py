"""数据结构定义。

Target 代表一个"可以清理的单元"，可能是单个文件，也可能是一个目录
（目录模式下会清理其内容或整个目录）。Scanner 产出 Target，引擎消费 Target。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class CleanMode(str, Enum):
    """删除方式。"""

    #: 永久删除，不恢复
    PERMANENT = "permanent"
    #: 删除到回收站（可恢复）
    RECYCLE = "recycle"


class TargetKind(str, Enum):
    """目标类型：文件 / 目录。"""

    FILE = "file"
    DIR = "dir"


class TargetAction(str, Enum):
    """目录目标执行的动作。"""

    #: 删除目录下的所有内容，但保留目录本身（适用于缓存目录，程序仍会重建）
    CLEAR = "clear"
    #: 删除目录本身（适用于 __pycache__ 等）
    DELETE = "delete"


@dataclass
class Target:
    """一个待清理目标。"""

    path: Path
    kind: TargetKind
    action: TargetAction
    category: str
    #: 该目标占用（或可释放）的字节数
    size: int
    #: 该目标包含的文件数量（目录时为内部文件数；File 时为 1）
    file_count: int = 1
    #: 可选的标签描述（来自规则定义）
    label: str = ""

    @property
    def display_size(self) -> str:
        return format_size(self.size)

    @property
    def action_label(self) -> str:
        """返回动作的中文标签。"""
        if self.kind is TargetKind.FILE:
            return "文件"
        if self.action is TargetAction.CLEAR:
            return "清空"
        return "删除"

    @property
    def kind_icon(self) -> str:
        """返回目标类型的图标。"""
        if self.kind is TargetKind.FILE:
            return "📄"
        if self.action is TargetAction.CLEAR:
            return "📂"
        return "🗑️"

    def describe(self) -> str:
        """用于预览台词的简短描述。"""
        if self.kind is TargetKind.FILE:
            return f"[文件] {self.path} ({self.display_size})"
        if self.action is TargetAction.CLEAR:
            return (
                f"[目录-清空] {self.path} ({self.file_count} 个文件, "
                f"{self.display_size})"
            )
        return (
            f"[目录-删除] {self.path} ({self.file_count} 个文件, "
            f"{self.display_size})"
        )

    def describe_compact(self) -> str:
        """紧凑描述，用于详细列表中的单行展示。"""
        label_part = f" ({self.label})" if self.label else ""
        if self.kind is TargetKind.FILE:
            return f"📄 {self.path}{label_part}  {self.display_size}"
        icon = "📂" if self.action is TargetAction.CLEAR else "🗑️"
        return f"{icon} {self.path}{label_part}  [{self.file_count} 文件, {self.display_size}]"

    def describe_tree(self, indent: int = 0) -> str:
        """树形展示格式。"""
        prefix = "  " * indent
        label_part = f"  ← {self.label}" if self.label else ""
        if self.kind is TargetKind.FILE:
            return f"{prefix}📄 {self.path.name}{label_part}  ({self.display_size})"
        icon = "📂" if self.action is TargetAction.CLEAR else "🗑️"
        return f"{prefix}{icon} {self.path.name}{label_part}  ({self.file_count} 文件, {self.display_size})"


@dataclass
class CategoryResult:
    """一个分类的扫描结果。"""

    key: str
    label: str
    targets: list[Target] = field(default_factory=list)
    #: 扫描时跳过/无法统计的条目数
    skipped: int = 0
    scanned: bool = False
    #: 风险等级: safe(绿色,随便清) / moderate(黄色,一般安全) / risky(红色,需显式开启)
    risk: str = "safe"
    #: 该分类是否需要管理员权限
    requires_admin: bool = False
    #: 因缺少管理员权限而未扫描(True 时 targets 为空)
    admin_blocked: bool = False
    #: 扫描耗时（秒）
    scan_duration: float = 0.0

    @property
    def total_size(self) -> int:
        return sum(t.size for t in self.targets)

    @property
    def total_count(self) -> int:
        return sum(t.file_count for t in self.targets)

    @property
    def liberatable(self) -> int:
        """该分类可释放的总字节数。"""
        return self.total_size

    @property
    def target_count(self) -> int:
        """目标条目数（非文件数）。"""
        return len(self.targets)

    def sorted_targets(self, by: str = "size_desc") -> list[Target]:
        """按指定方式排序目标列表。

        by: 'size_desc' | 'size_asc' | 'name_asc' | 'name_desc' | 'count_desc'
        """
        if by == "size_desc":
            return sorted(self.targets, key=lambda t: t.size, reverse=True)
        elif by == "size_asc":
            return sorted(self.targets, key=lambda t: t.size)
        elif by == "name_asc":
            return sorted(self.targets, key=lambda t: str(t.path).lower())
        elif by == "name_desc":
            return sorted(self.targets, key=lambda t: str(t.path).lower(), reverse=True)
        elif by == "count_desc":
            return sorted(self.targets, key=lambda t: t.file_count, reverse=True)
        return self.targets


@dataclass
class ScanReport:
    """一次完整扫描的汇总结果。"""

    categories: list[CategoryResult] = field(default_factory=list)
    #: 扫描总耗时（秒）
    total_duration: float = 0.0

    def by_key(self, key: str) -> CategoryResult | None:
        for c in self.categories:
            if c.key == key:
                return c
        return None

    @property
    def total_reclaimable(self) -> int:
        return sum(c.liberatable for c in self.categories)

    @property
    def total_targets(self) -> int:
        return sum(c.target_count for c in self.categories)

    @property
    def total_files(self) -> int:
        return sum(c.total_count for c in self.categories)


def format_size(num: float) -> str:
    """把字节数格式化成人类可读的字符串。"""
    if num is None:
        return "?"
    num = float(num)
    if num < 0:
        return "?"  # 不应出现负体积，保守显示
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def format_size_compact(num: float) -> str:
    """紧凑格式，不带空格。"""
    if num is None:
        return "?"
    num = float(num)
    if num < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(num)}{unit}"
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f} PB"
