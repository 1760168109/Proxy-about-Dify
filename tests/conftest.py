# -*- coding: utf-8 -*-
"""让各测试直接 import 平铺模块（proxy 根入 sys.path）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    """把 main 的四个模块级存储改指向 tmp_path，并关掉请求日志。

    `data/` 下的 usage.json / sessions.json / terminal_pending.json 与 request_logs/
    都是实时状态，而 main 在模块层就绑定了它们。端点级测试漏接其中任何一个，就会
    静默写用户的真实账本或日志——故隔离接线只应存在一处，不由每个测试各自复制。

    就地 patch 后返回 main 本身：调用方既可接住返回值，也可继续用模块级 `main`。
    """

    def _isolate(
        *,
        user: str = "test-user",
        cache_min_chars: int | None = None,
        session_store=None,
        terminal_store=None,
        log_requests: bool = False,
    ):
        import main
        from agent_bridge import AgentBridgeStore
        from cache import ReadCache
        from dify import DifyInputLimits
        from meter import UsageMeter
        from sessions import SessionStore
        from terminal import TerminalStore

        cache_kwargs = {} if cache_min_chars is None else {"min_chars": cache_min_chars}
        monkeypatch.setattr(main, "DIFY_USER_ID", user)
        monkeypatch.setattr(main, "LOG_REQUESTS", log_requests)
        monkeypatch.setattr(
            main, "store", session_store or SessionStore(tmp_path / "sessions.json")
        )
        monkeypatch.setattr(main, "meter", UsageMeter(tmp_path / "usage.json"))
        monkeypatch.setattr(
            main, "agent_store", AgentBridgeStore(tmp_path / "agents.json")
        )
        monkeypatch.setattr(
            main, "read_cache", ReadCache(tmp_path / "cache.json", **cache_kwargs)
        )
        monkeypatch.setattr(
            main,
            "terminal_store",
            terminal_store or TerminalStore(tmp_path / "terminal.json"),
        )

        async def _test_input_limits(**_kwargs):
            return DifyInputLimits({}, "test")

        # 端点测试不得访问真实 Dify；需要边界的用例会就地覆盖这个桩。
        monkeypatch.setattr(main, "_load_input_limits", _test_input_limits)
        return main

    return _isolate
