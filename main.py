# -*- coding: utf-8 -*-
"""lan · Dify「岚」↔ Anthropic Messages 本地代理。

监听 127.0.0.1:7272，供 Claude Code / CC Switch 使用。改 .py 后须重启。
"""
from __future__ import annotations

import json
import os
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
from dify import stream_chat_messages
from log import patch_request_log, write_request_log
from meter import UsageMeter
from outbound import (
    attach_images_to_outbound,
    outbound_log_extra,
    outbound_metrics_line,
    prepare_text_outbound,
)
from parse import parse_payload
from plan import MODEL_ALIASES, build_plan
from sessions import SessionStore, extract_cc_session_id
from terminal import TerminalResolution, TerminalStore
from tools import TERMINAL_TOOL_NAMES

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _env_flag(name: str, default: str) -> bool:
    return (os.getenv(name) or default).strip() not in ("0", "false", "False")


DIFY_BASE_URL = os.getenv("DIFY_BASE_URL", "https://api.dify.ai/v1").rstrip("/")
DIFY_USER_ID = os.getenv("DIFY_USER_ID", "Liu Sheng")
DIFY_API_KEY_FALLBACK = (os.getenv("DIFY_API_KEY") or "").strip()
LOG_REQUESTS = _env_flag("LOG_REQUESTS", "1")
# 结构化工具出口：对带工具的 opus 枪注入 [[cc_struct:on]]。
# 默认关——当前插件 prefill 与上游模型不兼容，重开条件见 README / 架构.md。
TOOL_STRUCTURED = _env_flag("TOOL_STRUCTURED", "0")

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "request_logs"

store = SessionStore(DATA_DIR / "sessions.json")
meter = UsageMeter(DATA_DIR / "usage.json")
read_cache = ReadCache(DATA_DIR / "read_cache.json")
terminal_store = TerminalStore(DATA_DIR / "terminal_pending.json")
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
    try:
        yield
    finally:
        if http_client is not None:
            await http_client.aclose()
            http_client = None


app = FastAPI(title="Dify Anthropic Proxy", version="1.0.0", lifespan=lifespan)


def _client() -> httpx.AsyncClient:
    if http_client is None:
        raise HTTPException(status_code=503, detail="HTTP client not ready")
    return http_client


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        a = authorization.strip()
        return a[7:].strip() if a.lower().startswith("bearer ") else a
    if DIFY_API_KEY_FALLBACK:
        return DIFY_API_KEY_FALLBACK
    raise HTTPException(
        status_code=401,
        detail="缺少 API Key：请在 x-api-key / Authorization 传入 app-xxxx，或配置 DIFY_API_KEY",
    )


def _response_log_patch(parts: dict[str, Any], *, dify_files: int = 0) -> dict[str, Any]:
    """出站摘要 → 日志 response 字段（stream / 非流共用）。"""
    tool_inputs = [
        {
            "name": t["name"],
            "input_head": json.dumps(t["input"], ensure_ascii=False)[:400],
        }
        for t in (parts.get("tool_uses") or [])[:8]
    ]
    usage = parts.get("usage") or {}
    return {
        "stop_reason": parts.get("stop_reason"),
        "text_len": parts.get("text_len"),
        "reasoning_len": parts.get("reasoning_len"),
        "tool_count": parts.get("tool_count"),
        "tool_names": parts.get("tool_names"),
        "tool_inputs": tool_inputs,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "error": parts.get("error"),
        "empty_upstream": parts.get("empty_upstream"),
        "envelope": parts.get("envelope"),
        "dify_event_counts": parts.get("dify_event_counts"),
        "dify_event_total": parts.get("dify_event_total"),
        "workflow_status": parts.get("workflow_status"),
        "workflow_error": parts.get("workflow_error"),
        "dify_files": dify_files,
        "text_head": (parts.get("text_head") or parts.get("text") or "")[:200],
        "after_success_chars": int(parts.get("after_success_chars") or 0),
        "terminal_pending": bool(parts.get("terminal_pending")),
        "terminal_pending_reason": parts.get("terminal_pending_reason"),
        "terminal_register_error": parts.get("terminal_register_error"),
        "after_success_reason": parts.get("after_success_reason"),
        "structured_reply_dropped": bool(parts.get("structured_reply_dropped")),
    }


