# -*- coding: utf-8 -*-
"""显式 terminal-tool：成功结果本地收尾，失败仍回 Dify。

工具相关内容仅保存 tool id / 工具名 / after_success，不保存参数或文件正文。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from parse import text_from_content
from persist import atomic_write_json, utc_now
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"users": {}}

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else self._empty()
        except (OSError, json.JSONDecodeError):
            return self._empty()

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def _prune(self, data: dict[str, Any]) -> bool:
        changed = False
        users = data.get("users")
        if not isinstance(users, dict):
            data["users"] = {}
            return True
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
        if len(all_entries) <= self.max_entries:
            return changed
        all_entries.sort()
        for _created, user, sid in all_entries[: len(all_entries) - self.max_entries]:
            bucket = users.get(user)
            if isinstance(bucket, dict):
                bucket.pop(sid, None)
                changed = True
                if not bucket:
                    users.pop(user, None)
        return changed

    def register(
        self,
        user: str,
        session_id: str | None,
        tool_uses: list[dict[str, Any]],
        after_success: str,
    ) -> bool:
        sid = (session_id or "").strip()
        text = (after_success or "").strip()
        if not user or not sid or not text or not tool_uses:
            return False
        if not is_terminal_tool_batch(tool_uses):
            return False
        tools: dict[str, str] = {}
        for tool in tool_uses:
            if not isinstance(tool, dict):
                return False
            tid = str(tool.get("id") or "").strip()
            name = str(tool.get("name") or "").strip()
            if not tid or tid in tools or name not in TERMINAL_TOOL_NAMES:
                return False
            tools[tid] = name
        with self._lock:
            data = self._read()
            self._prune(data)
            users = data.setdefault("users", {})
            bucket = users.setdefault(user, {})
            bucket[sid] = {
                "after_success": text[:8000],
                "tools": tools,
                "created_at": utc_now(),
                "created_epoch": self.clock(),
            }
            self._prune(data)
            self._write(data)
        return True

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

        with self._lock:
            data = self._read()
            pruned = self._prune(data)
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

            # 已等到相关结果，本批无论成功或回退都只消费一次。
            bucket.pop(sid, None)
            if not bucket:
                users.pop(user, None)
            self._write(data)

            ids = tuple(sorted(expected))
            names = tuple(str(tools[tid]) for tid in ids)
            if mixed:
                return TerminalResolution(
                    "fallback",
                    reason="mixed_current_user",
                    tool_ids=ids,
                    tool_names=names,
                )
            if actual != expected:
                return TerminalResolution(
                    "fallback",
                    reason="tool_result_set_mismatch",
                    tool_ids=ids,
                    tool_names=names,
                )
            for tid in ids:
                if not _result_succeeded(str(tools[tid]), results[tid]):
                    return TerminalResolution(
                        "fallback",
                        reason="tool_result_not_explicit_success:{}".format(tid),
                        tool_ids=ids,
                        tool_names=names,
                    )
            return TerminalResolution(
                "success",
                text=str(entry.get("after_success") or "").strip(),
                reason="all_terminal_tools_succeeded",
                tool_ids=ids,
                tool_names=names,
            )

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
            bucket = users.get(user) if isinstance(users.get(user), dict) else None
            if not isinstance(bucket, dict):
                return 0
            if sid:
                removed = 1 if sid in bucket else 0
                bucket.pop(sid, None)
            else:
                removed = len(bucket)
                bucket.clear()
            if not bucket:
                users.pop(user, None)
            if removed:
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
