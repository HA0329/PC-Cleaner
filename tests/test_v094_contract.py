"""v0.9.4 对外契约测试：用**真实子进程**验证 ``--json`` 信封、退出码与危险闸门。

为什么必须用真实子进程：``--json`` 是给自动化 / AI Agent 消费的**进程级**契约——
退出码、stdout 纯净性（单个 JSON 对象、无 ANSI、无人类文本）、stdin 关闭时的行为，
只有在真实进程里才成立。``tests/test_v093.py`` 已用 monkeypatch 覆盖了内部函数，
本文件补的是"从外面看进来"的那一层。

安全约定（硬性，绝不删除任何东西）：

- 只使用 ``--json``（仅扫描）、``--dry-run``，以及**必然被危险闸门拒绝**的参数组合
  （``--json --clean browser_privacy --yes`` 且不给 ``--risky``）；
- **禁止** ``--yes`` + 非 ``--dry-run`` 的其它组合，**禁止** ``--clean recycle_bin``；
- 每个子进程都设 ``PC_CLEANER_HOME``（指向临时目录，不碰真实用户配置 / 历史）
  与 ``PYTHONIOENCODING=utf-8``，并设 ``timeout=180``。

覆盖范围：

1. JSON 纯净性：5 种调用下 stdout 必须是单个合法 JSON 对象、无 ANSI、无旁白文本
   （旁白判定是"字符串感知"的：``--health --json`` 的 ``health.items[*].advice``
   本身就是中文建议文本，属数据而非旁白）；
2. 信封字段：``schema_version`` / ``ok`` / ``status`` / ``exit_code``，
   且 ``ok == (exit_code == 0)``；
3. 退出码：``--version``→0、非法分类→1、危险未授权→4、``--dry-run``→0；
4. 危险闸门不落地：被拒绝时不得出现 ``deleted > 0`` / ``freed_bytes > 0``；
5. ``--health --json``：16 项、字段齐备、状态枚举；
6. 健壮性：stdin 关闭、``--clean ""``。

性能：全盘扫描每个子进程约 10~15 秒，因此 7 个探针各自只启动一次并缓存
（会话级 fixture），整套用例的墙钟时间约 80 秒。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

import pytest

#: 项目根目录（子进程的 cwd，保证 ``python -m pc_cleaner`` 用的是本仓库代码）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: ``--json`` 顶层 ``status`` 的文档枚举（本文件的判定依据）。
#: 与 ``pc_cleaner.service.ENVELOPE_SCHEMA["properties"]["status"]["enum"]`` 保持一致
#: （v0.9.4 起包含 MCP 侧的 preview / needs_repreview / restored 与只读操作的 ok）。
DOCUMENTED_STATUSES = frozenset(
    {
        "scan",
        "dry_run",
        "deleted",
        "partial",
        "preview",
        "needs_confirmation",
        "needs_repreview",
        "cancelled",
        "interrupted",
        "restored",
        "ok",
        "error",
    }
)

#: ``--health`` 单项 ``status`` 的合法取值
HEALTH_ITEM_STATUSES = frozenset({"ok", "warn", "error", "unknown"})

#: stdout 里绝不允许出现的人类文本（这些只该出现在人类可读输出里）
FORBIDDEN_HUMAN_MARKERS = ("确认", "已扫描", "完成：")

#: ANSI 转义起始字节：JSON 模式下必须没有
ANSI_ESCAPE = "\x1b"

#: 每个子进程的超时（秒）
CLI_TIMEOUT = 180

#: 探针用例：名字 → 参数。全部只读，或被危险闸门拒绝（绝不删除）。
PROBE_CASES: dict[str, tuple[str, ...]] = {
    "scan": ("--json",),
    "all_dry_run": ("--json", "--all", "--dry-run"),
    "invalid_category": ("--json", "--clean", "v094_no_such_category"),
    "risky_yes_gate": ("--json", "--clean", "browser_privacy", "--yes"),
    "health": ("--health", "--json"),
    "empty_category": ("--json", "--clean", ""),
    "version": ("--version",),
}

#: 需要满足「stdout 是单个合法 JSON 对象」的 5 种情况
JSON_CASES = ("scan", "all_dry_run", "invalid_category", "risky_yes_gate", "health")

#: 需要满足「status 落在文档枚举内」的情况（v0.9.4 起 ``--health --json`` 也在内：
#: 只读操作的 status 为 ``ok`` / ``error``，两者都已在 ENVELOPE_SCHEMA 的枚举里）。
ENVELOPE_STATUS_CASES = (
    "scan",
    "all_dry_run",
    "invalid_category",
    "risky_yes_gate",
    "health",
)


class CliResult(NamedTuple):
    """一次真实子进程调用的结果。"""

    exit_code: int
    stdout: str
    stderr: str
    args: tuple[str, ...]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _child_env(home: Path) -> dict[str, str]:
    """构造子进程环境：隔离配置目录 + 固定 UTF-8 输出。"""
    env = dict(os.environ)
    env["PC_CLEANER_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_cli(
    args: Sequence[str],
    home: Path,
    *,
    stdin: Any = subprocess.DEVNULL,
) -> CliResult:
    """真实子进程运行 ``python -m pc_cleaner <args>``。

    ``stdin=subprocess.DEVNULL`` 是默认值：既避免子进程卡在交互读取，
    也顺带验证"stdin 关闭时仍能正常返回"。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pc_cleaner", *args],
        cwd=str(PROJECT_ROOT),
        env=_child_env(home),
        stdin=stdin,
        capture_output=True,
        timeout=CLI_TIMEOUT,
    )
    return CliResult(
        exit_code=proc.returncode,
        stdout=proc.stdout.decode("utf-8", errors="replace"),
        stderr=proc.stderr.decode("utf-8", errors="replace"),
        args=tuple(args),
    )


