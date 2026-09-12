"""Windows 快捷方式（``.lnk``）解析 —— 零依赖、纯标准库、只读。

为什么不用 COM（``WScript.Shell`` / ``IShellLink``）
---------------------------------------------------
1. **可测**：COM 只能在真实 Windows 会话里跑，单元测试无法构造断言；
2. **快**：一个快捷方式一次 COM 往返约几十毫秒，扫描上百个就是十几秒；
   本模块直接读文件字节，本机 81 个快捷方式 <1 秒；
3. **稳**：COM 在无桌面会话（服务 / 计划任务 / 部分 sandbox）下不可用，
   而本工具需要在这些环境下也能给出"失效快捷方式"报告；
4. **不触发副作用**：不加载 shell 扩展，不弹任何对话框。

解析依据：MS-SHLLINK（``[MS-SHLLINK]: Shell Link (.LNK) Binary File Format``）。
本模块只关心"目标路径"这一个信息，因此只解析到 ``LinkInfo`` 与
``StringData`` 即可，其余字段（图标、跟踪数据、属性存储）一律跳过。

安全原则（与项目整体一致）
--------------------------
- 只读：本模块**绝不修改、绝不删除**任何东西，只返回判定结果；
- 保守：任何"无法确定"的情况都返回 ``UNKNOWN`` / ``UNAVAILABLE``，
  只有"目标盘符存在但目标确实不存在"才判定为 ``BROKEN``；
- 不跟随链接：解析结果只用于判断，实际删除由 scanner/engine 按普通文件处理。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
#: 目标存在（快捷方式有效）
V_OK = "ok"
#: 目标确实不存在，且其所在卷可访问 → 判定为失效
V_BROKEN = "broken"
#: 目标所在卷/网络位置当前不可用（离线盘、未插入的移动盘、断开的共享）
#: —— **不可**判定为失效，否则会误删"插上盘就能用"的快捷方式
V_UNAVAILABLE = "unavailable"
#: 非文件型目标（URL / 网络共享 / shell 命名空间 / 无目标）
V_UNKNOWN = "unknown"
#: 文件不是合法的 .lnk（0 字节的 UWP 占位符、损坏文件、非快捷方式）
V_INVALID = "invalid"

#: 不需要走"目标是否存在"判定的目标前缀（URL 协议、shell 命名空间）
_NON_FILE_PREFIXES = (
    "http://", "https://", "ftp://", "ftps://", "file://",
    "mailto:", "steam:", "uplay:", "origin:", "microsoft-edge:",
    "shell:", "::{", "search-ms:", "ms-",
)


@dataclass
class ShortcutInfo:
    """一个 ``.lnk`` 的解析结果（只读快照）。"""

    path: Path
    #: 解析出的目标候选（按可信度降序）；解析不出时为空列表
    targets: list[str] = field(default_factory=list)
    #: 命令行参数（仅记录，不参与判定）
    arguments: str = ""
    #: 工作目录（仅记录）
    working_dir: str = ""
    #: 是否为网络共享目标（NetName / UNC）
    network: bool = False
    #: 从 IDList 的 URL 段解析出的链接地址（``HasLinkTargetIDList`` + URL 型快捷方式）
    url: str = ""
    #: 是否带 LinkTargetIDList（shell 命名空间快捷方式会带，文件型也会带）
    has_idlist: bool = False
    #: IDList 中是否出现文件系统路径段（用于区分"命名空间"与"依赖未挂载的盘"）
    has_filesystem_idlist: bool = False
    #: 是否显式声明"没有 LinkInfo"（ForceNoLinkInfo，系统工具快捷方式常见）
    force_no_link_info: bool = False
    #: 解析失败的原因（供报告使用）
    error: str = ""

    @property
    def target(self) -> str:
        """最可信的目标路径（没有则空串）。"""
        return self.targets[0] if self.targets else ""

    @property
    def display_target(self) -> str:
        t = self.target
        if t:
            return t
        if self.network:
            return "（网络位置）"
        if self.error:
            return f"（{self.error}）"
        return "（无目标）"


# ---------------------------------------------------------------------------
# 环境变量展开
# ---------------------------------------------------------------------------
def _expand_one(raw: str) -> str:
    """展开 ``%VAR%`` / ``~`` 并把路径规范化（折叠 ``..``）。

    v0.9.9：**必须折叠** ``..``。Windows 快捷方式常把目标写成
    ``%LOCALAPPDATA%\\Temp\\x\\..\\..\\..\\Program Files\\app.exe`` 这种带相对段的
    形式，而 ``os.path.exists()`` 在某些情形下不会替我们对这类路径求值 ——
    实测会把**完全有效的快捷方式**判成"目标不存在"，进而被当成死链删除。
    """
    out = os.path.expandvars(raw)
    if os.name != "nt" and "%" in out:
        import re

        out = re.sub(
            r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), out
        )
    out = os.path.expanduser(out)
    # 只对文件系统路径做规范化：``shell::`` / ``http://`` 这类不是路径
    if out.lower().startswith(_NON_FILE_PREFIXES):
        return out
    if ".." in out or "\\\\" in out or "/./" in out:
        try:
            out = os.path.normpath(out)
        except (OSError, ValueError):
            return out
    return out


def _read_utf16z(buf: bytes) -> str:
    """读取以 ``\\x00\\x00`` 结尾的 UTF-16LE 字符串（未终结则读到末尾）。"""
    if len(buf) < 2:
        return ""
    end = len(buf)
    for i in range(0, len(buf) - 1, 2):
        if buf[i] == 0 and buf[i + 1] == 0:
            end = i
            break
    try:
        return buf[:end].decode("utf-16-le", errors="ignore")
    except Exception:  # noqa: BLE001 畸形数据不作为解析失败处理
        return ""


def _read_cstr(buf: bytes) -> str:
    """读取以单字节 ``\\x00`` 结尾的 ANSI 字符串（``LocalBasePath`` 用）。"""
    end = buf.find(b"\x00")
    if end < 0:
        end = len(buf)
    # 快捷方式里的路径来自系统 ANSI 代码页；UTF-8 优先，失败再回退 latin-1，
    # 保证任何字节序列都不会抛异常（我们只用它做存在性判断）。
    raw = buf[:end]
    for enc in ("utf-8", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


# ---------------------------------------------------------------------------
# 主解析
# ---------------------------------------------------------------------------
def parse_shortcut(path: str | os.PathLike[str]) -> ShortcutInfo:
    """解析一个 ``.lnk`` 文件，返回 :class:`ShortcutInfo`（只读、不抛异常）。

    解析失败的常见原因：0 字节文件（UWP/商店应用的占位快捷方式）、
    非 ``.lnk`` 文件、文件被占用、权限不足。
    """
    p = Path(path)
    info = ShortcutInfo(path=p)
    try:
        data = p.read_bytes()
    except OSError as exc:
        info.error = f"无法读取: {exc.__class__.__name__}"
        return info

    if len(data) < 0x4C:
        info.error = "文件过短（可能是 UWP 占位快捷方式）"
        return info
    if data[:4] != b"\x4c\x00\x00\x00":
        info.error = "不是合法的 .lnk（头标识不匹配）"
        return info

    try:
        flags = struct.unpack_from("<I", data, 0x14)[0]
    except struct.error:
        info.error = "头结构损坏"
        return info

    has_link_target_idlist = bool(flags & 0x00000001)
    has_link_info = bool(flags & 0x00000002)
    has_name = bool(flags & 0x00000004)
    has_relative_path = bool(flags & 0x00000008)
    has_working_dir = bool(flags & 0x00000010)
    has_arguments = bool(flags & 0x00000020)
    has_icon_location = bool(flags & 0x00000040)
    is_unicode = bool(flags & 0x00000080)
    force_no_link_info = bool(flags & 0x00000100)
    info.force_no_link_info = force_no_link_info
    info.has_idlist = has_link_target_idlist

    off = 0x4C
    # 1) LinkTargetIDList：2 字节大小 + 该大小的数据（含结尾 2 字节终止符）
    if has_link_target_idlist:
        if off + 2 > len(data):
            info.error = "IDList 长度字段越界"
            return info
        idlist_size = struct.unpack_from("<H", data, off)[0]
        # 记录 IDList 里是否出现"文件系统"路径段：这条信息用于区分
        #   * 命名空间型（文件资源管理器 / 控制面板 / 回收站）→ 没有文件目标；
        #   * 依赖某块盘的路径型（``Q:\Tools\x.exe`` 而 Q: 未挂载）→ 目标是文件，
        #     只是所在卷当前不可用，**绝不能**判成"死链"。
        info.has_filesystem_idlist = _idlist_has_filesystem(data[off + 2 : off + 2 + idlist_size])
        # URL 型快捷方式（目标写成 https://…）把地址放在 IDList 的 URL 段里，
        # 同样要从原始字节里捞出来，否则会被误当成"文件目标但卷不可用"。
        info.url = _idlist_url(data[off + 2 : off + 2 + idlist_size])
        off += 2 + idlist_size
        if off > len(data):
            info.error = "IDList 数据越界"
            return info

    # 2) LinkInfo：目标路径的主要来源
    if has_link_info:
        off = _parse_link_info(data, off, info)

    # 3) StringData 序列（按固定顺序，且只有对应 flag 置位时存在）
    def _take_string() -> str:
        nonlocal off
        if off + 2 > len(data):
            return ""
        count = struct.unpack_from("<H", data, off)[0]
        off += 2
        size = count * (2 if is_unicode else 1)
        if off + size > len(data):
            size = max(len(data) - off, 0)
        chunk = data[off : off + size]
        off += size
        if is_unicode:
            return chunk.decode("utf-16-le", errors="ignore").rstrip("\x00")
        return _read_cstr(chunk)

    name = _take_string() if has_name else ""
    relative = _take_string() if has_relative_path else ""
    working = _take_string() if has_working_dir else ""
    args = _take_string() if has_arguments else ""
    _icon = _take_string() if has_icon_location else ""

    info.arguments = args
    info.working_dir = working or info.working_dir

    # 4) ExtraData：系统自带工具（任务管理器 / 注册表编辑器 / 控制面板等）的
    #    快捷方式普遍带 ``ForceNoLinkInfo``（无 LinkInfo），目标只存在于
    #    EnvironmentVariableDataBlock（``%windir%\system32\taskmgr.exe``）。
    #    不解析这一段会把它们全部误判为"损坏的快捷方式"——本机 81 个里有 34 个，
    #    属于**会把系统工具快捷方式删掉**的严重误判。
    _parse_extra_data(data, off, info)

    # 优先级：LinkInfo 本地路径 > LinkInfo 网络路径 > 相对路径（拼快捷方式所在目录）
    seen: set[str] = set()
    ordered: list[str] = []
    for cand in list(info.targets):
        if cand and cand.lower() not in seen:
            seen.add(cand.lower())
            ordered.append(cand)
    if relative:
        base = _expand_one(relative)
        if not os.path.isabs(base):
            # 相对路径的基准是快捷方式所在目录；先拼再规范化，折叠其中的 ..
            try:
                base = os.path.normpath(os.path.join(str(p.parent), base))
            except (OSError, ValueError):
                pass
        if base.lower() not in seen:
            seen.add(base.lower())
            ordered.append(base)
    info.targets = ordered
    if not info.targets and not info.network:
        # 区分三种"没有目标"：
        #   * 带 IDList 的命名空间型快捷方式（文件资源管理器 / 控制面板 / 回收站
        #     / 运行）——虚拟文件夹，**没有文件目标，并未失效**；
        #   * 真正解析不出目标的损坏文件（0 字节 UWP 占位符等）。
        # 前者绝不能判成"可清理的死链"。是否带 IDList 由 classify 判断，
        # 这里只如实记录"没解析出路径"。
        info.error = "未解析出目标路径"
    elif name and not info.targets:
        info.error = info.error or f"仅解析出名称: {name}"
    return info


def _parse_link_info(data: bytes, off: int, info: ShortcutInfo) -> int:
    """解析 ``LinkInfo`` 结构体，把候选目标写入 ``info``，返回下一个字段的偏移。

    失败时**静默跳过**（返回原偏移 + 结构体长度），让上层继续尝试 StringData。
    """
    start = off
    if start + 4 > len(data):
        info.error = "LinkInfo 越界"
        return len(data)
    link_info_size = struct.unpack_from("<I", data, start)[0]
    nxt = start + max(link_info_size, 4)
    if link_info_size < 0x1C or start + 0x1C > len(data):
        info.error = "LinkInfo 结构过小"
        return min(nxt, len(data))

    li_flags = struct.unpack_from("<I", data, start + 8)[0]
    has_volume_id_and_local_base = bool(li_flags & 0x00000001)
    # 本地基路径仅在同时给出 VolumeID 时有效（否则该字段位置是 CommonPathSuffix）
    if not has_volume_id_and_local_base:
        common = _read_cstr(data[start + 0x1C : min(nxt, len(data))])
        if common:
            info.targets.append(_expand_one(common))
        return min(nxt, len(data))

    if start + 0x24 > len(data):
        return min(nxt, len(data))
    local_base_off = struct.unpack_from("<I", data, start + 0x10)[0]
    common_suffix_off = struct.unpack_from("<I", data, start + 0x18)[0]
    # 两个偏移都相对 LinkInfo 起点
    limit = min(nxt, len(data))
    if local_base_off:
        pos = start + local_base_off
        if start <= pos < limit:
            base = _read_cstr(data[pos:limit])
            if base:
                suffix = ""
                if common_suffix_off:
                    spos = start + common_suffix_off
                    if start <= spos < limit:
                        suffix = _read_cstr(data[spos:limit])
                info.targets.append(_expand_one(base + suffix))
    if li_flags & 0x00000002:  # VolumeIDAndLocalBasePath 之外还有 NetName（网络共享）
        info.network = True
    # VolumeID 里的盘符信息不参与判定：LocalBasePath 已含完整路径
    return min(nxt, len(data))


# ---------------------------------------------------------------------------
# ExtraData（系统工具快捷方式的目标就藏在这里）
# ---------------------------------------------------------------------------
def _first_ansi_string(payload: bytes) -> str:
    """取区块里第一个以 NUL 结尾的 ANSI 字符串并展开环境变量。

    快捷方式的 ExtraData 用的是系统 ANSI 代码页：中文 Windows 上是 cp936。
    依次尝试 cp936 / utf-8 / latin-1，保证任何字节序列都不会抛异常
    （我们只用结果做存在性判断，不做任何写操作）。
    """
    first = payload.split(b"\x00", 1)[0]
    if not first:
        return ""
    for enc in ("cp936", "utf-8", "latin-1"):
        try:
            text = first.decode(enc)
        except UnicodeDecodeError:
            continue
        except Exception:  # noqa: BLE001
            return ""
        return _expand_one(text)
    return ""


def _parse_extra_data(data: bytes, off: int, info: ShortcutInfo) -> None:
    """解析 ``ExtraData`` 区块序列，取出环境变量形式的目标路径（只读）。

    结构：``<4 字节块大小><4 字节签名><数据>``，重复直到块大小为 0 或越界。
    我们只关心两类：

    * ``EnvironmentVariableDataBlock`` (``0xA0000001``)：两个以 NUL 结尾的
      ANSI 字符串，第一个是带 ``%VAR%`` 的目标路径，第二个是 Unicode 版本；
    * ``IconEnvironmentDataBlock`` (``0xA0000007``)：图标路径，仅作兜底。

    其它块（跟踪信息、属性存储、已知文件夹、MSI、Vista 兼容 ID 等）一律跳过。
    """
    guard = 0
    while off + 8 <= len(data) and guard < 64:
        guard += 1
        try:
            block_size = struct.unpack_from("<I", data, off)[0]
            signature = struct.unpack_from("<I", data, off + 4)[0]
        except struct.error:
            return
        if block_size < 8 or off + block_size > len(data):
            return
        payload = data[off + 8 : off + block_size]
        if signature == 0xA0000001:  # EnvironmentVariableDataBlock
            cand = _first_ansi_string(payload)
            if cand and cand not in info.targets:
                info.targets.insert(0, cand)
        elif signature == 0xA0000007:  # IconEnvironmentDataBlock
            cand = _first_ansi_string(payload)
            if cand and cand not in info.targets and not cand.lower().endswith(".ico"):
                info.targets.append(cand)
        off += block_size


# ---------------------------------------------------------------------------
# 目标可用性判定
# ---------------------------------------------------------------------------
def _idlist_url(idlist: bytes) -> str:
    """从 IDList 中解析 URL 段（URL 型快捷方式：目标写成 ``https://…``）。

    实测字节形态（本机 Windows 导出并逐字节核对）：URL 段 ItemID 的 payload
    里直接是 UTF-16LE 的 ``https://example.com/``，前缀字节是 ``0x61``
    （并非文档中常写的 ``sch:`` 文本前缀 —— 按那个去找会一无所获）。
    因此这里直接按 UTF-16LE 的 scheme 头定位，取到 NUL 终结为止。

    只接受 ``http/https/ftp/ftps`` 开头的结果，避免把无关的宽字符片段当成 URL。
    """
    if not idlist:
        return ""
    for scheme in (b"h\x00t\x00t\x00p\x00s\x00:\x00", b"h\x00t\x00t\x00p\x00:\x00",
                   b"f\x00t\x00p\x00:\x00"):
        pos = idlist.find(scheme)
        if pos < 0:
            continue
        tail = idlist[pos:]
        text = tail.decode("utf-16-le", errors="ignore").split("\x00", 1)[0].strip()
        low = text.lower()
        if low.startswith(("http://", "https://", "ftp://", "ftps://")) and len(text) > 8:
            return text
    return ""


def _idlist_has_filesystem(idlist: bytes) -> bool:
    """IDList 里是否出现「文件系统」盘符根项 —— 即目标确实指向某块盘上的文件。

    本机逐字节核对出的根项形态（``[MS-SHLLINK]`` 的 "root folder" ItemID）::

        ItemID = 2 字节大小 + payload，payload = 2F <盘符> 3A 5C 00 00 …
        整个 ItemID 长度固定 25 字节（大小字段 0x0019）

    实测三种快捷方式：

    * ``Q:\\Tools\\portable.exe``（Q: 未挂载）→ 有根项 payload ``2f 51 3a 5c``；
    * ``C:\\Windows\\System32\\cmd.exe``          → 有根项 payload ``2f 43 3a 5c``；
    * ``https://example.com``（URL 型）           → 无根项，只有 URL 段。

    因此"存在盘符根项"就是"目标是文件系统路径"的可靠证据；这正是识别
    **离线盘 / 未挂载盘**快捷方式的唯一线索（这类快捷方式没有 LocalBasePath，
    也不是命名空间虚拟文件夹）。任何解析异常都按"无证据"处理（宁可不判定，
    也不误判为死链）。
    """
    if not idlist:
        return False
    pos = 0
    guard = 0
    while pos + 3 <= len(idlist) and guard < 256:
        guard += 1
        size = struct.unpack_from("<H", idlist, pos)[0]
        if size < 3 or pos + size > len(idlist):
            break
        payload = idlist[pos + 2 : pos + size - 1]
        if len(payload) >= 5 and payload[0] == 0x2F:
            if payload[1:2].isalpha() and payload[2:4] == b":\\":
                return True
        pos += size
    return False


def _volume_root_available(target: str) -> bool:
    """目标所在"卷根"当前是否可访问（盘符 / UNC 顶层）。

    这是**避免误删**的关键：``E:\\Tools\\x.exe`` 在没插移动盘时不存在，
    但它并没有失效。只有当目标卷本身可访问、而目标路径不存在时，
    才能判定快捷方式真的坏了。
    """
    try:
        if target.startswith("\\\\"):
            # UNC：检查共享根（\\server\share）
            parts = [x for x in target.split("\\") if x]
            if len(parts) < 2:
                return False
            return os.path.exists("\\\\" + parts[0] + "\\" + parts[1])
        drive, _tail = os.path.splitdrive(target)
        if not drive:
            return True  # 无盘符（相对路径已转绝对）→ 按当前卷处理
        root = drive + os.sep
        return os.path.exists(root)
    except OSError:
        return False


def classify(info: ShortcutInfo) -> tuple[str, str]:
    """把解析结果判定为 ``(verdict, 说明)``。

    返回的 verdict 取值见模块顶部的 ``V_*`` 常量。判定顺序：

    1. 解析失败：带 LinkTargetIDList 的**系统命名空间快捷方式**（文件资源管理器 /
       控制面板 / 回收站 / 运行）判为 ``UNKNOWN``，其余（0 字节 UWP 占位符、
       损坏文件）判为 ``INVALID`` —— 两者都不会被清理；
    2. 候选目标是 URL / shell 命名空间 → ``UNKNOWN``；网络位置 → ``UNKNOWN``；
    3. 任一候选存在 → ``OK``；
    4. 目标卷不可用（离线盘 / 断开的共享）→ ``UNAVAILABLE``；
    5. 其余 → ``BROKEN``（目标卷在，目标确实没了）。
    """
    usable = [t for t in info.targets if not t.lower().startswith(_NON_FILE_PREFIXES)]

    # URL 型快捷方式：地址在 IDList 的 URL 段里，永远不是"死链"
    if info.url and not usable:
        return V_UNKNOWN, f"URL 型快捷方式（目标: {info.url}）"
    # 先判"有目标但不是文件路径"（URL / shell: 命名空间）——它们永远不该被清理
    if info.targets and not usable:
        return V_UNKNOWN, f"目标不是文件路径（URL / 命名空间）: {info.targets[0]}"

    if not info.targets:
        # 没有文件目标：命名空间 / URL / 网络位置都不是"死链"
        if info.network:
            return V_UNKNOWN, info.error or "目标是网络位置，未做存在性判定"
        if info.has_filesystem_idlist:
            # IDList 表明目标是文件系统路径，只是所在卷当前读不到（未挂载的盘 /
            # 断开的共享）——绝不能判成死链
            return V_UNAVAILABLE, "目标是文件路径但所在卷当前不可用（未挂载的盘/断开的共享）"
        if info.has_idlist:
            return V_UNKNOWN, "系统命名空间快捷方式（虚拟文件夹，无文件目标）"
        if info.error:
            return V_INVALID, info.error
        return V_UNKNOWN, "未解析出文件目标"

    for t in usable:
        try:
            if os.path.exists(t):
                return V_OK, f"目标存在: {t}"
        except OSError:
            continue

    primary = usable[0]
    if not _volume_root_available(primary):
        return V_UNAVAILABLE, f"目标所在卷当前不可用（离线盘/未挂载）: {primary}"
    return V_BROKEN, f"目标不存在: {primary}"


def is_broken(info: ShortcutInfo) -> bool:
    """快捷方式是否**确定失效**（唯一可被清理的判定）。"""
    return classify(info)[0] == V_BROKEN
