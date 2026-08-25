# -*- coding: utf-8 -*-
"""按次计费账本：上游接受 chat-messages 即记一枪，本地短路不计（因由见经验.md 守则 6）。

单价：OPUS_USD_PER_CALL（默认 1.0）· HAIKU_USD_PER_CALL（默认 0.0，仍计次数）。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from persist import atomic_write_json, read_json_dict, utc_now


def _fenv(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _stored_float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key)
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _estimated_usd(
    opus_calls: int, haiku_calls: int, opus_unit: float, haiku_unit: float
) -> float:
    """次数 × 当前单价。不作增量累加——改单价即全量重估。

    守则 6：账本口径只能有一份定义，落盘值与展示值必须同式。
    """
    return round(opus_calls * opus_unit + haiku_calls * haiku_unit, 4)


DEFAULT_OPUS_USD = 1.0
DEFAULT_HAIKU_USD = 0.0


class UsageMeter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write(self._empty())

    def _empty(self) -> dict[str, Any]:
        return {
            "updated_at": utc_now(),
            "opus_calls": 0,
            "haiku_calls": 0,
            "other_calls": 0,
            "opus_usd_per_call": _fenv("OPUS_USD_PER_CALL", DEFAULT_OPUS_USD),
            "haiku_usd_per_call": _fenv("HAIKU_USD_PER_CALL", DEFAULT_HAIKU_USD),
            "estimated_usd": 0.0,
            "by_kind": {},
            "last": None,
        }

    def _read(self) -> dict[str, Any]:
        return read_json_dict(self.path, self._empty)

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def record(
        self,
        *,
        route: str,
        kind: str = "chat",
        is_subagent: bool = False,
        is_main: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            opus_unit = _fenv(
                "OPUS_USD_PER_CALL",
                _stored_float(data, "opus_usd_per_call", DEFAULT_OPUS_USD),
            )
            haiku_unit = _fenv(
                "HAIKU_USD_PER_CALL",
                _stored_float(data, "haiku_usd_per_call", DEFAULT_HAIKU_USD),
            )
            data["opus_usd_per_call"] = opus_unit
            data["haiku_usd_per_call"] = haiku_unit

            r = (route or "").lower()
            add_usd = 0.0
            if r == "opus":
                data["opus_calls"] = int(data.get("opus_calls") or 0) + 1
                add_usd = opus_unit
            elif r == "haiku":
                data["haiku_calls"] = int(data.get("haiku_calls") or 0) + 1
                add_usd = haiku_unit
            else:
                data["other_calls"] = int(data.get("other_calls") or 0) + 1

            data["estimated_usd"] = _estimated_usd(
                int(data.get("opus_calls") or 0),
                int(data.get("haiku_calls") or 0),
                opus_unit,
                haiku_unit,
            )
            by = data.get("by_kind")
            if not isinstance(by, dict):
                by = {}
            key = kind or "chat"
            if is_subagent and not key.startswith("subagent"):
                key = "subagent_" + key
            by[key] = int(by.get(key) or 0) + 1
            data["by_kind"] = by
            data["last"] = {
                "at": utc_now(),
                "route": r,
                "kind": kind,
                "is_subagent": bool(is_subagent),
                "is_main": bool(is_main),
                "added_usd": add_usd,
            }
            data["updated_at"] = utc_now()
            self._write(data)
            return self._snapshot(data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot(self._read())

    def _snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        opus = int(data.get("opus_calls") or 0)
        haiku = int(data.get("haiku_calls") or 0)
        opus_unit = _stored_float(data, "opus_usd_per_call", DEFAULT_OPUS_USD)
        haiku_unit = _stored_float(data, "haiku_usd_per_call", DEFAULT_HAIKU_USD)
        est = _estimated_usd(opus, haiku, opus_unit, haiku_unit)
        return {
            "updated_at": data.get("updated_at"),
            "opus_calls": opus,
            "haiku_calls": haiku,
            "other_calls": int(data.get("other_calls") or 0),
            "opus_usd_per_call": opus_unit,
            "haiku_usd_per_call": haiku_unit,
            "estimated_usd": est,
            "billing_note": "count-based: each opus call = opus_usd_per_call USD",
            "by_kind": data.get("by_kind") if isinstance(data.get("by_kind"), dict) else {},
            "last": data.get("last"),
        }

    def reset(self) -> dict[str, Any]:
        with self._lock:
            data = self._empty()
            self._write(data)
            return self._snapshot(data)

    def statusline(self) -> str:
        s = self.snapshot()
        return "opus×{oc} ${usd:.0f} | haiku×{hc}".format(
            oc=s["opus_calls"], usd=float(s["estimated_usd"]), hc=s["haiku_calls"]
        )
