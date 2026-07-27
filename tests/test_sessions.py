# -*- coding: utf-8 -*-
"""会话绑定：extract / resolve 只读（hit·miss·missing）/ remember / new / switch / LRU。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from sessions import MAX_BY_CC, SessionStore, extract_cc_session_id

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"


def _store(tmp: Path) -> SessionStore:
    return SessionStore(tmp / "sessions.json")


def test_extract_variants():
    sid = str(uuid.uuid4())
    assert (
        extract_cc_session_id(
            {"metadata": {"user_id": json.dumps({"device_id": "d", "session_id": sid})}}
        )
        == sid
    )
    assert extract_cc_session_id({"metadata": {"user_id": {"session_id": sid}}}) == sid
    assert extract_cc_session_id({"metadata": {"user_id": {"session_id": "  " + S1 + "  "}}}) == S1
    assert extract_cc_session_id({"metadata": {"user_id": S1}}) == S1
    assert extract_cc_session_id({"metadata": {"user_id": "a" * 32}}) == "a" * 32


def test_extract_rejects_garbage():
    for body in (
        {},
        {"metadata": {}},
        {"metadata": {"user_id": {}}},
        {"metadata": {"user_id": {"session_id": ""}}},
        {"metadata": {"user_id": None}},
        {"metadata": {"user_id": "{not json"}},
        {"metadata": {"user_id": "short"}},
        {"metadata": {"user_id": "user@example.com"}},
    ):
        assert extract_cc_session_id(body) is None


def test_hit_miss_missing(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("u1", "cid-a", cc_session_id=S1)
    r = store.resolve_conversation("u1", S1)
    assert r["session_bind"] == "hit" and r["conversation_id"] == "cid-a"

    r2 = store.resolve_conversation("u1", S2)
    assert r2["session_bind"] == "miss" and r2["conversation_id"] is None
    st = store.get_state("u1")
    assert S2 not in (st.get("by_cc") or {})  # miss 不写盘
    assert st.get("current") == "cid-a"

    r3 = store.resolve_conversation("u1", None)
    assert r3["session_bind"] == "missing" and r3["conversation_id"] == "cid-a"


def test_missing_ghost_current_dropped(tmp_path: Path):
    p = tmp_path / "sessions.json"
    p.write_text(
        json.dumps(
            {
                "u1": {
                    "current": "cid-ghost",
                    "sessions": [],
                    "cc_session_id": S1,
                    "by_cc": {S1: {"dify_cid": "cid-other", "updated_at": "2020-01-01T00:00:00+00:00"}},
                }
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(p)
    r = store.resolve_conversation("u1", None)
    assert r["session_bind"] == "missing" and r["conversation_id"] is None


def test_new_session_variants(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("u1", "cid1", cc_session_id=S1)
    store.remember("u1", "cid2", cc_session_id=S2)

    out = store.new_session("u1", S1)  # 只解 S1
    assert S1 not in (out.get("by_cc") or {})
    assert S2 in (out.get("by_cc") or {})
    assert store.resolve_conversation("u1", S1)["session_bind"] == "miss"
    assert store.resolve_conversation("u1", S2)["session_bind"] == "hit"

    out2 = store.new_session("u1")  # 无参：按 current 反查
    assert out2.get("current") is None
    assert store.resolve_conversation("u1", S2)["session_bind"] == "miss"

    store.remember("u1", "c1", cc_session_id=S1)
    out3 = store.new_session("u1", clear_all=True)
    assert not out3.get("by_cc")


def test_switch_binding(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("u1", "cid-old", cc_session_id=S1)
    store.new_session("u1")
    store.switch("u1", "cid-switched", cc_session_id=S1)
    r = store.resolve_conversation("u1", S1)
    assert r["session_bind"] == "hit" and r["conversation_id"] == "cid-switched"

    store.new_session("u1")
    store.switch("u1", "cid-only-current")  # switch 会带上最近跟踪的 S1 重新绑定
    assert store.get_current("u1") == "cid-only-current"


def test_remember_without_sid_keeps_by_cc(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("u1", "cid1", cc_session_id=S1)
    store.remember("u1", "cid2")  # 无 sid：只动 current
    r = store.resolve_conversation("u1", S1)
    assert r["session_bind"] == "hit" and r["conversation_id"] == "cid1"
    assert store.get_current("u1") == "cid2"


def test_lru_drops_oldest(tmp_path: Path):
    p = tmp_path / "sessions.json"
    sids = ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa{}".format(i) for i in range(3)]
    entries = {
        sid: {"dify_cid": "cid-{}".format(i), "updated_at": "2020-01-0{}T00:00:00+00:00".format(i + 1)}
        for i, sid in enumerate(sids)
    }
    p.write_text(
        json.dumps(
            {"u1": {"current": "cid-2", "sessions": [], "cc_session_id": sids[2], "by_cc": entries}}
        ),
        encoding="utf-8",
    )
    store = SessionStore(p, max_by_cc=3)
    s3 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    store.remember("u1", "cid-3", cc_session_id=s3, max_by_cc=3)
    assert store.resolve_conversation("u1", sids[0])["session_bind"] == "miss"
    assert store.resolve_conversation("u1", sids[1])["session_bind"] == "hit"
    assert store.resolve_conversation("u1", s3)["session_bind"] == "hit"


def test_legacy_store_without_by_cc(tmp_path: Path):
    p = tmp_path / "sessions.json"
    p.write_text(
        json.dumps(
            {"legacy-user": {"current": "cid-leg", "sessions": [{"id": "cid-leg", "title": "", "updated_at": "t"}]}}
        ),
        encoding="utf-8",
    )
    store = SessionStore(p)
    assert store.resolve_conversation("legacy-user", S1)["session_bind"] == "miss"
    store.remember("legacy-user", "cid-leg", cc_session_id=S1)
    assert store.resolve_conversation("legacy-user", S1)["session_bind"] == "hit"


def test_max_by_cc_constant():
    assert MAX_BY_CC >= 1
