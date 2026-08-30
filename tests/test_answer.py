# -*- coding: utf-8 -*-
"""出流转换：外壳收口、node_finished 结构化、流式 SSE 组装。"""
from __future__ import annotations

import asyncio
import json

from answer import (
    DifyStreamAccum,
    dify_events_to_anthropic_sse,
    split_think_and_text,
)


def _envelope_answer() -> str:
    return json.dumps(
        {
            "reply": "写好了",
            "tool_calls": [
                {"name": "Write", "input": {"file_path": "C:\\a.md", "content": "x\ny"}}
            ],
        },
        ensure_ascii=False,
    )


def test_accum_finalize_envelope():
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": _envelope_answer()})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["envelope"] is True
    assert parts["stop_reason"] == "tool_use"
    assert parts["text"] == ""
    assert parts["tool_names"] == ["Write"]
    assert parts["structured_reply_dropped"] is True

    # 工具关的枪：只取 reply，calls 丢弃
    accum2 = DifyStreamAccum()
    accum2.ingest({"event": "message", "answer": _envelope_answer()})
    parts2 = accum2.finalize_parts(enable_tools=False)
    assert parts2["envelope"] is True
    assert parts2["tool_uses"] == []
    assert parts2["stop_reason"] == "end_turn"
    assert parts2["text"] == "写好了"


def test_accum_node_finished_structured_priority():
    """node_finished 直供 structured_output 时，answer 原始 JSON 让位于 reply。"""
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": _envelope_answer()})
    accum.ingest(
        {
            "event": "node_finished",
            "data": {
                "node_type": "llm",
                "outputs": {
                    "text": _envelope_answer(),
                    "structured_output": {
                        "reply": "来自节点的正文",
                        "tool_calls": [
                            {"name": "Read", "input": {"file_path": "C:\\n.md"}}
                        ],
                    },
                },
            },
        }
    )
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["envelope"] is True
    assert parts["text"] == ""
    assert parts["tool_names"] == ["Read"]
    assert parts["structured_reply_dropped"] is True


def test_structured_reply_cannot_smuggle_terminal_draft():
    accum = DifyStreamAccum()
    # 经 ingest 驱动，与生产同路；直接写 accum.structured_output 会让本测试
    # 在该属性改名或内化后误报红，而行为其实无恙。
    accum.ingest(
        {
            "event": "node_finished",
            "data": {
                "node_type": "llm",
                "outputs": {
                    "structured_output": {
                        "reply": "[[after_success]]结构化偷渡[[/after_success]]",
                        "tool_calls": [
                            {
                                "name": "Write",
                                "input": {"file_path": "C:\\a.md", "content": "x"},
                            }
                        ],
                    },
                },
            },
        }
    )
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["envelope"] is True
    assert parts["text"] == ""
    assert parts["after_success"] == ""
    assert parts["after_success_reason"] == "structured_envelope_unsupported"
    assert parts["structured_reply_dropped"] is True


def test_accum_plain_text_untouched():
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": "普通回答，无工具。"})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["envelope"] is False
    assert parts["stop_reason"] == "end_turn"
    assert parts["text"] == "普通回答，无工具。"


def test_accum_decodes_unicode_wire_after_tool_protocol_parsing():
    answer = (
        '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\a.md",'
        '"content":"灯⟦U+01F4A1⟧与字面⟦⟦U+01F4A1⟧"}}\n[[/tool_use]]'
    )
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": answer})

    parts = accum.finalize_parts(enable_tools=True, decode_unicode_wire=True)

    assert parts["tool_uses"][0]["input"]["content"] == "灯💡与字面⟦U+01F4A1⟧"
    assert parts["unicode_wire_decoded"] is True


def test_accum_terminal_write_hides_success_draft():
    answer = (
        '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\a.md","content":"x"}}\n'
        "[[/tool_use]]\n[[after_success]]\n已写入 a.md。\n[[/after_success]]"
    )
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": answer})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["tool_names"] == ["Write"]
    assert parts["text"] == ""
    assert parts["after_success"] == "已写入 a.md。"
    assert parts["after_success_chars"] == len("已写入 a.md。")


def test_accum_rejects_terminal_draft_for_read():
    answer = (
        '[[tool_use]]\n{"name":"Read","input":{"file_path":"C:\\\\a.md"}}\n'
        "[[/tool_use]]\n[[after_success]]\n读完了。\n[[/after_success]]"
    )
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": answer})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["tool_names"] == ["Read"]
    assert parts["after_success"] == ""
    assert "读完了" not in parts["text"]


def test_accum_rejects_terminal_when_any_tool_block_remains_malformed():
    answer = (
        '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\a.md","content":"x"}}\n'
        "[[/tool_use]]\n"
        "[[tool_use]]\n{broken tool block\n[[/tool_use]]\n"
        "[[after_success]]\n两份都写完了。\n[[/after_success]]"
    )
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": answer})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["tool_names"] == ["Write"]
    assert parts["after_success"] == ""
    assert "broken tool block" in parts["text"]


