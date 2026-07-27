# -*- coding: utf-8 -*-
"""出站装配：need_read / 缓存重放 / 标记次序 / 结构化标记。"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from cache import (
    ReadCache,
    ingest_messages_into_cache,
    is_wasted_call,
    rehydrate_body_payloads,
    should_annotate_need_read,
)
from outbound import (
    annotate_query_for_images,
    format_agent_lifecycle_block,
    inject_marker_after_route,
    prepare_text_outbound,
)
from parse import parse_payload
from plan import build_plan


def _long(n: int = 20) -> str:
    return "".join("line {}\n".format(i) for i in range(n))


def test_route_marker_stays_first():
    q = inject_marker_after_route("[[cc_route:opus]]\nhello", "[[cc_need_read]] need")
    assert q.startswith("[[cc_route:opus]]")
    assert "[[cc_need_read]]" in q
    q2 = inject_marker_after_route(q, "[[cc_images:1]] img")
    assert q2.startswith("[[cc_route:opus]]")
    # 幂等
    assert inject_marker_after_route(q2, "[[cc_images:1]] img") == q2


def test_user_text_mentioning_marker_does_not_suppress_proxy_block():
    query = (
        "[[cc_route:opus]]\n"
        "用户正在讨论日志里的 [[cc_agents:pending]]，这不是代理注入的状态块。"
    )
    block = "[[cc_agents:pending]]\n仍有后台 Agent 未完成。"

    injected = inject_marker_after_route(query, block)

    assert injected.count("[[cc_agents:pending]]") == 2
    assert injected.splitlines()[1:] == [
        "[[cc_agents:pending]]",
        "仍有后台 Agent 未完成。",
        "用户正在讨论日志里的 [[cc_agents:pending]]，这不是代理注入的状态块。",
    ]


def test_need_read_judgement():
    assert should_annotate_need_read(r"@美术书籍\艺术与错觉.md 解释第二章", "")
    assert not should_annotate_need_read("contact me at user@example.com please", "")
    assert not should_annotate_need_read("just chatting no path", "")
    body_ti = "Result of calling the Read tool:\n" + _long(30)
    assert not should_annotate_need_read(r"@x\a.md q", body_ti)
    agent_ti = (
        '[tool_use] name=Agent id=a1\n{"description":"' + "调查" * 100 + '"}\n\n'
        "[tool_result] tool_use_id=a1\n"
        "Async agent launched successfully. "
        + "internal metadata " * 30
    )
    assert should_annotate_need_read(r"@x\a.md q", agent_ti)
    read_ti = (
        '[tool_use] name=Read id=r1\n{"file_path":"C:\\\\x\\\\a.md"}\n\n'
        "[tool_result] tool_use_id=r1\n" + _long(30)
    )
    assert not should_annotate_need_read(r"@x\a.md q", read_ti)


def test_wasted_and_cache_roundtrip():
    assert is_wasted_call("Wasted call — file unchanged since your last Read.")
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=40)
        path = r"C:\work\cfg.env"
        text = "KEY=value\n" * 5
        assert cache.put("u", path, text)
        assert cache.get("u", path) == text.strip()
        # 路径归一：正反斜杠、大小写
        assert cache.get("u", "c:/work/CFG.ENV") == text.strip()


def test_write_refreshes_read_cache_and_edit_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=20)
        path = r"C:\work\note.md"
        old = "old line\n" * 8
        written = "written line\n" * 8
        assert cache.put("u", path, old)
        write_body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "w1",
                            "name": "Write",
                            "input": {"file_path": path, "content": written},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "w1",
                            "content": "File created successfully",
                        }
                    ],
                },
            ]
        }
        ingest_messages_into_cache(write_body, cache, "u")
        assert cache.get("u", path) == written.strip()

        edit_body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "e1",
                            "name": "Edit",
                            "input": {
                                "file_path": path,
                                "old_string": "written line",
                                "new_string": "edited line",
                                "replace_all": True,
                            },
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "e1",
                            "content": "File updated successfully",
                        }
                    ],
                },
            ]
        }
        ingest_messages_into_cache(edit_body, cache, "u")
        assert "edited line" in (cache.get("u", path) or "")
        assert "written line" not in (cache.get("u", path) or "")
        # 同一历史每枪重扫，不得因二次应用找不到 old_string 而误删。
        ingest_messages_into_cache(edit_body, cache, "u")
        assert "edited line" in (cache.get("u", path) or "")


def test_unappliable_edit_invalidates_stale_cache():
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=20)
        path = r"C:\work\note.md"
        assert cache.put("u", path, "cached old content\n" * 4)
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "e1",
                            "name": "Edit",
                            "input": {
                                "file_path": path,
                                "old_string": "not present",
                                "new_string": "replacement",
                            },
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "e1",
                            "content": "File updated successfully",
                        }
                    ],
                },
            ]
        }
        ingest_messages_into_cache(body, cache, "u")
        assert cache.get("u", path) is None


def test_rehydrate_pipeline_does_not_restore_pre_edit_snapshot():
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=20)
        path = r"C:\work\note.md"
        old = "old line\n" * 8
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "r1",
                            "name": "Read",
                            "input": {"file_path": path},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "r1", "content": old}
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "e1",
                            "name": "Edit",
                            "input": {
                                "file_path": path,
                                "old_string": "old line",
                                "new_string": "new line",
                                "replace_all": True,
                            },
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "e1",
                            "content": "File updated successfully",
                        }
                    ],
                },
            ]
        }
        parsed = parse_payload(body)
        rehydrate_body_payloads(
            query_user=parsed["query_user"],
            tool_invocation=parsed["inputs"]["Tool_invocation"],
            history=parsed["inputs"].get("History", ""),
            body=body,
            cache=cache,
            user_id="u",
        )
        current = cache.get("u", path) or ""
        assert "new line" in current
        assert "old line" not in current


def test_next_wasted_read_rehydrates_post_edit_state():
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=20)
        path = r"C:\work\note.md"
        old = "old line\n" * 8
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "r1",
                            "name": "Read",
                            "input": {"file_path": path},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "r1", "content": old}
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "e1",
                            "name": "Edit",
                            "input": {
                                "file_path": path,
                                "old_string": "old line",
                                "new_string": "new line",
                                "replace_all": True,
                            },
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "e1",
                            "content": "File updated successfully",
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "r2",
                            "name": "Read",
                            "input": {"file_path": path},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "r2",
                            "content": "Wasted call - file unchanged since your last Read.",
                        }
                    ],
                },
            ]
        }
        parsed = parse_payload(body)
        rh = rehydrate_body_payloads(
            query_user=parsed["query_user"],
            tool_invocation=parsed["inputs"]["Tool_invocation"],
            history=parsed["inputs"].get("History", ""),
            body=body,
            cache=cache,
            user_id="u",
        )
        assert "new line" in rh["query_user"]
        assert "old line" not in rh["query_user"]
        assert "Wasted call" not in rh["query_user"]


def test_rehydrate_history_and_id_map():
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=40)
        path = r"C:\Users\00\Desktop\book.md"
        assert cache.put("Tangtang", path, _long(25))
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_hist",
                            "name": "Read",
                            "input": {"file_path": path},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_hist",
                            "content": "Wasted call — file unchanged since your last Read.",
                        }
                    ],
                },
            ]
        }
        wasted = (
            "[tool_result] tool_use_id=toolu_hist\n"
            "Wasted call — file unchanged since your last Read.\n"
        )
        rh = rehydrate_body_payloads(
            query_user=wasted,
            tool_invocation="",
            history="user:\n" + wasted,
            body=body,
            cache=cache,
            user_id="Tangtang",
        )
        assert rh["hits"]
        assert "rehydrated from proxy read_cache" in rh["query_user"]
        assert "rehydrated from proxy read_cache" in rh["history"]
        assert "line 0" in rh["history"]


def _tools_def() -> list:
    return [
        {
            "name": "Read",
            "description": "read",
            "input_schema": {"required": ["file_path"], "properties": {"file_path": {}}},
        }
    ]


def test_golden_need_read_prepare():
    body = {
        "model": "alan",
        "stream": False,
        "system": "You are Claude Code",
        "messages": [
            {"role": "user", "content": r"@美术书籍\艺术与错觉-第一部分.md 这是第二章的意思吗？"}
        ],
        "tools": _tools_def(),
    }
    plan = build_plan(body)
    parsed = parse_payload(body)
    with tempfile.TemporaryDirectory() as td:
        ob = prepare_text_outbound(
            body=body,
            plan=plan,
            parsed=parsed,
            user_id="Tangtang",
            read_cache=ReadCache(Path(td) / "c.json"),
        )
    assert ob.need_read
    assert ob.query.startswith("[[cc_route:opus]]")
    assert "[[cc_need_read]]" in ob.query


def test_golden_rehydrate_prepare():
    path = r"C:\Users\00\Desktop\美术\a.md"
    body = {
        "model": "alan",
        "stream": False,
        "system": "You are Claude Code",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": path}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": (
                            "Wasted call — file unchanged since your last Read. "
                            "Refer to that earlier tool_result instead."
                        ),
                    }
                ],
            },
        ],
        "tools": _tools_def(),
    }
    plan = build_plan(body)
    parsed = parse_payload(body)
    with tempfile.TemporaryDirectory() as td:
        cache = ReadCache(Path(td) / "c.json", min_chars=40)
        assert cache.put("Tangtang", path, _long(30))
        ob = prepare_text_outbound(
            body=body, plan=plan, parsed=parsed, user_id="Tangtang", read_cache=cache
        )
    assert ob.cache_hits
    assert "rehydrated from proxy read_cache" in ob.query_user
    assert not ob.need_read
    assert ob.query.startswith("[[cc_route:opus]]")


def test_sidecar_skips_need_read():
    body = {
        "model": "alan",
        "stream": False,
        "system": 'Generate a concise, sentence-case title. Return JSON with a single "title" field.',
        "messages": [{"role": "user", "content": r"@path\file.md something"}],
    }
    plan = build_plan(body)
    assert plan.route == "haiku"
    with tempfile.TemporaryDirectory() as td:
        ob = prepare_text_outbound(
            body=body,
            plan=plan,
            parsed=parse_payload(body),
            user_id="Tangtang",
            read_cache=ReadCache(Path(td) / "c.json"),
        )
    assert not ob.need_read
    assert ob.query.startswith("[[cc_route:haiku]]")


def test_struct_marker_injection():
    body = {
        "model": "alan",
        "stream": False,
        "system": "You are Claude Code",
        "messages": [{"role": "user", "content": "你好"}],
        "tools": _tools_def(),
    }
    plan_on = build_plan(body, tool_structured=True)
    ob = prepare_text_outbound(
        body=body, plan=plan_on, parsed=parse_payload(body), user_id="u", read_cache=None
    )
    lines = ob.query.split("\n")
    assert lines[0] == "[[cc_route:opus]]"
    assert "[[cc_struct:on]]" in ob.query
    assert ob.tool_structured is True

    plan_off = build_plan(body, tool_structured=False)
    ob2 = prepare_text_outbound(
        body=body, plan=plan_off, parsed=parse_payload(body), user_id="u", read_cache=None
    )
    assert "[[cc_struct:on]]" not in ob2.query
    assert ob2.tool_structured is False


def test_tool_continue_decoration_struct_aware():
    body = {
        "model": "alan",
        "stream": False,
        "system": "You are Claude Code",
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "读一下"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "文件内容 " * 20}
                ],
            },
        ],
        "tools": _tools_def(),
    }
    plan_on = build_plan(body, tool_structured=True)
    ob = prepare_text_outbound(
        body=body, plan=plan_on, parsed=parse_payload(body), user_id="u", read_cache=None
    )
    assert ob.is_tool_continue
    assert "tool_calls" in ob.query  # 续写壳按结构化文案
    plan_off = build_plan(body)
    ob2 = prepare_text_outbound(
        body=body, plan=plan_off, parsed=parse_payload(body), user_id="u", read_cache=None
    )
    assert ob2.is_tool_continue
    assert "[[tool_use]]" in ob2.query


def _agent_tools_def() -> list:
    return _tools_def() + [
        {
            "name": "Agent",
            "description": "delegate work",
            "input_schema": {
                "required": ["description", "prompt", "subagent_type"],
                "properties": {
                    "description": {},
                    "prompt": {},
                    "subagent_type": {},
                    "run_in_background": {},
                },
            },
        }
    ]


def _agent_call(tid: str = "a1", description: str = "调查项目结构") -> dict:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": tid,
                "name": "Agent",
                "input": {
                    "description": description,
                    "prompt": "调查并汇报",
                    "subagent_type": "Explore",
                },
            }
        ],
    }


def _agent_launch_result(tid: str = "a1") -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": "Async agent launched successfully. (internal metadata)",
            }
        ],
    }


def _agent_notification(
    tid: str = "a1",
    result: str | None = "RESULT_UNIQUE",
    *,
    status: str = "completed",
) -> dict:
    result_tag = f"<result>{result}</result>\n" if result is not None else ""
    return {
        "role": "system",
        "content": (
            "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
            "<task-notification>\n"
            "<task-id>task-1</task-id>\n"
            f"<tool-use-id>{tid}</tool-use-id>\n"
            r"<output-file>C:\temp\task-1.output</output-file>" "\n"
            f"<status>{status}</status>\n"
            "<summary>Agent finished</summary>\n"
            f"{result_tag}"
            "</task-notification>"
        ),
    }


def _agent_body(*, state: str = "pending", trailing_path_query: bool = False) -> dict:
    messages = [
        {"role": "user", "content": "调查项目"},
        _agent_call(),
        _agent_launch_result(),
    ]
    if state in ("result_ready", "result_carry"):
        messages.append(_agent_notification())
    if state == "result_carry":
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "r2",
                            "name": "Read",
                            "input": {"file_path": r"C:\work\note.md"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "r2",
                            "content": "FOLLOWUP_READ_RESULT",
                        }
                    ],
                },
            ]
        )
    if trailing_path_query:
        messages.extend(
            [
                {"role": "assistant", "content": "后台调查仍在进行"},
                {"role": "user", "content": r"@C:\work\note.md 请核对"},
            ]
        )
    return {
        "model": "alan",
        "stream": False,
        "system": "You are Claude Code",
        "messages": messages,
        "tools": _agent_tools_def(),
    }


def _prepare_agent_body(body: dict, *, structured: bool = False):
    plan = build_plan(body, tool_structured=structured)
    return prepare_text_outbound(
        body=body,
        plan=plan,
        parsed=parse_payload(body),
        user_id="u",
        read_cache=None,
    )


def test_agent_pending_marker_is_near_route_and_blocks_overlapping_work():
    ob = _prepare_agent_body(_agent_body(state="pending"))

    assert ob.query.splitlines()[0] == "[[cc_route:opus]]"
    assert "[[cc_agents:pending]]" in ob.query
    assert ob.query.index("[[cc_agents:pending]]") < ob.query.index("[[cc_tool_continue]]")
    assert "Bash/Read/Glob" in ob.query
    assert "依赖其报告" in ob.query


def test_agent_result_ready_points_to_system_description_without_copying_result():
    ob = _prepare_agent_body(_agent_body(state="result_ready"))

    assert "[[cc_agents:result_ready]]" in ob.query
    assert "系统说明" in ob.query
    assert "<task-notification>" in ob.query and "<result>" in ob.query
    assert "RESULT_UNIQUE" not in ob.query
    assert not any(field in ob.query for field in ("agentId", "task-id", "output-file"))
    assert ob.dify_inputs["System_Description"].count("RESULT_UNIQUE") == 1


def test_agent_result_carry_marks_current_tool_continuation():
    ob = _prepare_agent_body(_agent_body(state="result_carry"))

    assert "[[cc_agents:result_carry]]" in ob.query
    assert "本轮 tool result" in ob.query
    assert "FOLLOWUP_READ_RESULT" in ob.query


def test_abnormal_agent_termination_does_not_claim_complete_report():
    for state in ("result_ready", "result_carry"):
        body = _agent_body(state=state)
        notification_index = next(
            i for i, message in enumerate(body["messages"])
            if message.get("role") == "system"
            and "<task-notification>" in str(message.get("content"))
        )
        body["messages"][notification_index] = _agent_notification(
            status="failed", result=None
        )
        ob = _prepare_agent_body(body)

        assert f"[[cc_agents:{state}]]" in ob.query
        assert "failed" in ob.query
        assert "完整报告" not in ob.query
        assert "错误" in ob.query


def test_agent_lifecycle_marker_skips_sidecar_and_non_main_window():
    body = _agent_body(state="pending")
    non_main_plan = replace(build_plan(body), is_main_window=False)
    non_main = prepare_text_outbound(
        body=body,
        plan=non_main_plan,
        parsed=parse_payload(body),
        user_id="u",
        read_cache=None,
    )
    assert "[[cc_agents:" not in non_main.query

    sidecar_body = dict(body)
    sidecar_body["system"] = (
        'Generate a concise, sentence-case title. Return JSON with a single "title" field.'
    )
    sidecar_plan = build_plan(sidecar_body)
    assert sidecar_plan.is_sidecar_summary
    sidecar = prepare_text_outbound(
        body=sidecar_body,
        plan=sidecar_plan,
        parsed=parse_payload(sidecar_body),
        user_id="u",
        read_cache=None,
    )
    assert "[[cc_agents:" not in sidecar.query


def test_agent_marker_coexists_with_struct_need_read_and_images():
    body = _agent_body(state="pending", trailing_path_query=True)
    parsed = parse_payload(body)
    ob = prepare_text_outbound(
        body=body,
        plan=build_plan(body, tool_structured=True),
        parsed=parsed,
        user_id="u",
        read_cache=None,
    )
    ob.query = annotate_query_for_images(ob.query, 1)

    assert ob.need_read is True
    assert ob.query.splitlines()[0] == "[[cc_route:opus]]"
    for marker in (
        "[[cc_agents:pending]]",
        "[[cc_struct:on]]",
        "[[cc_need_read]]",
        "[[cc_images:1]]",
    ):
        assert ob.query.count(marker) == 1
    assert ob.query.index("[[cc_agents:pending]]") < ob.query.index(r"@C:\work\note.md")


def test_agent_marker_limits_descriptions_and_omits_internal_metadata():
    lifecycle = {
        "pending": [
            {
                "tool_use_id": f"toolu_secret_{i}",
                "task_id": f"task-secret-{i}",
                "output_file": rf"C:\temp\secret-{i}.output",
                "description": f"任务 {i} " + "很长的描述" * 30,
            }
            for i in range(5)
        ]
    }
    block = format_agent_lifecycle_block(lifecycle)

    assert "任务 0" in block and "任务 2" in block
    assert "任务 3" not in block and "另有 2 个 Agent 任务" in block
    assert "…" in block
    assert not any(token in block for token in ("toolu_secret", "task-secret", "secret-0.output"))
