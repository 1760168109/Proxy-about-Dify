# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi.testclient import TestClient

from plan import build_plan
from status import build_action_status


STATUS_PROMPT = """Describe your most recent action in 3-5 words using present tense (-ing). Name the file or function, not the branch. Do not use tools.

Previous: "Reading three project skill files" — say something NEW.

Good: "Reading runAgent.ts"
Bad (too vague): "Investigating the issue"""


def _status_body(*, stream: bool = False) -> dict:
    return {
        "model": "alan",
        "stream": stream,
        "system": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
        "messages": [
            {"role": "user", "content": "审视三个项目文件"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "先读取文件。"},
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": r"C:\work\blank.md"},
                    },
                    {
                        "type": "tool_use",
                        "id": "r2",
                        "name": "Read",
                        "input": {"file_path": r"C:\work\translation-zh\SKILL.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "r1", "content": "one"},
                    {"type": "tool_result", "tool_use_id": "r2", "content": "two"},
                    {"type": "text", "text": STATUS_PROMPT},
                ],
            },
        ],
        "tools": [{"name": "Read", "input_schema": {}}],
    }


def test_action_status_is_an_explicit_unbilled_local_gun():
    plan = build_plan(_status_body())

    assert plan.kind == "status"
    assert plan.route == "local"
    assert plan.is_subagent
    assert not plan.bill
    assert not plan.enable_tools
    assert plan.attachment_scope == "none"
    assert not plan.attach_main


def test_unknown_status_wording_fails_open_to_normal_subagent_route():
    body = _status_body()
    body["messages"][-1]["content"][-1]["text"] = (
        "Summarize the latest action in a few words. Do not use tools."
    )

    plan = build_plan(body)

    assert plan.kind == "chat"
    assert plan.route == "opus"
    assert plan.bill


def test_main_window_quoting_status_prompt_is_not_short_circuited():
    body = _status_body()
    body["system"] = "You are Claude Code, Anthropic's official CLI for Claude."

    plan = build_plan(body)

    assert plan.kind == "chat"
    assert plan.route == "opus"
    assert plan.is_main_window
    assert plan.attachment_scope == "main"


def test_local_status_is_truthful_bounded_and_new():
    status = build_action_status(_status_body())

    words = status.split()
    assert 3 <= len(words) <= 5
    assert words[0].lower().endswith("ing")
    assert "SKILL.md" in status or "blank.md" in status
    assert status != "Reading three project skill files"


def test_action_status_skips_dify_meter_and_session_binding(
    isolated_main, monkeypatch
):
    main = isolated_main(user="local-status-test")
    upstream_calls: list[dict] = []

    def forbidden_upstream(**kwargs):
        upstream_calls.append(kwargs)
        raise AssertionError("local status must not call Dify")

    monkeypatch.setattr(main, "stream_chat_messages", forbidden_upstream)
    before = main.meter.snapshot()

    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages",
            json=_status_body(),
            headers={"x-api-key": "app-test"},
        )

    assert response.status_code == 200
    text = next(block["text"] for block in response.json()["content"] if block["type"] == "text")
    assert 3 <= len(text.split()) <= 5
    assert upstream_calls == []
    assert main.meter.snapshot() == before
    state = main.store.get_state("local-status-test")
    assert state["current"] is None and state["by_cc"] == {}