def _payload(result: CliResult) -> dict[str, Any]:
    """把 stdout 解析成 JSON 对象（解析失败即用例失败，正是要断言的契约）。"""
    data = json.loads(result.stdout)
    assert isinstance(data, dict), f"顶层必须是 JSON 对象，实际 {type(data).__name__}"
    return data


def _json_string_spans(text: str) -> list[tuple[int, int]]:
    """返回 JSON 文本里所有字符串字面量的 ``(start, end)`` 区间。

    用来区分「混进 stdout 的人类旁白」与「JSON 数据里的中文字符串」：
    ``--health --json`` 的 ``health.items[*].advice`` 本身就是给人看的中文建议
    （例如「请确认防病毒实时保护处于开启状态」），它属于**数据**，不是旁白。
    """
    spans: list[tuple[int, int]] = []
    start = -1
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if start < 0:
            if char == '"':
                start = index
            index += 1
            continue
        if char == "\\":  # 转义序列跳过下一个字符
            index += 2
            continue
        if char == '"':
            spans.append((start, index))
            start = -1
        index += 1
    return spans


def _marker_outside_json_string(text: str, marker: str) -> int | None:
    """返回 ``marker`` 出现在 JSON 字符串**之外**的位置；没有则返回 ``None``。

    JSON 字符串之外的文本只能是结构字符（``{}[]:,"`` 与空白）或旁白——
    出现中文字符说明 stdout 混进了人类可读输出。
    """
    spans = _json_string_spans(text)
    index = text.find(marker)
    while index >= 0:
        if not any(start <= index < end for start, end in spans):
            return index
        index = text.find(marker, index + 1)
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def cleaner_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """子进程的 ``PC_CLEANER_HOME``：pytest 临时目录（会话级 ``tmp_path``）。

    用会话级是为了让下面的探针缓存只启动一次子进程（每次 ``--json`` 全盘扫描
    约 10~15 秒）；同时仍然与真实用户配置 / 历史完全隔离。
    """
    return tmp_path_factory.mktemp("pc_cleaner_home_v094")


@pytest.fixture(scope="session")
def probe(cleaner_home: Path) -> dict[str, CliResult]:
    """把 7 个探针用例各跑一次并缓存，避免重复的全盘扫描。"""
    return {name: _run_cli(args, cleaner_home) for name, args in PROBE_CASES.items()}