# ── 辅助端点 ─────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "user": DIFY_USER_ID,
        "dify_base": DIFY_BASE_URL,
        "tool_structured": TOOL_STRUCTURED,
        "terminal_tools": sorted(TERMINAL_TOOL_NAMES),
        "terminal_pending": terminal_store.pending_count(DIFY_USER_ID),
        "usage": meter.snapshot(),
    }


@app.get("/v1/usage")
@app.get("/usage")
async def usage_get() -> dict[str, Any]:
    return meter.snapshot()


@app.get("/v1/usage/statusline")
@app.get("/usage/statusline")
async def usage_statusline() -> Any:
    return PlainTextResponse(meter.statusline() + "\n")


@app.post("/v1/usage/reset")
@app.post("/usage/reset")
async def usage_reset() -> dict[str, Any]:
    return meter.reset()


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "dify"}
            for mid in MODEL_ALIASES
        ],
    }


@app.get("/sessions")
async def sessions_list() -> dict[str, Any]:
    return store.get_state(DIFY_USER_ID)


class NewSessionBody(BaseModel):
    cc_session_id: str | None = None
    clear_all: bool = False


@app.post("/sessions/new")
async def sessions_new(
    body: NewSessionBody = Body(default_factory=NewSessionBody),
) -> dict[str, Any]:
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
async def sessions_switch(body: SwitchBody) -> dict[str, Any]:
    try:
        out = store.switch(DIFY_USER_ID, body.conversation_id, body.cc_session_id)
        sid = body.cc_session_id or out.get("cc_session_id")
        if sid:
            terminal_store.clear_session(DIFY_USER_ID, str(sid))
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/debug/last-request")
async def debug_last_request() -> Any:
    path = LOG_DIR / "last_request.json"
    if not path.exists():
        return {"ok": False, "message": "尚无记录；先用 CC 发一条消息。"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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

    accept_sse = "text/event-stream" in (request.headers.get("accept") or "").lower()
    plan = build_plan(body, accept_sse=accept_sse, tool_structured=TOOL_STRUCTURED)
    request_cc_session_id = extract_cc_session_id(body)

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
        log_path = None
        if LOG_REQUESTS:
            try:
                log_path = write_request_log(
                    LOG_DIR,
                    body,
                    kind="terminal_local",
                    extra={
                        **plan.log_extra(),
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
            except Exception as e:
                print("[lan] terminal log failed: {}".format(e))
        if plan.stream:
            return StreamingResponse(
                iter(
                    iter_plain_text_sse(
                        model=plan.model,
                        text=terminal_resolution.text,
                        input_tokens=local_input_tokens,
                        output_tokens=local_output_tokens,
                    )
                ),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        return JSONResponse(
            build_non_stream_message(
                model=plan.model,
                text=terminal_resolution.text,
                input_tokens=local_input_tokens,
                output_tokens=local_output_tokens,
            )
        )

    # 本地短路：不传 Dify、不计费
    if plan.is_placeholder:
        print("[lan] placeholder agent task → local short-circuit")
        if LOG_REQUESTS:
            try:
                write_request_log(
                    LOG_DIR,
                    body,
                    kind="placeholder",
                    extra={**plan.log_extra(), "skipped_dify": True},
                )
            except Exception as e:
                print("[lan] log failed: {}".format(e))
        text = (
            "（已忽略：子代理任务正文是未填充占位模板 sys/hist/msg，"
            "未调用 Dify、未计费。请让主会话用具体任务再派 Agent。）"
        )
        if plan.stream:
            return StreamingResponse(
                iter(iter_plain_text_sse(model=plan.model, text=text)),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        return JSONResponse(
            build_non_stream_message(
                model=plan.model, text=text, input_tokens=1, output_tokens=1
            )
        )

    parsed = parse_payload(body)
    ob = prepare_text_outbound(
        body=body, plan=plan, parsed=parsed, user_id=DIFY_USER_ID, read_cache=read_cache
    )

    print(
        "[lan] route={} model={} kind={} reasons={} tools={} subagent={} trim={} struct={}".format(
            plan.route,
            plan.cc_model,
            plan.kind,
            ",".join(plan.route_reasons),
            "on" if plan.enable_tools else "off",
            "yes" if plan.is_subagent else "no",
            plan.trim_mode or "-",
            "on" if plan.tool_structured else "off",
        )
    )

    log_path = None
    if LOG_REQUESTS:
        try:
            log_path = write_request_log(
                LOG_DIR,
                body,
                kind=("subagent" if plan.is_subagent else plan.kind),
                extra={
                    **plan.log_extra(),
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
            print("[lan] log → {}".format(log_path.name))
        except Exception as e:
            print("[lan] log failed: {}".format(e))

    if not (ob.query or "").strip():
        raise HTTPException(status_code=400, detail="Empty query after parsing messages")

    # 会话附着：仅主对话 opus
    cc_session_id = request_cc_session_id if plan.attach_main else None
    session_bind = "skip"
    conversation_id = None
    if plan.attach_main:
        resolved = store.resolve_conversation(DIFY_USER_ID, cc_session_id)
        cid = resolved.get("conversation_id")
        conversation_id = cid.strip() if isinstance(cid, str) and cid.strip() else None
        session_bind = str(resolved.get("session_bind") or "unknown")

    def remember(cid: str) -> None:
        if plan.attach_main:
            store.remember(DIFY_USER_ID, cid, cc_session_id=cc_session_id)

    client = _client()
    ob = await attach_images_to_outbound(
        ob,
        body=body,
        client=client,
        base_url=DIFY_BASE_URL,
        api_key=api_key,
        user=DIFY_USER_ID,
        is_sidecar=plan.is_sidecar_summary,
    )
    print(outbound_metrics_line(ob))

    input_tokens_hint = estimate_input_tokens_from_request(
        query=ob.query,
        inputs=ob.dify_inputs,
        body=body if plan.is_main_window else None,
    )

    bill_state = {"done": False}

    def _bill_on_accepted() -> None:
        if bill_state["done"] or not plan.bill:
            return
        bill_state["done"] = True
        snap = meter.record(
            route=plan.route,
            kind=plan.kind,
            is_subagent=plan.is_subagent,
            is_main=plan.is_main_window,
        )
        print(
            "[lan] bill route={} opus×{} ${} | {}".format(
                plan.route, snap["opus_calls"], snap["estimated_usd"], meter.statusline()
            )
        )

    print(
        "[lan] dify chat attach_main={} cid={} bind={} cc_sid={} files×{} query_chars={}".format(
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
                "dify_query_final_chars": len(ob.query or ""),
                "dify_query_final_head": (ob.query or "")[:400],
                "tool_continue": ob.is_tool_continue,
                "need_read": ob.need_read,
                "read_cache_hit": bool(ob.cache_hits),
            },
            log_dir=LOG_DIR,
            into="summary",
        )

    def _patch_response(parts: dict[str, Any]) -> None:
        if LOG_REQUESTS and log_path is not None:
            try:
                patch_request_log(
                    log_path,
                    _response_log_patch(parts, dify_files=len(ob.dify_files)),
                    log_dir=LOG_DIR,
                )
            except Exception as e:
                print("[lan] response log patch failed: {}".format(e))

    def _register_terminal(parts: dict[str, Any]) -> None:
        after_success = str(parts.get("after_success") or "").strip()
        if not after_success:
            return
        try:
            ok = terminal_store.register(
                DIFY_USER_ID,
                request_cc_session_id if plan.is_main_window else None,
                list(parts.get("tool_uses") or []),
                after_success,
            )
        except Exception as e:
            parts["terminal_pending"] = False
            parts["terminal_pending_reason"] = "store_error"
            parts["terminal_register_error"] = "{}: {}".format(type(e).__name__, e)
            print(
                "[lan] terminal register failed open: {}".format(
                    parts["terminal_register_error"]
                )
            )
            return
        parts["terminal_pending"] = ok
        if not ok:
            parts["terminal_pending_reason"] = (
                "missing_main_cc_session_or_ineligible_batch"
            )
        else:
            parts["terminal_pending_reason"] = "registered"
            print(
                "[lan] terminal pending → {} ids={}".format(
                    ",".join(parts.get("tool_names") or []),
                    ",".join(
                        str(t.get("id") or "")[:12]
                        for t in parts.get("tool_uses") or []
                        if isinstance(t, dict)
                    ),
                )
            )

    try:
        event_iter = stream_chat_messages(
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

        if plan.stream:
            result_out: dict[str, Any] = {}

            async def gen_and_patch():
                try:
                    async for line in dify_events_to_anthropic_sse(
                        event_iter,
                        model=plan.model,
                        on_conversation_id=remember,
                        enable_tools=plan.enable_tools,
                        input_tokens_hint=input_tokens_hint,
                        result_out=result_out,
                        on_final_parts=_register_terminal,
                    ):
                        yield line
                except Exception as e:
                    result_out.setdefault("error", str(e))
                    print("[lan] stream error: {}".format(e))
                    raise
                finally:
                    _patch_response(result_out)

            return StreamingResponse(
                gen_and_patch(),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        text, reasoning, tool_uses, usage, parts = await collect_dify_answer(
            event_iter,
            on_conversation_id=remember,
            enable_tools=plan.enable_tools,
            input_tokens_hint=input_tokens_hint,
        )
        in_tok = usage["input_tokens"]
        out_tok = usage["output_tokens"]
        print(
            "[lan] usage in={} out={} stop={} text_len={} env={}".format(
                in_tok,
                out_tok,
                parts.get("stop_reason"),
                parts.get("text_len"),
                1 if parts.get("envelope") else 0,
            )
        )
        _register_terminal(parts)
        _patch_response(parts)
        if tool_uses:
            print(
                "[lan] tool_use ×{} → {}".format(
                    len(tool_uses), ",".join(t.get("name") or "?" for t in tool_uses)
                )
            )
            return JSONResponse(
                build_non_stream_with_tools(
                    model=plan.model,
                    text=text,
                    reasoning=reasoning,
                    tool_uses=tool_uses,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            )
        return JSONResponse(
            build_non_stream_message(
                model=plan.model,
                text=text,
                reasoning=reasoning,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
        )
    except httpx.HTTPStatusError as e:
        detail = str(e)
        status = e.response.status_code if e.response is not None else 502
        _patch_response({"error": detail, "empty_upstream": True})
        raise HTTPException(
            status_code=401 if status == 401 else 502, detail=detail
        ) from e
    except httpx.RequestError as e:
        _patch_response({"error": "Upstream request failed: {}".format(e), "empty_upstream": True})
        raise HTTPException(
            status_code=502, detail="Upstream request failed: {}".format(e)
        ) from e
    except RuntimeError as e:
        _patch_response({"error": str(e), "empty_upstream": True})
        raise HTTPException(status_code=502, detail=str(e)) from e


def main() -> None:
    import socket
    import sys

    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "7272"))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                print("[lan] {}:{} already running — proxy is up.".format(host, port))
                print("[lan] to restart: stop the old process, then run lan again.")
                sys.exit(0)
    except OSError:
        pass

    print("[lan] starting http://{}:{}".format(host, port))
    print("[lan] CC Switch Base URL → http://{}:{}".format(host, port))
    print("[lan] tool_structured={}".format("on" if TOOL_STRUCTURED else "off"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
