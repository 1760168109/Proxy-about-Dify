# -*- coding: utf-8 -*-
"""入站上下文纯化：工具正文单载体与文件状态折叠。"""
from __future__ import annotations

from outbound import prepare_text_outbound
from parse import parse_payload
from plan import build_plan


PATH = r"C:\work\note.md"


def _read(tid: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": PATH}}
        ],
    }


def _result(tid: str, content: str, *, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return {"role": "user", "content": [block]}


def _agent_call(tid: str, description: str = "调查项目结构") -> dict:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": tid,
                "name": "Agent",
                "input": {
                    "description": description,
                    "prompt": "完整调查并汇报",
                    "subagent_type": "Explore",
                },
            }
        ],
    }


def _async_agent_result(tid: str, *, is_error: bool = False) -> dict:
    return _result(
        tid,
        "Async agent launched successfully. (This tool result is internal metadata.)",
        is_error=is_error,
    )


def _task_notification(
    tid: str,
    *,
    task_id: str = "task-1",
    status: str = "completed",
    summary: str = "Agent finished",
    result: str | None = "AGENT_RESULT_UNIQUE",
    role: str = "system",
) -> dict:
    result_tag = f"<result>{result}</result>\n" if result is not None else ""
    text = (
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
        "This is an automated background-task event, NOT a message from the user.\n\n"
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tid}</tool-use-id>\n"
        r"<output-file>C:\temp\agent.output</output-file>" "\n"
        f"<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n"
        f"{result_tag}"
        "</task-notification>"
    )
    if role == "user":
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    return {"role": role, "content": text}


def _lifecycle(parsed: dict, state: str) -> list[dict]:
    return parsed["agent_lifecycle"][state]


def test_current_tool_result_body_has_one_carrier():
    body = {
        "messages": [
            {"role": "user", "content": "读取笔记"},
            _read("r1"),
            _result("r1", "CURRENT_RESULT_UNIQUE\n正文"),
        ]
    }
    parsed = parse_payload(body)
    joined = "\n".join(
        [
            parsed["inputs"].get("Tool_invocation", ""),
            parsed["inputs"].get("History", ""),
            parsed["query_user"],
        ]
    )
    assert joined.count("CURRENT_RESULT_UNIQUE") == 1
    assert "CURRENT_RESULT_UNIQUE" in parsed["query_user"]
    assert "current result carried in sys.query" in parsed["inputs"]["Tool_invocation"]


def test_prior_tool_result_body_moves_out_of_history():
    body = {
        "messages": [
            {"role": "user", "content": "读取笔记"},
            _read("r1"),
            _result("r1", "PRIOR_RESULT_UNIQUE\n正文"),
            {"role": "assistant", "content": [{"type": "text", "text": "已经分析"}]},
            {"role": "user", "content": "继续"},
        ]
    }
    parsed = parse_payload(body)
    tools = parsed["inputs"]["Tool_invocation"]
    history = parsed["inputs"]["History"]
    assert tools.count("PRIOR_RESULT_UNIQUE") == 1
    assert "PRIOR_RESULT_UNIQUE" not in history
    assert "body carried in Tool_invocation" in history
    assert "PRIOR_RESULT_UNIQUE" in parsed["history_full"]


def test_tool_result_before_trailing_assistant_is_historical():
    body = {
        "messages": [
            {"role": "user", "content": "读取笔记"},
            _read("r1"),
            _result("r1", "HISTORICAL_RESULT_UNIQUE\n正文"),
            {"role": "assistant", "content": "已经分析"},
        ]
    }
    parsed = parse_payload(body)
    tools = parsed["inputs"]["Tool_invocation"]
    history = parsed["inputs"]["History"]
    assert "HISTORICAL_RESULT_UNIQUE" in tools
    assert "HISTORICAL_RESULT_UNIQUE" not in history
    assert "body carried in Tool_invocation" in history
    assert "current result carried in sys.query" not in tools


def test_new_full_read_supersedes_old_file_snapshot():
    body = {
        "messages": [
            {"role": "user", "content": "读取笔记"},
            _read("r1"),
            _result("r1", "OLD_FILE_SNAPSHOT\n旧正文"),
            _read("r2"),
            _result("r2", "NEW_FILE_SNAPSHOT\n新正文"),
            {"role": "assistant", "content": "读取完成"},
            {"role": "user", "content": "总结"},
        ]
    }
    tools = parse_payload(body)["inputs"]["Tool_invocation"]
    assert "OLD_FILE_SNAPSHOT" not in tools
    assert "superseded file state" in tools
    assert tools.count("NEW_FILE_SNAPSHOT") == 1


