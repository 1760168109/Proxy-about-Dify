# -*- coding: utf-8 -*-
"""Dify 上游 I/O：chat-messages SSE 与图片上传。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from parse import image_b64_byte_len

OnAccepted = Callable[[], None] | Callable[[], Awaitable[None]]
OnTransportEvent = Callable[[str, dict[str, Any]], None]

UPLOAD_RETRIES = 3
_RETRY_BASE_DELAY = 0.45


@dataclass(frozen=True)
class DifyInputLimits:
    """Dify 应用当前发布的文本输入字符上限。"""

    limits: dict[str, int]
    source: str
    error: str = ""


@dataclass
class _ParameterCacheEntry:
    limits: dict[str, int]
    expires_at: float
    error: str = ""


def parse_input_char_limits(payload: Any) -> dict[str, int]:
    """从 GET /parameters 提取 user_input_form 的 max_length。"""
    if not isinstance(payload, dict):
        raise ValueError("Dify parameters response must be an object")
    forms = payload.get("user_input_form")
    if not isinstance(forms, list):
        raise ValueError("Dify parameters response missing user_input_form")

    limits: dict[str, int] = {}
    for item in forms:
        if not isinstance(item, dict):
            continue
        for definition in item.values():
            if not isinstance(definition, dict):
                continue
            variable = definition.get("variable")
            raw_limit = definition.get("max_length")
            if not isinstance(variable, str) or not variable.strip():
                continue
            if isinstance(raw_limit, bool):
                continue
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                continue
            if limit > 0:
                limits[variable] = limit
    return limits


async def fetch_input_char_limits(
    *,
    base_url: str,
    api_key: str,
    client: httpx.AsyncClient,
) -> dict[str, int]:
    """读取当前 Dify 应用参数；认证与 chat-messages 使用同一应用密钥。"""
    response = await client.get(
        base_url.rstrip("/") + "/parameters",
        headers={"Authorization": "Bearer {}".format(api_key)},
        timeout=10.0,
    )
    response.raise_for_status()
    return parse_input_char_limits(response.json())


def _parameter_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return "HTTPStatusError status={}".format(exc.response.status_code)
    message = str(exc).strip().replace("\n", " ")
    return "{}: {}".format(type(exc).__name__, message[:240] or "(empty message)")


class DifyParameterCache:
    """按 Dify base URL + 应用密钥隔离的参数缓存。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        retry_seconds: float = 30.0,
        max_entries: int = 8,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.max_entries = max(1, int(max_entries))
        self._entries: dict[tuple[str, str], _ParameterCacheEntry] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(base_url: str, api_key: str) -> tuple[str, str]:
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        return base_url.rstrip("/"), fingerprint

    @staticmethod
    def _snapshot(
        entry: _ParameterCacheEntry, *, refreshed: bool = False
    ) -> DifyInputLimits:
        if entry.error:
            source = "stale" if entry.limits else "unavailable"
        else:
            source = "refresh" if refreshed else "cache"
        return DifyInputLimits(dict(entry.limits), source, entry.error)

    async def get(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
    ) -> DifyInputLimits:
        key = self._key(base_url, api_key)
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and now < entry.expires_at:
            return self._snapshot(entry)

        async with self._lock:
            now = time.monotonic()
            entry = self._entries.get(key)
            if entry is not None and now < entry.expires_at:
                return self._snapshot(entry)
            try:
                limits = await fetch_input_char_limits(
                    base_url=base_url,
                    api_key=api_key,
                    client=client,
                )
            except Exception as exc:
                error = _parameter_error(exc)
                retained = dict(entry.limits) if entry is not None else {}
                failed = _ParameterCacheEntry(
                    retained,
                    now + self.retry_seconds,
                    error,
                )
                self._entries[key] = failed
                return self._snapshot(failed)

            refreshed = _ParameterCacheEntry(
                dict(limits),
                now + self.ttl_seconds,
            )
            self._entries[key] = refreshed
            while len(self._entries) > self.max_entries:
                oldest = min(self._entries, key=lambda item: self._entries[item].expires_at)
                if oldest == key and len(self._entries) == 1:
                    break
                del self._entries[oldest]
            return self._snapshot(refreshed, refreshed=True)


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


