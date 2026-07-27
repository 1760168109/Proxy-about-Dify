# -*- coding: utf-8 -*-
"""按次计费账本：上游接受 chat-messages 即记一枪，本地短路不计（因由见经验.md 守则 6）。

单价：OPUS_USD_PER_CALL（默认 1.0）· HAIKU_USD_PER_CALL（默认 0.0，仍计次数）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from persist import atomic_write_json, utc_now


def _fenv(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class UsageMeter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._empty())

    def _empty(self) -> dict[str, Any]:
        return {
            "updated_at": utc_now(),
            "opus_calls": 0,
            "haiku_calls": 0,
            "other_calls": 0,
            "opus_usd_per_call": _fenv("OPUS_USD_PER_CALL", 1.0),
            "haiku_usd_per_call": _fenv("HAIKU_USD_PER_CALL", 0.0),
            "estimated_usd": 0.0,
            "by_kind": {},
            "last": None,
        }

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return self._empty()
            data = json.loads(raw)
            return data if isinstance(data, dict) else self._empty()
        except (OSError, json.JSONDecodeError):
            return self._empty()

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
                "OPUS_USD_PER_CALL", float(data.get("opus_usd_per_call") or 1.0)
            )
            haiku_unit = _fenv(
                "HAIKU_USD_PER_CALL", float(data.get("haiku_usd_per_call") or 0.0)
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

            # 与 _snapshot 同式（次数 × 当前单价）；不作增量累加，改单价即全量重估
            data["estimated_usd"] = round(
                int(data.get("opus_calls") or 0) * opus_unit
                + int(data.get("haiku_calls") or 0) * haiku_unit,
                4,
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
        opus_unit = float(data.get("opus_usd_per_call") or 1.0)
        haiku_unit = float(data.get("haiku_usd_per_call") or 0.0)
        # 按当前单价重算展示，防改 env 后旧累计偏差
        est = round(opus * opus_unit + haiku * haiku_unit, 4)
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
