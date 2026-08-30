# -*- coding: utf-8 -*-
"""CC / 子代理身份 → Dify conversation_id 绑定（详见架构.md「会话绑定」）。

主窗口真源是 ``by_cc[S]``，``current`` 只作展示与请求无 S 时的 sticky；
子代理真源是 ``by_agent[parent_sid][agent_id]``。两者必须保持独立，子代理成功
不得移动主 ``current``，也不得覆盖父窗口 CID。resolve 只读；写盘只在成功收尾、
new_session 或 switch 时发生。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from persist import atomic_write_json, read_json_dict, utc_now

# 每 user 最多保留多少条 CC→Dify 映射（LRU by updated_at）；子代理另有独立总上限。
MAX_BY_CC = 32
MAX_BY_AGENT = 64
MAX_SCOPE_EPOCHS = 128
_CURRENT_EPOCH_KEY = "__current__"


def _session_epoch_key(session_id: str | None) -> str:
    sid = (session_id or "").strip()
    return "session:" + sid if sid else _CURRENT_EPOCH_KEY

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
    def __init__(
        self,
        path: Path,
        *,
        max_by_cc: int = MAX_BY_CC,
        max_by_agent: int = MAX_BY_AGENT,
    ) -> None:
        self.path = path
        self.max_by_cc = max(1, int(max_by_cc))
        self.max_by_agent = max(1, int(max_by_agent))
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
                "by_agent": {},
                "binding_epoch": 0,
            }
            data[user] = b
        b.setdefault("current", None)
        b.setdefault("cc_session_id", None)
        if not isinstance(b.get("by_cc"), dict):
            b["by_cc"] = {}
        # 子代理 CID 刻意不进入 by_cc/current：后台代理不得移动或覆盖主窗口的
        # sticky conversation，故另以 (parent_sid, agent_id) 建命名空间。
        if not isinstance(b.get("by_agent"), dict):
            b["by_agent"] = {}
        try:
            b["binding_epoch"] = max(0, int(b.get("binding_epoch") or 0))
        except (TypeError, ValueError):
            b["binding_epoch"] = 0
        # 旧版本只有一个 user 级 epoch。迁移时为已知命名空间建立同值基线；
        # 后续解绑/切换只递增受影响的 scope key，避免 S1 的变更使并行 S2 的
        # 迟到结果被误判为过期。binding_epoch 仍是 selection/current 的兼容字段。
        epochs = b.get("scope_epochs")
        if not isinstance(epochs, dict):
            # 接受开发期曾使用过的 binding_epochs 名称，避免已有本地状态失效。
            epochs = b.get("binding_epochs")
        if not isinstance(epochs, dict):
            epochs = {}
        legacy_epoch = int(b.get("binding_epoch") or 0)
        epochs.setdefault(_CURRENT_EPOCH_KEY, legacy_epoch)
        for sid in b["by_cc"]:
            if isinstance(sid, str):
                epochs.setdefault(_session_epoch_key(sid), legacy_epoch)
        for parent_sid in b["by_agent"]:
            if isinstance(parent_sid, str):
                epochs.setdefault(_session_epoch_key(parent_sid), legacy_epoch)
        b["scope_epochs"] = epochs
        try:
            b["binding_reset_epoch"] = max(0, int(b.get("binding_reset_epoch") or 0))
        except (TypeError, ValueError):
            b["binding_reset_epoch"] = 0
        return b

    @staticmethod
    def _namespace_epoch(b: dict[str, Any], key: str) -> int:
        epochs = b.setdefault("scope_epochs", {})
        if not isinstance(epochs, dict):
            epochs = {}
            b["scope_epochs"] = epochs
        if key not in epochs:
            epochs[key] = 0
        try:
            value = int(epochs.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
            epochs[key] = value
        return max(0, value)

    @classmethod
    def _bump_namespace(cls, b: dict[str, Any], key: str) -> None:
        value = cls._namespace_epoch(b, key) + 1
        b["scope_epochs"][key] = value
        if len(b["scope_epochs"]) > MAX_SCOPE_EPOCHS:
            # Tombstones must outlive any in-flight request. Instead of unsafely
            # evicting one scope, cross the reset fence and invalidate all old tokens;
            # this is rare, bounded, and fail-closed.
            b["binding_reset_epoch"] = int(b.get("binding_reset_epoch") or 0) + 1
            b["binding_epoch"] = int(b.get("binding_epoch") or 0) + 1
            b["scope_epochs"] = {}

    @staticmethod
    def _bump_selection(b: dict[str, Any]) -> None:
        b["binding_epoch"] = int(b.get("binding_epoch") or 0) + 1

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
        by_agent = b.get("by_agent") if isinstance(b.get("by_agent"), dict) else {}
        by_agent_out: dict[str, Any] = {}
        for parent_sid, agents in by_agent.items():
            if not isinstance(parent_sid, str) or not isinstance(agents, dict):
                continue
            parent_out: dict[str, Any] = {}
            for agent_id, ent in agents.items():
                if not isinstance(agent_id, str):
                    continue
                parent_out[agent_id] = {
                    "dify_cid": _ent_cid(ent),
                    "updated_at": ent.get("updated_at") if isinstance(ent, dict) else None,
                }
            if parent_out:
                by_agent_out[parent_sid] = parent_out
        return {
            "user": user,
            "current": b.get("current"),
            "cc_session_id": b.get("cc_session_id"),
            "by_cc": by_cc_out,
            "by_agent": by_agent_out,
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
        missing（请求无 S → sticky current；有 by_cc 映射时，current 若不在其中则丢弃）。

        ``current`` 只是无 session id 请求的候选指针；它不替代 ``by_cc`` 的
        映射真源。by_cc 为空时仍允许 current-only sticky——它可能来自本来
        就没有 session id 的请求，或无 sid 的手动 switch。
        """
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            sid = (cc_session_id or "").strip() or None
            cur = b.get("current")
            cur_s = cur if isinstance(cur, str) and cur.strip() else None

            if not sid:
                if cur_s and b.get("by_cc") and not self._current_in_by_cc(b, cur_s):
                    return {
                        "conversation_id": None,
                        "session_bind": "missing",
                        "cc_session_id": None,
                        "binding_epoch": int(b.get("binding_epoch") or 0),
                        "scope_epoch": self._namespace_epoch(b, _session_epoch_key(None)),
                        "reset_epoch": int(b.get("binding_reset_epoch") or 0),
                    }
                return {
                    "conversation_id": cur_s,
                    "session_bind": "missing",
                    "cc_session_id": None,
                    "binding_epoch": int(b.get("binding_epoch") or 0),
                    "scope_epoch": self._namespace_epoch(b, _session_epoch_key(None)),
                    "reset_epoch": int(b.get("binding_reset_epoch") or 0),
                }

            by_cc = b.get("by_cc") if isinstance(b.get("by_cc"), dict) else {}
            mapped = _ent_cid(by_cc.get(sid))
            if mapped:
                return {
                    "conversation_id": mapped,
                    "session_bind": "hit",
                    "cc_session_id": sid,
                    "binding_epoch": int(b.get("binding_epoch") or 0),
                    "scope_epoch": self._namespace_epoch(b, _session_epoch_key(sid)),
                    "reset_epoch": int(b.get("binding_reset_epoch") or 0),
                }
            return {
                "conversation_id": None,
                "session_bind": "miss",
                "cc_session_id": sid,
                "binding_epoch": int(b.get("binding_epoch") or 0),
                "scope_epoch": self._namespace_epoch(b, _session_epoch_key(sid)),
                "reset_epoch": int(b.get("binding_reset_epoch") or 0),
            }

    def resolve_agent_conversation(
        self,
        user: str,
        parent_cc_session_id: str | None,
        agent_id: str | None,
    ) -> dict[str, Any]:
        """按 ``(parent session, agent_id)`` 只读解析子代理 Dify CID。

        不触碰 ``current`` 或主 ``by_cc``，因为子代理的成功响应不能改变主窗口
        下一轮请求的 sticky conversation。
        """
        parent_sid = (parent_cc_session_id or "").strip()
        child_id = (agent_id or "").strip()
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            epoch = int(b.get("binding_epoch") or 0)
            scope_epoch = self._namespace_epoch(b, _session_epoch_key(parent_sid))
            reset_epoch = int(b.get("binding_reset_epoch") or 0)
            if not parent_sid or not child_id:
                return {
                    "conversation_id": None,
                    "session_bind": "missing",
                    "parent_cc_session_id": parent_sid or None,
                    "agent_id": child_id or None,
                    "binding_epoch": epoch,
                    "scope_epoch": scope_epoch,
                    "reset_epoch": reset_epoch,
                }
            by_agent = b.get("by_agent") if isinstance(b.get("by_agent"), dict) else {}
            agents = by_agent.get(parent_sid)
            mapped = _ent_cid(agents.get(child_id)) if isinstance(agents, dict) else None
            return {
                "conversation_id": mapped,
                "session_bind": "hit" if mapped else "miss",
                "parent_cc_session_id": parent_sid,
                "agent_id": child_id,
                "binding_epoch": epoch,
                "scope_epoch": scope_epoch,
                "reset_epoch": reset_epoch,
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

        定向操作只推进该 session 的 scope fence；``clear_all`` 才推进 reset fence。
        """
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            by_cc = b["by_cc"]
            unbound: list[str] = []
            explicit = (cc_session_id or "").strip() or None
            self._bump_selection(b)

            if clear_all:
                unbound = list(by_cc.keys())
                b["binding_reset_epoch"] = int(b.get("binding_reset_epoch") or 0) + 1
                b["scope_epochs"] = {}
                by_cc.clear()
                b["by_agent"].clear()
                b["current"] = None
                b["cc_session_id"] = None
            elif explicit:
                self._bump_namespace(b, _session_epoch_key(explicit))
                old_cid = _ent_cid(by_cc.get(explicit)) if explicit in by_cc else None
                if explicit in by_cc:
                    del by_cc[explicit]
                    unbound.append(explicit)
                if old_cid and b.get("current") == old_cid:
                    b["current"] = None
                if b.get("cc_session_id") == explicit:
                    b["cc_session_id"] = None
                b["by_agent"].pop(explicit, None)
            else:
                self._bump_namespace(b, _session_epoch_key(None))
                cur = b.get("current")
                cur_s = cur if isinstance(cur, str) and cur.strip() else None
                if cur_s:
                    for sid in list(by_cc.keys()):
                        if _ent_cid(by_cc.get(sid)) == cur_s:
                            del by_cc[sid]
                            unbound.append(sid)
                for sid in unbound:
                    self._bump_namespace(b, _session_epoch_key(sid))
                    b["by_agent"].pop(sid, None)
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
        """切换 current；有 S（参数或已跟踪）时同步写入 by_cc[S]。

        selection epoch 保护当前指针，session scope epoch 保护目标 S 的映射；
        因而其它 S 的并行结果仍可落到自己的 ``by_cc``。
        """
        cid = (conversation_id or "").strip()
        if not cid:
            raise ValueError("conversation_id 不能为空")
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            sid = (cc_session_id or "").strip() or None
            if not sid:
                last = b.get("cc_session_id")
                if isinstance(last, str) and last.strip():
                    sid = last.strip()
            self._bump_selection(b)
            self._bump_namespace(b, _session_epoch_key(None))
            if sid:
                self._bump_namespace(b, _session_epoch_key(sid))

            now = utc_now()
            self._touch_current(b, cid)
            if sid:
                b["cc_session_id"] = sid
                b["by_cc"][sid] = {"dify_cid": cid, "updated_at": now}
                self._trim_by_cc(
                    b["by_cc"], self.max_by_cc, protect_cid=str(b.get("current") or "")
                )

            self._write(data)
            return self._state_from_bucket(user, b)

    def remember(
        self,
        user: str,
        conversation_id: str,
        cc_session_id: str | None = None,
        max_by_cc: int | None = None,
        expected_epoch: int | None = None,
        expected_scope_epoch: int | None = None,
        expected_reset_epoch: int | None = None,
    ) -> bool:
        """对话成功后写入会话映射。

        ``expected_epoch`` 是兼容字段，表示用户级 current 选择仍未变化；新调用方
        同时提供 ``expected_scope_epoch`` / ``expected_reset_epoch``。作用域仍有效而
        其它 CC session 发生切换时，只写自己的 ``by_cc[S]``，不抢走新的 current。
        """
        cid = (conversation_id or "").strip()
        if not cid:
            return False
        limit = self.max_by_cc if max_by_cc is None else max(1, int(max_by_cc))
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            if (expected_scope_epoch is None) != (expected_reset_epoch is None):
                return False
            sid = (cc_session_id or "").strip() or None
            scope_key = _session_epoch_key(sid)
            scope_valid = True
            reset_valid = True
            if expected_scope_epoch is not None:
                try:
                    scope_valid = self._namespace_epoch(b, scope_key) == int(expected_scope_epoch)
                except (TypeError, ValueError):
                    scope_valid = False
            if expected_reset_epoch is not None:
                try:
                    reset_valid = int(b.get("binding_reset_epoch") or 0) == int(expected_reset_epoch)
                except (TypeError, ValueError):
                    reset_valid = False
            if not scope_valid or not reset_valid:
                return False
            # 带新式 scope/reset fence 的调用若没有 selection token，只能写目标
            # session 映射，不能借机移动 current；主流程会同时传三者。
            selection_valid = expected_scope_epoch is None
            if expected_epoch is not None:
                try:
                    expected = int(expected_epoch)
                except (TypeError, ValueError):
                    return False
                selection_valid = int(b.get("binding_epoch") or 0) == expected
                if expected_scope_epoch is None and not selection_valid:
                    return False
            if not sid and not selection_valid:
                return False
            now = utc_now()
            if sid:
                b["by_cc"][sid] = {"dify_cid": cid, "updated_at": now}
            if selection_valid:
                self._touch_current(b, cid)
                if sid:
                    b["cc_session_id"] = sid
            self._trim_by_cc(
                b["by_cc"],
                limit,
                protect_cid=cid if selection_valid else str(b.get("current") or ""),
            )

            self._write(data)
            return True

    def remember_agent(
        self,
        user: str,
        conversation_id: str,
        *,
        parent_cc_session_id: str | None,
        agent_id: str | None,
        expected_epoch: int | None = None,
        expected_scope_epoch: int | None = None,
        expected_reset_epoch: int | None = None,
    ) -> bool:
        """成功后绑定子代理 CID；不修改主 current / by_cc。

        新调用方按父 session 检查作用域 token；其它父 session 的解绑/切换不会
        误伤本 child，clear-all 仍由 reset epoch 统一阻断。旧 ``expected_epoch``
        调用保持原有的用户级兼容语义。
        """
        cid = (conversation_id or "").strip()
        parent_sid = (parent_cc_session_id or "").strip()
        child_id = (agent_id or "").strip()
        if not cid or not parent_sid or not child_id:
            return False
        with self._lock:
            data = self._read()
            b = self._bucket(data, user)
            if (expected_scope_epoch is None) != (expected_reset_epoch is None):
                return False
            scope_valid = True
            reset_valid = True
            if expected_scope_epoch is not None:
                try:
                    scope_valid = self._namespace_epoch(
                        b, _session_epoch_key(parent_sid)
                    ) == int(expected_scope_epoch)
                except (TypeError, ValueError):
                    scope_valid = False
            if expected_reset_epoch is not None:
                try:
                    reset_valid = int(b.get("binding_reset_epoch") or 0) == int(expected_reset_epoch)
                except (TypeError, ValueError):
                    reset_valid = False
            if not scope_valid or not reset_valid:
                return False
            if expected_epoch is not None:
                try:
                    expected = int(expected_epoch)
                except (TypeError, ValueError):
                    return False
                if expected_scope_epoch is None and int(b.get("binding_epoch") or 0) != expected:
                    return False
            by_agent = b["by_agent"]
            agents = by_agent.get(parent_sid)
            if not isinstance(agents, dict):
                agents = {}
                by_agent[parent_sid] = agents
            agents[child_id] = {"dify_cid": cid, "updated_at": utc_now()}
            self._trim_by_agent(by_agent, self.max_by_agent)
            self._write(data)
            return True

    @staticmethod
    def _trim_by_cc(
        by_cc: dict[str, Any], max_by_cc: int, *, protect_cid: str = ""
    ) -> None:
        if len(by_cc) <= max_by_cc:
            return

        def sort_key(item: tuple[str, Any]) -> str:
            # 以 ISO 串直接排序：persist.utc_now() 恒定宽度、恒定 +00:00 偏移，
            # 故字典序等于时序。改动那个格式前须先把本键迁为浮点 epoch。
            _k, ent = item
            return str(ent.get("updated_at") or "") if isinstance(ent, dict) else ""

        ordered = sorted(by_cc.items(), key=sort_key)
        protected_key = None
        if protect_cid:
            matching = [item for item in ordered if _ent_cid(item[1]) == protect_cid]
            if matching:
                protected_key = matching[-1][0]
        removable = [
            (key, ent)
            for key, ent in ordered
            if key != protected_key
        ]
        need = len(by_cc) - max_by_cc
        for key, _ in removable[:need]:
            del by_cc[key]

    @staticmethod
    def _trim_by_agent(by_agent: dict[str, Any], max_by_agent: int) -> None:
        entries: list[tuple[str, str, str]] = []
        for parent_sid, agents in by_agent.items():
            if not isinstance(parent_sid, str) or not isinstance(agents, dict):
                continue
            for agent_id, ent in agents.items():
                if not isinstance(agent_id, str):
                    continue
                updated = str(ent.get("updated_at") or "") if isinstance(ent, dict) else ""
                entries.append((updated, parent_sid, agent_id))
        if len(entries) <= max_by_agent:
            return
        for _updated, parent_sid, agent_id in sorted(entries)[: len(entries) - max_by_agent]:
            agents = by_agent.get(parent_sid)
            if isinstance(agents, dict):
                agents.pop(agent_id, None)
                if not agents:
                    by_agent.pop(parent_sid, None)
