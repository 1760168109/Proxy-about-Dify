# -*- coding: utf-8 -*-
"""请求落盘：每枪一份 JSON（summary + raw_body），供排障与验收。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from parse import fold_messages_to_query, system_to_text, text_from_content


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def write_request_log(
    log_dir: Path,
    body: dict[str, Any],
    *,
    kind: str = "chat",
    extra: dict[str, Any] | None = None,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_body(body)
    if extra:
        summary.update(extra)
    path = log_dir / "{}_{}.json".format(_utc_stamp(), kind or "chat")
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "folded_query": fold_messages_to_query(body),
        "raw_body": body,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    (log_dir / "last_request.json").write_text(text, encoding="utf-8", newline="\n")
    return path


def patch_request_log(
    path: Path | None,
    patch: dict[str, Any],
    *,
    log_dir: Path | None = None,
    into: str = "response",
) -> None:
    """落盘后增量补字段：into='response' 合并进 summary.response / 顶层 response；
    into='summary' 直接合并进 summary。"""
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
            top = data.get("response")
            if not isinstance(top, dict):
                top = {}
            top.update(patch)
            data["response"] = top
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        p.write_text(text, encoding="utf-8", newline="\n")
        latest = (log_dir or p.parent) / "last_request.json"
        if latest.is_file():
            try:
                last = json.loads(latest.read_text(encoding="utf-8"))
                if isinstance(last, dict) and last.get("captured_at") == data.get("captured_at"):
                    latest.write_text(text, encoding="utf-8", newline="\n")
            except Exception:
                pass
        else:
            latest.write_text(text, encoding="utf-8", newline="\n")
    except Exception as e:
        print("[lan] patch log failed: {}".format(e))
