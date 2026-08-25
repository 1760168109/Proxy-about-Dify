# -*- coding: utf-8 -*-
from __future__ import annotations

from meter import UsageMeter


def test_zero_opus_unit_is_a_valid_configured_price(tmp_path, monkeypatch):
    monkeypatch.setenv("OPUS_USD_PER_CALL", "0.0")
    meter = UsageMeter(tmp_path / "usage.json")
    snapshot = meter.record(route="opus")
    assert snapshot["opus_usd_per_call"] == 0.0
    assert snapshot["estimated_usd"] == 0.0
