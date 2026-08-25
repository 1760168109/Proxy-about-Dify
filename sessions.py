# -*- coding: utf-8 -*-
"""CC session_id → Dify conversation_id 绑定 + 会话档案（语义详见架构.md「会话绑定」）。

真源：by_cc[S]；current 只作展示与「请求无 S」时的 sticky。
resolve 只读；写盘仅 remember / new_session / switch。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from persist import atomic_write_json, read_json_dict, utc_now

# 每 user 最多保留多少条 CC→Dify 映射（LRU by updated_at）
MAX_BY_CC = 32

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def extract_cc_session_id(body: dict[str, Any] | None) -> str | None:
    """metadata.user_id（JSON 串或 dict）里的 session_id；裸串仅收 UUID / 32hex。"""
    if not isinstance(body, dict):
        return None
    meta = body.get("metadata")
    if not isinstance(meta, dict):
        return None
    uid = meta.get("user_id")
    if isinstance(uid, dict):
        sid = uid.get("session_id")
        return sid.strip() if isinstance(sid, str) and sid.strip() else None
    if isinstance(uid, str):
        s = uid.strip()
        if not s:
            return None
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                return None
            if isinstance(obj, dict):
                sid = obj.get("session_id")
                if isinstance(sid, str) and sid.strip():
                    return sid.strip()
            return None
        if _UUID_RE.match(s) or _HEX32_RE.match(s):
            return s
    return None


def _ent_cid(ent: Any) -> str | None:
    if isinstance(ent, dict):
        cid = ent.get("dify_cid") or ent.get("id")
        return cid.strip() if isinstance(cid, str) and cid.strip() else None
    if isinstance(ent, str) and ent.strip():
        return ent.strip()
    return None


class SessionStore:
    def __init__(self, path: Path, *, max_by_cc: int = MAX_BY_CC) -> None:
        self.path = path
        self.max_by_cc = max(1, int(max_by_cc))
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write({})

    def _read(self) -> dict[str, Any]:
        return read_json_dict(self.path)

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def _bucket(self, data: dict[str, Any], user: str) -> dict[str, Any]:
        b = data.get(user)
        if not isinstance(b, dict):
            b = {
                "current": None,
                "cc_session_id": None,
                "by_cc": {},
                "binding_epoch": 0,
            }
            data[user] = b
        b.setdefault("current", None)
        b.setdefault("cc_session_id", None)
        if not isinstance(b.get("by_cc"), dict):
            b["by_cc"] = {}
        try:
            b["binding_epoch"] = max(0, int(b.get("binding_epoch") or 0))
        except (TypeError, ValueError):
            b["binding_epoch"] = 0
        return b

    @staticmethod
    def _bump_epoch(b: dict[str, Any]) -> int:
        b["binding_epoch"] = int(b.get("binding_epoch") or 0) + 1
        return b["binding_epoch"]

    def _state_from_bucket(self, user: str, b: dict[str, Any]) -> dict[str, Any]:
        by_cc = b.get("by_cc") if isinstance(b.get("by_cc"), dict) else {}
        by_cc_out: dict[str, Any] = {}
        for sid, ent in by_cc.items():
            if not isinstance(sid, str):
                continue
            by_cc_out[sid] = {
                "dify_cid": _ent_cid(ent),
                "updated_at": ent.get("updated_at") if isinstance(ent, dict) else None,
            }
        return {
            "user": user,
            "current": b.get("current"),
            "cc_session_id": b.get("cc_session_id"),
            "by_cc": by_cc_out,
        }

    def get_state(self, user: str) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            return self._state_from_bucket(user, self._bucket(data, user))

    def get_current(self, user: str) -> str | None:
        cur = self.get_state(user).get("current")
        return cur if isinstance(cur, str) and cur else None

    def _current_in_by_cc(self, b: dict[str, Any], cur: str) -> bool:
        by_cc = b.get("by_cc") if isinstance(b.get("by_cc"), dict) else {}
        return any(_ent_cid(ent) == cur for ent in by_cc.values())

    def resolve_conversation(
        self, user: str, cc_session_id: str | None
    ) -> dict[str, Any]:
        """本枪应附着的 Dify cid（只读，不写盘）。

        session_bind：hit（命中 by_cc[S]）· miss（新 S → 不传 cid，Dify 新建）·
        missing（请求无 S → sticky current；current 是幽灵则丢弃）。
        """
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            cur = b.get("current")
            cur_s = cur if isinstance(cur, str) and cur.strip() else None

            sid = (cc_session_id or "").strip() or None
            if not sid:
                if cur_s and b.get("by_cc") and not self._current_in_by_cc(b, cur_s):
                    return {
                        "conversation_id": None,
                        "session_bind": "missing",
                        "cc_session_id": None,
                        "binding_epoch": int(b.get("binding_epoch") or 0),
                    }
                return {
                    "conversation_id": cur_s,
                    "session_bind": "missing",
                    "cc_session_id": None,
                    "binding_epoch": int(b.get("binding_epoch") or 0),
                }

            by_cc = b.get("by_cc") if isinstance(b.get("by_cc"), dict) else {}
            mapped = _ent_cid(by_cc.get(sid))
            if mapped:
                return {
                    "conversation_id": mapped,
                    "session_bind": "hit",
                    "cc_session_id": sid,
                    "binding_epoch": int(b.get("binding_epoch") or 0),
                }
            return {
                "conversation_id": None,
                "session_bind": "miss",
                "cc_session_id": sid,
                "binding_epoch": int(b.get("binding_epoch") or 0),
            }

    def new_session(
        self,
        user: str,
        cc_session_id: str | None = None,
        *,
        clear_all: bool = False,
    ) -> dict[str, Any]:
        """解绑并清空 current。

        - clear_all=True：清空全部 by_cc
        - 显式 cc_session_id：只删该 S；若其 cid==current 则清 current
        - 无参：按 current 反查删除所有指向该 cid 的映射，再清 current
        """
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            self._bump_epoch(b)
            by_cc = b["by_cc"]
            unbound: list[str] = []
            explicit = (cc_session_id or "").strip() or None

            if clear_all:
                unbound = list(by_cc.keys())
                by_cc.clear()
                b["current"] = None
                b["cc_session_id"] = None
            elif explicit:
                old_cid = _ent_cid(by_cc.get(explicit)) if explicit in by_cc else None
                if explicit in by_cc:
                    del by_cc[explicit]
                    unbound.append(explicit)
                if old_cid and b.get("current") == old_cid:
                    b["current"] = None
                if b.get("cc_session_id") == explicit:
                    b["cc_session_id"] = None
            else:
                cur = b.get("current")
                cur_s = cur if isinstance(cur, str) and cur.strip() else None
                if cur_s:
                    for sid in list(by_cc.keys()):
                        if _ent_cid(by_cc.get(sid)) == cur_s:
                            del by_cc[sid]
                            unbound.append(sid)
                b["current"] = None
                b["cc_session_id"] = None

            self._write(data)
            state = self._state_from_bucket(user, b)
            state["unbound_cc"] = unbound
            return state

    @staticmethod
    def _touch_current(b: dict[str, Any], cid: str) -> None:
        """current 指向（switch / remember 共用）。

        不另存会话档案列表：由 by_cc 的 dify_cid + updated_at 即可复原同一批事实，
        而那份列表的 title 字段从未有过写入方。
        """
        b["current"] = cid

    def switch(
        self,
        user: str,
        conversation_id: str,
        cc_session_id: str | None = None,
    ) -> dict[str, Any]:
        """切换 current；有 S（参数或已跟踪）时同步写入 by_cc[S]。"""
        cid = (conversation_id or "").strip()
        if not cid:
            raise ValueError("conversation_id 不能为空")
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            self._bump_epoch(b)
            now = utc_now()
            self._touch_current(b, cid)

            sid = (cc_session_id or "").strip() or None
            if not sid:
                last = b.get("cc_session_id")
                if isinstance(last, str) and last.strip():
                    sid = last.strip()
            if sid:
                b["cc_session_id"] = sid
                b["by_cc"][sid] = {"dify_cid": cid, "updated_at": now}
                self._trim_by_cc(b["by_cc"], self.max_by_cc)

            self._write(data)
            return self._state_from_bucket(user, b)

    def remember(
        self,
        user: str,
        conversation_id: str,
        cc_session_id: str | None = None,
        max_by_cc: int | None = None,
        expected_epoch: int | None = None,
    ) -> bool:
        """对话成功后写入档案、current，并绑定 CC session_id（唯一成功写入口）。"""
        cid = (conversation_id or "").strip()
        if not cid:
            return False
        limit = self.max_by_cc if max_by_cc is None else max(1, int(max_by_cc))
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            if expected_epoch is not None:
                try:
                    expected = int(expected_epoch)
                except (TypeError, ValueError):
                    return False
                if int(b.get("binding_epoch") or 0) != expected:
                    return False
            now = utc_now()
            self._touch_current(b, cid)

            sid = (cc_session_id or "").strip() or None
            if sid:
                b["cc_session_id"] = sid
                b["by_cc"][sid] = {"dify_cid": cid, "updated_at": now}
                self._trim_by_cc(b["by_cc"], limit)

            self._write(data)
            return True

    @staticmethod
    def _trim_by_cc(by_cc: dict[str, Any], max_by_cc: int) -> None:
        if max_by_cc <= 0 or len(by_cc) <= max_by_cc:
            return

        def sort_key(item: tuple[str, Any]) -> str:
            # 以 ISO 串直接排序：persist.utc_now() 恒定宽度、恒定 +00:00 偏移，
            # 故字典序等于时序。改动那个格式前须先把本键迁为浮点 epoch。
            _k, ent = item
            return str(ent.get("updated_at") or "") if isinstance(ent, dict) else ""

        ordered = sorted(by_cc.items(), key=sort_key)
        for k, _ in ordered[: len(by_cc) - max_by_cc]:
            del by_cc[k]
