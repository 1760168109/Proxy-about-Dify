# -*- coding: utf-8 -*-
"""进程内 single-flight：同一请求只执行一次，其余等待者共享结果。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
FlightState = Literal["start", "join", "replay"]


def request_fingerprint(body: object, *, namespace: str = "") -> str:
    """对一次 Anthropic 请求做稳定指纹；只记录摘要，不落原文。"""
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    payload = "{}\0{}".format(namespace, canonical).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FlightLease(Generic[T]):
    """一次请求取得的共享任务及其命中方式。"""

    task: asyncio.Task[T]
    state: FlightState
    age_seconds: float


@dataclass
class _Entry(Generic[T]):
    task: asyncio.Task[T]
    created_at: float
    completed_at: float | None = None


class SingleFlight(Generic[T]):
    """合并相同的并发请求，并短期保留成功结果供自动重试回放。

    等待者使用 ``asyncio.shield``：某个 HTTP 客户端断线或取消，不会把仍在 Dify
    运行的唯一上游任务一并取消。失败结果不缓存，下一次请求可重新尝试。
    """

    def __init__(
        self,
        *,
        success_ttl_seconds: float = 180.0,
        max_completed: int = 32,
    ) -> None:
        self.success_ttl_seconds = max(0.0, float(success_ttl_seconds))
        self.max_completed = max(0, int(max_completed))
        self._entries: dict[str, _Entry[T]] = {}
        self._lock = asyncio.Lock()

    def _task_succeeded(self, task: asyncio.Task[T]) -> bool:
        if not task.done() or task.cancelled():
            return False
        try:
            return task.exception() is None
        except asyncio.CancelledError:
            return False

    def _prune_locked(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if not entry.task.done():
                continue
            if not self._task_succeeded(entry.task):
                self._entries.pop(key, None)
                continue
            completed_at = entry.completed_at or now
            if now - completed_at > self.success_ttl_seconds:
                self._entries.pop(key, None)

        completed = sorted(
            (
                (entry.completed_at or entry.created_at, key)
                for key, entry in self._entries.items()
                if entry.task.done() and self._task_succeeded(entry.task)
            ),
            reverse=True,
        )
        for _completed_at, key in completed[self.max_completed :]:
            self._entries.pop(key, None)

    def _mark_done(self, key: str, task: asyncio.Task[T]) -> None:
        entry = self._entries.get(key)
        if entry is None or entry.task is not task:
            return
        entry.completed_at = time.monotonic()
        if not self._task_succeeded(task):
            self._entries.pop(key, None)

    async def acquire(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
    ) -> FlightLease[T]:
        """取得或创建任务；factory 只会由 ``start`` 请求调用一次。"""
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(key)
            if entry is not None:
                state: FlightState = "replay" if entry.task.done() else "join"
                return FlightLease(
                    task=entry.task,
                    state=state,
                    age_seconds=max(0.0, now - entry.created_at),
                )

            task = asyncio.create_task(factory(), name="lan-singleflight-{}".format(key[:12]))
            entry = _Entry(task=task, created_at=now)
            self._entries[key] = entry
            task.add_done_callback(lambda done, k=key: self._mark_done(k, done))
            return FlightLease(task=task, state="start", age_seconds=0.0)

    def stats(self) -> dict[str, int]:
        active = sum(1 for entry in self._entries.values() if not entry.task.done())
        completed = sum(1 for entry in self._entries.values() if entry.task.done())
        return {"active": active, "completed": completed, "total": len(self._entries)}

    async def close(self) -> None:
        """服务关闭时取消仍在运行的上游任务。"""
        async with self._lock:
            tasks = [entry.task for entry in self._entries.values() if not entry.task.done()]
            self._entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