def parse_sse_lines(
    buffer: str, *, stats: dict[str, int] | None = None
) -> tuple[list[dict[str, Any]], str]:
    """从缓冲切出完整的单行 ``data:`` JSON；返回 ``(events, rest)``。

    这里只做线缆拆分，不实现完整 SSE 事件组装：``event:`` 头、跨多行的
    ``data:``、空 data、``[DONE]`` 和非 JSON 行不会被绑定到下一条消息。事件
    语义与 answer 增量的累加由 ``answer.py`` 的消费者负责。
    """
    events: list[dict[str, Any]] = []
    segments = buffer.split("\n")
    rest = segments.pop() if segments else ""
    for raw in segments:
        line = raw.replace("\r", "").strip()
        if stats is not None:
            stats["wire_lines"] = stats.get("wire_lines", 0) + 1
            if line.startswith("event: ping"):
                stats["ping_lines"] = stats.get("ping_lines", 0) + 1
        if not line.startswith("data:"):
            if stats is not None:
                stats["ignored_lines"] = stats.get("ignored_lines", 0) + 1
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            if stats is not None:
                stats["empty_data_lines"] = stats.get("empty_data_lines", 0) + 1
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            if stats is not None:
                stats["malformed_data_lines"] = stats.get("malformed_data_lines", 0) + 1
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
    on_transport_event: OnTransportEvent | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式 POST /chat-messages；逐个 yield 解析后的原始事件 dict。

    ``on_accepted``：上游 HTTP 状态小于 400、响应流已被接受后回调一次；这只
    表示 Dify 已开始处理，尚不表示收到正文或完成下游交付（调用方可在这里计费）。
    on_transport_event：只报告线缆/事件类型、计数与异常摘要，不含 SSE data 正文。
    其中 ``dify_event_yielded`` 仅表示事件已交给下游转译器，不表示已送达 Claude Code；
    ``completed``（在关闭事件中）只表示上游迭代到了 EOF，不表示业务工作流成功。
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

    def observe(event: str, **fields: Any) -> None:
        if on_transport_event is None:
            return
        try:
            on_transport_event(event, fields)
        except Exception as exc:
            print("[lan] transport observer failed open: {!r}".format(exc))

    started = time.monotonic()
    chunks = 0
    wire_chars = 0
    parsed_events = 0
    yielded_events = 0
    parse_stats: dict[str, int] = {}
    last_event = ""
    terminal_event_seen = ""
    completed = False
    accepted = False

    def record_yielded_event(event: dict[str, Any]) -> dict[str, Any]:
        nonlocal parsed_events, yielded_events, last_event, terminal_event_seen
        parsed_events += 1
        last_event = str(event.get("event") or "unknown")
        if parsed_events == 1:
            observe("dify_first_event", event_type=last_event)
        if last_event in ("message_end", "workflow_finished", "error"):
            terminal_event_seen = last_event
            observe(
                "dify_terminal_event",
                event_type=last_event,
                event_index=parsed_events,
            )
        yielded_events += 1
        observe(
            "dify_event_yielded",
            event_type=last_event,
            event_index=yielded_events,
        )
        return event

    observe("dify_request_open", method="POST", path="/chat-messages")
    try:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            observe(
                "dify_response_headers",
                status_code=resp.status_code,
                http_version=resp.http_version,
                content_type=resp.headers.get("content-type") or "",
            )
            if resp.status_code >= 400:
                err_text = await resp.aread()
                try:
                    err_obj = json.loads(err_text.decode("utf-8", errors="replace"))
                    msg = (
                        err_obj.get("message")
                        or err_obj.get("error")
                        or err_text.decode("utf-8", errors="replace")
                    )
                except Exception:
                    msg = err_text.decode("utf-8", errors="replace")
                raise httpx.HTTPStatusError(
                    "Dify error: {}".format(msg), request=resp.request, response=resp
                )

            if on_accepted is not None:
                try:
                    maybe = on_accepted()
                    if maybe is not None and hasattr(maybe, "__await__"):
                        await maybe  # type: ignore[misc]
                except Exception as exc:
                    # Dify 已接受请求；账本等本地旁路故障不能丢掉已生成的答复。
                    print("[lan] on_accepted side effect failed open: {!r}".format(exc))
            accepted = True
            observe("dify_request_accepted", status_code=resp.status_code)

            buffer = ""
            first_chunk = True
            async for chunk in resp.aiter_text():
                chunks += 1
                wire_chars += len(chunk)
                if first_chunk:
                    first_chunk = False
                    observe(
                        "dify_first_byte",
                        chunk_chars=len(chunk),
                        upstream_elapsed_seconds=round(time.monotonic() - started, 3),
                    )
                buffer += chunk
                events, buffer = parse_sse_lines(buffer, stats=parse_stats)
                for ev in events:
                    yield record_yielded_event(ev)
            if buffer.strip():
                events, _ = parse_sse_lines(buffer + "\n", stats=parse_stats)
                for ev in events:
                    yield record_yielded_event(ev)
            completed = True
    except BaseException as exc:
        observe(
            "dify_stream_cancelled"
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
            else "dify_stream_error",
            exception_type=type(exc).__name__,
            exception_message=str(exc).strip()[:400],
            chunks=chunks,
            wire_chars=wire_chars,
            parsed_events=parsed_events,
            yielded_events=yielded_events,
            **parse_stats,
            last_event=last_event,
            terminal_event_seen=terminal_event_seen,
        )
        raise
    finally:
        observe(
            "dify_stream_closed",
            completed=completed,
            accepted=accepted,
            chunks=chunks,
            wire_chars=wire_chars,
            parsed_events=parsed_events,
            yielded_events=yielded_events,
            **parse_stats,
            last_event=last_event,
            terminal_event_seen=terminal_event_seen,
            eof_without_terminal=bool(completed and not terminal_event_seen),
            upstream_elapsed_seconds=round(time.monotonic() - started, 3),
        )


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
    try:
        binary = base64.b64decode(raw, validate=False)
    except Exception:
        binary = raw.encode("utf-8", errors="ignore")
    h = hashlib.sha256(binary).hexdigest()
    return "{}:{}".format(len(binary), h)


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

    # 循环的每条出口都 return / raise / continue，且最后一轮不会 continue，
    # 故循环无正常退出路径——不在此后另设「兜底再抛」的死代码。
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
            status = e.response.status_code if e.response is not None else None
            if status is not None and status < 500 and status not in (408, 429):
                raise
            if attempt < n_try:
                await _sleep_backoff(attempt)
                continue
            raise
        except (httpx.TransportError, httpx.TimeoutException, OSError) as e:
            if attempt < n_try:
                await _sleep_backoff(attempt)
                continue
            raise ValueError(
                format_exception_note(
                    e, attempt=attempt, media_type=media_type, bytes_len=len(binary)
                )
            ) from e

    raise RuntimeError("unreachable: upload loop exhausted without outcome")


