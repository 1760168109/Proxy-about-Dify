# -*- coding: utf-8 -*-
"""显式 terminal-tool：成功结果本地收尾，失败仍回 Dify。

工具相关内容仅保存 tool id / 工具名 / after_success，不保存参数或文件正文。
"""
from __future__ import annotations

import json
import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from parse import text_from_content
from persist import atomic_write_json, read_json_dict
from tools import TERMINAL_TOOL_NAMES, is_terminal_tool_batch


_SUCCESS_RE = {
    "write": re.compile(
        r"(?ix)^\s*(?:"
        r"file\s+created\s+successfully(?:\s+at\s*:)?|"
        r"file\s+(?:written|saved|updated)\s+successfully|"
        r"(?:the\s+)?file\s+has\s+been\s+(?:written|saved|updated)\s+successfully|"
        r"(?:the\s+)?file\s+.+?\s+has\s+been\s+(?:written|saved|updated)\s+successfully|"
        r"(?:the\s+)?file\s+was\s+(?:written|saved|updated)\s+successfully|"
        r"successfully\s+(?:created|written|saved|updated)(?:\s+(?:the\s+)?file)?|"
        r"file\s+state\s+is\s+current|"
        r"(?:文件)?\s*(?:已\s*)?(?:成功\s*(?:写入|保存|创建|更新)|"
        r"(?:写入|保存|创建|更新)\s*(?:成功|完成))"
        r")"
    ),
    "edit": re.compile(
        r"(?ix)^\s*(?:"
        r"file\s+(?:updated|edited)\s+successfully|"
        r"(?:the\s+)?file\s+has\s+been\s+(?:updated|edited)\s+successfully|"
        r"(?:the\s+)?file\s+.+?\s+has\s+been\s+(?:updated|edited)\s+successfully|"
        r"(?:the\s+)?file\s+was\s+(?:updated|edited)\s+successfully|"
        r"successfully\s+(?:updated|edited|replaced)(?:\s+(?:the\s+)?file)?|"
        r"file\s+state\s+is\s+current|"
        r"(?:文件)?\s*(?:已\s*)?(?:成功\s*(?:编辑|修改|更新|替换)|"
        r"(?:编辑|修改|更新|替换)\s*(?:成功|完成))"
        r")"
    ),
}


@dataclass(frozen=True)
class TerminalResolution:
    status: str = "none"  # none | success | fallback
    text: str = ""
    reason: str = ""
    tool_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()


def _current_tool_result_batch(
    body: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], bool] | None:
    """末条有效消息须为 user；返回结果块与是否混有其他用户内容。"""
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return None
    current: dict[str, Any] | None = None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            continue
        if role != "user":
            return None
        current = message
        break
    if current is None or not isinstance(current.get("content"), list):
        return None

    results: dict[str, dict[str, Any]] = {}
    mixed = False
    for block in current["content"]:
        if not isinstance(block, dict):
            mixed = True
            continue
        if block.get("type") == "tool_result":
            tid = str(block.get("tool_use_id") or block.get("id") or "").strip()
            if not tid or tid in results:
                mixed = True
                continue
            results[tid] = block
            continue
        if block.get("type") == "text":
            if str(block.get("text") or "").strip():
                mixed = True
            continue
        mixed = True
    return (results, mixed) if results else None


def _result_succeeded(name: str, block: dict[str, Any]) -> bool:
    if block.get("is_error"):
        return False
    text = text_from_content(block.get("content")).strip()
    if not text:
        return False
    pattern = _SUCCESS_RE.get((name or "").strip().lower())
    return bool(pattern and pattern.search(text))


