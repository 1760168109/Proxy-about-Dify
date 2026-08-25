# -*- coding: utf-8 -*-
"""Dify 事件流 → Anthropic Messages（SSE / 非流 JSON）。

思考块：Dify 推理分离（reasoning_chunk）→ type=thinking 块；未分离时拆 <think> 标签。
工具出站三通道的消费端在 finalize_parts：
node_finished.structured_output → answer 外壳 → 正文标记解析，逐级回落。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator

from tools import (
    extract_structured_envelope,
    extract_tool_uses,
    find_stream_cut,
    has_protocol_residue,
    is_terminal_tool_batch,
    parse_after_success,
    terminal_draft_follows_tools,
    tool_uses_from_calls,
    toolu_id,
)
from unicode_wire import (
    UnicodeWireStreamDecoder,
    decode_unicode_wire_text,
    decode_unicode_wire_value,
)

# 单个 input_json_delta 的载荷上限（长 Write 分片下发）
_INPUT_JSON_CHUNK = 8000
# 投机流式尾部扣留：覆盖最长的 [[after_success]] 半截前缀，防隐藏协议外泄。
_HOLD = 32


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _chars_to_tokens(chars: int) -> int:
    """字符粗估 token（≈4 字符/token；Dify 精确用量在 message_end 才有）。"""
    return max(1, int(chars) // 4)


def sse_event(payload: dict[str, Any]) -> str:
    return "event: {}\ndata: {}\n\n".format(
        payload.get("type", "message"), json.dumps(payload, ensure_ascii=False)
    )


# ── SSE 原语 ─────────────────────────────────────────────────────────


def build_message_start(
    model: str, message_id: str, *, input_tokens: int = 0, output_tokens: int = 0
) -> dict[str, Any]:
    # CC 状态栏用 input_tokens 估上下文占用；写死 0 会显示空白
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
            },
        },
    }


def build_thinking_block_start(index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "thinking", "thinking": ""},
    }


def build_thinking_delta(text: str, index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "thinking_delta", "thinking": text},
    }


def build_text_block_start(index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def build_text_delta(text: str, index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def build_tool_use_block_start(index: int, tool_id: str, name: str) -> dict[str, Any]:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
    }


def build_input_json_delta(partial_json: str, index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }


def build_content_block_stop(index: int = 0) -> dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


def build_message_delta(
    stop_reason: str = "end_turn",
    *,
    output_tokens: int = 0,
    input_tokens: int | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {"output_tokens": int(output_tokens or 0)}
    if input_tokens is not None:
        usage["input_tokens"] = int(input_tokens or 0)
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    }


def build_message_stop() -> dict[str, Any]:
    return {"type": "message_stop"}


# ── 用量与思考拆分 ───────────────────────────────────────────────────


def _content_chars_without_image_bytes(content: Any) -> int:
    """content 文本长度；image base64 按固定开销计，防状态栏虚高。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return len(str(content))
    n = 0
    for block in content:
        if not isinstance(block, dict):
            n += len(str(block))
            continue
        btype = block.get("type")
        if btype == "image":
            n += 256
            continue
        if btype == "text":
            n += len(block.get("text") or "")
            continue
        try:
            n += len(json.dumps(block, ensure_ascii=False))
        except Exception:
            n += 64
    return n


