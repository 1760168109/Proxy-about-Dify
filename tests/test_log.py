# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from log import patch_request_log, write_request_log


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
