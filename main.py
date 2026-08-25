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
from cache import ReadCache, ingest_messages_into_cache
from dify import DifyInputLimits, DifyParameterCache, stream_chat_messages
from log import patch_request_log, response_log_patch, write_request_log
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

DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "request_logs"

store = SessionStore(DATA_DIR / "sessions.json")
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
    if body.clear_all:
        terminal_store.clear_all(DIFY_USER_ID)
    elif isinstance(body.cc_session_id, str) and body.cc_session_id.strip():
        terminal_store.clear_session(DIFY_USER_ID, body.cc_session_id.strip())
    else:
        for sid in out.get("unbound_cc") or []:
            terminal_store.clear_session(DIFY_USER_ID, str(sid))
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

    accept_sse = "text/event-stream" in (request.headers.get("accept") or "").lower()
    plan = build_plan(body, accept_sse=accept_sse, tool_structured=TOOL_STRUCTURED)
    request_cc_session_id = extract_cc_session_id(body)

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
        """本地出口的统一交付：按本枪裁定的形态回 SSE / JSON，并记一行完成日志。"""
        _req_log(
            "done http=200 elapsed={:.1f}s local={}".format(
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

    # 显式 terminal-tool：只消费与本 CC session 精确匹配的 Write/Edit 结果。
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
            "Dify 输入字段 {!r} 为 {} 个字符，超过该应用发布的 max_length={}。"
            "输入未被裁剪，也未发送至 Dify。"
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
        },
    )
    if log_path is not None:
        _req_log("log → {}".format(log_path.name))

    # 当前判枪下到不了：非 placeholder 枪的 route_tag 恒非空，build_dify_query 至少返回它。
    # 留作 fail-safe——判枪或 query 组装哪天产出空串，宁可 400 也不给 Dify 发一个空 query。
    if not (ob.query or "").strip():
        raise HTTPException(status_code=400, detail="Empty query after parsing messages")

    # 会话附着：仅主对话 opus
    cc_session_id = request_cc_session_id if plan.attach_main else None
    session_bind = "skip"
    conversation_id = None
    binding_epoch: int | None = None
    session_lock = (
        _session_lock(cc_session_id or "__missing__") if plan.attach_main else None
    )

    async def prepare_session_attachment() -> None:
        nonlocal conversation_id, session_bind, binding_epoch
        if not plan.attach_main:
            return
        resolved = store.resolve_conversation(DIFY_USER_ID, cc_session_id)
        cid = resolved.get("conversation_id")
        conversation_id = cid.strip() if isinstance(cid, str) and cid.strip() else None
        session_bind = str(resolved.get("session_bind") or "unknown")
        try:
            binding_epoch = int(resolved.get("binding_epoch"))
        except (TypeError, ValueError):
            binding_epoch = None

    def remember(cid: str) -> None:
        if plan.attach_main:
            try:
                ok = store.remember(
                    DIFY_USER_ID,
                    cid,
                    cc_session_id=cc_session_id,
                    expected_epoch=binding_epoch,
                )
                if ok is False:
                    _req_log("conversation remember skipped: binding epoch changed")
            except Exception as exc:
                _req_log("conversation remember failed open: {!r}".format(exc))

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

    _req_log(
        "dify chat attach_main={} cid={} bind={} cc_sid={} files×{} query_chars={}".format(
            "yes" if plan.attach_main else "no",
            (conversation_id or "")[:12] or "-",
            session_bind,
            (cc_session_id or "")[:8] or "-",
            len(ob.dify_files),
            len(ob.query or ""),
        )
    )
    if LOG_REQUESTS and log_path is not None:
        patch_request_log(
            log_path,
            {
                "attach_main": plan.attach_main,
                "conversation_id_out": conversation_id,
                "cc_session_id": cc_session_id,
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
        _patch_summary(
            {
                "conversation_id_out": conversation_id,
                "cc_session_id": cc_session_id,
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
        )

    flight_state = "direct"
    flight_key = ""
    try:
        if plan.stream:
            result_out: dict[str, Any] = {}

            async def gen_and_patch():
                lock_acquired = False
                if session_lock is not None:
                    await session_lock.acquire()
                    lock_acquired = True
                try:
                    await prepare_session_attachment()
                    _patch_session_state()
                    _req_log(
                        "session attach cid={} bind={}".format(
                            (conversation_id or "")[:12] or "-",
                            session_bind,
                        )
                    )
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
                        yield line
                except Exception as e:
                    result_out.setdefault("error", str(e))
                    _req_log("stream error: {}".format(e))
                    raise
                finally:
                    _patch_response(result_out)
                    elapsed = time.monotonic() - request_started
                    _patch_summary(
                        {
                            "delivery_status": "stream_closed",
                            "elapsed_seconds": round(elapsed, 3),
                        }
                    )
                    _req_log("done http=stream elapsed={:.1f}s".format(elapsed))
                    if lock_acquired:
                        session_lock.release()

            return StreamingResponse(
                gen_and_patch(),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        namespace = "{}\0{}\0{}".format(DIFY_BASE_URL, DIFY_USER_ID, api_key)
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
                _req_log(
                    "session attach cid={} bind={}".format(
                        (conversation_id or "")[:12] or "-",
                        session_bind,
                    )
                )
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
