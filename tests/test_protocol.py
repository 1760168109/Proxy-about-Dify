# -*- coding: utf-8 -*-
"""Claude Code 网关模型身份发布。"""
from __future__ import annotations

from protocol import (
    MODEL_ALIASES,
    PRIMARY_DISCOVERY_MODEL_ID,
    discovery_models,
)


def test_discovery_advertises_filterable_plain_identity_first():
    # 走真实的 MODEL_ALIASES，而非手写副本——否则新增别名撞车不会被任何测试发现。
    models = discovery_models()
    assert models[0]["id"] == PRIMARY_DISCOVERY_MODEL_ID
    assert models[0]["id"] == "anthropic/alan"
    assert "[1m]" not in models[0]["id"].lower()
    assert models[0]["display_name"] == "岚"
    assert len({item["id"].lower() for item in models}) == len(models)
    assert {item["id"] for item in models} >= set(MODEL_ALIASES)