# ---------------------------------------------------------------------------
# 1. JSON 纯净性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", JSON_CASES)
def test_json_stdout_is_single_clean_json_object(
    probe: dict[str, CliResult], case: str
) -> None:
    """stdout 必须是**单个**合法 JSON 对象：无前缀/后缀、无 ANSI、无旁白文本。"""
    result = probe[case]
    out = result.stdout

    assert ANSI_ESCAPE not in out, f"{case}: stdout 含 ANSI 转义序列"
    for marker in FORBIDDEN_HUMAN_MARKERS:
        pos = _marker_outside_json_string(out, marker)
        assert pos is None, (
            f"{case}: stdout 在 JSON 字符串之外出现旁白文本 {marker!r}"
            f"（位置 {pos}）: {out[max(0, pos - 60) : pos + 60]!r}"
        )

    # 整个 stdout 直接 json.loads：任何多余前缀/后缀都会在这里失败
    data = json.loads(out)
    assert isinstance(data, dict), f"{case}: 顶层不是 JSON 对象"


@pytest.mark.parametrize(
    "case", ("scan", "all_dry_run", "invalid_category", "risky_yes_gate")
)
def test_json_payload_contains_no_human_text_markers_at_all(
    probe: dict[str, CliResult], case: str
) -> None:
    """4 个 ``--json`` 情况：人类文本标记连数据里都不该出现（最强断言）。

    ``--health --json`` 不在此列：它的 ``health.items[*].advice`` 本身就是中文
    建议文本，见 ``test_health_json_advice_is_data_not_narration``。
    """
    out = probe[case].stdout
    for marker in FORBIDDEN_HUMAN_MARKERS:
        assert marker not in out, f"{case}: stdout 出现人类文本 {marker!r}"


def test_health_json_advice_is_data_not_narration(probe: dict[str, CliResult]) -> None:
    """如实记录：``--health --json`` 的 JSON **数据**里可能出现「确认」。

    ``health.items[*].advice`` 是给人看的中文建议（例如「请确认防病毒实时保护
    处于开启状态」），它是合法的 JSON 字符串值，不属于混入 stdout 的旁白；
    因此对 health 只断言「旁白文本不出现在 JSON 字符串之外」。
    """
    out = probe["health"].stdout
    json.loads(out)  # 必须是合法 JSON

    for marker in FORBIDDEN_HUMAN_MARKERS:
        pos = _marker_outside_json_string(out, marker)
        assert pos is None, (
            f"health: JSON 字符串之外出现旁白文本 {marker!r}（位置 {pos}）"
        )


# ---------------------------------------------------------------------------
# 2. 信封字段
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", JSON_CASES)
def test_envelope_required_fields_and_ok_matches_exit_code(
    probe: dict[str, CliResult], case: str
) -> None:
    """顶层必须含 4 个契约字段，且 ``ok == (exit_code == 0)``。"""
    result = probe[case]
    payload = _payload(result)

    for field in ("schema_version", "ok", "status", "exit_code"):
        assert field in payload, f"{case}: 缺少信封字段 {field!r}"

    assert isinstance(payload["schema_version"], int) and not isinstance(
        payload["schema_version"], bool
    ), f"{case}: schema_version 必须是 int"
    assert isinstance(payload["ok"], bool), f"{case}: ok 必须是 bool"
    assert (
        isinstance(payload["status"], str) and payload["status"]
    ), f"{case}: status 必须是非空 str"
    assert isinstance(payload["exit_code"], int) and not isinstance(
        payload["exit_code"], bool
    ), f"{case}: exit_code 必须是 int"

    assert payload["ok"] == (payload["exit_code"] == 0), (
        f"{case}: ok={payload['ok']!r} 与 exit_code={payload['exit_code']!r} 不自洽"
    )
    # 进程退出码必须等于信封自报的 exit_code
    assert result.exit_code == payload["exit_code"], (
        f"{case}: 进程退出码 {result.exit_code} != 信封 exit_code {payload['exit_code']}"
    )


@pytest.mark.parametrize("case", ENVELOPE_STATUS_CASES)
def test_envelope_status_within_documented_enum(
    probe: dict[str, CliResult], case: str
) -> None:
    """``status`` 必须落在文档枚举内（``--health`` 见单独用例）。"""
    payload = _payload(probe[case])
    assert payload["status"] in DOCUMENTED_STATUSES, (
        f"{case}: status={payload['status']!r} 不在文档枚举 {sorted(DOCUMENTED_STATUSES)}"
    )


