# -*- coding: utf-8 -*-
"""Claude Code ↔ lan 的模型身份与兼容别名。"""
from __future__ import annotations

from typing import Any

# 「发布什么身份」与「认领什么名字」是同一事实的两半，故与发现项同处一室。
MODEL_ALIASES = ("alan", "anthropic/alan", "dify-lan", "lan", "岚")
DEFAULT_MODEL = "alan"

# Claude Code 的网关模型发现只保留 id 中含 claude / anthropic 的条目。
# 这里只发布普通代理身份，不宣称上游没有提供的上下文能力。
PRIMARY_DISCOVERY_MODEL_ID = "anthropic/alan"
PRIMARY_DISCOVERY_MODEL = {
    "id": PRIMARY_DISCOVERY_MODEL_ID,
    "object": "model",
    "created": 0,
    "owned_by": "dify",
    "display_name": "岚",
}


def discovery_models(
    legacy_aliases: tuple[str, ...] = MODEL_ALIASES,
) -> list[dict[str, Any]]:
    """规范身份居首；旧别名继续发布，供既有接入工具兼容。"""
    out = [dict(PRIMARY_DISCOVERY_MODEL)]
    seen = {PRIMARY_DISCOVERY_MODEL_ID.lower()}
    for model_id in legacy_aliases:
        if model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        out.append(
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "dify",
                "display_name": "岚（兼容别名）",
            }
        )
    return out