def estimate_input_tokens_from_request(
    *,
    query: str = "",
    inputs: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> int:
    """字符粗估（≈4 字符/token）；Dify 精确用量在 message_end 才有。"""
    n = len(query or "")
    for v in (inputs or {}).values():
        n += len(v) if isinstance(v, str) else len(str(v)) if v is not None else 0
    if body:
        system = body.get("system")
        if isinstance(system, str):
            n += len(system)
        elif system is not None:
            n += _content_chars_without_image_bytes(system)
        for m in body.get("messages") or []:
            if isinstance(m, dict):
                n += _content_chars_without_image_bytes(m.get("content"))
    return _chars_to_tokens(n)


def extract_dify_usage(ev: dict[str, Any]) -> dict[str, int]:
    """message_end.metadata.usage → Anthropic 字段名。"""
    out = {"input_tokens": 0, "output_tokens": 0}
    if not isinstance(ev, dict):
        return out
    candidates: list[Any] = []
    meta = ev.get("metadata")
    if isinstance(meta, dict):
        candidates.append(meta)
        if isinstance(meta.get("usage"), dict):
            candidates.append(meta["usage"])
    if isinstance(ev.get("usage"), dict):
        candidates.append(ev["usage"])
    candidates.append(ev)
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        got = False
        try:
            if obj.get("prompt_tokens") is not None:
                out["input_tokens"] = int(obj["prompt_tokens"])
                got = True
            elif obj.get("input_tokens") is not None:
                out["input_tokens"] = int(obj["input_tokens"])
                got = True
        except (TypeError, ValueError):
            pass
        try:
            if obj.get("completion_tokens") is not None:
                out["output_tokens"] = int(obj["completion_tokens"])
                got = True
            elif obj.get("output_tokens") is not None:
                out["output_tokens"] = int(obj["output_tokens"])
                got = True
        except (TypeError, ValueError):
            pass
        if got:
            break
    return out


_THINK_TAG_RE = re.compile(r"(?is)<think>(.*?)</think>|<thinking>(.*?)</thinking>")


def split_think_and_text(raw: str) -> tuple[str, str]:
    """整段回复拆思考与正文（兼容 <think> / <thinking>）。"""
    if not raw:
        return "", ""
    thinks = [
        (m.group(1) or m.group(2) or "").strip() for m in _THINK_TAG_RE.finditer(raw)
    ]
    return "\n\n".join(t for t in thinks if t), _THINK_TAG_RE.sub("", raw).strip()


def _pull_reasoning_from_event(ev: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning", "thought", "thinking"):
        v = ev.get(key)
        if isinstance(v, str) and v:
            return v
    for nest in ("data", "metadata"):
        obj = ev.get(nest)
        if isinstance(obj, dict):
            for key in ("reasoning_content", "reasoning", "thought"):
                v = obj.get(key)
                if isinstance(v, str) and v:
                    return v
    return ""


# ── 事件累加器 ───────────────────────────────────────────────────────


class DifyStreamAccum:
    """Dify SSE 事件单源累加器；流式 / 非流共用同一 ingest。"""

    __slots__ = (
        "answer_buf",
        "reasoning_buf",
        "usage",
        "error",
        "last_cid",
        "saw_separate_reasoning",
        "event_counts",
        "event_total",
        "workflow_status",
        "workflow_error",
        "structured_output",
        "conversation_id_committed",
    )

    def __init__(self, *, input_tokens_hint: int = 0) -> None:
        self.answer_buf = ""
        self.reasoning_buf = ""
        self.usage = {"input_tokens": int(input_tokens_hint or 0), "output_tokens": 0}
        self.error: str | None = None
        self.last_cid: str | None = None
        self.saw_separate_reasoning = False
        self.event_counts: dict[str, int] = {}
        self.event_total = 0
        self.workflow_status: str | None = None
        self.workflow_error: str | None = None
        self.structured_output: dict[str, Any] | None = None
        self.conversation_id_committed = False

    def ingest(
        self, ev: dict[str, Any], *, on_conversation_id=None
    ) -> tuple[str, str, str]:
        """处理单事件 → (kind, reasoning_delta, answer_delta)；
        kind: error | reasoning | answer | end | other"""
        self.event_total += 1
        etype = ev.get("event")
        etype_key = str(etype or "unknown")
        self.event_counts[etype_key] = self.event_counts.get(etype_key, 0) + 1

        cid = ev.get("conversation_id")
        if isinstance(cid, str) and cid:
            self.last_cid = cid

        if etype == "error":
            self.error = (
                ev.get("message") or ev.get("code") or json.dumps(ev, ensure_ascii=False)
            )
            return "error", "", ""

        if etype in ("reasoning_chunk", "reasoning", "agent_thought"):
            piece = _pull_reasoning_from_event(ev)
            if not piece and etype == "agent_thought":
                piece = ev.get("thought") if isinstance(ev.get("thought"), str) else ""
            if piece:
                self.saw_separate_reasoning = True
                self.reasoning_buf += piece
                return "reasoning", piece, ""
            return "other", "", ""

        if etype in ("message", "agent_message"):
            r_delta = ""
            r = _pull_reasoning_from_event(ev)
            if r:
                self.saw_separate_reasoning = True
                self.reasoning_buf += r
                r_delta = r
            a_delta = ""
            piece = ev.get("answer")
            if isinstance(piece, str) and piece:
                self.answer_buf += piece
                a_delta = piece
            if r_delta or a_delta:
                return "answer", r_delta, a_delta
            return "other", "", ""

        if etype == "message_end":
            cid2 = ev.get("conversation_id")
            if isinstance(cid2, str) and cid2:
                self.last_cid = cid2
            u = extract_dify_usage(ev)
            if u.get("input_tokens"):
                self.usage["input_tokens"] = u["input_tokens"]
            if u.get("output_tokens"):
                self.usage["output_tokens"] = u["output_tokens"]
            r = _pull_reasoning_from_event(ev)
            if r and not self.saw_separate_reasoning and r not in self.reasoning_buf:
                self.reasoning_buf = r
                self.saw_separate_reasoning = True
                return "end", r, ""
            return "end", "", ""

        if etype == "node_finished":
            # LLM 节点结构化输出：最高优先的工具通道
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if data.get("node_type") == "llm":
                outs = data.get("outputs")
                if isinstance(outs, dict):
                    so = outs.get("structured_output")
                    if isinstance(so, dict) and so:
                        self.structured_output = so
            return "other", "", ""

        if etype == "workflow_finished":
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            st = data.get("status")
            if isinstance(st, str) and st:
                self.workflow_status = st
            err = data.get("error")
            if err:
                # 不写 self.error：交 finalize 给可见提示而非 api_error
                self.workflow_error = str(err)
            return "end", "", ""

        return "other", "", ""

    def _commit_conversation_id(self, callback) -> None:
        """只在成功收尾后绑定会话，避免失败枪污染 sessions。"""
        if (
            self.conversation_id_committed
            or not self.last_cid
            or self.error
            or self.workflow_error
        ):
            return
        if str(self.workflow_status or "").strip().lower() in {
            "failed",
            "error",
            "cancelled",
            "canceled",
            "stopped",
        }:
            return
        if callback is not None:
            try:
                callback(self.last_cid)
            except Exception as exc:
                print("[lan] conversation remember failed open: {!r}".format(exc))
        self.conversation_id_committed = True

    def _structured_parts(self) -> tuple[str, list[dict[str, Any]]] | None:
        so = self.structured_output
        if not isinstance(so, dict) or not so:
            return None
        if "tool_calls" not in so and "reply" not in so:
            return None
        reply = so.get("reply")
        return (
            (reply if isinstance(reply, str) else "").strip(),
            tool_uses_from_calls(so.get("tool_calls")),
        )

    def finalize_parts(
        self,
        *,
        enable_tools: bool = False,
        input_tokens_hint: int = 0,
        decode_unicode_wire: bool = False,
        on_conversation_id=None,
    ) -> dict[str, Any]:
        """拆 thinking / text / tool_use，补 usage，返回出站摘要结构。"""
        think = self.reasoning_buf.strip()
        body = self.answer_buf
        if not think:
            t2, body = split_think_and_text(self.answer_buf)
            think = t2
        else:
            _, body = split_think_and_text(body)

        tool_uses: list[dict[str, Any]] = []
        after_success = ""
        after_success_reason = "none"
        envelope = False
        structured_reply_dropped = False
        protocol_source = body or ""
        env = self._structured_parts()
        if env is None:
            env = extract_structured_envelope(body or "")
        if env is not None:
            envelope = True
            body, env_tools = env
            if enable_tools:
                tool_uses = env_tools
            elif env_tools:
                # 无工具枪不得向 CC 回 tool_use；留痕便于排查岚分支误路由
                print(
                    "[lan] envelope tool_calls×{} dropped (tools off)".format(len(env_tools))
                )
        elif enable_tools:
            body, tool_uses = extract_tool_uses(body or "")

        if enable_tools:
            envelope_reply_present = bool((body or "").strip()) if envelope else False
            success_parse = parse_after_success(body or "")
            body = success_parse.visible
            if success_parse.found and not success_parse.valid:
                after_success_reason = success_parse.reason
            elif success_parse.valid and not tool_uses:
                # 标记误用于普通答复时不吞正文。
                body = (
                    body
                    + ("\n\n" if body else "")
                    + success_parse.success
                ).strip()
                after_success_reason = "misused_without_tools"
            elif success_parse.valid and envelope:
                after_success_reason = "structured_envelope_unsupported"
            elif success_parse.valid and not terminal_draft_follows_tools(protocol_source):
                after_success_reason = "draft_precedes_tool_protocol"
            elif success_parse.valid and not is_terminal_tool_batch(tool_uses):
                after_success_reason = "ineligible_or_conflicting_tool_batch"
            elif success_parse.valid and has_protocol_residue(body or ""):
                after_success_reason = "protocol_residue"
            elif success_parse.valid:
                after_success = success_parse.success
                after_success_reason = "eligible"

            # 结构化契约规定 calls 非空时 reply 必须为空；解析端也强制守住。
            if envelope and tool_uses and envelope_reply_present:
                body = ""
                structured_reply_dropped = True

        # 隐藏工具协议先解析，线缆表示后还原；工具参数与成功草稿同样递归解码。
        if decode_unicode_wire:
            body = decode_unicode_wire_text(body or "")
            think = decode_unicode_wire_text(think or "")
            decoded_tools = decode_unicode_wire_value(tool_uses)
            tool_uses = decoded_tools if isinstance(decoded_tools, list) else []
            after_success = decode_unicode_wire_text(after_success or "")

        empty_upstream = (
            not (body or "").strip() and not tool_uses and not (think or "").strip()
        )
        if empty_upstream:
            counts = ",".join(
                "{}={}".format(k, self.event_counts[k])
                for k in sorted(self.event_counts.keys())
            ) or "none"
            wf_err = self.workflow_error or self.error
            if wf_err:
                body = (
                    "（Dify 工作流未产出正文：status={} error={}；events={} [{}]。"
                    "若 error 含 File validation：请检查「岚」应用的文件上传/图片开关并重新发布。）"
                ).format(self.workflow_status or "?", wf_err, self.event_total, counts)
            else:
                body = (
                    "（本轮上游未返回可见正文/工具调用。代理已收完 Dify 流：events={} [{}]。"
                    "常见原因：工作流 0 步失败、额度/模型异常、或会话变量过重。）"
                ).format(self.event_total, counts)
            print(
                "[lan] WARN empty upstream events={} counts={{{}}} wf_status={} wf_error={}".format(
                    self.event_total, counts, self.workflow_status, self.workflow_error
                )
            )

        out_tok = int(self.usage.get("output_tokens") or 0)
        if not out_tok and (body or think):
            out_tok = _chars_to_tokens(len(body or "") + len(think or ""))
        in_tok = (
            int(self.usage.get("input_tokens") or 0) or int(input_tokens_hint or 0) or 1
        )
        self.usage["input_tokens"] = in_tok
        self.usage["output_tokens"] = out_tok
        self._commit_conversation_id(on_conversation_id)
        return {
            "text": body or "",
            "reasoning": think or "",
            "tool_uses": tool_uses,
            "usage": dict(self.usage),
            "stop_reason": "tool_use" if tool_uses else "end_turn",
            "text_len": len(body or ""),
            "reasoning_len": len(think or ""),
            "tool_count": len(tool_uses),
            "tool_names": [t.get("name") or "?" for t in tool_uses],
            "after_success": after_success,
            "after_success_chars": len(after_success),
            "after_success_reason": after_success_reason,
            "envelope": envelope,
            "structured_reply_dropped": structured_reply_dropped,
            "unicode_wire_decoded": bool(decode_unicode_wire),
            "empty_upstream": empty_upstream,
            "dify_event_counts": dict(self.event_counts),
            "dify_event_total": self.event_total,
            "workflow_status": self.workflow_status,
            "workflow_error": self.workflow_error,
        }


# ── 出站组装 ─────────────────────────────────────────────────────────


def _merge_thinking(text: str, reasoning: str) -> tuple[str, str]:
    """分离式 reasoning 与正文内 <think> 合流 → (think, body)。"""
    think, body = split_think_and_text(text or "")
    if reasoning:
        think = (reasoning.strip() + ("\n\n" + think if think else "")).strip()
    return think, body


def build_non_stream_message(
    *,
    model: str,
    text: str,
    reasoning: str = "",
    message_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict[str, Any]:
    """非流：reasoning + text → thinking 块 + text 块。"""
    think, body = _merge_thinking(text, reasoning)
    content: list[dict[str, Any]] = []
    if think:
        # signature 留空：Dify 无 Anthropic 可校验签名
        content.append({"type": "thinking", "thinking": think, "signature": ""})
    content.append({"type": "text", "text": body})
    return {
        "id": message_id or _msg_id(),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
        },
    }


def build_non_stream_with_tools(
    *,
    model: str,
    text: str,
    reasoning: str = "",
    tool_uses: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict[str, Any]:
    """非流消息：thinking? + text? + tool_use*；有 tool 时 stop_reason=tool_use。"""
    think, body = _merge_thinking(text, reasoning)
    tools_list = list(tool_uses or [])
    if body:
        body, more = extract_tool_uses(body)
        tools_list.extend(more)
    content: list[dict[str, Any]] = []
    if think:
        content.append({"type": "thinking", "thinking": think, "signature": ""})
    if body:
        content.append({"type": "text", "text": body})
    content.extend(tools_list)
    if not content:
        content.append({"type": "text", "text": ""})
    out_tok = int(output_tokens or 0)
    if not out_tok and (body or think):
        out_tok = _chars_to_tokens(len(body or "") + len(think or ""))
    return {
        "id": message_id or _msg_id(),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": "tool_use" if tools_list else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(input_tokens or 0) or 1,
            "output_tokens": out_tok,
        },
    }


def iter_sse_from_parts(
    *,
    model: str,
    thinking: str,
    text: str,
    tool_uses: list[dict[str, Any]],
    message_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    skip_message_start: bool = False,
    start_index: int = 0,
    skip_thinking: bool = False,
) -> list[str]:
    """最终 thinking/text/tool_use → Anthropic SSE 行列表。

    skip_* / start_index：流式路径已提前直播部分块时衔接用。
    """
    mid = message_id or _msg_id()
    in_tok = int(input_tokens or 0) or 1
    out_tok = int(output_tokens or 0)
    if not out_tok and (text or thinking):
        out_tok = _chars_to_tokens(len(text or "") + len(thinking or ""))
    lines: list[str] = []
    if not skip_message_start:
        lines.append(
            sse_event(build_message_start(model, mid, input_tokens=in_tok, output_tokens=0))
        )
    idx = int(start_index or 0)

    if thinking and not skip_thinking:
        lines.append(sse_event(build_thinking_block_start(idx)))
        lines.append(sse_event(build_thinking_delta(thinking, idx)))
        lines.append(sse_event(build_content_block_stop(idx)))
        idx += 1

    if text:
        lines.append(sse_event(build_text_block_start(idx)))
        lines.append(sse_event(build_text_delta(text, idx)))
        lines.append(sse_event(build_content_block_stop(idx)))
        idx += 1

    for tu in tool_uses:
        tid = tu.get("id") or toolu_id()
        inp = tu["input"]
        lines.append(sse_event(build_tool_use_block_start(idx, tid, tu["name"])))
        partial = json.dumps(inp, ensure_ascii=False)
        for ofs in range(0, max(1, len(partial)), _INPUT_JSON_CHUNK):
            piece = partial[ofs : ofs + _INPUT_JSON_CHUNK]
            if not piece:
                break
            lines.append(sse_event(build_input_json_delta(piece, idx)))
        lines.append(sse_event(build_content_block_stop(idx)))
        idx += 1

    if not thinking and not text and not tool_uses and not skip_message_start:
        lines.append(sse_event(build_text_block_start(0)))
        lines.append(sse_event(build_text_delta("", 0)))
        lines.append(sse_event(build_content_block_stop(0)))

    stop = "tool_use" if tool_uses else "end_turn"
    lines.append(
        sse_event(build_message_delta(stop, output_tokens=out_tok, input_tokens=in_tok))
    )
    lines.append(sse_event(build_message_stop()))
    return lines


def iter_plain_text_sse(
    *,
    model: str,
    text: str,
    message_id: str | None = None,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> list[str]:
    """本地短路等：纯 text 块的完整 SSE 行列表（iter_sse_from_parts 特例）。"""
    return iter_sse_from_parts(
        model=model,
        thinking="",
        text=text or "",
        tool_uses=[],
        message_id=message_id,
        input_tokens=int(input_tokens or 0) or 1,
        output_tokens=int(output_tokens or 0) or _chars_to_tokens(len(text or "")),
    )


# ── 流式主路径 ───────────────────────────────────────────────────────


async def dify_events_to_anthropic_sse(
    events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    on_conversation_id=None,
    enable_tools: bool = False,
    input_tokens_hint: int = 0,
    result_out: dict[str, Any] | None = None,
    on_final_parts=None,
    decode_unicode_wire: bool = False,
) -> AsyncIterator[str]:
    """消费 Dify 事件流，产出 Anthropic SSE。

    工具枪：思考直播；正文在出现工具标记（或整包 JSON 起手）前试探直播，
    其后只缓冲，收齐统一解析；result_out 收尾写入出站摘要。
    """
    message_id = _msg_id()
    msg_started = False
    thinking_index: int | None = None
    text_index: int | None = None
    next_index = 0
    thinking_open = False
    text_open = False
    answer_streamed = 0
    accum = DifyStreamAccum(input_tokens_hint=input_tokens_hint)
    reasoning_wire_decoder = UnicodeWireStreamDecoder() if decode_unicode_wire else None
    answer_wire_decoder = UnicodeWireStreamDecoder() if decode_unicode_wire else None

    def decode_reasoning_delta(piece: str, *, final: bool = False) -> str:
        if reasoning_wire_decoder is None:
            return piece or ""
        return reasoning_wire_decoder.feed(piece or "", final=final)

    def decode_answer_delta(piece: str, *, final: bool = False) -> str:
        if answer_wire_decoder is None:
            return piece or ""
        return answer_wire_decoder.feed(piece or "", final=final)

    def ensure_message_start() -> list[str]:
        nonlocal msg_started
        out = []
        if not msg_started:
            out.append(
                sse_event(
                    build_message_start(
                        model, message_id, input_tokens=accum.usage["input_tokens"]
                    )
                )
            )
            msg_started = True
        return out

    def open_thinking() -> list[str]:
        nonlocal thinking_index, next_index, thinking_open
        out = ensure_message_start()
        if not thinking_open:
            thinking_index = next_index
            next_index += 1
            out.append(sse_event(build_thinking_block_start(thinking_index)))
            thinking_open = True
        return out

    def open_text() -> list[str]:
        nonlocal text_index, next_index, text_open, thinking_open, thinking_index
        out = []
        if thinking_open and thinking_index is not None:
            out.append(sse_event(build_content_block_stop(thinking_index)))
            thinking_open = False
        out.extend(ensure_message_start())
        if not text_open:
            text_index = next_index
            next_index += 1
            out.append(sse_event(build_text_block_start(text_index)))
            text_open = True
        return out

    def flush_answer_live() -> list[str]:
        nonlocal answer_streamed
        out: list[str] = []
        unsent = accum.answer_buf[answer_streamed:]
        if not unsent:
            return out
        visible = decode_answer_delta(unsent)
        answer_streamed = len(accum.answer_buf)
        if not visible:
            return out
        out.extend(open_text())
        out.append(sse_event(build_text_delta(visible, text_index or 0)))
        return out

    # 工具模式状态
    tools_thinking_streamed = False
    tools_text_streamed_len = 0
    tools_streamed_text = ""
    tools_locked = False

    if enable_tools:
        for line in ensure_message_start():
            yield line

    def flush_tools_text_speculative() -> list[str]:
        """未见标记时流式吐正文；见标记（或整包 JSON 起手）后只缓冲。"""
        nonlocal tools_text_streamed_len, tools_streamed_text, tools_locked
        out: list[str] = []
        buf = accum.answer_buf
        if tools_locked:
            return out
        if buf.lstrip()[:1] == "{":
            tools_locked = True
            return out
        cut = find_stream_cut(buf.lower())
        if cut >= 0:
            tools_locked = True
            unsent = buf[tools_text_streamed_len:cut]
            visible = decode_answer_delta(unsent, final=True)
            tools_text_streamed_len = cut
            if visible:
                out.extend(open_text())
                out.append(sse_event(build_text_delta(visible, text_index or 0)))
                tools_streamed_text += visible
            return out
        safe_end = max(tools_text_streamed_len, len(buf) - _HOLD)
        unsent = buf[tools_text_streamed_len:safe_end]
        visible = decode_answer_delta(unsent)
        tools_text_streamed_len = safe_end
        if visible:
            out.extend(open_text())
            out.append(sse_event(build_text_delta(visible, text_index or 0)))
            tools_streamed_text += visible
        return out

    try:
        async for ev in events:
            kind, r_delta, a_delta = accum.ingest(ev, on_conversation_id=on_conversation_id)
            if kind == "error":
                break
            if enable_tools:
                if r_delta:
                    visible_reasoning = decode_reasoning_delta(r_delta)
                    if visible_reasoning:
                        for line in open_thinking():
                            yield line
                        yield sse_event(
                            build_thinking_delta(visible_reasoning, thinking_index or 0)
                        )
                        tools_thinking_streamed = True
                if a_delta or accum.answer_buf:
                    for line in flush_tools_text_speculative():
                        yield line
                continue
            if r_delta:
                visible_reasoning = decode_reasoning_delta(r_delta)
                if visible_reasoning:
                    for line in open_thinking():
                        yield line
                    yield sse_event(
                        build_thinking_delta(visible_reasoning, thinking_index or 0)
                    )
            if accum.saw_separate_reasoning and (
                a_delta or answer_streamed < len(accum.answer_buf)
            ):
                for line in flush_answer_live():
                    yield line
    except Exception as e:
        # 上游连接/协议异常：转 SSE error 优雅回传，勿裸断流
        accum.error = accum.error or "{}: {}".format(type(e).__name__, e)

    if accum.error:
        if result_out is not None:
            result_out.update(
                {
                    "stop_reason": "error",
                    "error": str(accum.error),
                    "text_len": len(accum.answer_buf),
                    "reasoning_len": len(accum.reasoning_buf),
                }
            )
        yield sse_event(
            {"type": "error", "error": {"type": "api_error", "message": str(accum.error)}}
        )
        return

    reasoning_tail = decode_reasoning_delta("", final=True)
    if reasoning_tail:
        for line in open_thinking():
            yield line
        yield sse_event(build_thinking_delta(reasoning_tail, thinking_index or 0))
        if enable_tools:
            tools_thinking_streamed = True

    # ── 工具模式收尾：收齐后统一拆块 ──
    if enable_tools:
        if thinking_open and thinking_index is not None:
            yield sse_event(build_content_block_stop(thinking_index))
            thinking_open = False
            next_index = max(next_index, (thinking_index or 0) + 1)

        parts = accum.finalize_parts(
            enable_tools=True,
            input_tokens_hint=input_tokens_hint,
            decode_unicode_wire=decode_unicode_wire,
            on_conversation_id=on_conversation_id,
        )
        final_text = parts.get("text") or ""
        tool_uses = parts.get("tool_uses") or []

        emit_text = ""
        if tools_text_streamed_len > 0:
            # 前缀已直播：从净化后的正文补尾，绝不回切含隐藏标记的原始缓冲。
            if not tool_uses:
                streamed_prefix = re.sub(r"\n{3,}", "\n\n", tools_streamed_text).strip()
                if streamed_prefix and final_text.startswith(streamed_prefix):
                    emit_text = final_text[len(streamed_prefix) :]
                elif not streamed_prefix:
                    emit_text = final_text
            if text_open and text_index is not None:
                if emit_text:
                    yield sse_event(build_text_delta(emit_text, text_index))
                yield sse_event(build_content_block_stop(text_index))
                text_open = False
                next_index = max(next_index, (text_index or 0) + 1)
                emit_text = ""
        else:
            emit_text = final_text

        if tool_uses:
            print(
                "[lan] tool_use ×{} → {}".format(
                    len(tool_uses), ",".join(parts.get("tool_names") or [])
                )
            )
        if on_final_parts is not None:
            on_final_parts(parts)
        if result_out is not None:
            result_out.update(parts)
            result_out["text_head"] = (final_text or "")[:200]
        print(
            "[lan] usage in={} out={} stop={} text_len={} env={} streamed_prefix={}".format(
                parts["usage"]["input_tokens"],
                parts["usage"]["output_tokens"],
                parts["stop_reason"],
                parts.get("text_len"),
                1 if parts.get("envelope") else 0,
                tools_text_streamed_len,
            )
        )
        for line in iter_sse_from_parts(
            model=model,
            thinking="" if tools_thinking_streamed else parts["reasoning"],
            text=emit_text,
            tool_uses=tool_uses,
            message_id=message_id,
            input_tokens=parts["usage"]["input_tokens"],
            output_tokens=parts["usage"]["output_tokens"],
            skip_message_start=True,
            start_index=next_index,
            skip_thinking=tools_thinking_streamed,
        ):
            yield line
        return

    # ── 非工具收尾 ──
    if not accum.saw_separate_reasoning and accum.answer_buf:
        think, body = split_think_and_text(accum.answer_buf)
        if decode_unicode_wire:
            think = decode_unicode_wire_text(think)
            body = decode_unicode_wire_text(body)
        if think:
            for line in open_thinking():
                yield line
            yield sse_event(build_thinking_delta(think, thinking_index or 0))
        if body or not think:
            for line in open_text():
                yield line
            if body:
                yield sse_event(build_text_delta(body, text_index or 0))
        answer_streamed = len(accum.answer_buf)
    elif accum.saw_separate_reasoning and answer_streamed < len(accum.answer_buf):
        for line in flush_answer_live():
            yield line

    if accum.saw_separate_reasoning:
        answer_tail = decode_answer_delta("", final=True)
        if answer_tail:
            for line in open_text():
                yield line
            yield sse_event(build_text_delta(answer_tail, text_index or 0))

    parts = accum.finalize_parts(
        enable_tools=False,
        input_tokens_hint=input_tokens_hint,
        decode_unicode_wire=decode_unicode_wire,
        on_conversation_id=on_conversation_id,
    )
    if parts.get("empty_upstream") and (parts.get("text") or "").strip():
        for line in open_text():
            yield line
        yield sse_event(build_text_delta(str(parts["text"]), text_index or 0))
    elif not msg_started:
        for line in ensure_message_start():
            yield line
        for line in open_text():
            yield line

    if thinking_open and thinking_index is not None:
        yield sse_event(build_content_block_stop(thinking_index))
    if text_open and text_index is not None:
        yield sse_event(build_content_block_stop(text_index))

    if result_out is not None:
        result_out.update(parts)
    print(
        "[lan] usage in={} out={} stop={} text_len={}".format(
            parts["usage"]["input_tokens"],
            parts["usage"]["output_tokens"],
            parts.get("stop_reason"),
            parts.get("text_len"),
        )
    )
    yield sse_event(
        build_message_delta(
            str(parts.get("stop_reason") or "end_turn"),
            output_tokens=parts["usage"]["output_tokens"],
            input_tokens=parts["usage"]["input_tokens"],
        )
    )
    yield sse_event(build_message_stop())


async def collect_dify_answer(
    events: AsyncIterator[dict[str, Any]],
    *,
    on_conversation_id=None,
    enable_tools: bool = False,
    input_tokens_hint: int = 0,
    decode_unicode_wire: bool = False,
) -> tuple[str, str, list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """非流收集；返回 (text, reasoning, tool_uses, usage, parts)。"""
    accum = DifyStreamAccum(input_tokens_hint=input_tokens_hint)
    async for ev in events:
        kind, _, _ = accum.ingest(ev, on_conversation_id=on_conversation_id)
        if kind == "error":
            raise RuntimeError(str(accum.error or "Dify stream error"))
    parts = accum.finalize_parts(
        enable_tools=enable_tools,
        input_tokens_hint=input_tokens_hint,
        decode_unicode_wire=decode_unicode_wire,
        on_conversation_id=on_conversation_id,
    )
    return parts["text"], parts["reasoning"], parts["tool_uses"], parts["usage"], parts