def test_health_json_status_is_ok_or_error(probe: dict[str, CliResult]) -> None:
    """``--health --json`` 的 ``status`` 为 ``"ok"``（无 error 项）或 ``"error"``。

    v0.9.4 起 ``"ok"`` 已加入 ``ENVELOPE_SCHEMA`` 的 status 枚举，因此本用例
    与 ``test_envelope_status_within_documented_enum`` 一致，不再是"记录不一致"。
    """
    payload = _payload(probe["health"])
    status = payload["status"]

    assert status in ("ok", "error"), f"意外的 health status: {status!r}"
    assert status in DOCUMENTED_STATUSES, f"status={status!r} 应在文档枚举内"


# ---------------------------------------------------------------------------
# 3. 退出码
# ---------------------------------------------------------------------------
def test_exit_code_version_is_zero(probe: dict[str, CliResult]) -> None:
    """``--version`` → 0，并打印形如 ``x.y.z`` 的版本号。"""
    result = probe["version"]
    assert result.exit_code == 0, result.stderr
    assert re.search(r"\d+\.\d+\.\d+", result.stdout), result.stdout


def test_exit_code_invalid_category_is_one(probe: dict[str, CliResult]) -> None:
    """``--json --clean <非法分类>`` → 1，``status="error"``。"""
    result = probe["invalid_category"]
    payload = _payload(result)

    assert result.exit_code == 1, result.stderr
    assert payload["exit_code"] == 1
    assert payload["ok"] is False
    assert payload["status"] == "error"


def test_exit_code_risky_without_risky_flag_is_four(
    probe: dict[str, CliResult],
) -> None:
    """``--json --clean browser_privacy --yes``（无 ``--risky``）→ 4 且需确认。"""
    result = probe["risky_yes_gate"]
    payload = _payload(result)

    assert result.exit_code == 4, result.stderr
    assert payload["exit_code"] == 4
    assert payload["ok"] is False
    assert payload["status"] == "needs_confirmation"
    assert payload["action"]["needs_confirmation"] is True


def test_exit_code_all_dry_run_is_zero(probe: dict[str, CliResult]) -> None:
    """``--json --all --dry-run`` → 0 且 ``status="dry_run"``。"""
    result = probe["all_dry_run"]
    payload = _payload(result)

    assert result.exit_code == 0, result.stderr
    assert payload["exit_code"] == 0
    assert payload["ok"] is True
    assert payload["status"] == "dry_run"


# ---------------------------------------------------------------------------
# 4. 危险闸门不落地 + dry-run 不落地
# ---------------------------------------------------------------------------
def test_dangerous_gate_reports_no_deletion(probe: dict[str, CliResult]) -> None:
    """闸门拒绝时：不得出现 ``deleted > 0`` / ``freed_bytes > 0``。"""
    payload = _payload(probe["risky_yes_gate"])
    action = payload["action"]

    assert action["needs_confirmation"] is True
    for field in ("deleted", "freed_bytes", "recycled_bytes", "failed"):
        value = action.get(field, 0) or 0
        assert int(value) == 0, f"闸门拒绝后 {field}={value!r}，必须为 0"
    # 拒绝原因必须机器可读地给出来，自动化才能据此决定下一步
    assert action.get("reasons"), "闸门拒绝时必须给出 reasons"


def test_dry_run_reports_no_deletion(probe: dict[str, CliResult]) -> None:
    """``--dry-run`` 只给预览：``action.dry_run=True``、``would_delete`` 是列表、零删除。"""
    payload = _payload(probe["all_dry_run"])
    action = payload["action"]

    assert action["dry_run"] is True
    assert isinstance(action["would_delete"], list)
    for field in ("deleted", "freed_bytes", "recycled_bytes"):
        value = action.get(field, 0) or 0
        assert int(value) == 0, f"dry-run 出现 {field}={value!r}"


