# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient


OLD_PARENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FORK_PARENT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TOOL_ID = "toolu_agent_review"
AGENT_ID = "agent-review"


def _body(*, notification: str | None = None) -> dict:
    messages: list[dict] = [
        {"role": "user", "content": "派代理审视"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": TOOL_ID,
                    "name": "Agent",
                    "input": {
                        "description": "审视配置",
                        "prompt": "审视并报告",
                        "subagent_type": "claude",
                    },
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": TOOL_ID,
                    "content": "Async agent launched successfully. (internal metadata)",
                }
            ],
        },
        {"role": "assistant", "content": "代理仍在运行"},
    ]
    if notification is not None:
        messages.append({"role": "user", "content": notification})
        messages.append({"role": "assistant", "content": "等待用户下一步"})
    messages.append({"role": "user", "content": "请调阅子代理报告"})
    return {
        "model": "alan",
        "stream": False,
        "metadata": {"user_id": {"session_id": FORK_PARENT}},
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": messages,
        "tools": [{"name": "Agent", "input_schema": {}}],
    }


def _archive(main, tmp_path: Path, report: str = "ARCHIVED_FINAL_REPORT") -> None:
    transcript = tmp_path / "agent-agent-review.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    transcript.with_suffix(".meta.json").write_text(
        json.dumps({"toolUseId": TOOL_ID, "description": "审视配置"}),
        encoding="utf-8",
    )
    main.agent_store.record_stop(
        {
            "session_id": OLD_PARENT,
            "agent_id": AGENT_ID,
            "agent_type": "claude",
            "agent_transcript_path": str(transcript),
            "last_assistant_message": report,
        }
    )


def _upstream_capture(captured: list[dict]):
    def fake_stream_chat_messages(**kwargs):
        captured.append(kwargs)

        async def events():
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "已整合报告"}
            yield {"event": "message_end", "conversation_id": "cid-fork"}

        return events()

    return fake_stream_chat_messages


def test_fork_recovers_completed_report_by_original_agent_tool_id(
    isolated_main, monkeypatch, tmp_path: Path
):
    main = isolated_main(user="archive-recovery-test")
    _archive(main, tmp_path)
    captured: list[dict] = []
    monkeypatch.setattr(main, "stream_chat_messages", _upstream_capture(captured))

    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages", json=_body(), headers={"x-api-key": "app-test"}
        )

    assert response.status_code == 200
    assert len(captured) == 1
    call = captured[0]
    assert "[[cc_agents:result_ready]]" in call["query"]
    assert "[[cc_agents:pending]]" not in call["query"]
    assert call["inputs"]["Current_Context"].count("ARCHIVED_FINAL_REPORT") == 1
    assert "<lan-agent-report" in call["inputs"]["Current_Context"]
    assert "ARCHIVED_FINAL_REPORT" not in call["query"]


def test_real_message_result_wins_without_archive_duplication(
    isolated_main, monkeypatch, tmp_path: Path
):
    main = isolated_main(user="archive-primary-test")
    _archive(main, tmp_path, report="ARCHIVE_MUST_NOT_APPEAR")
    notification = (
        "<system-reminder>\n"
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
        "<task-notification>\n"
        f"<task-id>{AGENT_ID}</task-id>\n"
        f"<tool-use-id>{TOOL_ID}</tool-use-id>\n"
        "<status>completed</status>\n"
        "<summary>Agent finished</summary>\n"
        "<result>PRIMARY_MESSAGE_REPORT</result>\n"
        "</task-notification>\n"
        "</system-reminder>"
    )
    captured: list[dict] = []
    monkeypatch.setattr(main, "stream_chat_messages", _upstream_capture(captured))

    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages",
            json=_body(notification=notification),
            headers={"x-api-key": "app-test"},
        )

    assert response.status_code == 200
    call = captured[0]
    assert call["inputs"]["System_Description"].count("PRIMARY_MESSAGE_REPORT") == 1
    assert "ARCHIVE_MUST_NOT_APPEAR" not in json.dumps(
        call["inputs"], ensure_ascii=False
    )


def test_archive_read_failure_fails_open_to_normal_message_chain(
    isolated_main, monkeypatch
):
    main = isolated_main(user="archive-fail-open-test")

    def broken_find(**_kwargs):
        raise OSError("archive unavailable")

    monkeypatch.setattr(main.agent_store, "find_completed", broken_find)
    captured: list[dict] = []
    monkeypatch.setattr(main, "stream_chat_messages", _upstream_capture(captured))

    with TestClient(main.app) as client:
        response = client.post(
            "/v1/messages", json=_body(), headers={"x-api-key": "app-test"}
        )

    assert response.status_code == 200
    assert len(captured) == 1
    assert "[[cc_agents:pending]]" in captured[0]["query"]