async def upload_images(
    images: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    user: str,
    client: httpx.AsyncClient,
    max_images: int = 8,
) -> tuple[list[dict[str, str]], list[str], list[dict[str, Any]]]:
    """抽好的 image dicts → 上传 → chat-messages files 载荷。

    返回 (files_payload, notes, source_mapping)；映射保留原图索引，
    即使中间上传失败或发生去重也不移动事实。
    """
    notes: list[str] = []
    mapping: list[dict[str, Any]] = []
    if not images:
        return [], notes, mapping

    if len(images) > max_images:
        notes.append("images_truncated={}->{}".format(len(images), max_images))

    files: list[dict[str, str]] = []
    seen_fp: dict[str, int] = {}
    for i, img in enumerate(images):
        if i >= max_images:
            mapping.append(
                {"source_index": i, "status": "skipped", "reason": "max_images"}
            )
            continue
        if img.get("kind") == "url":
            if img.get("url"):
                file_index = len(files)
                files.append(
                    {
                        "type": "image",
                        "transfer_method": "remote_url",
                        "url": str(img["url"]),
                    }
                )
                notes.append("image_{}_remote_url".format(i + 1))
                mapping.append(
                    {"source_index": i, "status": "ok", "file_index": file_index}
                )
            else:
                mapping.append(
                    {"source_index": i, "status": "failed", "reason": "missing_url"}
                )
            continue
        data = str(img.get("data") or "")
        fp = _b64_fingerprint(data) if data else ""
        if fp and fp in seen_fp:
            notes.append("image_{}_dedup_skip".format(i + 1))
            mapping.append(
                {
                    "source_index": i,
                    "status": "dedup",
                    "file_index": seen_fp[fp],
                }
            )
            continue
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
            file_index = len(files)
            files.append(
                {"type": "image", "transfer_method": "local_file", "upload_file_id": fid}
            )
            if fp:
                seen_fp[fp] = file_index
            mapping.append(
                {"source_index": i, "status": "ok", "file_index": file_index}
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
            mapping.append(
                {
                    "source_index": i,
                    "status": "failed",
                    "reason": "upload_error",
                }
            )
    return files, notes, mapping