# ---------------------------------------------------------------------------
# 5. --health --json
# ---------------------------------------------------------------------------
def test_health_items_count_is_sixteen(probe: dict[str, CliResult]) -> None:
    """``health.items`` 必须是 16 项，或在**慢项超预算**时退化为 15 项。

    v0.9.10 修正：``collect()`` 有明确的超时预算（``SLOW_BUDGET_SECONDS = 11``）——
    慢项累计超过预算时，剩余慢项会被**合并成一条 ``slow_skipped``**（这是设计，
    用于保证体检在慢机器上也能快速返回）。原断言硬要求恰好 16 项，于是在
    公共 CI runner（负载抖动大）上会间歇性失败：实测 windows/3.13 只拿到 14 项。
    现在按契约判定：要么 16 项齐全，要么出现且仅出现一条 ``slow_skipped``。
    """
    health = _payload(probe["health"])["health"]
    assert isinstance(health, dict)
    keys = [i.get("key") for i in health["items"]]
    assert len(keys) in (15, 16), keys
    if len(keys) == 15:
        assert keys.count("slow_skipped") == 1, (
            f"只有 15 项时必须恰好有一条 slow_skipped（慢项超预算），实际: {keys}"
        )
    else:
        assert "slow_skipped" not in keys, f"16 项齐全时不应出现 slow_skipped: {keys}"
    assert len(set(keys)) == len(keys), f"体检项 key 必须唯一: {keys}"


def test_health_items_have_required_shape_and_status_enum(
    probe: dict[str, CliResult],
) -> None:
    """每项含 ``key/label/status/detail``，且 ``status`` 在合法枚举内。"""
    health = _payload(probe["health"])["health"]
    for item in health["items"]:
        for field in ("key", "label", "status", "detail"):
            assert field in item, f"体检项缺少字段 {field!r}: {item!r}"
            assert isinstance(item[field], str), f"{field} 必须是 str: {item!r}"
        assert item["status"] in HEALTH_ITEM_STATUSES, (
            f"体检项 {item['key']!r} 的 status={item['status']!r} 非法"
        )
        assert item["key"], "体检项 key 不能为空"


def test_health_schema_version_present(probe: dict[str, CliResult]) -> None:
    """``health.schema_version`` 必须存在且为 int。"""
    health = _payload(probe["health"])["health"]
    assert "schema_version" in health
    assert isinstance(health["schema_version"], int) and not isinstance(
        health["schema_version"], bool
    )


# ---------------------------------------------------------------------------
# 6. 健壮性
# ---------------------------------------------------------------------------
def test_json_scan_with_stdin_closed_returns_normally(
    probe: dict[str, CliResult],
) -> None:
    """stdin 关闭（DEVNULL）时 ``--json`` 仍正常返回，不因 EOF 掉进交互菜单。"""
    result = probe["scan"]  # 所有探针子进程都用 stdin=subprocess.DEVNULL
    payload = _payload(result)

    assert result.exit_code == 0, result.stderr
    assert payload["status"] == "scan"
    assert payload["ok"] is True
    assert isinstance(payload.get("categories"), list)


def test_json_empty_clean_category_reports_error(probe: dict[str, CliResult]) -> None:
    """``--json --clean ""`` 必须**报错**（exit 1 / status "error"），而不是静默只扫描。

    v0.9.4 修复：空字符串此前被 ``_parse_keys`` 拆成空列表，等价于"没有 --clean"，
    于是退化为只扫描（exit 0 / status "scan"）——自动化场景下看起来像"清理成功"。
    现在明确返回错误并给出可用分类列表。
    """
    result = probe["empty_category"]
    payload = _payload(result)

    assert result.exit_code == 1, result.stderr
    assert payload["exit_code"] == 1
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["action"]["not_executed"] is True
    assert ANSI_ESCAPE not in result.stdout


# ---------------------------------------------------------------------------
# 7. 平台相关（Windows 专属字段）
# ---------------------------------------------------------------------------
def test_recycle_bin_size_field_matches_platform(probe: dict[str, CliResult]) -> None:
    """``recycle_bin_size_bytes`` 只在 Windows 上出现（Linux 上必须缺席）。"""
    payload = _payload(probe["scan"])
    if sys.platform == "win32":
        assert isinstance(payload.get("recycle_bin_size_bytes"), int)
    else:
        assert "recycle_bin_size_bytes" not in payload


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属字段")
def test_windows_scan_envelope_has_recycle_bin_size(
    probe: dict[str, CliResult],
) -> None:
    """Windows 上必须带回收站体积字段且非负。"""
    payload = _payload(probe["scan"])
    size = payload["recycle_bin_size_bytes"]
    assert isinstance(size, int) and size >= 0
