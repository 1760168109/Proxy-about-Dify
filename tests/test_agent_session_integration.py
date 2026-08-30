# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi.testclient import TestClient


PARENT = "99999999-9999-4999-8999-999999999999"


def _child_body(marker: str, *, text: str = "identical review task") -> dict:
    return {
        "model": "alan",
        "stream": False,
        "metadata": {"user_id": {"session_id": PARENT}},
        "system": [
            {
                "type": "text",
                "text": (
                    "You are a Claude agent, built on Anthropic's Claude Agent SDK.\n"
                    + marker
                ),
            }
        ],
        "messages": [{"role": "user", "content": text}],
        "tools": [{"name": "Read", "input_schema": {}}],
    }


def _main_body(session_id: str, *, text: str = "main request") -> dict:
    return {
        "model": "alan",
        "stream": False,
        "metadata": {"user_id": {"session_id": session_id}},
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": text}],
    }


def test_two_identical_agents_get_two_cids_and_reuse_their_own_continuations(
    isolated_main, monkeypatch
):
    main = isolated_main(user="agent-session-test")
    _transport_one, marker_one = main.agent_store.record_start(
        {"session_id": PARENT, "agent_id": "agent-one", "agent_type": "claude"}
    )
    _transport_two, marker_two = main.agent_store.record_start(
        {"session_id": PARENT, "agent_id": "agent-two", "agent_type": "claude"}
    )

    expected_cids = ["cid-agent-one", "cid-agent-two", "cid-agent-one", "cid-agent-two"]
    captured: list[dict] = []

    def fake_stream_chat_messages(**kwargs):
        call_index = len(captured)
        captured.append(kwargs)

        async def events():
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "agent result"}
            yield {
                "event": "message_end",
                "conversation_id": expected_cids[call_index],
                "metadata": {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            }

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)

    with TestClient(main.app) as client:
        for marker, text in (
            (marker_one, "identical review task"),
            (marker_two, "identical review task"),
            (marker_one, "tool continuation one"),
            (marker_two, "tool continuation two"),
        ):
            response = client.post(
                "/v1/messages",
                json=_child_body(marker, text=text),
                headers={"x-api-key": "app-test"},
            )
            assert response.status_code == 200

    assert [call["conversation_id"] for call in captured] == [
        None,
        None,
        "cid-agent-one",
        "cid-agent-two",
    ]
    assert all("lan_agent_transport" not in call["query"] for call in captured)
    state = main.store.get_state("agent-session-test")
    assert state["current"] is None
    assert state["by_cc"] == {}
    assert state["by_agent"][PARENT]["agent-one"]["dify_cid"] == "cid-agent-one"
    assert state["by_agent"][PARENT]["agent-two"]["dify_cid"] == "cid-agent-two"


def test_hook_endpoints_return_context_and_archive_stop_report(isolated_main, tmp_path):
    main = isolated_main(user="hook-endpoint-test")
    transcript = tmp_path / "agent-hooked.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    with TestClient(main.app) as client:
        started = client.post(
            "/hooks/subagent-start",
            json={
                "hook_event_name": "SubagentStart",
                "session_id": PARENT,
                "agent_id": "agent-hooked",
                "agent_type": "Explore",
            },
        )
        stopped = client.post(
            "/hooks/subagent-stop",
            json={
                "hook_event_name": "SubagentStop",
                "session_id": PARENT,
                "agent_id": "agent-hooked",
                "agent_type": "Explore",
                "agent_transcript_path": str(transcript),
                "last_assistant_message": "HOOK_FINAL_REPORT",
            },
        )

    assert started.status_code == 200
    context = started.json()["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("[[lan_agent_transport:")
    assert stopped.status_code == 200
    assert stopped.json()["ok"] is True
    found = main.agent_store.find_completed(agent_ids={"agent-hooked"})
    assert found[0]["report"] == "HOOK_FINAL_REPORT"

    with TestClient(main.app) as client:
        cleared = client.post(
            "/sessions/new",
            json={"cc_session_id": PARENT},
        )
    assert cleared.status_code == 200
    assert main.agent_store.find_completed(agent_ids={"agent-hooked"}) == []


def test_main_inflight_other_session_keeps_mapping_without_overwriting_switch(
    isolated_main, monkeypatch
):
    user = "main-session-epoch-test"
    main = isolated_main(user=user)
    other_session = "88888888-8888-4888-8888-888888888888"

    def fake_stream_chat_messages(**kwargs):
        async def events():
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            # The request has already captured its scope token before this simulated switch.
            main.store.switch(user, "cid-selected", cc_session_id=PARENT)
            yield {"event": "message", "answer": "late other-session result"}
            yield {"event": "message_end", "conversation_id": "cid-other-late"}

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)

    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages",
            json=_main_body(other_session),
            headers={"x-api-key": "app-test"},
        )

    assert response.status_code == 200
    state = main.store.get_state(user)
    assert state["current"] == "cid-selected"
    assert state["cc_session_id"] == PARENT
    assert state["by_cc"][PARENT]["dify_cid"] == "cid-selected"
    assert state["by_cc"][other_session]["dify_cid"] == "cid-other-late"