def test_successful_write_supersedes_read_but_keeps_new_content():
    new_content = "NEW_WRITE_STATE\n" + "新正文\n" * 10
    body = {
        "messages": [
            {"role": "user", "content": "更新笔记"},
            _read("r1"),
            _result("r1", "OLD_READ_STATE\n旧正文"),
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": PATH, "content": new_content},
                    }
                ],
            },
            _result("w1", "File created successfully"),
        ]
    }
    parsed = parse_payload(body)
    tools = parsed["inputs"]["Tool_invocation"]
    assert "OLD_READ_STATE" not in tools
    assert tools.count("NEW_WRITE_STATE") == 1
    assert "File created successfully" in parsed["query_user"]
    assert "File created successfully" not in tools


def test_edit_keeps_last_full_base_and_delta_until_reread():
    body = {
        "messages": [
            {"role": "user", "content": "修改笔记"},
            _read("r1"),
            _result("r1", "BASE_STATE_UNIQUE\nold text\nunchanged"),
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "Edit",
                        "input": {
                            "file_path": PATH,
                            "old_string": "old text",
                            "new_string": "new text",
                        },
                    }
                ],
            },
            _result("e1", "File updated successfully"),
        ]
    }
    tools = parse_payload(body)["inputs"]["Tool_invocation"]
    assert "BASE_STATE_UNIQUE" in tools
    assert "old text" in tools and "new text" in tools


def test_recap_query_uses_full_history_not_compact_refs():
    body = {
        "model": "alan",
        "messages": [
            {"role": "user", "content": "读取笔记"},
            _read("r1"),
            _result("r1", "RECAP_NEEDS_THIS_BODY\n正文"),
            {"role": "assistant", "content": "已读"},
            {
                "role": "user",
                "content": "The user stepped away and is coming back. Recap in under 40 words. Lead with the overall goal and current task.",
            },
        ],
    }
    parsed = parse_payload(body)
    assert "RECAP_NEEDS_THIS_BODY" not in parsed["inputs"]["History"]
    plan = build_plan(body)
    assert plan.kind == "recap"
    outbound = prepare_text_outbound(
        body=body, plan=plan, parsed=parsed, user_id="u", read_cache=None
    )
    assert "RECAP_NEEDS_THIS_BODY" in outbound.query
    assert not outbound.dify_inputs.get("Tool_invocation")


def test_agent_async_launch_is_pending():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "pending")] == ["a1"]
    assert _lifecycle(parsed, "result_ready") == []
    assert _lifecycle(parsed, "result_carry") == []
    assert "agent_pending=1" in parsed["notes"]


def test_agent_pending_persists_across_other_tool_results():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                    }
                ],
            },
            _result("b1", "C:\\work"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "pending")] == ["a1"]
    assert _lifecycle(parsed, "result_ready") == []


def test_matching_agent_notification_is_result_ready_and_single_carrier():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            _task_notification("a1", result="RESULT_UNIQUE"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "result_ready")] == ["a1"]
    assert _lifecycle(parsed, "pending") == []
    assert parsed["inputs"]["System_Description"].count("RESULT_UNIQUE") == 1
    assert "RESULT_UNIQUE" not in parsed["query_user"]
    assert "RESULT_UNIQUE" not in parsed["inputs"].get("History", "")
    assert "RESULT_UNIQUE" not in parsed["inputs"].get("Tool_invocation", "")


def test_notification_after_real_tool_result_does_not_replace_query_user():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "q1",
                        "name": "AskUserQuestion",
                        "input": {"questions": []},
                    }
                ],
            },
            _result("q1", "GENUINE_USER_TOOL_RESULT"),
            _task_notification("a1"),
        ]
    }
    parsed = parse_payload(body)

    assert "GENUINE_USER_TOOL_RESULT" in parsed["query_user"]
    assert "SYSTEM NOTIFICATION" not in parsed["query_user"]
    assert [x["tool_use_id"] for x in _lifecycle(parsed, "result_ready")] == ["a1"]


def test_notification_followed_by_tool_batch_result_is_result_carry():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            _task_notification("a1"),
            _read("r2"),
            _result("r2", "FOLLOWUP_READ_RESULT"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "result_carry")] == ["a1"]
    assert _lifecycle(parsed, "result_ready") == []
    assert "agent_result_carry=1" in parsed["notes"]


def test_notification_state_closes_after_terminal_assistant_answer():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            _task_notification("a1"),
            {"role": "assistant", "content": "已经整合报告并完成答复"},
            {"role": "user", "content": "下一件事"},
        ]
    }
    parsed = parse_payload(body)

    assert all(not parsed["agent_lifecycle"][state] for state in parsed["agent_lifecycle"])


def test_non_agent_background_notification_is_ignored():
    body = {
        "messages": [
            {"role": "user", "content": "运行命令"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {"command": "sleep 1"},
                    }
                ],
            },
            _result("b1", "Async command launched successfully"),
            _task_notification("b1", result="BASH_RESULT"),
        ]
    }
    parsed = parse_payload(body)

    assert all(not parsed["agent_lifecycle"][state] for state in parsed["agent_lifecycle"])