def test_accum_rejects_terminal_for_orphan_tool_close():
    answer = (
        '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\a.md","content":"x"}}\n'
        "[[/tool_use]]\n[[/tool_uses]]\n"
        "[[after_success]]完成。[[/after_success]]"
    )
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": answer})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["tool_names"] == ["Write"]
    assert parts["after_success"] == ""
    assert parts["after_success_reason"] == "protocol_residue"


def test_accum_rejects_terminal_draft_before_tool_block():
    answer = (
        "[[after_success]]完成。[[/after_success]]\n"
        '[[tool_use]]\n{"name":"Write","input":{"file_path":"C:\\\\a.md",'
        '"content":"x"}}\n[[/tool_use]]'
    )
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": answer})
    parts = accum.finalize_parts(enable_tools=True)
    assert parts["tool_names"] == ["Write"]
    assert parts["after_success"] == ""
    assert parts["after_success_reason"] == "draft_precedes_tool_protocol"


def test_accum_reasoning_separated():
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "reasoning_content": "想一想。", "answer": ""})
    accum.ingest({"event": "message", "answer": "答案。"})
    parts = accum.finalize_parts(enable_tools=False)
    assert parts["reasoning"] == "想一想。"
    assert parts["text"] == "答案。"


def test_accum_think_tags_fallback():
    accum = DifyStreamAccum()
    accum.ingest({"event": "message", "answer": "<think>推理</think>正文"})
    parts = accum.finalize_parts(enable_tools=False)
    assert parts["reasoning"] == "推理"
    assert parts["text"] == "正文"


def test_accum_workflow_error_visible():
    accum = DifyStreamAccum()
    accum.ingest(
        {"event": "workflow_finished", "data": {"status": "failed", "error": "File validation"}}
    )
    parts = accum.finalize_parts(enable_tools=False)
    assert parts["empty_upstream"] is True
    assert "File validation" in parts["text"]


def test_conversation_id_commits_only_after_successful_finalize():
    seen: list[str] = []
    failed = DifyStreamAccum()
    failed.ingest({"event": "error", "conversation_id": "cid-bad", "message": "nope"})
    failed.finalize_parts(on_conversation_id=seen.append)
    assert seen == []

    failed_workflow = DifyStreamAccum()
    failed_workflow.ingest(
        {"event": "message", "conversation_id": "cid-wf", "answer": "partial"}
    )
    failed_workflow.ingest(
        {
            "event": "workflow_finished",
            "conversation_id": "cid-wf",
            "data": {"status": "completed", "error": "late failure"},
        }
    )
    failed_workflow.finalize_parts(on_conversation_id=seen.append)
    assert seen == []

    successful = DifyStreamAccum()
    successful.ingest(
        {"event": "message", "conversation_id": "cid-ok", "answer": "done"},
        on_conversation_id=seen.append,
    )
    assert seen == []
    successful.finalize_parts(on_conversation_id=seen.append)
    successful.finalize_parts(on_conversation_id=seen.append)
    assert seen == ["cid-ok"]

    end_only = DifyStreamAccum()
    end_only.ingest(
        {
            "event": "message_end",
            "conversation_id": "cid-end-only",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        }
    )
    end_only.finalize_parts(on_conversation_id=seen.append)
    assert seen == ["cid-ok", "cid-end-only"]


def test_split_think_and_text():
    t, b = split_think_and_text("<thinking>a</thinking>hello")
    assert t == "a" and b == "hello"
    t2, b2 = split_think_and_text("no tags")
    assert t2 == "" and b2 == "no tags"


# ── 流式 SSE ──


def _run_sse(events: list[dict], **kw) -> list[str]:
    async def _gen():
        for ev in events:
            yield ev

    async def _collect():
        out = []
        async for line in dify_events_to_anthropic_sse(_gen(), model="alan", **kw):
            out.append(line)
        return out

    return asyncio.run(_collect())


