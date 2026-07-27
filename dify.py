# -*- coding: utf-8 -*-
"""Dify 上游 I/O：chat-messages SSE 与图片上传。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

import httpx

from parse import image_b64_byte_len

OnAccepted = Callable[[], None] | Callable[[], Awaitable[None]]

UPLOAD_RETRIES = 3
_RETRY_BASE_DELAY = 0.45


async def _sleep_backoff(attempt: int) -> None:
    await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

_MEDIA_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def parse_sse_lines(buffer: str) -> tuple[list[dict[str, Any]], str]:
    """从缓冲切出完整 data: 行；返回 (events, rest)。"""
    events: list[dict[str, Any]] = []
    segments = buffer.split("\n")
    rest = segments.pop() if segments else ""
    for raw in segments:
        line = raw.replace("\r", "").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, rest


async def stream_chat_messages(
    *,
    base_url: str,
    api_key: str,
    user: str,
    query: str,
    conversation_id: str | None,
    client: httpx.AsyncClient,
    inputs: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    on_accepted: OnAccepted | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式 POST /chat-messages；yield 解析后的事件 dict。

    on_accepted：HTTP <400 接受流后回调一次（按次计费的「真正传入」时点）。
    """
    url = base_url.rstrip("/") + "/chat-messages"
    clean_inputs: dict[str, str] = {}
    for k, v in (inputs or {}).items():
        clean_inputs[k] = "" if v is None else (v if isinstance(v, str) else str(v))
    body: dict[str, Any] = {
        "inputs": clean_inputs,
        "query": query,
        "response_mode": "streaming",
        "user": user,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    if files:
        body["files"] = files

    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
    }

    async with client.stream("POST", url, headers=headers, json=body) as resp:
        if resp.status_code >= 400:
            err_text = await resp.aread()
            try:
                err_obj = json.loads(err_text.decode("utf-8", errors="replace"))
                msg = err_obj.get("message") or err_obj.get("error") or err_text.decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                msg = err_text.decode("utf-8", errors="replace")
            raise httpx.HTTPStatusError(
                "Dify error: {}".format(msg), request=resp.request, response=resp
            )

        if on_accepted is not None:
            maybe = on_accepted()
            if maybe is not None and hasattr(maybe, "__await__"):
                await maybe  # type: ignore[misc]

        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk
            events, buffer = parse_sse_lines(buffer)
            for ev in events:
                yield ev
        if buffer.strip():
            events, _ = parse_sse_lines(buffer + "\n")
            for ev in events:
                yield ev


# ── 图片上传 ─────────────────────────────────────────────────────────


def format_exception_note(
    exc: BaseException,
    *,
    attempt: int | None = None,
    media_type: str = "",
    bytes_len: int = 0,
    status: int | None = None,
    body_head: str = "",
) -> str:
    """upload 失败时生成非空、可观测的 notes 片段。"""
    parts: list[str] = []
    if attempt is not None:
        parts.append("attempt={}".format(attempt))
    parts.append(type(exc).__name__)
    rep = repr(exc)
    s = str(exc)
    if rep and rep not in ("", "''", '""'):
        parts.append(rep[:400])
    elif s.strip():
        parts.append(s.strip()[:400])
    else:
        parts.append("(empty message)")
    if status is not None:
        parts.append("status={}".format(status))
    if body_head:
        parts.append("body={}".format(body_head[:300].replace("\n", " ")))
    if bytes_len:
        parts.append("bytes={}".format(bytes_len))
    if media_type:
        parts.append("media={}".format(media_type))
    return " | ".join(parts)


def _filename_for(media_type: str, index: int) -> str:
    return "cc_image_{}{}".format(
        index + 1, _MEDIA_EXT.get((media_type or "").lower(), ".png")
    )


def _b64_fingerprint(data: str) -> str:
    raw = data.split(",", 1)[-1] if data.startswith("data:") else data
    h = hashlib.sha256(raw[:4096].encode("utf-8", errors="ignore")).hexdigest()[:16]
    return "{}:{}:{}".format(len(raw), h, raw[-32:] if len(raw) > 32 else raw)


