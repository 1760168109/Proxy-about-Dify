# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import pytest

from singleflight import SingleFlight, request_fingerprint


async def _acquire_and_wait(flights, key, factory):
    """复现 main.py 的形态：acquire 取租约，再 shield 等待共享任务。

    生产不走更短的封装（它要在 CancelledError 分支补 delivery_status），
    故测试也走同一条路，否则守则 20 验的是一条生产不经过的代码路径。
    """
    lease = await flights.acquire(key, factory)
    result = await asyncio.shield(lease.task)
    return result, lease


def test_request_fingerprint_is_stable_and_content_sensitive():
    left = {"messages": [{"role": "user", "content": "同一枪"}], "model": "alan"}
    right = {"model": "alan", "messages": [{"content": "同一枪", "role": "user"}]}
    assert request_fingerprint(left, namespace="user") == request_fingerprint(
        right, namespace="user"
    )
    assert request_fingerprint(left, namespace="other") != request_fingerprint(
        right, namespace="user"
    )


def test_concurrent_waiters_share_one_factory_call():
    asyncio.run(_concurrent_waiters_share_one_factory_call())


async def _concurrent_waiters_share_one_factory_call():
    flights: SingleFlight[str] = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "answer"

    first = asyncio.create_task(_acquire_and_wait(flights, "same", factory))
    await started.wait()
    second = asyncio.create_task(_acquire_and_wait(flights, "same", factory))
    await asyncio.sleep(0)
    release.set()

    (first_result, first_lease), (second_result, second_lease) = await asyncio.gather(
        first, second
    )
    assert first_result == second_result == "answer"
    assert first_lease.state == "start"
    assert second_lease.state == "join"
    assert calls == 1


def test_success_is_replayed_but_failure_is_not_cached():
    asyncio.run(_success_is_replayed_but_failure_is_not_cached())


async def _success_is_replayed_but_failure_is_not_cached():
    flights: SingleFlight[str] = SingleFlight(success_ttl_seconds=60)
    calls = 0

    async def success() -> str:
        nonlocal calls
        calls += 1
        return "done"

    result, first = await _acquire_and_wait(flights, "ok", success)
    replayed, second = await _acquire_and_wait(flights, "ok", success)
    assert result == replayed == "done"
    assert first.state == "start"
    assert second.state == "replay"
    assert calls == 1

    failures = 0

    async def fail() -> str:
        nonlocal failures
        failures += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _acquire_and_wait(flights, "bad", fail)
    with pytest.raises(RuntimeError, match="boom"):
        await _acquire_and_wait(flights, "bad", fail)
    assert failures == 2


def test_cancelled_waiter_does_not_cancel_shared_upstream_task():
    asyncio.run(_cancelled_waiter_does_not_cancel_shared_upstream_task())


async def _cancelled_waiter_does_not_cancel_shared_upstream_task():
    flights: SingleFlight[str] = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "survived"

    waiter = asyncio.create_task(_acquire_and_wait(flights, "same", factory))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    joined = asyncio.create_task(_acquire_and_wait(flights, "same", factory))
    await asyncio.sleep(0)
    release.set()
    (result, lease) = await joined
    assert result == "survived"
    assert lease.state == "join"
    assert calls == 1
