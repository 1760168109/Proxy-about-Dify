# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import base64

import dify
import httpx
from outbound import annotate_query_for_images


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def test_image_fingerprint_covers_middle_bytes():
    first = _b64(b"head" + b"A" * 10_000 + b"tail")
    second = _b64(b"head" + b"B" * 10_000 + b"tail")
    assert dify._b64_fingerprint(first) != dify._b64_fingerprint(second)


def test_parse_input_char_limits_uses_published_form_lengths():
    payload = {
        "user_input_form": [
            {
                "paragraph": {
                    "variable": "Tool_invocation",
                    "max_length": 233_333,
                }
            },
            {
                "text-input": {
                    "variable": "Current_Context",
                    "max_length": "48000",
                }
            },
            {
                "paragraph": {
                    "variable": "Tool_invocation_1",
                    "max_length": 233_333,
                }
            },
            {"select": {"variable": "Mode", "options": ["a", "b"]}},
        ]
    }

    assert dify.parse_input_char_limits(payload) == {
        "Tool_invocation": 233_333,
        "Current_Context": 48_000,
        "Tool_invocation_1": 233_333,
    }


def test_parameter_cache_reuses_success_and_fails_open_with_stale_limits():
    asyncio.run(_run_parameter_cache_case())


async def _run_parameter_cache_case() -> None:
    calls = 0
    fail = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/parameters"
        assert request.headers["authorization"] == "Bearer app-test"
        if fail:
            return httpx.Response(503, request=request, json={"message": "temporary"})
        return httpx.Response(
            200,
            request=request,
            json={
                "user_input_form": [
                    {
                        "paragraph": {
                            "variable": "Tool_invocation",
                            "max_length": 233_333,
                        }
                    }
                ]
            },
        )

    cache = dify.DifyParameterCache(ttl_seconds=300, retry_seconds=30)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await cache.get(
            base_url="https://example.test/v1",
            api_key="app-test",
            client=client,
        )
        second = await cache.get(
            base_url="https://example.test/v1",
            api_key="app-test",
            client=client,
        )
        assert first.source == "refresh"
        assert second.source == "cache"
        assert calls == 1

        key = cache._key("https://example.test/v1", "app-test")
        cache._entries[key].expires_at = 0
        fail = True
        stale = await cache.get(
            base_url="https://example.test/v1",
            api_key="app-test",
            client=client,
        )
        retry_cache = await cache.get(
            base_url="https://example.test/v1",
            api_key="app-test",
            client=client,
        )

        empty_cache = dify.DifyParameterCache(ttl_seconds=300, retry_seconds=30)
        unavailable = await empty_cache.get(
            base_url="https://example.test/v1",
            api_key="app-test",
            client=client,
        )

    assert stale.source == retry_cache.source == "stale"
    assert stale.limits == {"Tool_invocation": 233_333}
    assert "status=503" in stale.error
    assert unavailable.source == "unavailable"
    assert unavailable.limits == {}
    assert "status=503" in unavailable.error
    assert calls == 3


def test_image_upload_mapping_preserves_failed_and_deduped_source_indexes(monkeypatch):
    async def fake_upload(**kwargs):
        if kwargs["b64_data"] == bad:
            raise ValueError("broken image")
        return "fid-good"

    monkeypatch.setattr(dify, "upload_base64_image", fake_upload)
    bad = _b64(b"bad")
    good = _b64(b"good")
    images = [
        {"kind": "base64", "data": bad, "media_type": "image/png"},
        {"kind": "base64", "data": good, "media_type": "image/png"},
        {"kind": "base64", "data": good, "media_type": "image/png"},
    ]
    files, notes, mapping = asyncio.run(
        dify.upload_images(
            images,
            base_url="https://example.invalid",
            api_key="key",
            user="u",
            client=None,
        )
    )
    assert len(files) == 1
    assert [item["status"] for item in mapping] == ["failed", "ok", "dedup"]
    assert mapping[1]["file_index"] == mapping[2]["file_index"] == 0
    assert any("image_1_upload_failed" in note for note in notes)

    query = annotate_query_for_images(
        "[[cc_route:opus]]\n[image] [image] [image]",
        mapping,
    )
    assert query.startswith("[[cc_route:opus]]")
    assert "[[cc_images:failed]]" in query
    assert "Image #1 不可用" in query
    assert "Image #2 → Dify files[0]" in query
    assert "[image #1 unavailable]" in query
