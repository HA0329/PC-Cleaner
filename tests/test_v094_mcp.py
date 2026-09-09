"""v0.9.4：MCP 服务端（两阶段确认 / 只读默认 / 漂移检测）的单元测试。

这些测试**不执行任何删除**：涉及执行的用例都 monkeypatch 掉
``pc_cleaner.mcp.delete_targets`` / ``restore_paths``，只断言调用参数。
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from pc_cleaner import mcp
from pc_cleaner.models import CategoryResult, Target, TargetAction, TargetKind


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_manifests():
    mcp._MANIFESTS.clear()
    yield
    mcp._MANIFESTS.clear()


def _target(tmp_path: Path, name: str, size: int = 10) -> Target:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return Target(
        path=p,
        kind=TargetKind.FILE,
        action=TargetAction.DELETE,
        category="system_temp",
        size=size,
    )


def _category(targets: list[Target], key: str = "system_temp", risk: str = "safe") -> CategoryResult:
    return CategoryResult(
        key=key, label="测试分类", risk=risk, scanned=True, targets=list(targets)
    )


def _call(name: str, args: dict, *, allow_delete: bool = True) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    resp = mcp.handle_message(msg, allow_delete=allow_delete)
    assert resp is not None
    return resp


def _payload(resp: dict) -> dict:
    return resp["result"]["structuredContent"]


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------
def test_initialize_echoes_supported_protocol():
    resp = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "pc-cleaner"
    assert "tools" in resp["result"]["capabilities"]


def test_initialize_falls_back_for_unknown_protocol():
    resp = mcp.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "9.9"}}
    )
    assert resp["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_notification_has_no_response():
    assert mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_returns_method_not_found():
    resp = mcp.handle_message({"jsonrpc": "2.0", "id": 7, "method": "no/such"})
    assert resp["error"]["code"] == -32601


def test_tools_list_hides_write_tools_by_default():
    names = [
        t["name"]
        for t in mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    ]
    assert names == ["scan", "health", "history", "preview_delete"]
    assert "delete" not in names and "undo" not in names


def test_tools_list_exposes_write_tools_when_allowed():
    names = [
        t["name"]
        for t in mcp.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, allow_delete=True
        )["result"]["tools"]
    ]
    assert "delete" in names and "undo" in names


def test_serve_framing_and_parse_error():
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "{ not json",
        "",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    out = io.StringIO()
    code = mcp.serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    assert code == 0
    parsed = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    assert parsed[0]["result"] == {}
    assert parsed[1]["error"]["code"] == -32700
    assert "tools" in parsed[2]["result"]
    # stdout 里只有 JSON，没有多余人类文本
    assert out.getvalue().count("\n") == len(parsed)


# ---------------------------------------------------------------------------
# 两阶段确认
# ---------------------------------------------------------------------------
def test_preview_delete_returns_token_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp, "_scan_categories", lambda cats, deep: ([_category([_target(tmp_path, "a.bin")])], [])
    )
    called = {"n": 0}
    monkeypatch.setattr(mcp, "delete_targets", lambda *a, **k: called.__setitem__("n", 1))

    payload = _payload(_call("preview_delete", {"categories": ["system_temp"]}))
    assert payload["ok"] is True
    assert payload["status"] == "preview"
    assert payload["confirm_token"]
    assert payload["target_count"] == 1
    assert payload["needs_acknowledge_danger"] is False
    assert called["n"] == 0  # 预览绝不删除


def test_preview_delete_requires_categories():
    payload = _payload(_call("preview_delete", {}))
    assert payload["ok"] is False
    assert "categories" in payload["error"]


def test_preview_delete_rejects_unknown_category(monkeypatch):
    monkeypatch.setattr(mcp, "_scan_categories", lambda cats, deep: ([], ["no_such"]))
    payload = _payload(_call("preview_delete", {"categories": ["no_such"]}))
    assert payload["ok"] is False and "无法识别" in payload["error"]


def test_delete_requires_allow_delete_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp, "_scan_categories", lambda cats, deep: ([_category([_target(tmp_path, "a.bin")])], [])
    )
    token = _payload(_call("preview_delete", {"categories": ["system_temp"]}))["confirm_token"]
    resp = _call("delete", {"confirm_token": token}, allow_delete=False)
    assert resp["result"]["isError"] is True
    assert "未启用写能力" in _payload(resp)["error"]


def test_delete_rejects_unknown_token():
    payload = _payload(_call("delete", {"confirm_token": "nope"}))
    assert payload["ok"] is False and "无效" in payload["error"]


def test_delete_rejects_expired_token(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp, "_scan_categories", lambda cats, deep: ([_category([_target(tmp_path, "a.bin")])], [])
    )
    token = _payload(_call("preview_delete", {"categories": ["system_temp"]}))["confirm_token"]
    mcp._MANIFESTS[token].expires = time.time() - 1
    payload = _payload(_call("delete", {"confirm_token": token}))
    assert payload["ok"] is False and "过期" in payload["error"]


def test_delete_executes_when_manifest_unchanged(tmp_path, monkeypatch):
    targets = [_target(tmp_path, "a.bin", 10)]
    monkeypatch.setattr(
        mcp, "_scan_categories", lambda cats, deep: ([_category(targets)], [])
    )
    seen: dict = {}

    def fake_delete(tg, mode, **kwargs):
        seen["count"] = len(tg)
        seen["mode"] = getattr(mode, "value", mode)
        return {"deleted": len(tg), "failed": 0, "skipped": 0, "skipped_in_use": 0, "freed": 10, "recycled": 0}

    monkeypatch.setattr(mcp, "delete_targets", fake_delete)
    token = _payload(_call("preview_delete", {"categories": ["system_temp"]}))["confirm_token"]
    payload = _payload(_call("delete", {"confirm_token": token}))
    assert payload["ok"] is True
    assert payload["status"] == "deleted"
    assert seen == {"count": 1, "mode": "recycle"}
    # token 一次性
    again = _payload(_call("delete", {"confirm_token": token}))
    assert again["ok"] is False


def test_delete_detects_manifest_drift(tmp_path, monkeypatch):
    """自预览以来出现了**体积可观的新目标** → 拒绝执行（needs_repreview）。"""
    state = {"n": 0}

    def fake_scan(cats, deep):
        state["n"] += 1
        targets = [_target(tmp_path, "a.bin", 10)]
        if state["n"] > 1:  # 第二次扫描多出一个 2MB 的目标（超过阈值）
            targets.append(_target(tmp_path, "big.bin", 2 * 1024 * 1024))
        return ([_category(targets)], [])

    monkeypatch.setattr(mcp, "_scan_categories", fake_scan)
    monkeypatch.setattr(mcp, "delete_targets", lambda *a, **k: pytest.fail("不应执行删除"))
    token = _payload(_call("preview_delete", {"categories": ["system_temp"]}))["confirm_token"]
    payload = _payload(_call("delete", {"confirm_token": token}))
    assert payload["ok"] is False
    assert payload["status"] == "needs_repreview"
    assert payload["new_targets_bytes"] == 2 * 1024 * 1024


def test_delete_tolerates_vanished_targets_and_ignores_new_small_ones(tmp_path, monkeypatch):
    """消失的目标容忍；新出现的小目标不删也不阻断；只删预览过的路径。"""
    state = {"n": 0}
    a = _target(tmp_path, "a.bin", 10)
    b = _target(tmp_path, "b.bin", 20)

    def fake_scan(cats, deep):
        state["n"] += 1
        if state["n"] == 1:
            return ([_category([a, b])], [])
        # 第二次：b 消失，新增一个 1KB 的小目标
        return ([_category([a, _target(tmp_path, "small.bin", 1024)])], [])

    monkeypatch.setattr(mcp, "_scan_categories", fake_scan)
    seen: dict = {}

    def fake_delete(tg, mode, **kwargs):
        seen["paths"] = sorted(str(t.path).lower() for t in tg)
        return {"deleted": len(tg), "failed": 0, "skipped": 0, "skipped_in_use": 0, "freed": 10, "recycled": 0}

    monkeypatch.setattr(mcp, "delete_targets", fake_delete)
    token = _payload(_call("preview_delete", {"categories": ["system_temp"]}))["confirm_token"]
    payload = _payload(_call("delete", {"confirm_token": token}))
    assert payload["status"] == "deleted"
    assert payload["vanished"] == 1
    assert seen["paths"] == [str(a.path).lower()]  # 只删预览过的 a


def test_delete_requires_acknowledge_danger_for_risky(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_scan_categories",
        lambda cats, deep: ([_category([_target(tmp_path, "a.bin")], key="downloads", risk="risky")], []),
    )
    monkeypatch.setattr(mcp, "delete_targets", lambda *a, **k: pytest.fail("未确认不应执行"))
    token = _payload(_call("preview_delete", {"categories": ["downloads"]}))["confirm_token"]
    payload = _payload(_call("delete", {"confirm_token": token}))
    assert payload["ok"] is False and "acknowledge_danger" in payload["error"]


def test_preview_marks_permanent_mode_as_dangerous(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp, "_scan_categories", lambda cats, deep: ([_category([_target(tmp_path, "a.bin")])], [])
    )
    payload = _payload(
        _call("preview_delete", {"categories": ["system_temp"], "mode": "permanent"})
    )
    assert payload["needs_acknowledge_danger"] is True
    assert any("永久删除" in r for r in payload["dangerous"])


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------
def test_undo_requires_allow_delete():
    payload = _payload(_call("undo", {}, allow_delete=False))
    assert payload["ok"] is False and "未启用写能力" in payload["error"]


def test_undo_dry_run_lists_paths(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "load_history",
        lambda: [
            {
                "session_id": "s1",
                "ts": "2026-01-01 00:00:00",
                "mode": "recycle",
                "targets": [{"path": "C:/tmp/x.bin", "size": 1}],
            }
        ],
    )
    monkeypatch.setattr(mcp, "restore_paths", lambda paths: pytest.fail("dry_run 不应恢复"))
    payload = _payload(_call("undo", {"dry_run": True}))
    assert payload["status"] == "dry_run"
    assert payload["would_restore"] == ["C:/tmp/x.bin"]


def test_undo_rejects_permanent_session(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "load_history",
        lambda: [{"session_id": "s1", "mode": "permanent", "targets": []}],
    )
    payload = _payload(_call("undo", {}))
    assert payload["ok"] is False and "无法恢复" in payload["error"]
