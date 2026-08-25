# -*- coding: utf-8 -*-
"""terminal-tool 穿过 /v1/messages：第二枪本地收尾且不重复计费。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from sessions import SessionStore
from terminal import TerminalStore


def _body(session_id: str) -> dict:
    return {
        "model": "alan",
        "stream": False,
        "metadata": {"user_id": {"session_id": session_id}},
        "messages": [{"role": "user", "content": "写一份本地说明"}],
        "tools": [
            {
                "name": "Write",
                "description": "write file",
                "input_schema": {
                    "type": "object",
                    "required": ["file_path", "content"],
                    "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
                },
            }
        ],
    }


def _sse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def _tool_from_sse(text: str) -> dict:
    events = _sse_events(text)
    start = next(
        event
        for event in events
        if event.get("type") == "content_block_start"
        and event.get("content_block", {}).get("type") == "tool_use"
    )
    index = start["index"]
    partial = "".join(
        event["delta"]["partial_json"]
        for event in events
        if event.get("type") == "content_block_delta"
        and event.get("index") == index
        and event.get("delta", {}).get("type") == "input_json_delta"
    )
    block = dict(start["content_block"])
    block["input"] = json.loads(partial)
    return block


@pytest.mark.parametrize("first_stream", [False, True])
def test_terminal_success_short_circuits_second_dify_call(
    isolated_main, monkeypatch, first_stream: bool
):
    user = "terminal-test-user"
    isolated_main(user=user, cache_min_chars=1)

    upstream_calls = []

    def fake_stream_chat_messages(**kwargs):
        upstream_calls.append(kwargs)
        callback = kwargs.get("on_accepted")
        if callback:
            callback()

        async def events():
            yield {
                "event": "message",
                "answer": (
                    '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\note.md",'
                    '"content":"terminal integration"}}\n[[/tool_use]]\n'
                    "[[after_success]]\n已成功落盘。\n[[/after_success]]"
                ),
            }
            yield {
                "event": "message_end",
                "metadata": {"usage": {"prompt_tokens": 20, "completion_tokens": 8}},
            }

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)

    sid = "11111111-1111-4111-8111-111111111111"
    first_body = _body(sid)
    first_body["stream"] = first_stream
    with TestClient(main.app) as client:
        first = client.post(
            "/v1/messages", json=first_body, headers={"x-api-key": "app-test"}
        )
        assert first.status_code == 200
        if first_stream:
            tool = _tool_from_sse(first.text)
            assert any(
                event.get("delta", {}).get("stop_reason") == "tool_use"
                for event in _sse_events(first.text)
            )
        else:
            message = first.json()
            tool = next(
                block for block in message["content"] if block.get("type") == "tool_use"
            )
            assert message["stop_reason"] == "tool_use"
        assert "after_success" not in first.text and "已成功落盘" not in first.text
        assert main.terminal_store.pending_count(user) == 1
        assert main.meter.snapshot()["opus_calls"] == 1

        second_body = _body(sid)
        second_body["stream"] = True
        second_body["messages"] = [
            first_body["messages"][0],
            {"role": "assistant", "content": [tool]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool["id"],
                        "content": "File created successfully at: C:\\note.md",
                    }
                ],
            },
        ]
        second = client.post(
            "/v1/messages", json=second_body, headers={"x-api-key": "app-test"}
        )
        assert second.status_code == 200
        assert "已成功落盘" in second.text
        assert '"stop_reason": "end_turn"' in second.text
        start = next(
            event for event in _sse_events(second.text) if event.get("type") == "message_start"
        )
        assert start["message"]["usage"]["input_tokens"] > 1

    assert len(upstream_calls) == 1
    assert main.meter.snapshot()["opus_calls"] == 1
    assert main.terminal_store.pending_count(user) == 0
    assert "terminal integration" in (main.read_cache.get(user, r"C:\note.md") or "")


def test_terminal_error_returns_to_dify_and_bills_continuation(
    isolated_main, tmp_path: Path, monkeypatch
):
    user = "terminal-fallback-user"
    isolated_main(user=user, cache_min_chars=1)

    upstream_calls = []

    def fake_stream_chat_messages(**kwargs):
        upstream_calls.append(kwargs)
        callback = kwargs.get("on_accepted")
        if callback:
            callback()
        call_no = len(upstream_calls)

        async def events():
            if call_no == 1:
                answer = (
                    '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\note.md",'
                    '"content":"terminal integration"}}\n[[/tool_use]]\n'
                    "[[after_success]]\n已成功落盘。\n[[/after_success]]"
                )
            else:
                answer = "写入被拒绝，文件没有落盘。"
            yield {"event": "message", "answer": answer}
            yield {"event": "message_end"}

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)

    sid = "22222222-2222-4222-8222-222222222222"
    first_body = _body(sid)
    with TestClient(main.app) as client:
        first = client.post(
            "/v1/messages", json=first_body, headers={"x-api-key": "app-test"}
        )
        tool = next(
            block for block in first.json()["content"] if block.get("type") == "tool_use"
        )
        second_body = _body(sid)
        second_body["messages"] = [
            first_body["messages"][0],
            {"role": "assistant", "content": [tool]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool["id"],
                        "content": "Permission denied",
                        "is_error": True,
                    }
                ],
            },
        ]
        second = client.post(
            "/v1/messages", json=second_body, headers={"x-api-key": "app-test"}
        )
        assert second.status_code == 200
        assert "写入被拒绝" in second.text
        assert "已成功落盘" not in second.text

    assert len(upstream_calls) == 2
    assert main.meter.snapshot()["opus_calls"] == 2
    assert main.terminal_store.pending_count(user) == 0


@pytest.mark.parametrize("stream", [False, True])
def test_terminal_register_storage_error_fails_open(
    isolated_main, tmp_path: Path, monkeypatch, stream: bool
):
    user = "terminal-register-error"
    terminal = TerminalStore(tmp_path / "terminal.json")
    isolated_main(user=user, cache_min_chars=1, terminal_store=terminal)

    def broken_register(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(terminal, "register", broken_register)

    def fake_stream_chat_messages(**kwargs):
        callback = kwargs.get("on_accepted")
        if callback:
            callback()

        async def events():
            yield {
                "event": "message",
                "answer": (
                    '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\note.md",'
                    '"content":"x"}}\n[[/tool_use]]\n'
                    "[[after_success]]完成。[[/after_success]]"
                ),
            }
            yield {"event": "message_end"}

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    body = _body("33333333-3333-4333-8333-333333333333")
    body["stream"] = stream
    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages", json=body, headers={"x-api-key": "app-test"}
        )
    assert response.status_code == 200
    if stream:
        assert _tool_from_sse(response.text)["name"] == "Write"
    else:
        assert any(
            block.get("name") == "Write" for block in response.json()["content"]
        )
    assert "after_success" not in response.text and "完成。" not in response.text
    assert terminal.pending_count(user) == 0


def test_terminal_resolve_storage_error_returns_to_dify(
    isolated_main, tmp_path: Path, monkeypatch
):
    user = "terminal-resolve-error"
    terminal = TerminalStore(tmp_path / "terminal.json")
    isolated_main(user=user, cache_min_chars=1, terminal_store=terminal)

    def broken_resolve(*_args, **_kwargs):
        raise OSError("state unavailable")

    monkeypatch.setattr(terminal, "resolve", broken_resolve)
    upstream_calls = []

    def fake_stream_chat_messages(**kwargs):
        upstream_calls.append(kwargs)
        callback = kwargs.get("on_accepted")
        if callback:
            callback()

        async def events():
            yield {"event": "message", "answer": "已由普通 Dify 续写处理。"}
            yield {"event": "message_end"}

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    body = _body("44444444-4444-4444-8444-444444444444")
    body["messages"] = [
        {"role": "assistant", "content": []},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_pending",
                    "content": "File created successfully at: C:\\note.md",
                }
            ],
        },
    ]
    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages", json=body, headers={"x-api-key": "app-test"}
        )
    assert response.status_code == 200
    assert "普通 Dify 续写处理" in response.text
    assert len(upstream_calls) == 1
    assert main.meter.snapshot()["opus_calls"] == 1


def test_session_endpoints_clear_only_the_intended_terminal_pending(
    isolated_main, tmp_path: Path, monkeypatch
):
    user = "terminal-session-lifecycle"
    sessions = SessionStore(tmp_path / "sessions.json")
    terminal = TerminalStore(tmp_path / "terminal.json")
    isolated_main(user=user, session_store=sessions, terminal_store=terminal)
    tool = {
        "type": "tool_use",
        "id": "w1",
        "name": "Write",
        "input": {"file_path": "C:\\a.md"},
    }
    sid1 = "55555555-5555-4555-8555-555555555555"
    sid2 = "66666666-6666-4666-8666-666666666666"
    assert terminal.register(user, sid1, [tool], "完成一。")
    assert terminal.register(user, sid2, [{**tool, "id": "w2"}], "完成二。")

    with TestClient(main.app) as client:
        # sid1 尚未进入 SessionStore.by_cc，也必须按显式请求清理。
        response = client.post("/sessions/new", json={"cc_session_id": sid1})
        assert response.status_code == 200
        assert terminal.pending_count(user) == 1

        # 无法确定 sid 的 switch 不得把其他窗口的 pending 全清。
        response = client.post(
            "/sessions/switch", json={"conversation_id": "cid-without-session"}
        )
        assert response.status_code == 200
        assert terminal.pending_count(user) == 1
        assert terminal.clear_session(user, sid2) == 1


def test_health_prunes_corrupt_terminal_epoch(isolated_main, tmp_path: Path):
    user = "terminal-health-corrupt"
    path = tmp_path / "terminal.json"
    path.write_text(
        json.dumps(
            {
                "users": {
                    user: {
                        "s": {
                            "after_success": "完成。",
                            "tools": {"w": "Write"},
                            "created_epoch": "not-a-number",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    isolated_main(user=user, terminal_store=TerminalStore(path))
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["terminal_pending"] == 0
