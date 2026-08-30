# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from log import (
    append_transport_trace,
    patch_request_log,
    read_transport_trace,
    write_request_log,
)


def test_request_logs_are_isolated_by_request_id(tmp_path):
    first = {"model": "alan", "messages": [{"role": "user", "content": "first"}]}
    second = {"model": "alan", "messages": [{"role": "user", "content": "second"}]}
    p1 = write_request_log(tmp_path, first, request_id="req-a")
    p2 = write_request_log(tmp_path, second, request_id="req-b")
    assert p1 != p2

    patch_request_log(p1, {"text_len": 5}, log_dir=tmp_path)
    patch_request_log(p2, {"text_len": 6}, log_dir=tmp_path)
    d1 = json.loads(p1.read_text(encoding="utf-8"))
    d2 = json.loads(p2.read_text(encoding="utf-8"))
    assert d1["raw_body"] == first
    assert d2["raw_body"] == second
    assert d1["summary"]["response"]["text_len"] == 5
    assert d2["summary"]["response"]["text_len"] == 6


def test_transport_trace_is_append_only_and_ignores_partial_tail(tmp_path):
    request_path = tmp_path / "request.json"
    append_transport_trace(
        request_path,
        request_id="abc123",
        sequence=1,
        event="dify_response_headers",
        elapsed_seconds=0.25,
        fields={"status_code": 200},
        durable=True,
    )
    append_transport_trace(
        request_path,
        request_id="abc123",
        sequence=2,
        event="downstream_send_ok",
        elapsed_seconds=0.5,
        fields={"event_types": ["message_start"]},
    )
    trace_path = request_path.with_suffix(".trace.jsonl")
    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"partial"')

    rows = read_transport_trace(trace_path)
    assert [row["event"] for row in rows] == [
        "dify_response_headers",
        "downstream_send_ok",
    ]
    assert rows[0]["request_id"] == "abc123"
    assert rows[1]["event_types"] == ["message_start"]
