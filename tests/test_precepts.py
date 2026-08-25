# -*- coding: utf-8 -*-
"""守则锚定：`经验.md` 中此前无任何测试守护的几条。

这些守则跨模块，共同点不是「测某个模块」而是「守某条用事故换来的判断」，
故不按模块分散，集中于此。每个测试的 docstring 首句即它所锚定的守则。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from answer import build_non_stream_message, build_non_stream_with_tools
from dify import DifyInputLimits
from parse import (
    INPUT_KEYS,
    extract_images_from_content,
    materialize_inputs,
    normalize_path,
)
from unicode_wire import decode_unicode_wire_text

_HEADERS = {"x-api-key": "app-test"}


def _no_upstream(recorder: list):
    """一个绝不该被调用的上游；被调用即在 recorder 留痕。"""

    def fake_stream_chat_messages(**kwargs):
        async def events():
            recorder.append(kwargs)
            yield {"event": "message", "answer": "不该到达上游"}

        return events()

    return fake_stream_chat_messages


def test_main_gun_materializes_all_keys_and_empty_string_is_an_override():
    """守则 5：inputs 空串是覆盖，不是缺失。主枪必须全键物化。"""
    sparse = {"claudeMd": "全局规则", "History": "旧历史"}

    main = materialize_inputs(sparse, mode=None)
    assert set(main) == set(INPUT_KEYS)
    assert len(main) == 13
    assert main["claudeMd"] == "全局规则"
    # 未提供的键必须以空串出现——它的作用是盖掉 Dify 侧上轮残留的会话变量。
    assert main["Memory"] == ""
    assert main["Current_Context"] == ""
    assert all(isinstance(v, str) for v in main.values())

    # 旁路摘要：全键空串，清空会话变量
    cleared = materialize_inputs(sparse, mode="empty")
    assert set(cleared) == set(INPUT_KEYS)
    assert set(cleared.values()) == {""}

    # haiku 子任务：丢掉全部解析键，且不留空壳
    stripped = materialize_inputs({**sparse, "外部键": "保留"}, mode="strip")
    assert stripped == {"外部键": "保留"}


def test_only_image_blocks_are_extracted_documents_stay_out():
    """守则 7：文档恒走 Read，files 仅图。非 image 块不得进 Dify files[]。"""
    content = [
        {"type": "text", "text": "看这个"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
        },
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "cGRm",
            },
        },
        {
            "type": "tool_result",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": "am9r",
                    },
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "eHg=",
                    },
                },
            ],
        },
    ]
    images = extract_images_from_content(content)
    assert [img["media_type"] for img in images] == ["image/png", "image/jpeg"]

    # 关掉 tool_result 递归时，内嵌图也不该漏进来
    top_only = extract_images_from_content(content, include_tool_result=False)
    assert [img["media_type"] for img in top_only] == ["image/png"]


def test_thinking_block_always_carries_an_empty_signature():
    """守则 11：thinking 签名不可伪造。signature 恒为空串，且不得省略该键。"""
    plain = build_non_stream_message(model="alan", text="正文", reasoning="推理")
    thinking = [b for b in plain["content"] if b.get("type") == "thinking"]
    assert len(thinking) == 1
    assert thinking[0]["thinking"] == "推理"
    assert thinking[0]["signature"] == ""

    with_tools = build_non_stream_with_tools(
        model="alan",
        text="正文",
        reasoning="推理",
        tool_uses=[
            {"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {}}
        ],
    )
    tool_thinking = [b for b in with_tools["content"] if b.get("type") == "thinking"]
    assert tool_thinking and tool_thinking[0]["signature"] == ""
    assert with_tools["stop_reason"] == "tool_use"

    # 无 reasoning 时不产空 thinking 块（否则 CC 会显示一个空思考区）
    bare = build_non_stream_message(model="alan", text="只有正文")
    assert all(b.get("type") != "thinking" for b in bare["content"])


def test_unc_prefix_is_part_of_path_identity():
    """守则 15：文件状态的同一性判定只能有一个定义。

    read_cache 的键与「最新完整文件状态」的键同出 normalize_path；UNC 的前导
    双斜杠必须保留，否则同一文件会在两套账里被算作两个。
    """
    unc = r"\\server\share\note.md"
    assert normalize_path(unc) == unc.casefold()
    assert normalize_path(unc).startswith("\\\\")

    # 单斜杠是另一个路径，不得与 UNC 混为一谈
    assert normalize_path(r"\server\share\note.md") != normalize_path(unc)

    # 引号、@ 前缀、正斜杠、重复分隔符都归一到同一个键
    assert normalize_path('"@//server/share//note.md"') == normalize_path(unc)
    assert normalize_path("C:/Work/a.py") == normalize_path(r'"C:\Work\a.py"')


# ── 端点面：以下两条守则此前只有单元层锚点，端点行为无人守 ──


def test_health_reports_both_dify_input_boundaries(isolated_main):
    async def run() -> str:
        main = isolated_main()
        async with main.lifespan(main.app):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get("/health")
        assert response.status_code == 200
        return response.json()["protocol"]["input_limits"]

    description = asyncio.run(run())
    assert "max_length" in description
    assert "sys.getsizeof <= 204800" in description


def test_oversized_variable_is_rejected_locally_without_calling_dify(
    isolated_main, monkeypatch
):
    """守则 21 端点面：已知字符上限超限即 400——不调 Dify、不计费、不裁剪。"""
    asyncio.run(_run_oversize_case(isolated_main(), monkeypatch))


async def _run_oversize_case(main, monkeypatch) -> None:
    upstream: list = []
    monkeypatch.setattr(main, "stream_chat_messages", _no_upstream(upstream))

    async def configured_limits(**_kwargs):
        return DifyInputLimits({"Tool_invocation": 100_000}, "test")

    monkeypatch.setattr(main, "_load_input_limits", configured_limits)

    # 历史轮的 tool_result 正文进 Tool_invocation（当前轮的会进 query）。
    # 应用发布的 max_length 是字符数；正文完整保留到校验点。
    oversized = "汉" * 110_000
    body = {
        "model": "alan",
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [
            {"role": "user", "content": "读这个文件"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_big",
                        "name": "Read",
                        "input": {"file_path": "C:\\big.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_big",
                        "content": oversized,
                    }
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "已读"}]},
            {"role": "user", "content": "继续分析"},
        ],
    }

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post("/v1/messages", json=body, headers=_HEADERS)

    assert response.status_code == 400
    error = response.json()
    assert error["type"] == "error"
    assert error["error"]["type"] == "invalid_request_error"
    assert "未被裁剪" in error["error"]["message"]
    assert upstream == []
    assert main.meter.snapshot()["opus_calls"] == 0


def test_119k_incident_payload_returns_400_without_triggering_compaction(
    isolated_main, monkeypatch
):
    """守则 21/22：表单未超字符数，也须阻止不可在下一轮恢复的持久变量。"""
    asyncio.run(_run_incident_size_case(isolated_main(), monkeypatch))


async def _run_incident_size_case(main, monkeypatch) -> None:
    captured: list[dict] = []

    async def configured_limits(**_kwargs):
        return DifyInputLimits(
            {"Tool_invocation": 233_333, "Current_Context": 233_333},
            "test",
        )

    def fake_stream_chat_messages(**kwargs):
        async def events():
            captured.append(kwargs)
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "正常进入上游"}
            yield {"event": "message_end"}

        return events()

    monkeypatch.setattr(main, "_load_input_limits", configured_limits)
    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    incident_text = "汉" * 119_173
    body = {
        "model": "alan",
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [
            {"role": "user", "content": "读取文件"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_incident",
                        "name": "Read",
                        "input": {"file_path": "C:\\incident.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_incident",
                        "content": incident_text,
                    }
                ],
            },
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "继续"},
        ],
    }

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post("/v1/messages", json=body, headers=_HEADERS)

    assert response.status_code == 400
    assert response.status_code != 413
    error = response.json()
    assert error["type"] == "error"
    assert error["error"]["type"] == "invalid_request_error"
    assert "持久变量内存占用" in error["error"]["message"]
    assert "下一轮 conversation" in error["error"]["message"]
    assert captured == []
    assert main.meter.snapshot()["opus_calls"] == 0


def test_119k_incident_payload_uses_published_shards_without_truncation(
    isolated_main, monkeypatch
):
    """守则 14/21：语义分流后仍超限时，只走已发布的同名无损分片。"""
    asyncio.run(_run_incident_shard_case(isolated_main(), monkeypatch))


async def _run_incident_shard_case(main, monkeypatch) -> None:
    captured: list[dict] = []

    async def configured_limits(**_kwargs):
        return DifyInputLimits(
            {
                "Tool_invocation": 233_333,
                "Tool_invocation_1": 233_333,
                "Tool_invocation_2": 233_333,
                "Current_Context": 233_333,
            },
            "test",
        )

    def fake_stream_chat_messages(**kwargs):
        async def events():
            captured.append(kwargs)
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "正常进入上游"}
            yield {"event": "message_end"}

        return events()

    monkeypatch.setattr(main, "_load_input_limits", configured_limits)
    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    incident_text = "汉" * 119_173
    body = {
        "model": "alan",
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [
            {"role": "user", "content": "读取文件"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_incident",
                        "name": "Read",
                        "input": {"file_path": "C:\\incident.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_incident",
                        "content": incident_text,
                    }
                ],
            },
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "继续"},
        ],
    }

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post("/v1/messages", json=body, headers=_HEADERS)

    assert response.status_code == 200
    assert len(captured) == 1
    sent = captured[0]["inputs"]
    assert sent["Tool_invocation"] == ""
    assert incident_text in sent["Tool_invocation_1"] + sent["Tool_invocation_2"]
    assert "[[cc_input_shards:on]]" in captured[0]["query"]
    assert all(
        len(sent[key]) < len(incident_text)
        for key in ("Tool_invocation_1", "Tool_invocation_2")
    )
    assert main.meter.snapshot()["opus_calls"] == 1


def test_profitable_unicode_wire_crosses_the_full_endpoint_losslessly(
    isolated_main, monkeypatch
):
    """守则 21/22：Dify 收线缆表示，Claude Code 必须无损收到原字符。"""
    asyncio.run(_run_unicode_wire_endpoint_case(isolated_main(), monkeypatch))


async def _run_unicode_wire_endpoint_case(main, monkeypatch) -> None:
    captured: list[dict] = []

    async def configured_limits(**_kwargs):
        return DifyInputLimits({"Tool_invocation": 233_333}, "test")

    def fake_stream_chat_messages(**kwargs):
        async def events():
            captured.append(kwargs)
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "仍是⟦U+01F4A1⟧"}
            yield {"event": "message_end"}

        return events()

    monkeypatch.setattr(main, "_load_input_limits", configured_limits)
    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    source = "x" * 52_000 + "💡"
    body = {
        "model": "alan",
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [
            {"role": "user", "content": "读取文件"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_wire",
                        "name": "Read",
                        "input": {"file_path": "C:\\wire.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_wire",
                        "content": source,
                    }
                ],
            },
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "继续"},
        ],
    }

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post("/v1/messages", json=body, headers=_HEADERS)

    assert response.status_code == 200
    assert len(captured) == 1
    sent = captured[0]["inputs"]["Tool_invocation"]
    assert "💡" not in sent
    assert "⟦U+01F4A1⟧" in sent
    assert decode_unicode_wire_text(sent).endswith(source)
    text = "".join(
        block.get("text", "")
        for block in response.json()["content"]
        if block.get("type") == "text"
    )
    assert text == "仍是💡"
    assert main.meter.snapshot()["opus_calls"] == 1


def test_upstream_success_is_not_delivery_when_client_detaches(
    isolated_main, monkeypatch, tmp_path: Path
):
    """守则 19：上游成功不等于答复送达。

    `workflow=succeeded` / `upstream done` 只证明 Dify → lan 收完；客户端是否还在，
    须由 delivery_status 单独记录，且断线不得取消已在运行的上游（照计一枪）。
    """
    asyncio.run(_run_detach_case(isolated_main(), monkeypatch, tmp_path))


async def _run_detach_case(main, monkeypatch, tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(main, "LOG_REQUESTS", True)
    monkeypatch.setattr(main, "LOG_DIR", log_dir)

    def fake_stream_chat_messages(**kwargs):
        async def events():
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "上游照常完成"}
            yield {
                "event": "message_end",
                "metadata": {"usage": {"prompt_tokens": 9, "completion_tokens": 4}},
            }

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)

    async def always_disconnected(_self) -> bool:
        return True

    monkeypatch.setattr(main.Request, "is_disconnected", always_disconnected)

    body = {
        "model": "alan",
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "一枪长任务"}],
    }

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post("/v1/messages", json=body, headers=_HEADERS)

    assert response.status_code == 200
    # 上游真的运行过，故照记一枪——计费只看是否进入上游（守则 6）
    assert main.meter.snapshot()["opus_calls"] == 1

    logged = json.loads((log_dir / "last_request.json").read_text(encoding="utf-8"))
    summary = logged["summary"]
    assert summary["delivery_status"] == "client_disconnected_before_delivery"
    # 上游侧的完成事实与交付事实分开记录，不可合并判断
    assert summary["response"]["stop_reason"] == "end_turn"
    assert summary["response"]["empty_upstream"] is False
