# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import httpx

import main


def test_identical_nonstream_retries_join_and_replay_one_dify_call(
    isolated_main, monkeypatch
):
    asyncio.run(_run_identical_nonstream_case(isolated_main, monkeypatch))


async def _run_identical_nonstream_case(isolated_main, monkeypatch) -> None:
    user = "singleflight-integration-user"
    isolated_main(user=user)

    started = asyncio.Event()
    release = asyncio.Event()
    upstream_calls = 0

    def fake_stream_chat_messages(**kwargs):
        async def events():
            nonlocal upstream_calls
            upstream_calls += 1
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            started.set()
            await release.wait()
            yield {"event": "message", "answer": "共享结果"}
            yield {
                "event": "message_end",
                "metadata": {"usage": {"prompt_tokens": 12, "completion_tokens": 3}},
            }

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    body = {
        "model": "alan",
        "metadata": {
            "user_id": {
                "session_id": "77777777-7777-4777-8777-777777777777"
            }
        },
        "messages": [{"role": "user", "content": "同一枪"}],
    }
    headers = {"x-api-key": "app-test"}

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first = asyncio.create_task(
                client.post("/v1/messages", json=body, headers=headers)
            )
            await started.wait()
            second = asyncio.create_task(
                client.post("/v1/messages", json=body, headers=headers)
            )
            await asyncio.sleep(0)
            release.set()
            first_response, second_response = await asyncio.gather(first, second)

            replay = await client.post("/v1/messages", json=body, headers=headers)

    assert first_response.status_code == second_response.status_code == 200
    assert replay.status_code == 200
    assert first_response.json() == second_response.json() == replay.json()
    assert upstream_calls == 1
    assert main.meter.snapshot()["opus_calls"] == 1


def test_same_session_different_first_turns_serialize_conversation_creation(
    isolated_main, monkeypatch
):
    asyncio.run(_run_session_serialization_case(isolated_main, monkeypatch))


async def _run_session_serialization_case(isolated_main, monkeypatch) -> None:
    user = "session-serialization-user"
    isolated_main(user=user)

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    upstream_calls = 0
    attached_ids: list[str | None] = []

    def fake_stream_chat_messages(**kwargs):
        async def events():
            nonlocal upstream_calls
            upstream_calls += 1
            attached_ids.append(kwargs.get("conversation_id"))
            if upstream_calls == 1:
                first_started.set()
                await release_first.wait()
            callback = kwargs.get("on_accepted")
            if callback:
                callback()
            yield {"event": "message", "answer": "结果"}
            yield {
                "event": "message_end",
                "conversation_id": "cid-{}".format(upstream_calls),
                "metadata": {"usage": {"prompt_tokens": 12, "completion_tokens": 3}},
            }

        return events()

    monkeypatch.setattr(main, "stream_chat_messages", fake_stream_chat_messages)
    sid = "88888888-8888-4888-8888-888888888888"
    headers = {"x-api-key": "app-test"}

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            def body(text: str) -> dict:
                return {
                    "model": "alan",
                    "metadata": {"user_id": {"session_id": sid}},
                    "messages": [{"role": "user", "content": text}],
                }
            first = asyncio.create_task(
                client.post("/v1/messages", json=body("第一枪"), headers=headers)
            )
            await first_started.wait()
            second = asyncio.create_task(
                client.post("/v1/messages", json=body("第二枪"), headers=headers)
            )
            await asyncio.sleep(0)
            assert upstream_calls == 1
            release_first.set()
            first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == second_response.status_code == 200
    assert upstream_calls == 2
    assert attached_ids == [None, "cid-1"]