async def upload_base64_image(
    *,
    base_url: str,
    api_key: str,
    user: str,
    client: httpx.AsyncClient,
    media_type: str,
    b64_data: str,
    filename: str,
    retries: int = UPLOAD_RETRIES,
) -> str:
    """POST /files/upload → upload_file_id；网络/5xx/408/429 自动重试。"""
    raw = b64_data
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        binary = base64.b64decode(raw, validate=False)
    except Exception as e:
        raise ValueError(format_exception_note(e, media_type=media_type)) from e
    if not binary:
        raise ValueError("empty image bytes media={}".format(media_type))

    url = base_url.rstrip("/") + "/files/upload"
    headers = {"Authorization": "Bearer {}".format(api_key)}
    n_try = max(1, int(retries))
    last_exc: BaseException | None = None

    for attempt in range(1, n_try + 1):
        files = {"file": (filename, binary, media_type or "application/octet-stream")}
        try:
            resp = await client.post(url, headers=headers, files=files, data={"user": user})
            if resp.status_code >= 400:
                err = httpx.HTTPStatusError(
                    "Dify file upload error: status={} body={}".format(
                        resp.status_code, (resp.text or "")[:500]
                    ),
                    request=resp.request,
                    response=resp,
                )
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    last_exc = err
                    if attempt < n_try:
                        await _sleep_backoff(attempt)
                        continue
                raise err
            try:
                obj = resp.json()
            except Exception as e:
                raise ValueError(
                    format_exception_note(
                        e,
                        attempt=attempt,
                        media_type=media_type,
                        bytes_len=len(binary),
                        status=resp.status_code,
                        body_head=(resp.text or "")[:200],
                    )
                ) from e
            fid = obj.get("id") or obj.get("file_id") or obj.get("upload_file_id")
            if not fid:
                raise ValueError("upload response missing id: {}".format(str(obj)[:300]))
            return str(fid)
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else None
            if status is not None and status < 500 and status not in (408, 429):
                raise
            if attempt < n_try:
                await _sleep_backoff(attempt)
                continue
            raise
        except (httpx.TransportError, httpx.TimeoutException, OSError) as e:
            last_exc = e
            if attempt < n_try:
                await _sleep_backoff(attempt)
                continue
            raise ValueError(
                format_exception_note(
                    e, attempt=attempt, media_type=media_type, bytes_len=len(binary)
                )
            ) from e

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("upload failed with no exception")


async def upload_images(
    images: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    user: str,
    client: httpx.AsyncClient,
    max_images: int = 8,
) -> tuple[list[dict[str, str]], list[str]]:
    """抽好的 image dicts → 上传 → chat-messages files 载荷。

    返回 (files_payload, notes)；base64 去重、url 图直挂 remote_url。
    """
    notes: list[str] = []
    if not images:
        return [], notes

    if len(images) > max_images:
        notes.append("images_truncated={}->{}".format(len(images), max_images))
        images = images[:max_images]

    files: list[dict[str, str]] = []
    seen_fp: set[str] = set()
    for i, img in enumerate(images):
        if img.get("kind") == "url":
            if img.get("url"):
                files.append(
                    {
                        "type": "image",
                        "transfer_method": "remote_url",
                        "url": str(img["url"]),
                    }
                )
                notes.append("image_{}_remote_url".format(i + 1))
            continue
        data = str(img.get("data") or "")
        fp = _b64_fingerprint(data) if data else ""
        if fp and fp in seen_fp:
            notes.append("image_{}_dedup_skip".format(i + 1))
            continue
        if fp:
            seen_fp.add(fp)
        media = str(img.get("media_type") or "image/png")
        blen = image_b64_byte_len(img)
        try:
            fid = await upload_base64_image(
                base_url=base_url,
                api_key=api_key,
                user=user,
                client=client,
                media_type=media,
                b64_data=data,
                filename=_filename_for(media, i),
            )
            files.append(
                {"type": "image", "transfer_method": "local_file", "upload_file_id": fid}
            )
            notes.append(
                "image_{}_uploaded id={} media={} bytes={}".format(i + 1, fid[:12], media, blen)
            )
        except Exception as e:
            status = None
            body_head = ""
            if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                status = e.response.status_code
                try:
                    body_head = (e.response.text or "")[:300]
                except Exception:
                    body_head = ""
            notes.append(
                "image_{}_upload_failed: {}".format(
                    i + 1,
                    format_exception_note(
                        e, media_type=media, bytes_len=blen, status=status, body_head=body_head
                    ),
                )
            )
    return files, notes
