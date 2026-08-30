# -*- coding: utf-8 -*-
"""请求落盘：每枪一份 JSON（summary + raw_body），供排障与验收。

日志字段表集中在本模块：`summarize_body` 定义入站字段，`response_log_patch` 定义
响应字段。调用方只给 kind 与本枪特有的 extra，不各自决定日志长什么样。
"""
from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from parse import fold_messages_to_query, system_to_text, text_from_content
from persist import atomic_write_json

# 每枪一份完整 raw_body，大枪可达数百 KB。只保留最近这些份，其余按时序淘汰——
# 否则长驻进程下 request_logs 只增不减（其余三个存储都有容量或 TTL，唯此处没有）。
LOG_KEEP_FILES = 200

_LATEST_NAME = "last_request.json"


def _utc_stamp() -> str:
    """紧凑 UTC 戳，作文件名用：ISO 的 `:` 与 `+` 在 Windows 路径上不可用。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _captured_at() -> str:
    """微秒级 ISO。不用 persist.utc_now()——它是秒级，而这个值被
    patch_request_log 当作「同一份日志」的判据，同一秒内的两枪会撞成一份。"""
    return datetime.now(timezone.utc).isoformat()


def transport_trace_path(request_log_path: Path) -> Path:
    """请求 JSON 对应的追加式传输轨迹文件。"""
    return Path(request_log_path).with_suffix(".trace.jsonl")


def append_transport_trace(
    request_log_path: Path | None,
    *,
    request_id: str,
    sequence: int,
    event: str,
    elapsed_seconds: float,
    fields: dict[str, Any] | None = None,
    durable: bool = False,
) -> Path | None:
    """追加一条不含正文的传输生命周期记录。

    与主请求 JSON 的原子重写不同，这里每条记录独立追加并刷盘。即使进程在最终
    ``finally`` 前被终止，已经发生的边界事件仍可用于还原断点。
    """
    if request_log_path is None:
        return None
    try:
        path = transport_trace_path(Path(request_log_path))
        record: dict[str, Any] = {
            "at": _captured_at(),
            "request_id": str(request_id or ""),
            "sequence": int(sequence),
            "event": str(event or "unknown"),
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
            "pid": os.getpid(),
        }
        if fields:
            record.update(fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
            stream.flush()
            # Most chunks are intentionally only flushed: fsync per token would turn
            # a long reasoning stream into thousands of synchronous disk stalls. Key
            # boundary events opt into durable persistence so abrupt termination still
            # leaves the useful diagnosis on disk.
            if durable:
                os.fsync(stream.fileno())
        return path
    except Exception as exc:
        print("[lan] transport trace failed: {}".format(exc))
        return None


def read_transport_trace(path: Path, *, limit: int = 40) -> list[dict[str, Any]]:
    """读取轨迹尾部；忽略强制终止留下的残缺末行。"""
    if limit <= 0:
        return []
    try:
        tail: deque[dict[str, Any]] = deque(maxlen=int(limit))
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    tail.append(item)
        return list(tail)
    except OSError:
        return []


def summarize_body(body: dict[str, Any]) -> dict[str, Any]:
    system = system_to_text(body.get("system"))
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    roles = [m.get("role") or "?" for m in messages if isinstance(m, dict)]
    last_user = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = text_from_content(m.get("content"))
            break
    return {
        "model": body.get("model"),
        "stream_in_body": body.get("stream") if "stream" in body else None,
        "system_chars": len(system),
        "system_head": system[:240],
        "message_count": len(messages),
        "roles": roles,
        "last_user_chars": len(last_user),
        "last_user_head": last_user[:240],
    }


def response_log_patch(parts: dict[str, Any], *, dify_files: int = 0) -> dict[str, Any]:
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
        "unicode_wire_decoded": bool(parts.get("unicode_wire_decoded")),
    }


def prune_logs(log_dir: Path, keep: int = LOG_KEEP_FILES) -> int:
    """只留最近 keep 份；文件名以 UTC 戳开头，字典序即时序。返回删除数。"""
    if keep <= 0:
        return 0
    try:
        files = sorted(
            (p for p in log_dir.glob("*.json") if p.name != _LATEST_NAME),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
        try:
            transport_trace_path(stale).unlink()
        except OSError:
            pass
    return removed


def write_request_log(
    log_dir: Path,
    body: dict[str, Any],
    *,
    kind: str = "chat",
    extra: dict[str, Any] | None = None,
    request_id: str | None = None,
    fold_query: bool = False,
) -> Path:
    """落盘一份请求日志并返回其路径。

    fold_query：只有 title 枪的出站 query 等于整包折叠（见 prepare_text_outbound）。
    其余枪型下那份折叠谁也没发出去，而 raw_body 就在同一文件里，需要时可原样重算。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_body(body)
    if extra:
        summary.update(extra)
    suffix = (request_id or "").strip() or "req_{}".format(_utc_stamp()[-12:-1])
    path = log_dir / "{}_{}_{}.json".format(_utc_stamp(), kind or "chat", suffix)
    summary.setdefault("request_log_file", path.name)
    summary.setdefault("transport_trace_file", transport_trace_path(path).name)
    payload: dict[str, Any] = {
        "captured_at": _captured_at(),
        "summary": summary,
        "raw_body": body,
    }
    if fold_query:
        payload["folded_query"] = fold_messages_to_query(body)
    atomic_write_json(path, payload)
    atomic_write_json(log_dir / _LATEST_NAME, payload)
    prune_logs(log_dir)
    return path


def patch_request_log(
    path: Path | None,
    patch: dict[str, Any],
    *,
    log_dir: Path | None = None,
    into: str = "response",
) -> None:
    """落盘后增量补字段：into='response' 合并进 summary.response；
    into='summary' 直接合并进 summary。本函数不抛——调用方无须再包 fail-open。"""
    if path is None or not patch:
        return
    try:
        p = Path(path)
        if not p.is_file():
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        summary = data.get("summary")
        if not isinstance(summary, dict):
            summary = {}
            data["summary"] = summary
        if into == "summary":
            summary.update(patch)
        else:
            prev = summary.get("response")
            if not isinstance(prev, dict):
                prev = {}
            prev.update(patch)
            summary["response"] = prev
        atomic_write_json(p, data)
        latest = (log_dir or p.parent) / _LATEST_NAME
        if latest.is_file():
            try:
                last = json.loads(latest.read_text(encoding="utf-8"))
                if isinstance(last, dict) and last.get("captured_at") == data.get("captured_at"):
                    atomic_write_json(latest, data)
            except Exception:
                pass
        else:
            atomic_write_json(latest, data)
    except Exception as e:
        print("[lan] patch log failed: {}".format(e))
