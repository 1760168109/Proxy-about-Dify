# -*- coding: utf-8 -*-
"""判枪：路由、旁路检测、流式裁决、结构化开关。"""
from __future__ import annotations

from plan import build_plan


def _title_body() -> dict:
    return {
        "model": "alan",
        "stream": True,
        "system": 'Generate a concise, sentence-case title. Return JSON with a single "title" field.',
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_title_gun():
    p = build_plan(_title_body())
    assert p.route == "haiku"
    assert p.kind == "title"
    assert not p.enable_tools and not p.attach_main
    assert p.trim_mode == "empty"
    assert p.query_mode == "title_fold"
    assert p.bill


def test_placeholder_gun_no_bill():
    body = {
        "model": "alan",
        "system": "You are a Claude agent, built on Anthropic's Claude Agent SDK",
        "messages": [
            {
                "role": "user",
                "content": "=== SYSTEM CONTEXT ===\nsys\n=== CONVERSATION HISTORY ===\nhist\n=== USER MESSAGE ===\nmsg\n",
            }
        ],
    }
    p = build_plan(body)
    assert p.is_placeholder and p.bill is False and p.route == "local"


def test_main_chat_opus_attaches():
    body = {
        "model": "alan",
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "你好"}],
        "tools": [{"name": "Read", "input_schema": {}}],
    }
    p = build_plan(body)
    assert p.route == "opus" and p.kind == "chat"
    assert p.enable_tools and p.attach_main and p.is_main_window


def test_smoke_token_forces_haiku():
    body = {
        "model": "alan",
        "system": "You are Claude Code",
        "messages": [{"role": "user", "content": "testandlife 冒烟一下"}],
    }
    p = build_plan(body)
    assert p.route == "haiku"
    assert p.trim_mode == "strip"
    assert not p.attach_main


def test_model_name_haiku_routes_haiku():
    body = {
        "model": "claude-haiku-x",
        "system": "You are Claude Code",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert build_plan(body).route == "haiku"


def test_subagent_mechanical_vs_analysis():
    base_system = "You are a Claude agent, built on Anthropic's Claude Agent SDK"
    mech = build_plan(
        {
            "model": "alan",
            "system": base_system,
            "messages": [{"role": "user", "content": "递归列出 C:/x 的目录结构"}],
        }
    )
    assert mech.is_subagent and mech.route == "haiku" and not mech.attach_main
    heavy = build_plan(
        {
            "model": "alan",
            "system": base_system,
            "messages": [{"role": "user", "content": "分析这个仓库的架构并给出评估"}],
        }
    )
    assert heavy.is_subagent and heavy.route == "opus" and not heavy.attach_main


def test_compact_detection():
    body = {
        "model": "alan",
        "system": "You are Claude Code",
        "messages": [
            {
                "role": "user",
                "content": (
                    "CRITICAL: Respond with TEXT ONLY.\n"
                    "Your summary should include the following sections:\n"
                    "Primary Request and Intent ..."
                ),
            }
        ],
        "tools": [{"name": "Read", "input_schema": {}}],
    }
    p = build_plan(body)
    assert p.kind == "compact" and p.route == "haiku"
    assert not p.enable_tools and not p.attach_main
    assert p.trim_mode == "empty"


# ── 流式裁决：省略 stream → JSON；Accept 头可请求 SSE ──


def _chat(stream=None) -> dict:
    b = {
        "model": "alan",
        "system": "You are Claude Code",
        "messages": [{"role": "user", "content": "hi"}],
    }
    if stream is not None:
        b["stream"] = stream
    return b


def test_stream_omitted_defaults_json():
    assert build_plan(_chat()).stream is False


def test_stream_omitted_accept_sse():
    assert build_plan(_chat(), accept_sse=True).stream is True


def test_stream_explicit_wins_over_accept():
    assert build_plan(_chat(stream=False), accept_sse=True).stream is False
    assert build_plan(_chat(stream=True)).stream is True


# ── 结构化开关：仅带工具的 opus 枪 ──


def test_tool_structured_only_for_opus_tool_guns():
    tools = [{"name": "Read", "input_schema": {}}]
    p = build_plan({**_chat(), "tools": tools}, tool_structured=True)
    assert p.tool_structured is True

    # 无工具 → 不结构化
    p2 = build_plan(_chat(), tool_structured=True)
    assert p2.tool_structured is False

    # haiku 枪 → 不结构化
    p3 = build_plan(
        {
            "model": "alan",
            "system": "You are Claude Code",
            "messages": [{"role": "user", "content": "testandlife hi"}],
            "tools": tools,
        },
        tool_structured=True,
    )
    assert p3.route == "haiku" and p3.tool_structured is False

    # 旁路摘要 → 工具关 → 不结构化
    p4 = build_plan({**_title_body(), "tools": tools}, tool_structured=True)
    assert p4.kind == "title" and not p4.enable_tools and p4.tool_structured is False