def _entry_epoch(entry: Any) -> float:
    if not isinstance(entry, dict):
        return 0.0
    try:
        value = float(entry.get("created_epoch") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _result_fingerprint(results: dict[str, dict[str, Any]]) -> str:
    """只对当前 tool_result 的完整事实做幂等键，不保存正文到持久状态。"""
    raw = json.dumps(
        results,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class TerminalRegistration:
    """register 的结果。`__bool__` 与旧 bool 返回同义，故既有调用面不变。"""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


# 登记的拒绝原因在此命名：register 自己知道它为何拒绝，main 只能猜。
# 架构.md 列举的登记失败情形若在日志里不可区分，「为何回上游」就无从排查（守则 17）。
REG_OK = "registered"
REG_NO_SESSION = "missing_user_or_cc_session"
REG_EMPTY_DRAFT = "empty_after_success_draft"
REG_NO_TOOLS = "empty_tool_batch"
REG_INELIGIBLE_BATCH = "ineligible_or_conflicting_tool_batch"
REG_BAD_TOOL_SHAPE = "tool_missing_id_or_duplicate_or_not_write_edit"


class TerminalStore:
    """JSON 落盘：每个 user/session 至多一个待决终结批次。"""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int = 24 * 60 * 60,
        max_entries: int = 64,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.clock = clock
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write(self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"users": {}, "replays": {}}

    def _read(self) -> dict[str, Any]:
        return read_json_dict(self.path, self._empty)

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def _prune(self, data: dict[str, Any]) -> bool:
        changed = False
        users = data.get("users")
        if not isinstance(users, dict):
            data["users"] = {}
            users = data["users"]
        cutoff = self.clock() - self.ttl_seconds
        all_entries: list[tuple[float, str, str]] = []
        for user, bucket in list(users.items()):
            if not isinstance(bucket, dict):
                del users[user]
                changed = True
                continue
            for sid, entry in list(bucket.items()):
                created = _entry_epoch(entry)
                if created < cutoff:
                    del bucket[sid]
                    changed = True
                else:
                    all_entries.append((created, str(user), str(sid)))
            if not bucket:
                del users[user]
                changed = True
        if len(all_entries) > self.max_entries:
            all_entries.sort()
            for _created, user, sid in all_entries[: len(all_entries) - self.max_entries]:
                bucket = users.get(user)
                if isinstance(bucket, dict):
                    bucket.pop(sid, None)
                    changed = True
                    if not bucket:
                        users.pop(user, None)

        replays = data.get("replays")
        if not isinstance(replays, dict):
            data["replays"] = {}
            replays = data["replays"]
            changed = True
        replay_entries: list[tuple[float, str, str, str]] = []
        for user, sessions in list(replays.items()):
            if not isinstance(sessions, dict):
                del replays[user]
                changed = True
                continue
            for sid, bucket in list(sessions.items()):
                if not isinstance(bucket, dict):
                    del sessions[sid]
                    changed = True
                    continue
                for fp, entry in list(bucket.items()):
                    created = _entry_epoch(entry)
                    if created < cutoff:
                        del bucket[fp]
                        changed = True
                    else:
                        replay_entries.append((created, str(user), str(sid), str(fp)))
                if not bucket:
                    del sessions[sid]
                    changed = True
            if not sessions:
                del replays[user]
                changed = True
        if len(replay_entries) > self.max_entries:
            replay_entries.sort()
            for _created, user, sid, fp in replay_entries[: len(replay_entries) - self.max_entries]:
                sessions = replays.get(user)
                bucket = sessions.get(sid) if isinstance(sessions, dict) else None
                if isinstance(bucket, dict):
                    bucket.pop(fp, None)
                    changed = True
                    if not bucket:
                        sessions.pop(sid, None)
                    if not sessions:
                        replays.pop(user, None)
        return changed

    def register(
        self,
        user: str,
        session_id: str | None,
        tool_uses: list[dict[str, Any]],
        after_success: str,
    ) -> TerminalRegistration:
        sid = (session_id or "").strip()
        text = (after_success or "").strip()
        if not user or not sid:
            return TerminalRegistration(False, REG_NO_SESSION)
        if not text:
            return TerminalRegistration(False, REG_EMPTY_DRAFT)
        if not tool_uses:
            return TerminalRegistration(False, REG_NO_TOOLS)
        if not is_terminal_tool_batch(tool_uses):
            return TerminalRegistration(False, REG_INELIGIBLE_BATCH)
        tools: dict[str, str] = {}
        for tool in tool_uses:
            if not isinstance(tool, dict):
                return TerminalRegistration(False, REG_BAD_TOOL_SHAPE)
            tid = str(tool.get("id") or "").strip()
            name = str(tool.get("name") or "").strip()
            if not tid or tid in tools or name not in TERMINAL_TOOL_NAMES:
                return TerminalRegistration(False, REG_BAD_TOOL_SHAPE)
            tools[tid] = name
        with self._lock:
            data = self._read()
            self._prune(data)
            users = data.setdefault("users", {})
            bucket = users.setdefault(user, {})
            replays = data.setdefault("replays", {})
            replay_sessions = replays.get(user)
            if isinstance(replay_sessions, dict):
                replay_sessions.pop(sid, None)
                if not replay_sessions:
                    replays.pop(user, None)
            bucket[sid] = {
                "after_success": text[:8000],
                "tools": tools,
                "created_epoch": self.clock(),
            }
            self._prune(data)
            self._write(data)
        return TerminalRegistration(True, REG_OK)

    def resolve(
        self, user: str, session_id: str | None, body: dict[str, Any]
    ) -> TerminalResolution:
        sid = (session_id or "").strip()
        if not user or not sid:
            return TerminalResolution()
        batch = _current_tool_result_batch(body)
        if batch is None:
            return TerminalResolution()
        results, mixed = batch
        fingerprint = "" if mixed else _result_fingerprint(results)

        with self._lock:
            data = self._read()
            pruned = self._prune(data)
            replays = data.get("replays") if isinstance(data.get("replays"), dict) else {}
            replay_sessions = replays.get(user) if isinstance(replays.get(user), dict) else {}
            replay = replay_sessions.get(sid) if isinstance(replay_sessions, dict) else None
            if fingerprint and isinstance(replay, dict) and fingerprint in replay:
                replay_entry = replay[fingerprint]
                if pruned:
                    self._write(data)
                return TerminalResolution(
                    "success",
                    text=str(replay_entry.get("after_success") or "").strip(),
                    reason="all_terminal_tools_succeeded_replay",
                    tool_ids=tuple(str(x) for x in replay_entry.get("tool_ids") or ()),
                    tool_names=tuple(str(x) for x in replay_entry.get("tool_names") or ()),
                )
            users = data.get("users") if isinstance(data.get("users"), dict) else {}
            bucket = users.get(user) if isinstance(users.get(user), dict) else {}
            entry = bucket.get(sid) if isinstance(bucket, dict) else None
            if not isinstance(entry, dict):
                if pruned:
                    self._write(data)
                return TerminalResolution()
            tools = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
            expected = set(str(tid) for tid in tools)
            actual = set(results)
            if not (expected & actual):
                if pruned:
                    self._write(data)
                return TerminalResolution()

            ids = tuple(sorted(expected))
            names = tuple(str(tools[tid]) for tid in ids)
            resolution: TerminalResolution
            if mixed:
                resolution = TerminalResolution(
                    "fallback",
                    reason="mixed_current_user",
                    tool_ids=ids,
                    tool_names=names,
                )
            elif actual != expected:
                resolution = TerminalResolution(
                    "fallback",
                    reason="tool_result_set_mismatch",
                    tool_ids=ids,
                    tool_names=names,
                )
            else:
                failed_tid = next(
                    (
                        tid
                        for tid in ids
                        if not _result_succeeded(str(tools[tid]), results[tid])
                    ),
                    None,
                )
                if failed_tid is not None:
                    resolution = TerminalResolution(
                        "fallback",
                        reason="tool_result_not_explicit_success:{}".format(failed_tid),
                        tool_ids=ids,
                        tool_names=names,
                    )
                else:
                    resolution = TerminalResolution(
                        "success",
                        text=str(entry.get("after_success") or "").strip(),
                        reason="all_terminal_tools_succeeded",
                        tool_ids=ids,
                        tool_names=names,
                    )

            # 已等到相关结果，本批无论成功或回退都只消费一次；成功保留短期回放。
            bucket.pop(sid, None)
            if not bucket:
                users.pop(user, None)
            if resolution.status == "success":
                replay_sessions = data.setdefault("replays", {}).setdefault(user, {})
                replay_bucket = replay_sessions.setdefault(sid, {})
                replay_bucket[fingerprint] = {
                    "after_success": resolution.text[:8000],
                    "tool_ids": list(ids),
                    "tool_names": list(names),
                    "created_epoch": self.clock(),
                }
            self._write(data)
            return resolution

    def clear_session(self, user: str, session_id: str) -> int:
        sid = (session_id or "").strip()
        if not sid:
            return 0
        return self._clear(user, sid)

    def clear_all(self, user: str) -> int:
        return self._clear(user, None)

    def _clear(self, user: str, sid: str | None) -> int:
        if not user:
            return 0
        with self._lock:
            data = self._read()
            users = data.get("users") if isinstance(data.get("users"), dict) else {}
            replays = data.get("replays") if isinstance(data.get("replays"), dict) else {}
            bucket = users.get(user) if isinstance(users.get(user), dict) else None
            replay_sessions = replays.get(user) if isinstance(replays.get(user), dict) else None
            if not isinstance(bucket, dict) and not isinstance(replay_sessions, dict):
                return 0
            replay_removed = False
            if sid:
                removed = 1 if isinstance(bucket, dict) and sid in bucket else 0
                if isinstance(bucket, dict):
                    bucket.pop(sid, None)
                if isinstance(replay_sessions, dict):
                    replay_removed = sid in replay_sessions
                    replay_sessions.pop(sid, None)
            else:
                removed = len(bucket) if isinstance(bucket, dict) else 0
                if isinstance(bucket, dict):
                    bucket.clear()
                if isinstance(replay_sessions, dict):
                    replay_removed = bool(replay_sessions)
                    replay_sessions.clear()
            if isinstance(bucket, dict) and not bucket:
                users.pop(user, None)
            if isinstance(replay_sessions, dict) and not replay_sessions:
                replays.pop(user, None)
            if removed or replay_removed:
                self._write(data)
            return removed

    def pending_count(self, user: str | None = None) -> int:
        with self._lock:
            data = self._read()
            pruned = self._prune(data)
            if pruned:
                self._write(data)
            users = data.get("users") if isinstance(data.get("users"), dict) else {}
            if user is not None:
                bucket = users.get(user)
                return len(bucket) if isinstance(bucket, dict) else 0
            return sum(len(bucket) for bucket in users.values() if isinstance(bucket, dict))
