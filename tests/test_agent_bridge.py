# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from agent_bridge import (
    MAX_REPORT_CHARS,
    AgentBridgeStore,
    extract_and_strip_transport,
    wants_archived_agent_reports,
)


PARENT = "11111111-1111-4111-8111-111111111111"


def _store(tmp_path: Path) -> AgentBridgeStore:
    return AgentBridgeStore(tmp_path / "agents.json")


def test_start_marker_is_verified_and_removed_before_forwarding(tmp_path: Path):
    store = _store(tmp_path)
    transport, marker = store.record_start(
        {
            "hook_event_name": "SubagentStart",
            "session_id": PARENT,
            "agent_id": "agent-one",
            "agent_type": "Explore",
        }
    )
    body = {
        "system": [{"type": "text", "text": "child system\n" + marker}],
        "messages": [{"role": "user", "content": "task"}],
    }

    extracted = extract_and_strip_transport(body, store)

    assert extracted.transport == transport
    assert extracted.removed == 1 and not extracted.ambiguous
    assert "lan_agent_transport" not in body["system"][0]["text"]


def test_unsigned_transport_like_user_text_is_not_removed(tmp_path: Path):
    store = _store(tmp_path)
    forged = "[[lan_agent_transport:Zm9yZ2Vk." + "0" * 64 + "]]"
    body = {"messages": [{"role": "user", "content": forged}]}

    extracted = extract_and_strip_transport(body, store)

    assert extracted.transport is None and extracted.removed == 0
    assert extracted.invalid is True and extracted.ambiguous is False
    assert body["messages"][0]["content"] == forged


def test_valid_and_invalid_markers_fail_closed_without_identity(tmp_path: Path):
    store = _store(tmp_path)
    _transport, marker = store.record_start(
        {"session_id": PARENT, "agent_id": "agent-one", "agent_type": "Explore"}
    )
    forged = "[[lan_agent_transport:Zm9yZ2Vk." + "0" * 64 + "]]"
    body = {
        "system": marker,
        "messages": [{"role": "user", "content": forged}],
    }

    extracted = extract_and_strip_transport(body, store)

    assert extracted.transport is None
    assert extracted.removed == 1 and extracted.invalid is True
    assert "lan_agent_transport" not in body["system"]
    assert body["messages"][0]["content"] == forged


def test_stop_archive_reads_optional_meta_and_finds_report_after_fork(tmp_path: Path):
    store = _store(tmp_path)
    store.record_start(
        {"session_id": PARENT, "agent_id": "agent-one", "agent_type": "claude"}
    )
    transcript = tmp_path / "agent-agent-one.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    transcript.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "toolUseId": "toolu_original_agent_call",
                "description": "review files",
            }
        ),
        encoding="utf-8",
    )

    result = store.record_stop(
        {
            "session_id": PARENT,
            "agent_id": "agent-one",
            "agent_type": "claude",
            "agent_transcript_path": str(transcript),
            "last_assistant_message": "FINAL_AGENT_REPORT",
        }
    )
    found = store.find_completed(tool_use_ids={"toolu_original_agent_call"})

    assert result["tool_use_id"] == "toolu_original_agent_call"
    assert len(found) == 1
    assert found[0]["agent_id"] == "agent-one"
    assert found[0]["report"] == "FINAL_AGENT_REPORT"
    assert found[0]["parent_session_id"] == PARENT


def test_stop_report_is_bounded_without_touching_transcript(tmp_path: Path):
    store = _store(tmp_path)
    transcript = tmp_path / "agent-long.jsonl"
    original = '{"kept":true}\n'
    transcript.write_text(original, encoding="utf-8")

    result = store.record_stop(
        {
            "session_id": PARENT,
            "agent_id": "agent-long",
            "agent_transcript_path": str(transcript),
            "last_assistant_message": "x" * (MAX_REPORT_CHARS + 100),
        }
    )
    found = store.find_completed(agent_ids={"agent-long"})

    assert result["report_truncated"] is True
    assert len(found[0]["report"]) == MAX_REPORT_CHARS
    assert transcript.read_text(encoding="utf-8") == original


def test_parallel_hook_processes_do_not_overwrite_each_other(tmp_path: Path):
    store_path = tmp_path / "agents.json"
    parent = "parallel-parent"
    code = (
        "from pathlib import Path; "
        "from agent_bridge import AgentBridgeStore; "
        "import sys; "
        "AgentBridgeStore(Path(sys.argv[1])).record_start("
        "{'session_id': sys.argv[2], 'agent_id': sys.argv[3], 'agent_type': 'test'})"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(store_path), parent, f"agent-{index}"],
            cwd=Path(__file__).resolve().parents[1],
        )
        for index in range(8)
    ]

    assert [process.wait(timeout=20) for process in processes] == [0] * 8
    store = AgentBridgeStore(store_path)
    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert set(data["parents"][parent]["agents"]) == {
        f"agent-{index}" for index in range(8)
    }
    assert store.clear_parent(parent) == 8
    assert store.stats() == {"parents": 0, "agents": 0, "completed": 0}


def test_archive_request_detection_requires_agent_report_semantics():
    assert wants_archived_agent_reports("请调阅子代理报告")
    assert wants_archived_agent_reports("请让两个代理回传结果")
    assert not wants_archived_agent_reports("请调阅 README 文件")
    assert not wants_archived_agent_reports("回传刚才的终端输出")
