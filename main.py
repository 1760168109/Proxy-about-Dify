# -*- coding: utf-8 -*-
"""lan · Dify「岚」↔ Anthropic Messages 本地代理。

监听 127.0.0.1:7272，供 Claude Code / CC Switch 使用。改 .py 后须重启。
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from answer import (
    build_non_stream_message,
    build_non_stream_with_tools,
    collect_dify_answer,
    dify_events_to_anthropic_sse,
    estimate_input_tokens_from_request,
    iter_plain_text_sse,
)
from agent_bridge import (
    AgentBridgeStore,
    extract_and_strip_transport,
    merge_archived_reports,
    wants_archived_agent_reports,
)
from cache import ReadCache, ingest_messages_into_cache
from dify import DifyInputLimits, DifyParameterCache, stream_chat_messages
from log import (
    append_transport_trace,
    patch_request_log,
    read_transport_trace,
    response_log_patch,
    write_request_log,
)
from meter import UsageMeter
from outbound import (
    DifyInputLengthError,
    attach_images_to_outbound,
    input_shard_capacity_hint,
    outbound_log_extra,
    outbound_metrics_line,
    prepare_text_outbound,
)
from parse import parse_payload
from plan import build_plan
from protocol import PRIMARY_DISCOVERY_MODEL_ID, discovery_models
from sessions import SessionStore, extract_cc_session_id
from singleflight import SingleFlight, request_fingerprint
from status import build_action_status
from terminal import TerminalResolution, TerminalStore
from tools import TERMINAL_TOOL_NAMES
from unicode_wire import DifyPersistenceSizeError

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _env_flag(name: str, default: str) -> bool:
    return (os.getenv(name) or default).strip() not in ("0", "false", "False")


DIFY_BASE_URL = os.getenv("DIFY_BASE_URL", "https://api.dify.ai/v1").rstrip("/")
DIFY_USER_ID = os.getenv("DIFY_USER_ID", "Liu Sheng")
DIFY_API_KEY_FALLBACK = (os.getenv("DIFY_API_KEY") or "").strip()
LISTEN_HOST = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
AUX_ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN") or "").strip()
LOG_REQUESTS = _env_flag("LOG_REQUESTS", "1")
# 结构化工具出口：对带工具的 opus 枪注入 [[cc_struct:on]]。
# 默认关——当前插件 prefill 与上游模型不兼容，重开条件见 经验.md Backlog。
TOOL_STRUCTURED = _env_flag("TOOL_STRUCTURED", "0")
NONSTREAM_REPLAY_TTL_SECONDS = 180.0

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse_event_types(payload: bytes | bytearray | memoryview | str) -> list[str]:
    """只从下游线缆头部提取 event 名；不触碰 data 正文。"""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        text = bytes(payload).decode("utf-8", errors="replace")
    else:
        text = payload
    result: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            value = line[6:].strip()
            if value:
                result.append(value[:80])
    return result


def _trace_chunk(index: int, event_types: list[str]) -> bool:
    """采样普通分片，完整保留协议边界与异常分片。"""
    if index <= 12 or index % 25 == 0:
        return True
    return any(
        event_type in {"message_start", "message_delta", "message_stop", "error"}
        for event_type in event_types
    )


class TracedStreamingResponse(StreamingResponse):
    """StreamingResponse that records the actual ASGI send boundary.

    The generator can produce a chunk successfully while the socket write fails. This
    wrapper records both sides without logging SSE data, so a trace can distinguish
    converter failure from downstream disconnect. A successful ASGI ``send`` only
    proves that the server transport accepted the chunk; it does not prove that Claude
    Code parsed, displayed, or persisted it.
    """

    def __init__(self, content, *, trace=None, **kwargs):
        self._transport_trace = trace or (lambda *_args, **_kwargs: None)
        super().__init__(content, **kwargs)

    async def stream_response(self, send) -> None:
        body_chunks = 0
        body_bytes = 0
        completed = False

        async def traced_send(message):
            nonlocal body_chunks, body_bytes
            message_type = message.get("type")
            if message_type == "http.response.start":
                self._transport_trace(
                    "downstream_response_start",
                    _durable=True,
                    status_code=message.get("status"),
                )
            elif message_type == "http.response.body":
                body_chunks += 1
                body = message.get("body") or b""
                if isinstance(body, str):
                    chunk_bytes = len(body.encode("utf-8"))
                    event_types = _sse_event_types(body)
                else:
                    chunk_bytes = len(body)
                    event_types = _sse_event_types(body)
                body_bytes += chunk_bytes
                is_final_body = not bool(message.get("more_body"))
                if is_final_body:
                    self._transport_trace(
                        "downstream_body_final_send_attempt",
                        _durable=True,
                        body_chunk_index=body_chunks,
                    )
                if "message_start" in event_types:
                    self._transport_trace(
                        "downstream_message_start_send_attempt",
                        _durable=True,
                        body_chunk_index=body_chunks,
                    )
                if "message_stop" in event_types:
                    self._transport_trace(
                        "downstream_message_stop_send_attempt",
                        _durable=True,
                        body_chunk_index=body_chunks,
                    )
                sampled = _trace_chunk(body_chunks, event_types)
                if sampled:
                    self._transport_trace(
                        "downstream_send_attempt",
                        body_chunk_index=body_chunks,
                        body_bytes=chunk_bytes,
                        more_body=bool(message.get("more_body")),
                        event_types=event_types,
                    )
            try:
                await send(message)
            except BaseException as exc:
                if message_type == "http.response.body":
                    if "message_start" in event_types:
                        self._transport_trace(
                            "downstream_message_start_send_error",
                            _durable=True,
                            body_chunk_index=body_chunks,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc).strip()[:400],
                        )
                    if "message_stop" in event_types:
                        self._transport_trace(
                            "downstream_message_stop_send_error",
                            _durable=True,
                            body_chunk_index=body_chunks,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc).strip()[:400],
                        )
                    if is_final_body:
                        self._transport_trace(
                            "downstream_body_final_send_error",
                            _durable=True,
                            body_chunk_index=body_chunks,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc).strip()[:400],
                        )
                    self._transport_trace(
                        "downstream_send_error",
                        _durable=True,
                        body_chunk_index=body_chunks,
                        body_bytes=len(message.get("body") or b""),
                        event_types=event_types,
                        exception_type=type(exc).__name__,
                        exception_message=str(exc).strip()[:400],
                    )
                else:
                    self._transport_trace(
                        "downstream_send_error",
                        _durable=True,
                        message_type=message_type,
                        exception_type=type(exc).__name__,
                        exception_message=str(exc).strip()[:400],
                    )
                raise
            else:
                if message_type == "http.response.body" and "message_start" in event_types:
                    self._transport_trace(
                        "downstream_message_start_sent",
                        _durable=True,
                        body_chunk_index=body_chunks,
                    )
                if message_type == "http.response.body" and "message_stop" in event_types:
                    self._transport_trace(
                        "downstream_message_stop_sent",
                        _durable=True,
                        body_chunk_index=body_chunks,
                    )
                if message_type == "http.response.body" and is_final_body:
                    self._transport_trace(
                        "downstream_body_final_sent",
                        _durable=True,
                        body_chunk_index=body_chunks,
                    )
                if message_type == "http.response.body" and sampled:
                    self._transport_trace(
                        "downstream_send_ok",
                        body_chunk_index=body_chunks,
                        body_bytes=len(message.get("body") or b""),
                        more_body=bool(message.get("more_body")),
                        event_types=event_types,
                    )

        try:
            await super().stream_response(traced_send)
            completed = True
        finally:
            self._transport_trace(
                "downstream_response_finished",
                _durable=True,
                completed=completed,
                body_chunks=body_chunks,
                body_bytes=body_bytes,
            )

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        except BaseException as exc:
            self._transport_trace(
                "downstream_response_error",
                _durable=True,
                exception_type=type(exc).__name__,
                exception_message=str(exc).strip()[:400],
            )
            raise

DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "request_logs"

store = SessionStore(DATA_DIR / "sessions.json")
agent_store = AgentBridgeStore(DATA_DIR / "agents.json")
meter = UsageMeter(DATA_DIR / "usage.json")
read_cache = ReadCache(DATA_DIR / "read_cache.json")
terminal_store = TerminalStore(DATA_DIR / "terminal_pending.json")
http_client: httpx.AsyncClient | None = None
NonStreamAnswer = tuple[str, str, list[dict[str, Any]], dict[str, int], dict[str, Any]]
nonstream_flights: SingleFlight[NonStreamAnswer] | None = None
parameter_cache: DifyParameterCache | None = None
session_locks: dict[tuple[int, str], asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client, nonstream_flights, parameter_cache, session_locks
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
    session_locks = {}
    parameter_cache = DifyParameterCache()
    nonstream_flights = SingleFlight(
        success_ttl_seconds=NONSTREAM_REPLAY_TTL_SECONDS,
        max_completed=32,
    )
    try:
        yield
    finally:
        if nonstream_flights is not None:
            await nonstream_flights.close()
            nonstream_flights = None
        parameter_cache = None
        session_locks = {}
        if http_client is not None:
            await http_client.aclose()
            http_client = None


app = FastAPI(title="Dify Anthropic Proxy", version="1.0.0", lifespan=lifespan)


def _client() -> httpx.AsyncClient:
    if http_client is None:
        raise HTTPException(status_code=503, detail="HTTP client not ready")
    return http_client


async def _load_input_limits(
    *, api_key: str, client: httpx.AsyncClient
) -> DifyInputLimits:
    if parameter_cache is None:
        return DifyInputLimits({}, "unavailable", "parameter cache not ready")
    return await parameter_cache.get(
        base_url=DIFY_BASE_URL,
        api_key=api_key,
        client=client,
    )


def _nonstream_flights() -> SingleFlight[NonStreamAnswer]:
    if nonstream_flights is None:
        raise HTTPException(status_code=503, detail="Single-flight registry not ready")
    return nonstream_flights


def _session_lock(session_id: str) -> asyncio.Lock:
    # loop 维度必需：asyncio.Lock 绑定创建时的事件循环，而本字典是模块级全局；
    # 集成测试每个用例各起一个 loop，无此维度会复用已死的 Lock。
    key = (id(asyncio.get_running_loop()), session_id)
    lock = session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        session_locks[key] = lock
    return lock


_LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


def _supplied_credential(authorization: str | None, x_api_key: str | None) -> str:
    """x-api-key 优先，其次 Authorization（剥 bearer 前缀）；取不到返回空串。"""
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        raw = authorization.strip()
        return raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    return ""


def _aux_auth_enforced() -> bool:
    """管理端点是否需要凭据：配了 ADMIN_TOKEN，或监听地址不是本机。"""
    return bool(AUX_ADMIN_TOKEN) or LISTEN_HOST not in _LOCAL_HOSTS


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str:
    supplied = _supplied_credential(authorization, x_api_key)
    if supplied:
        return supplied
    if DIFY_API_KEY_FALLBACK:
        return DIFY_API_KEY_FALLBACK
    raise HTTPException(
        status_code=401,
        detail="缺少 API Key：请在 x-api-key / Authorization 传入 app-xxxx，或配置 DIFY_API_KEY",
    )


def _require_aux_auth(
    *,
    authorization: str | None,
    x_api_key: str | None,
) -> None:
    """管理端点：本机默认免 token；外部监听必须显式配置 ADMIN_TOKEN。"""
    if AUX_ADMIN_TOKEN:
        supplied = _supplied_credential(authorization, x_api_key)
        if not supplied or not secrets.compare_digest(supplied, AUX_ADMIN_TOKEN):
            raise HTTPException(status_code=401, detail="管理端点需要 ADMIN_TOKEN")
        return
    if LISTEN_HOST not in _LOCAL_HOSTS:
        raise HTTPException(
            status_code=503,
            detail="HOST 非本机地址；请配置 ADMIN_TOKEN 后再访问管理端点",
        )


# ── 辅助端点 ─────────────────────────────────────────────────────────


@app.get("/health")
async def health(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    if _aux_auth_enforced():
        try:
            _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
        except HTTPException:
            return {
                "ok": True,
                "service": "lan-proxy",
                "management_auth_required": True,
            }
    return {
        "ok": True,
        "service": "lan-proxy",
        "user": DIFY_USER_ID,
        "dify_base": DIFY_BASE_URL,
        "tool_structured": TOOL_STRUCTURED,
        "terminal_tools": sorted(TERMINAL_TOOL_NAMES),
        "terminal_pending": terminal_store.pending_count(DIFY_USER_ID),
        "agent_bridge": agent_store.stats(),
        "nonstream_flights": (
            nonstream_flights.stats() if nonstream_flights is not None else None
        ),
        "protocol": {
            "advertised_model": PRIMARY_DISCOVERY_MODEL_ID,
            "count_tokens": "optional; Claude Code inference fallback",
            "input_limits": (
                "Dify /parameters max_length (characters; cached) + "
                "published same-name shards + conversation variable "
                "sys.getsizeof <= 204800"
            ),
        },
        "usage": meter.snapshot(),
    }


@app.get("/v1/usage")
@app.get("/usage")
async def usage_get(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    return meter.snapshot()


@app.get("/v1/usage/statusline")
@app.get("/usage/statusline")
async def usage_statusline(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> Any:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    return PlainTextResponse(meter.statusline() + "\n")


@app.post("/v1/usage/reset")
@app.post("/usage/reset")
async def usage_reset(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    return meter.reset()


@app.get("/v1/models")
async def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    """故意不鉴权。Claude Code 建立 provider 配置时可能先拉这张表做模型发现；
    这里暴露的全部内容只是几个模型 id 与 display_name，不含密钥或上游地址。

    下面这行只记「这次发现请求带没带凭据」（不记凭据本身），用于确证上述前提：
    若长期观察到恒为 yes，则本端点可以安全地改为要求鉴权。
    """
    print(
        "[lan] models discovery credential={}".format(
            "yes" if _supplied_credential(authorization, x_api_key) else "no"
        )
    )
    return {
        "object": "list",
        "data": discovery_models(),
    }


@app.get("/sessions")
async def sessions_list(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    return store.get_state(DIFY_USER_ID)


class NewSessionBody(BaseModel):
    cc_session_id: str | None = None
    clear_all: bool = False


@app.post("/sessions/new")
async def sessions_new(
    body: NewSessionBody = Body(default_factory=NewSessionBody),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    out = store.new_session(
        DIFY_USER_ID, body.cc_session_id, clear_all=bool(body.clear_all)
    )

    def clear_agent_state(parent_sid: str | None = None, *, all_parents: bool = False) -> None:
        """与主会话解绑同步清理 hook 档案；清理失败不阻断主 API。"""
        try:
            if all_parents:
                agent_store.clear_all()
            else:
                agent_store.clear_parent(parent_sid)
        except Exception as exc:
            print("[lan] agent state cleanup failed open: {!r}".format(exc))

    if body.clear_all:
        terminal_store.clear_all(DIFY_USER_ID)
        clear_agent_state(all_parents=True)
    elif isinstance(body.cc_session_id, str) and body.cc_session_id.strip():
        # 显式解绑只影响这一父 session；不能顺手清掉其它并行 CC 会话的
        # 子代理报告或身份记录。
        explicit_sid = body.cc_session_id.strip()
        terminal_store.clear_session(DIFY_USER_ID, explicit_sid)
        clear_agent_state(explicit_sid)
    else:
        for sid in out.get("unbound_cc") or []:
            terminal_store.clear_session(DIFY_USER_ID, str(sid))
            clear_agent_state(str(sid))
    return out


class SwitchBody(BaseModel):
    conversation_id: str = Field(..., min_length=1)
    cc_session_id: str | None = None


@app.post("/sessions/switch")
async def sessions_switch(
    body: SwitchBody,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    try:
        out = store.switch(DIFY_USER_ID, body.conversation_id, body.cc_session_id)
        sid = body.cc_session_id or out.get("cc_session_id")
        if sid:
            terminal_store.clear_session(DIFY_USER_ID, str(sid))
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/hooks/subagent-start")
async def hook_subagent_start(
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    """登记子代理身份，并把可重复验证的 transport marker 注入 child 请求。

    marker 不在这里消费；同一个 child 的后续请求可以继续携带它，直到对应
    agents.json 记录被清理或容量裁剪后自动失效。
    """
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    if body.get("hook_event_name") != "SubagentStart":
        raise HTTPException(status_code=400, detail="Expected SubagentStart hook payload")
    try:
        transport, marker = agent_store.record_start(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    print(
        "[lan] hook start parent={} agent={} type={}".format(
            transport.parent_session_id[:8],
            transport.agent_id[:16],
            transport.agent_type or "-",
        )
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": marker,
        }
    }


@app.post("/hooks/subagent-stop")
async def hook_subagent_stop(
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    """保存有界完成报告；不把报告正文通过 hook 反注入 Claude Code。"""
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    if body.get("hook_event_name") != "SubagentStop":
        raise HTTPException(status_code=400, detail="Expected SubagentStop hook payload")
    try:
        archived = agent_store.record_stop(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    print(
        "[lan] hook stop parent={} agent={} tool={} report_chars={}".format(
            str(archived.get("parent_session_id") or "")[:8],
            str(archived.get("agent_id") or "")[:16],
            str(archived.get("tool_use_id") or "-")[:16],
            archived.get("report_chars") or 0,
        )
    )
    return {"ok": True, **archived}


@app.get("/debug/last-request")
async def debug_last_request(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> Any:
    _require_aux_auth(authorization=authorization, x_api_key=x_api_key)
    path = LOG_DIR / "last_request.json"
    if not path.exists():
        return {"ok": False, "message": "尚无记录；先用 CC 发一条消息。"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "raw_body" in data:
            data["raw_body"] = {"redacted": True}
        if isinstance(data, dict):
            trace_name = (data.get("summary") or {}).get("transport_trace_file")
            trace_path = (
                LOG_DIR / str(trace_name)
                if isinstance(trace_name, str) and trace_name
                else path.with_suffix(".trace.jsonl")
            )
            data["transport_trace"] = read_transport_trace(trace_path)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 主业务 ───────────────────────────────────────────────────────────


@app.post("/v1/messages")
async def messages(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    api_key = _extract_api_key(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    request_id = uuid.uuid4().hex[:8]
    request_started = time.monotonic()

    def _req_log(message: str) -> None:
        print("[lan] req={} {}".format(request_id, message))

    request_cc_session_id = extract_cc_session_id(body)
    # SubagentStart 会把签名身份标记写入子代理请求。解析正文前先验签并剥离，
    # 防止 transport 身份进入模型上下文；父 session 不符时 fail-closed，绝不串接 CID。
    transport_extraction = extract_and_strip_transport(body, agent_store)
    # extract 会原地净化 body；后续判枪、Dify 出站与请求日志都只看剥离后的正文。
    # marker 的审计信息单列在 log extra，不能把签名 transport 当成提示词再落盘。
    agent_transport = transport_extraction.transport
    transport_parent_mismatch = bool(
        agent_transport
        and request_cc_session_id
        and agent_transport.parent_session_id != request_cc_session_id
    )
    if transport_parent_mismatch:
        agent_transport = None
    if agent_transport:
        hook_identity_status = "verified"
    elif transport_parent_mismatch:
        hook_identity_status = "parent_mismatch"
    elif transport_extraction.ambiguous:
        hook_identity_status = "ambiguous"
    elif transport_extraction.invalid:
        hook_identity_status = "invalid"
    else:
        hook_identity_status = "missing"
    accept_sse = "text/event-stream" in (request.headers.get("accept") or "").lower()
    plan = build_plan(
        body,
        accept_sse=accept_sse,
        tool_structured=TOOL_STRUCTURED,
        agent_id=agent_transport.agent_id if agent_transport else None,
    )

    def _log_request(kind: str, extra: dict[str, Any] | None = None) -> Path | None:
        """请求日志的唯一入口：LOG_REQUESTS 门、fail-open 与 extra 基座各只存在一处。"""
        if not LOG_REQUESTS:
            return None
        try:
            return write_request_log(
                LOG_DIR,
                body,
                kind=kind,
                extra={
                    **plan.log_extra(),
                    "request_id": request_id,
                    "parent_cc_session_id": (
                        agent_transport.parent_session_id if agent_transport else None
                    ),
                    "agent_id": agent_transport.agent_id if agent_transport else None,
                    "agent_type": agent_transport.agent_type if agent_transport else None,
                    "transport_markers_removed": transport_extraction.removed,
                    "transport_ambiguous": transport_extraction.ambiguous,
                    "transport_parent_mismatch": transport_parent_mismatch,
                    "hook_identity_status": hook_identity_status,
                    **(extra or {}),
                },
                request_id=request_id,
                fold_query=(plan.kind == "title"),
            )
        except Exception as e:
            _req_log("log failed: {}".format(e))
            return None

    def _local_answer(
        text: str,
        *,
        label: str,
        input_tokens: int = 1,
        output_tokens: int = 1,
    ):
        """本地出口的统一响应：按本枪形态构造 SSE / JSON。

        这里的 local_response_ready 只表示代理已经构造好响应；本地短路目前
        不经过 TracedStreamingResponse，所以它不等于客户端已收到。
        """
        _req_log(
            "local_response_ready http=200 elapsed={:.1f}s local={}".format(
                time.monotonic() - request_started, label
            )
        )
        if plan.stream:
            return StreamingResponse(
                iter(
                    iter_plain_text_sse(
                        model=plan.model,
                        text=text,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                ),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        return JSONResponse(
            build_non_stream_message(
                model=plan.model,
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )

    # 显式 terminal-tool：首枪只登记 after_success；下一枪仅在同一主会话的
    # Write/Edit tool_result 全部明确成功时本地释放草案，否则消费待决并回到 Dify。
    terminal_resolution = TerminalResolution()
    terminal_resolve_error = ""
    if plan.is_main_window:
        try:
            terminal_resolution = terminal_store.resolve(
                DIFY_USER_ID, request_cc_session_id, body
            )
        except Exception as e:
            # terminal 是可选省枪层；状态故障必须回落到普通 Dify 主链。
            terminal_resolve_error = "{}: {}".format(type(e).__name__, e)
            print("[lan] terminal resolve failed open: {}".format(terminal_resolve_error))
    if terminal_resolution.status == "success":
        local_input_tokens = estimate_input_tokens_from_request(body=body)
        local_output_tokens = max(1, len(terminal_resolution.text) // 4)
        try:
            ingest_messages_into_cache(body, read_cache, DIFY_USER_ID)
        except Exception as e:
            print("[lan] terminal cache ingest skipped: {}".format(e))
        print(
            "[lan] terminal local → {} ids={} (Dify skipped)".format(
                ",".join(terminal_resolution.tool_names),
                ",".join(x[:12] for x in terminal_resolution.tool_ids),
            )
        )
        log_path = _log_request(
            "terminal_local",
            {
                "gun_kind": "terminal_local",
                "skipped_dify": True,
                "terminal_local": True,
                "terminal_reason": terminal_resolution.reason,
                "terminal_tool_ids": list(terminal_resolution.tool_ids),
                "terminal_tool_names": list(terminal_resolution.tool_names),
                "cc_session_id": request_cc_session_id,
            },
        )
        patch_request_log(
            log_path,
            {
                "stop_reason": "end_turn",
                "text_len": len(terminal_resolution.text),
                "reasoning_len": 0,
                "tool_count": 0,
                "input_tokens": local_input_tokens,
                "output_tokens": local_output_tokens,
                "terminal_local": True,
                "skipped_dify": True,
            },
            log_dir=LOG_DIR,
        )
        return _local_answer(
            terminal_resolution.text,
            label="terminal",
            input_tokens=local_input_tokens,
            output_tokens=local_output_tokens,
        )

    # 本地短路：不传 Dify、不计费
    if plan.is_placeholder:
        _req_log("placeholder agent task → local short-circuit")
        _log_request("placeholder", {"skipped_dify": True})
        return _local_answer(
            "（已忽略：子代理任务正文是未填充占位模板 sys/hist/msg，"
            "未调用 Dify、未计费。请让主会话用具体任务再派 Agent。）",
            label="placeholder",
        )

    # 这是 Claude Code 的 UI heartbeat，不是用户任务：本地生成短句即可，
    # 否则每个子代理会为状态栏额外占用一枪、一个 CID 续写和一份上下文。
    if plan.is_action_status:
        status_text = build_action_status(body)
        _req_log("action status local → {!r} (Dify skipped)".format(status_text))
        log_path = _log_request(
            "status_local",
            {
                "gun_kind": "status",
                "skipped_dify": True,
                "local_status": True,
                "agent_phase": "status",
                "cc_session_id": request_cc_session_id,
            },
        )
        patch_request_log(
            log_path,
            {
                "stop_reason": "end_turn",
                "text_len": len(status_text),
                "reasoning_len": 0,
                "tool_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "local_status": True,
                "skipped_dify": True,
            },
            log_dir=LOG_DIR,
        )
        return _local_answer(
            status_text,
            label="status",
            input_tokens=0,
            output_tokens=0,
        )

    client = _client()
    if plan.is_sidecar_summary:
        input_limits = DifyInputLimits({}, "skipped")
    else:
        input_limits = await _load_input_limits(api_key=api_key, client=client)
        if input_limits.error:
            _req_log(
                "input limits source={} fail_open={} error={}".format(
                    input_limits.source,
                    "no" if input_limits.limits else "yes",
                    input_limits.error,
                )
            )

    parsed = parse_payload(body)
    agent_link_count = 0
    agent_archive_status: dict[str, Any] = {"count": 0, "source": "none"}
    agent_archive_error = ""
    try:
        # 消息链内的任务通知是真源；hook 档案只是有界的 fork/恢复兜底。
        # 因此查档案前排除链内已有报告，避免同一报告被注入两次。
        agent_link_count = agent_store.link_notifications(
            list(parsed.get("agent_notifications") or []),
            parent_hint=request_cc_session_id,
        )
        trusted_tool_ids = {
            str(item.get("tool_use_id") or "")
            for item in (parsed.get("agent_notifications") or [])
            if isinstance(item, dict) and item.get("tool_use_id")
        }
        pending_tool_ids = {
            str(item.get("tool_use_id") or "")
            for item in (parsed.get("agent_lifecycle", {}).get("pending") or [])
            if isinstance(item, dict) and item.get("tool_use_id")
        }
        all_agent_tool_ids = {
            str(item.get("tool_use_id") or "")
            for item in (parsed.get("agent_calls") or [])
            if isinstance(item, dict) and item.get("tool_use_id")
        }
        # false pending 可自动查一次完成档案；已结束的历史委派只有在用户明确索取
        # 报告时才查。否则每轮都会把旧报告重新灌入当前上下文。
        eligible_tool_ids = set(pending_tool_ids)
        if wants_archived_agent_reports(str(parsed.get("current_user") or "")):
            eligible_tool_ids.update(all_agent_tool_ids)
        eligible_tool_ids.difference_update(trusted_tool_ids)
        if plan.is_main_window and eligible_tool_ids:
            archived = agent_store.find_completed(tool_use_ids=eligible_tool_ids)
            agent_archive_status = merge_archived_reports(parsed, archived)
    except Exception as exc:
        # hook 档案是恢复兜底；任何读写故障都回落到现有消息链。
        agent_archive_error = "{}: {}".format(type(exc).__name__, exc)
        _req_log("agent archive failed open: {}".format(agent_archive_error))
    try:
        ob = prepare_text_outbound(
            body=body,
            plan=plan,
            parsed=parsed,
            user_id=DIFY_USER_ID,
            read_cache=read_cache,
            input_char_limits=input_limits.limits,
            input_limits_source=input_limits.source,
        )
    except DifyInputLengthError as e:
        print(
            "[lan] input preflight rejected key={} chars={} limit={} source={} (not truncated)".format(
                e.key, e.length, e.limit, input_limits.source
            )
        )
        _log_request(
            "rejected_input",
            {
                "skipped_dify": True,
                "rejected_input": True,
                "input_length_error": {
                    "key": e.key,
                    "chars": e.length,
                    "limit": e.limit,
                    "lengths": dict(e.lengths),
                    "limits": dict(e.limits),
                    "source": input_limits.source,
                },
                "parse_notes": parsed.get("notes"),
            },
        )
        message = (
            "Dify 逻辑输入字段 {!r} 为 {} 个字符，单字段 max_length={}，"
            "当前已发布的同名分片也无法完整容纳。输入未被裁剪，也未发送至 Dify。"
        ).format(e.key, e.length, e.limit)
        message += input_shard_capacity_hint(e.key, input_limits.limits)
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": message},
                "request_id": "req_{}".format(request_id),
            },
        )
    except DifyPersistenceSizeError as e:
        print(
            "[lan] persisted input preflight rejected key={} size={} limit={} (not truncated)".format(
                e.key, e.size, e.limit
            )
        )
        _log_request(
            "rejected_input",
            {
                "skipped_dify": True,
                "rejected_input": True,
                "input_persistence_error": {
                    "key": e.key,
                    "python_size": e.size,
                    "limit": e.limit,
                    "sizes": dict(e.sizes),
                },
                "parse_notes": parsed.get("notes"),
            },
        )
        message = (
            "Dify 输入字段 {!r} 的持久变量内存占用为 {} 字节，超过下一轮 conversation "
            "恢复上限 {}。输入未被裁剪，也未发送至 Dify。"
        ).format(e.key, e.size, e.limit)
        message += input_shard_capacity_hint(e.key, input_limits.limits)
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": message},
                "request_id": "req_{}".format(request_id),
            },
        )

    _req_log(
        "route={} model={} kind={} reasons={} tools={} subagent={} trim={} struct={}".format(
            plan.route,
            plan.model,
            plan.kind,
            ",".join(plan.route_reasons),
            "on" if plan.enable_tools else "off",
            "yes" if plan.is_subagent else "no",
            plan.trim_mode or "-",
            "on" if plan.tool_structured else "off",
        )
    )

    log_path = _log_request(
        ("subagent" if plan.is_subagent else plan.kind),
        {
            **outbound_log_extra(ob),
            "parse_notes": parsed.get("notes"),
            "tools_count": len(body.get("tools") or [])
            if isinstance(body.get("tools"), list)
            else 0,
            "terminal_fallback": terminal_resolution.status == "fallback",
            "terminal_fallback_reason": terminal_resolution.reason,
            "terminal_resolve_error": terminal_resolve_error,
            "agent_notification_links": agent_link_count,
            "agent_report_source": agent_archive_status.get("source"),
            "agent_archive_reports": agent_archive_status.get("count"),
            "agent_archive_error": agent_archive_error,
        },
    )
    if log_path is not None:
        _req_log("log → {}".format(log_path.name))

    trace_sequence = 0

    def _transport_trace(event: str, **fields: Any) -> None:
        nonlocal trace_sequence
        durable = bool(fields.pop("_durable", False))
        trace_sequence += 1
        append_transport_trace(
            log_path,
            request_id=request_id,
            sequence=trace_sequence,
            event=event,
            elapsed_seconds=time.monotonic() - request_started,
            fields=fields,
            durable=durable,
        )

    _transport_trace(
        "request_log_created",
        _durable=True,
        stream=bool(plan.stream),
        route=plan.route,
        gun_kind=plan.kind,
        attachment_scope=plan.attachment_scope,
    )

    # 当前判枪下到不了：非 placeholder 枪的 route_tag 恒非空，build_dify_query 至少返回它。
    # 留作 fail-safe——判枪或 query 组装哪天产出空串，宁可 400 也不给 Dify 发一个空 query。
    if not (ob.query or "").strip():
        raise HTTPException(status_code=400, detail="Empty query after parsing messages")

    # 会话附着：主窗口与子代理使用不同命名空间。锁键沿用同一命名空间，使并发
    # 子代理只串行各自的首次 conversation 创建，不可能竞态附着到父窗口 CID。
    attachment_scope = plan.attachment_scope
    cc_session_id = request_cc_session_id if attachment_scope == "main" else None
    parent_cc_session_id = (
        agent_transport.parent_session_id
        if attachment_scope == "subagent" and agent_transport
        else request_cc_session_id
        if attachment_scope == "main"
        else None
    )
    agent_id = (
        agent_transport.agent_id
        if attachment_scope == "subagent" and agent_transport
        else None
    )
    session_bind = "skip"
    conversation_id = None
    binding_epoch: int | None = None
    scope_epoch: int | None = None
    reset_epoch: int | None = None
    session_lock = (
        _session_lock(
            "{}:{}:{}".format(
                attachment_scope,
                parent_cc_session_id or "__missing__",
                agent_id or "-",
            )
        )
        if attachment_scope != "none"
        else None
    )

    async def prepare_session_attachment() -> None:
        nonlocal conversation_id, session_bind, binding_epoch, scope_epoch, reset_epoch
        if attachment_scope == "none":
            return
        if attachment_scope == "subagent":
            resolved = store.resolve_agent_conversation(
                DIFY_USER_ID, parent_cc_session_id, agent_id
            )
        else:
            resolved = store.resolve_conversation(DIFY_USER_ID, cc_session_id)
        cid = resolved.get("conversation_id")
        conversation_id = cid.strip() if isinstance(cid, str) and cid.strip() else None
        session_bind = str(resolved.get("session_bind") or "unknown")
        try:
            binding_epoch = int(resolved.get("binding_epoch"))
        except (TypeError, ValueError):
            binding_epoch = None
        try:
            scope_epoch = int(resolved.get("scope_epoch"))
        except (TypeError, ValueError):
            scope_epoch = None
        try:
            reset_epoch = int(resolved.get("reset_epoch"))
        except (TypeError, ValueError):
            reset_epoch = None

    def remember(cid: str) -> None:
        nonlocal conversation_id
        # Dify 只在成功收尾后才给出可用 CID；先更新本枪日志状态，再按 scope
        # 写入对应 namespace，避免失败枪污染主/子会话绑定。
        conversation_id = cid
        if attachment_scope == "main":
            try:
                ok = store.remember(
                    DIFY_USER_ID,
                    cid,
                    cc_session_id=cc_session_id,
                    expected_epoch=binding_epoch,
                    expected_scope_epoch=scope_epoch,
                    expected_reset_epoch=reset_epoch,
                )
                if ok is False:
                    _req_log("conversation remember skipped: binding scope changed")
            except Exception as exc:
                _req_log("conversation remember failed open: {!r}".format(exc))
        elif attachment_scope == "subagent":
            try:
                ok = store.remember_agent(
                    DIFY_USER_ID,
                    cid,
                    parent_cc_session_id=parent_cc_session_id,
                    agent_id=agent_id,
                    expected_epoch=None,
                    expected_scope_epoch=scope_epoch,
                    expected_reset_epoch=reset_epoch,
                )
                if ok is False:
                    _req_log("agent conversation remember skipped: binding scope changed")
            except Exception as exc:
                _req_log("agent conversation remember failed open: {!r}".format(exc))
        _patch_session_state()

    ob = await attach_images_to_outbound(
        ob,
        body=body,
        client=client,
        base_url=DIFY_BASE_URL,
        api_key=api_key,
        user=DIFY_USER_ID,
        is_sidecar=plan.is_sidecar_summary,
    )
    metrics_line = outbound_metrics_line(ob)
    _req_log(metrics_line)

    def _log_session_attachment() -> None:
        _req_log(
            "dify chat scope={} cid={} bind={} parent_sid={} agent={} "
            "files×{} query_chars={}".format(
                attachment_scope,
                (conversation_id or "")[:12] or "-",
                session_bind,
                (parent_cc_session_id or "")[:8] or "-",
                (agent_id or "")[:12] or "-",
                len(ob.dify_files),
                len(ob.query or ""),
            )
        )

    input_tokens_hint = estimate_input_tokens_from_request(
        query=ob.query,
        inputs=ob.dify_inputs,
        body=body if plan.is_main_window else None,
    )

    def _bill_on_accepted() -> None:
        # 守则 6：上游接受流即记一枪。此处不设「只记一次」的闸——若上游真的二次开流，
        # 那是第二次 LLM 运行，本就该再记一枪，不该被吞掉。
        if not plan.bill:
            return
        try:
            snap = meter.record(
                route=plan.route,
                kind=plan.kind,
                is_subagent=plan.is_subagent,
                is_main=plan.is_main_window,
            )
        except Exception as exc:
            # 上游已接受请求时，计费故障不能反向抛掉已获得的答案。
            _req_log("bill record failed open: {!r}".format(exc))
            return
        _req_log(
            "bill route={} opus×{} ${} | {}".format(
                plan.route, snap["opus_calls"], snap["estimated_usd"], meter.statusline()
            )
        )

    if LOG_REQUESTS and log_path is not None:
        patch_request_log(
            log_path,
            {
                "attach_main": plan.attach_main,
                "attachment_scope": attachment_scope,
                "conversation_id_out": conversation_id,
                "cc_session_id": cc_session_id,
                "parent_cc_session_id": parent_cc_session_id,
                "agent_id": agent_id,
                "session_bind": session_bind,
                "dify_files": len(ob.dify_files),
                "image_failed": ob.image_failed,
                "image_notes": ob.img_notes[:12],
                "image_b64_bytes": ob.image_b64_bytes,
                "image_upload_status": ob.image_upload_status,
                # 附图映射只有在 attach_images_to_outbound 之后才有值；
                # 它是守则 9「不装看见」的取证材料，必须落在这一批而非出站装配那一批。
                "image_mapping": list(ob.image_mapping or []),
                "dify_query_final_chars": len(ob.query or ""),
                "dify_query_final_head": (ob.query or "")[:400],
                "tool_continue": ob.is_tool_continue,
            },
            log_dir=LOG_DIR,
            into="summary",
        )

    def _patch_response(parts: dict[str, Any]) -> None:
        if LOG_REQUESTS and log_path is not None:
            try:
                patch_request_log(
                    log_path,
                    response_log_patch(parts, dify_files=len(ob.dify_files)),
                    log_dir=LOG_DIR,
                )
            except Exception as e:
                # response_log_patch 还会读取工具摘要的外部形状；即使上游产出
                # 畸形 parts，日志旁路也不能反向打断已经得到的答复。
                print("[lan] response log patch failed: {}".format(e))

    def _patch_summary(values: dict[str, Any]) -> None:
        # patch_request_log 的整个函数体在 try 内、自身不抛（log.py），故此处不再包一层。
        if LOG_REQUESTS and log_path is not None:
            patch_request_log(
                log_path,
                values,
                log_dir=LOG_DIR,
                into="summary",
            )

    def _patch_session_state() -> None:
        agent_phase = (
            "initial"
            if attachment_scope == "subagent" and session_bind == "miss"
            else "continuation"
            if attachment_scope == "subagent" and session_bind == "hit"
            else "none"
        )
        _patch_summary(
            {
                "conversation_id_out": conversation_id,
                "cc_session_id": cc_session_id,
                "parent_cc_session_id": parent_cc_session_id,
                "agent_id": agent_id,
                "attachment_scope": attachment_scope,
                "agent_phase": agent_phase,
                "session_bind": session_bind,
            }
        )

    def _register_terminal(parts: dict[str, Any]) -> None:
        after_success = str(parts.get("after_success") or "").strip()
        if not after_success:
            return
        try:
            outcome = terminal_store.register(
                DIFY_USER_ID,
                request_cc_session_id if plan.is_main_window else None,
                list(parts.get("tool_uses") or []),
                after_success,
            )
        except Exception as e:
            parts["terminal_pending"] = False
            parts["terminal_pending_reason"] = "store_error"
            parts["terminal_register_error"] = "{}: {}".format(type(e).__name__, e)
            _req_log(
                "terminal register failed open: {}".format(
                    parts["terminal_register_error"]
                )
            )
            return
        parts["terminal_pending"] = bool(outcome)
        parts["terminal_pending_reason"] = outcome.reason
        if outcome:
            _req_log(
                "terminal pending → {} ids={}".format(
                    ",".join(parts.get("tool_names") or []),
                    ",".join(
                        str(t.get("id") or "")[:12]
                        for t in parts.get("tool_uses") or []
                        if isinstance(t, dict)
                    ),
                )
            )

    def _on_dify_transport(event: str, fields: dict[str, Any]) -> None:
        # Dify 回调字段只包含事件计数/类型与异常摘要，不含 data 正文。
        _transport_trace(
            event,
            _durable=event
            in {
                "dify_response_headers",
                "dify_first_event",
                "dify_terminal_event",
                "dify_stream_cancelled",
                "dify_stream_error",
                "dify_stream_closed",
            },
            **fields,
        )

    def _new_event_iter():
        return stream_chat_messages(
            base_url=DIFY_BASE_URL,
            api_key=api_key,
            user=DIFY_USER_ID,
            query=ob.query,
            conversation_id=conversation_id,
            client=client,
            inputs=ob.dify_inputs,
            files=ob.dify_files or None,
            on_accepted=_bill_on_accepted,
            on_transport_event=_on_dify_transport,
        )

    flight_state = "direct"
    flight_key = ""
    try:
        if plan.stream:
            result_out: dict[str, Any] = {}
            stream_completed = False
            stream_exception: BaseException | None = None
            stream_error_event = False
            message_stop_generated = False

            async def gen_and_patch():
                nonlocal stream_completed, stream_exception, stream_error_event, message_stop_generated
                lock_acquired = False
                if session_lock is not None:
                    await session_lock.acquire()
                    lock_acquired = True
                try:
                    await prepare_session_attachment()
                    _patch_session_state()
                    _log_session_attachment()
                    event_iter = _new_event_iter()
                    async for line in dify_events_to_anthropic_sse(
                        event_iter,
                        model=plan.model,
                        on_conversation_id=remember,
                        enable_tools=plan.enable_tools,
                        input_tokens_hint=input_tokens_hint,
                        result_out=result_out,
                        on_final_parts=_register_terminal,
                        decode_unicode_wire=ob.unicode_wire_active,
                    ):
                        event_types = _sse_event_types(line)
                        generated_index = int(result_out.get("_trace_generated_chunks") or 0) + 1
                        result_out["_trace_generated_chunks"] = generated_index
                        if _trace_chunk(generated_index, event_types):
                            _transport_trace(
                                "downstream_chunk_generated",
                                body_chunk_index=generated_index,
                                body_bytes=len(line.encode("utf-8")),
                                event_types=event_types,
                            )
                        if "error" in event_types:
                            stream_error_event = True
                        if "message_stop" in event_types:
                            message_stop_generated = True
                        yield line
                    stream_completed = True
                    _transport_trace(
                        "downstream_stream_generated_complete",
                        _durable=True,
                        message_stop_generated=message_stop_generated,
                        error_event_generated=stream_error_event,
                    )
                except asyncio.CancelledError as exc:
                    stream_exception = exc
                    result_out.setdefault("error", "CancelledError")
                    _transport_trace(
                        "downstream_generator_cancelled",
                        _durable=True,
                        exception_type=type(exc).__name__,
                        message_stop_generated=message_stop_generated,
                        error_event_generated=stream_error_event,
                    )
                    raise
                except BaseException as exc:
                    stream_exception = exc
                    result_out.setdefault(
                        "error",
                        "{}: {}".format(type(exc).__name__, str(exc)).strip(),
                    )
                    _req_log(
                        "stream error: {}: {}".format(type(exc).__name__, str(exc))
                    )
                    _transport_trace(
                        "downstream_generator_error",
                        _durable=True,
                        exception_type=type(exc).__name__,
                        exception_message=str(exc).strip()[:400],
                        message_stop_generated=message_stop_generated,
                        error_event_generated=stream_error_event,
                    )
                    raise
                finally:
                    client_disconnected: bool | None
                    try:
                        client_disconnected = await request.is_disconnected()
                    except Exception as exc:
                        client_disconnected = None
                        _transport_trace(
                            "downstream_disconnect_probe_error",
                            exception_type=type(exc).__name__,
                            exception_message=str(exc).strip()[:400],
                        )
                    delivery_status = (
                        "stream_error_delivered"
                        if stream_completed and stream_error_event
                        else "stream_complete"
                        if stream_completed and message_stop_generated
                        else "stream_complete_without_message_stop"
                        if stream_completed
                        else "client_disconnected"
                        if client_disconnected is True
                        else "stream_incomplete"
                    )
                    _transport_trace(
                        "downstream_stream_finally",
                        _durable=True,
                        stream_completed=stream_completed,
                        client_disconnected=client_disconnected,
                        delivery_status=delivery_status,
                        exception_type=(
                            type(stream_exception).__name__
                            if stream_exception is not None
                            else ""
                        ),
                        message_stop_generated=message_stop_generated,
                        error_event_generated=stream_error_event,
                    )
                    _patch_response(result_out)
                    elapsed = time.monotonic() - request_started
                    _patch_summary(
                        {
                            "delivery_status": delivery_status,
                            "stream_completed": stream_completed,
                            "message_stop_generated": message_stop_generated,
                            "error_event_generated": stream_error_event,
                            "client_disconnected": client_disconnected,
                            "elapsed_seconds": round(elapsed, 3),
                        }
                    )
                    _req_log("done http=stream elapsed={:.1f}s".format(elapsed))
                    if lock_acquired:
                        session_lock.release()

            return TracedStreamingResponse(
                gen_and_patch(),
                trace=_transport_trace,
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        namespace = "{}\0{}\0{}\0{}\0{}\0{}".format(
            DIFY_BASE_URL,
            DIFY_USER_ID,
            api_key,
            attachment_scope,
            parent_cc_session_id or "",
            agent_id or "",
        )
        flight_key = request_fingerprint(body, namespace=namespace)
        upstream_started = time.monotonic()

        async def _run_collect_once() -> NonStreamAnswer:
            try:
                result = await collect_dify_answer(
                    _new_event_iter(),
                    on_conversation_id=remember,
                    enable_tools=plan.enable_tools,
                    input_tokens_hint=input_tokens_hint,
                    decode_unicode_wire=ob.unicode_wire_active,
                )
            except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
                # 内层唯一不可替代的作用：owner 若已因 shield 的 CancelledError 离场，
                # 共享任务稍后失败时只有这里还能把错误写进 owner 自己的日志。
                detail = (
                    "Upstream request failed: {}".format(e)
                    if isinstance(e, httpx.RequestError)
                    else str(e)
                )
                _patch_response({"error": detail, "empty_upstream": True})
                _req_log(
                    "upstream failed key={} elapsed={:.1f}s error={}".format(
                        flight_key[:12], time.monotonic() - upstream_started, detail
                    )
                )
                raise

            text, reasoning, tool_uses, usage, parts = result
            _register_terminal(parts)
            _patch_response(parts)
            _req_log(
                "upstream done key={} elapsed={:.1f}s in={} out={} stop={} text_len={}".format(
                    flight_key[:12],
                    time.monotonic() - upstream_started,
                    usage["input_tokens"],
                    usage["output_tokens"],
                    parts.get("stop_reason"),
                    parts.get("text_len"),
                )
            )
            return result

        async def _collect_once() -> NonStreamAnswer:
            lock_acquired = False
            if session_lock is not None:
                await session_lock.acquire()
                lock_acquired = True
            try:
                await prepare_session_attachment()
                _patch_session_state()
                _log_session_attachment()
                return await _run_collect_once()
            finally:
                if lock_acquired:
                    session_lock.release()

        lease = await _nonstream_flights().acquire(flight_key, _collect_once)
        flight_state = lease.state
        _patch_summary(
            {
                "request_fingerprint": flight_key[:16],
                "singleflight_state": flight_state,
                "singleflight_age_seconds": round(lease.age_seconds, 3),
            }
        )
        _req_log(
            "flight key={} state={} age={:.1f}s".format(
                flight_key[:12], flight_state, lease.age_seconds
            )
        )
        try:
            text, reasoning, tool_uses, usage, parts = await asyncio.shield(lease.task)
        except asyncio.CancelledError:
            elapsed = time.monotonic() - request_started
            _patch_summary(
                {
                    "delivery_status": "client_detached",
                    "elapsed_seconds": round(elapsed, 3),
                }
            )
            _req_log(
                "detached elapsed={:.1f}s flight={} upstream=continues".format(
                    elapsed, flight_state
                )
            )
            raise

        if flight_state != "start":
            # 共享任务只替 owner 落过响应；join/replay 仍需补齐自己的请求日志。
            _patch_response(parts)

        in_tok = usage["input_tokens"]
        out_tok = usage["output_tokens"]
        elapsed = time.monotonic() - request_started
        client_disconnected = await request.is_disconnected()
        delivery_status = (
            "client_disconnected_before_delivery"
            if client_disconnected
            else "ready"
        )
        _patch_summary(
            {
                "delivery_status": delivery_status,
                "elapsed_seconds": round(elapsed, 3),
            }
        )
        _req_log(
            "deliver status={} http=200 elapsed={:.1f}s flight={} stop={} text_len={} env={}".format(
                delivery_status,
                elapsed,
                flight_state,
                parts.get("stop_reason"),
                parts.get("text_len"),
                1 if parts.get("envelope") else 0,
            )
        )
        if tool_uses:
            _req_log(
                "tool_use ×{} → {}".format(
                    len(tool_uses), ",".join(t.get("name") or "?" for t in tool_uses)
                )
            )
            return JSONResponse(
                build_non_stream_with_tools(
                    model=plan.model,
                    text=text,
                    reasoning=reasoning,
                    tool_uses=tool_uses,
                    message_id="msg_{}".format(flight_key[:24]),
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            )
        return JSONResponse(
            build_non_stream_message(
                model=plan.model,
                text=text,
                reasoning=reasoning,
                message_id="msg_{}".format(flight_key[:24]),
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
        )
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        # 三类上游故障的记账完全同形，只在 detail 文案与 401 映射上不同；
        # 守则 19：此处记 delivery_status=error，与内层的 upstream failed 行分开。
        if isinstance(e, httpx.HTTPStatusError):
            detail = str(e)
            upstream_status = e.response.status_code if e.response is not None else 502
            status = 401 if upstream_status == 401 else 502
        elif isinstance(e, httpx.RequestError):
            detail = "Upstream request failed: {}".format(e)
            status = 502
        else:
            detail = str(e)
            status = 502
        _patch_response({"error": detail, "empty_upstream": True})
        elapsed = time.monotonic() - request_started
        _patch_summary(
            {"delivery_status": "error", "elapsed_seconds": round(elapsed, 3)}
        )
        _req_log(
            "done http={} elapsed={:.1f}s flight={} error={}".format(
                status, elapsed, flight_state, detail
            )
        )
        raise HTTPException(status_code=status, detail=detail) from e


def main() -> None:
    import socket
    import sys
    import urllib.request

    import uvicorn

    host = LISTEN_HOST
    port = int(os.getenv("PORT", "7272"))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
                try:
                    probe_request = urllib.request.Request(
                        "http://{}:{}/health".format(probe_host, port),
                        headers={"x-api-key": AUX_ADMIN_TOKEN}
                        if AUX_ADMIN_TOKEN
                        else {},
                    )
                    with urllib.request.urlopen(probe_request, timeout=0.5) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    if (
                        response.status == 200
                        and isinstance(payload, dict)
                        and payload.get("ok") is True
                        and payload.get("service") == "lan-proxy"
                    ):
                        print("[lan] {}:{} already running — proxy is up.".format(host, port))
                        print("[lan] to restart: stop the old process, then run lan again.")
                        sys.exit(0)
                except Exception:
                    pass
                print(
                    "[lan] {}:{} is occupied by another service; refusing to start.".format(
                        host, port
                    )
                )
                sys.exit(1)
    except OSError:
        pass

    print("[lan] starting http://{}:{}".format(host, port))
    print("[lan] CC Switch Base URL → http://{}:{}".format(host, port))
    print("[lan] model={} count_tokens=client_fallback".format(PRIMARY_DISCOVERY_MODEL_ID))
    print("[lan] tool_structured={}".format("on" if TOOL_STRUCTURED else "off"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
