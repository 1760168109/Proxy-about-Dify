# -*- coding: utf-8 -*-
"""terminal-tool 待决状态：精确成功才本地释放，其余回 Dify。"""
from __future__ import annotations

import json
from pathlib import Path

from terminal import TerminalStore


def _tool(tid: str, name: str = "Write", path: str | None = None) -> dict:
    return {
        "type": "tool_use",
        "id": tid,
        "name": name,
        "input": {"file_path": path or "C:\\{}.md".format(tid)},
    }


def _body(*blocks: dict, mixed_text: str = "") -> dict:
    content = list(blocks)
    if mixed_text:
        content.append({"type": "text", "text": mixed_text})
    return {"messages": [{"role": "user", "content": content}]}


def _result(tid: str, text: str, *, is_error: bool = False) -> dict:
    out = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if is_error:
        out["is_error"] = True
    return out


def test_terminal_write_persists_and_resolves_once(tmp_path: Path):
    path = tmp_path / "terminal.json"
    store = TerminalStore(path)
    assert store.register("u", "s1", [_tool("w1")], "已写入。")
    assert store.pending_count("u") == 1

    # 模拟代理在工具执行期间重启。
    reloaded = TerminalStore(path)
    resolution = reloaded.resolve(
        "u", "s1", _body(_result("w1", "File created successfully at: C:\\a.md"))
    )
    assert resolution.status == "success"
    assert resolution.text == "已写入。"
    assert resolution.tool_names == ("Write",)
    assert reloaded.pending_count("u") == 0
    assert reloaded.resolve("u", "s1", _body(_result("w1", "File created successfully"))).status == "none"


def test_terminal_multiple_tools_require_exact_explicit_success(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    tools = [_tool("w1", "Write"), _tool("e1", "Edit")]
    assert store.register("u", "s1", tools, "两处均已更新。")
    resolution = store.resolve(
        "u",
        "s1",
        _body(
            _result("w1", "The file has been updated successfully."),
            _result("e1", "File updated successfully"),
        ),
    )
    assert resolution.status == "success"
    assert resolution.text == "两处均已更新。"


def test_terminal_error_or_unknown_result_falls_back(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    assert store.register("u", "s1", [_tool("w1")], "完成。")
    denied = store.resolve(
        "u", "s1", _body(_result("w1", "Permission denied", is_error=True))
    )
    assert denied.status == "fallback" and "not_explicit_success" in denied.reason

    assert store.register("u", "s1", [_tool("w2")], "完成。")
    unknown = store.resolve("u", "s1", _body(_result("w2", "Tool returned.")))
    assert unknown.status == "fallback"


def test_terminal_negated_success_never_short_circuits(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    for i, text in enumerate(
        (
            "写入未成功",
            "文件未成功写入",
            "File was not updated successfully",
            "Edit was not updated successfully",
        )
    ):
        tid = "w{}".format(i)
        assert store.register("u", "s1", [_tool(tid)], "完成。")
        assert store.resolve("u", "s1", _body(_result(tid, text))).status == "fallback"


def test_terminal_known_success_ignores_keywords_inside_path(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    assert store.register("u", "s1", [_tool("w1")], "完成。")
    result = store.resolve(
        "u",
        "s1",
        _body(_result("w1", r"File created successfully at: C:\error\invalid.md")),
    )
    assert result.status == "success"


def test_terminal_mixed_user_text_falls_back_and_is_consumed(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    assert store.register("u", "s1", [_tool("e1", "Edit")], "已修改。")
    resolution = store.resolve(
        "u",
        "s1",
        _body(_result("e1", "File updated successfully"), mixed_text="再解释一下"),
    )
    assert resolution.status == "fallback"
    assert resolution.reason == "mixed_current_user"
    assert store.pending_count("u") == 0


def test_terminal_session_isolation_and_ineligible_batch(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    assert not store.register("u", "s1", [_tool("r1", "Read")], "读完。")
    assert not store.register("u", None, [_tool("w0")], "完成。")
    assert store.register("u", "s1", [_tool("w1")], "完成。")
    assert store.resolve("u", "s2", _body(_result("w1", "File created successfully"))).status == "none"
    assert store.pending_count("u") == 1
    assert store.clear_session("u", "s1") == 1
    assert store.pending_count("u") == 0
    assert store.register("u", "s1", [_tool("w2")], "完成。")
    assert store.register("u", "s2", [_tool("w3")], "完成。")
    assert store.clear_all("u") == 2
    assert store.pending_count("u") == 0


def test_terminal_rejects_conflicting_mutations_to_same_path(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    tools = [
        _tool("w1", "Write", r"C:\Work\same.md"),
        _tool("e1", "Edit", r"c:/work/same.md"),
    ]
    assert not store.register("u", "s1", tools, "两处都完成。")


def test_terminal_result_set_must_match_exactly(tmp_path: Path):
    store = TerminalStore(tmp_path / "terminal.json")
    tools = [_tool("w1"), _tool("w2")]
    assert store.register("u", "s1", tools, "完成。")
    partial = store.resolve(
        "u", "s1", _body(_result("w1", "File created successfully at: C:\\w1.md"))
    )
    assert partial.status == "fallback"
    assert partial.reason == "tool_result_set_mismatch"
    assert store.pending_count("u") == 0

    assert store.register("u", "s1", [_tool("w3")], "完成。")
    extra = store.resolve(
        "u",
        "s1",
        _body(
            _result("w3", "File created successfully at: C:\\w3.md"),
            _result("other", "File created successfully at: C:\\other.md"),
        ),
    )
    assert extra.status == "fallback"
    assert extra.reason == "tool_result_set_mismatch"


def test_terminal_expiry_is_fixed_and_pruned(tmp_path: Path):
    now = [1_000.0]
    store = TerminalStore(
        tmp_path / "terminal.json", ttl_seconds=60, clock=lambda: now[0]
    )
    assert store.register("u", "s1", [_tool("w1")], "完成。")
    now[0] += 61
    assert store.pending_count("u") == 0
    assert json.loads((tmp_path / "terminal.json").read_text(encoding="utf-8"))["users"] == {}


def test_terminal_corrupt_epoch_is_pruned_instead_of_crashing(tmp_path: Path):
    path = tmp_path / "terminal.json"
    path.write_text(
        json.dumps(
            {
                "users": {
                    "u": {
                        "s1": {
                            "after_success": "完成。",
                            "tools": {"w1": "Write"},
                            "created_epoch": "bad",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    store = TerminalStore(path)
    assert store.pending_count("u") == 0
    assert json.loads(path.read_text(encoding="utf-8"))["users"] == {}