def _payloads(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        for seg in line.strip().split("\n"):
            if seg.startswith("data:"):
                out.append(json.loads(seg[5:].strip()))
    return out


def test_sse_envelope_tool_gun():
    """结构化外壳整包 JSON：不得把原始 JSON 直播成正文；收口出 tool_use。"""
    ans = _envelope_answer()
    lines = _run_sse(
        [
            {"event": "message", "answer": ans[: len(ans) // 2]},
            {"event": "message", "answer": ans[len(ans) // 2 :]},
            {"event": "message_end", "metadata": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}},
        ],
        enable_tools=True,
    )
    evs = _payloads(lines)
    kinds = [e.get("type") for e in evs]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    # 原始 JSON 不得出现在 text_delta
    for e in evs:
        if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
            assert '"tool_calls"' not in e["delta"]["text"]
    tool_starts = [
        e
        for e in evs
        if e.get("type") == "content_block_start"
        and e.get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0]["content_block"]["name"] == "Write"
    # input_json_delta 拼回合法 JSON
    idx = tool_starts[0]["index"]
    partial = "".join(
        e["delta"]["partial_json"]
        for e in evs
        if e.get("type") == "content_block_delta"
        and e.get("index") == idx
        and e["delta"].get("type") == "input_json_delta"
    )
    assert json.loads(partial)["content"] == "x\ny"
    deltas = [e for e in evs if e.get("type") == "message_delta"]
    assert deltas[-1]["delta"]["stop_reason"] == "tool_use"


def test_sse_text_protocol_tool_gun_speculative():
    """文本协议：标记前的正文可直播，标记后不外泄。"""
    prose = "我来读文件。这是一段说明性的前缀正文，足够长以便直播。"
    tool = '\n[[tool_use]]\n{"name":"Read","input":{"file_path":"C:\\\\a.md"}}\n[[/tool_use]]\n'
    lines = _run_sse(
        [
            {"event": "message", "answer": prose},
            {"event": "message", "answer": tool},
            {"event": "message_end"},
        ],
        enable_tools=True,
    )
    evs = _payloads(lines)
    streamed_text = "".join(
        e["delta"]["text"]
        for e in evs
        if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"
    )
    assert "[[tool_use]]" not in streamed_text
    assert "file_path" not in streamed_text
    assert streamed_text.startswith("我来读文件。")
    tool_starts = [
        e
        for e in evs
        if e.get("type") == "content_block_start"
        and e.get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(tool_starts) == 1 and tool_starts[0]["content_block"]["name"] == "Read"


def test_sse_terminal_draft_never_streams_and_callback_receives_it():
    seen = []
    answer = (
        '[[tool_use]]\n{"name":"Edit","input":{"file_path":"C:\\\\a.md",'
        '"old_string":"a","new_string":"b"}}\n[[/tool_use]]\n'
        "[[after_success]]\n修改完成。\n[[/after_success]]"
    )
    lines = _run_sse(
        [
            {"event": "message", "answer": answer[:45]},
            {"event": "message", "answer": answer[45:]},
            {"event": "message_end"},
        ],
        enable_tools=True,
        on_final_parts=lambda parts: seen.append(dict(parts)),
    )
    payloads = _payloads(lines)
    wire = "\n".join(lines)
    assert "[[after_success]]" not in wire and "修改完成" not in wire
    assert seen and seen[0]["after_success"] == "修改完成。"
    assert any(
        event.get("type") == "content_block_start"
        and event.get("content_block", {}).get("name") == "Edit"
        for event in payloads
    )


def test_sse_no_tool_after_success_misuse_shows_text_without_markers():
    prefix = "普通回答的前缀足够长，会先直播给客户端。然后误写 "
    lines = _run_sse(
        [
            {"event": "message", "answer": prefix},
            {
                "event": "message",
                "answer": "[[after_success]]隐藏草案[[/after_success]]",
            },
            {"event": "message_end"},
        ],
        enable_tools=True,
    )
    payloads = _payloads(lines)
    text = "".join(
        event["delta"]["text"]
        for event in payloads
        if event.get("type") == "content_block_delta"
        and event.get("delta", {}).get("type") == "text_delta"
    )
    assert text == prefix + "\n\n隐藏草案"
    assert "[[after_success]]" not in "".join(lines)


def test_sse_tools_speculative_prefix_keeps_leading_whitespace_and_tail():
    answer = "\n" + ("A" * 80)
    lines = _run_sse(
        [
            {"event": "message", "answer": answer},
            {"event": "message_end"},
        ],
        enable_tools=True,
    )
    payloads = _payloads(lines)
    text = "".join(
        event["delta"]["text"]
        for event in payloads
        if event.get("type") == "content_block_delta"
        and event.get("delta", {}).get("type") == "text_delta"
    )
    assert text == answer


def test_sse_plain_chat_no_tools():
    lines = _run_sse(
        [
            {"event": "message", "answer": "你好"},
            {"event": "message", "answer": "，柳生。"},
            {"event": "message_end"},
        ],
        enable_tools=False,
    )
    evs = _payloads(lines)
    text = "".join(
        e["delta"]["text"]
        for e in evs
        if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"
    )
    assert text == "你好，柳生。"
    deltas = [e for e in evs if e.get("type") == "message_delta"]
    assert deltas[-1]["delta"]["stop_reason"] == "end_turn"


def test_sse_decodes_unicode_wire_across_dify_chunks():
    lines = _run_sse(
        [
            {"event": "message", "reasoning_content": "先辨认⟦U+01", "answer": ""},
            {"event": "message", "reasoning_content": "F4A1⟧再答", "answer": ""},
            {"event": "message", "answer": "结果⟦U+01"},
            {"event": "message", "answer": "F4A1⟧与字面⟦⟦"},
            {"event": "message_end"},
        ],
        enable_tools=False,
        decode_unicode_wire=True,
    )
    events = _payloads(lines)
    reasoning = "".join(
        event["delta"]["thinking"]
        for event in events
        if event.get("type") == "content_block_delta"
        and event.get("delta", {}).get("type") == "thinking_delta"
    )
    text = "".join(
        event["delta"]["text"]
        for event in events
        if event.get("type") == "content_block_delta"
        and event.get("delta", {}).get("type") == "text_delta"
    )

    assert reasoning == "先辨认💡再答"
    assert text == "结果💡与字面⟦"