def test_synchronous_agent_result_is_not_async_pending():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _result("a1", "Synchronous Project Survey Report"),
        ]
    }
    parsed = parse_payload(body)

    assert all(not parsed["agent_lifecycle"][state] for state in parsed["agent_lifecycle"])


def test_two_agents_are_classified_independently():
    calls = _agent_call("a1", "调查结构")["content"] + _agent_call(
        "a2", "调查测试"
    )["content"]
    results = _async_agent_result("a1")["content"] + _async_agent_result("a2")[
        "content"
    ]
    body = {
        "messages": [
            {"role": "user", "content": "并行调查"},
            {"role": "assistant", "content": calls},
            {"role": "user", "content": results},
            _task_notification("a1", task_id="task-a1", result="A1_RESULT"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "result_ready")] == ["a1"]
    assert [x["tool_use_id"] for x in _lifecycle(parsed, "pending")] == ["a2"]


def test_legacy_user_agent_notification_is_promoted_out_of_user_query():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            {"role": "assistant", "content": "后台任务尚未返回"},
            _task_notification("a1", role="user", result="LEGACY_RESULT_UNIQUE"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "result_ready")] == ["a1"]
    assert _lifecycle(parsed, "pending") == []
    assert "SYSTEM NOTIFICATION" not in parsed["query_user"]
    assert "LEGACY_RESULT_UNIQUE" not in parsed.get("history_full", "")
    assert parsed["inputs"]["System_Description"].count("LEGACY_RESULT_UNIQUE") == 1


def test_user_pasted_notification_without_legacy_metadata_stays_user_text():
    pasted = _task_notification("a1", role="user", result="PASTED_RESULT")
    pasted["content"][0].pop("cache_control")
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            pasted,
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "pending")] == ["a1"]
    assert "PASTED_RESULT" in parsed["query_user"]
    assert "PASTED_RESULT" not in parsed["inputs"].get("System_Description", "")


def test_mixed_legacy_notification_preserves_following_user_block():
    mixed = _task_notification("a1", role="user", result="MIXED_LEGACY_RESULT")
    mixed["content"][0].pop("cache_control")
    mixed["content"].append(
        {"type": "text", "text": "COMPACT_PROMPT_UNIQUE: summarize the conversation"}
    )
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            {"role": "assistant", "content": "后台任务尚未返回"},
            mixed,
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "result_ready")] == ["a1"]
    assert "COMPACT_PROMPT_UNIQUE" in parsed["query_user"]
    assert "SYSTEM NOTIFICATION" not in parsed["query_user"]
    assert parsed["inputs"]["System_Description"].count("MIXED_LEGACY_RESULT") == 1


def test_closed_agent_report_does_not_reappear_in_later_tool_chain():
    body = {
        "messages": [
            {"role": "user", "content": "调查项目"},
            _agent_call("a1"),
            _async_agent_result("a1"),
            _task_notification("a1"),
            {"role": "assistant", "content": "已经整合报告并完成答复"},
            {"role": "user", "content": "读取另一个无关文件"},
            _read("r2"),
            _result("r2", "UNRELATED_READ_RESULT"),
        ]
    }
    parsed = parse_payload(body)

    assert all(not parsed["agent_lifecycle"][state] for state in parsed["agent_lifecycle"])


def test_error_or_out_of_order_async_result_does_not_create_pending():
    errored = parse_payload(
        {
            "messages": [
                _agent_call("a1"),
                _async_agent_result("a1", is_error=True),
            ]
        }
    )
    out_of_order = parse_payload(
        {
            "messages": [
                _async_agent_result("a1"),
                _agent_call("a1"),
            ]
        }
    )

    assert all(not errored["agent_lifecycle"][state] for state in errored["agent_lifecycle"])
    assert all(
        not out_of_order["agent_lifecycle"][state]
        for state in out_of_order["agent_lifecycle"]
    )


def test_notification_before_agent_call_does_not_hide_later_pending_state():
    body = {
        "messages": [
            _task_notification("a1", result="STALE_NOTIFICATION"),
            _agent_call("a1"),
            _async_agent_result("a1"),
        ]
    }
    parsed = parse_payload(body)

    assert [x["tool_use_id"] for x in _lifecycle(parsed, "pending")] == ["a1"]
    assert _lifecycle(parsed, "result_ready") == []


def test_latest_agent_notification_status_wins():
    body = {
        "messages": [
            _agent_call("a1"),
            _async_agent_result("a1"),
            _task_notification("a1", status="completed", result="FIRST_RESULT"),
            _task_notification("a1", status="failed", result=None),
        ]
    }
    parsed = parse_payload(body)

    ready = _lifecycle(parsed, "result_ready")
    assert len(ready) == 1 and ready[0]["status"] == "failed"
    assert ready[0]["has_result"] is False
