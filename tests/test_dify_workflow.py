# -*- coding: utf-8 -*-
"""Dify DSL 的持久变量分片接线契约。"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "岚.yml"
BASE_INPUTS = [
    "claudeMd",
    "Memory",
    "Environment",
    "Language",
    "Output_Style",
    "Context_management",
    "CLAUDE",
    "MEMORY",
    "currentDate",
]
SHARD_GROUPS = ("Tool_invocation", "History", "Current_Context")


def _group(base: str) -> list[str]:
    return [base, *(f"{base}_{index}" for index in range(1, 5))]


EXPECTED_INPUT_ORDER = [
    *BASE_INPUTS,
    *_group("Tool_invocation"),
    "System_Description",
    *_group("History"),
    *_group("Current_Context"),
]


def test_dify_workflow_wires_all_persisted_input_shards() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    start_variables = re.findall(r"(?m)^ {10}variable: ([A-Za-z0-9_]+)$", text)
    assert start_variables == EXPECTED_INPUT_ORDER

    items_start = text.index("        items:\n")
    items_end = text.index("        title: 注入上下文", items_start)
    assigner = text[items_start:items_end]
    assigned = re.findall(
        r"variable_selector:\n {10}- conversation\n {10}- ([A-Za-z0-9_]+)",
        assigner,
    )
    assert assigned == EXPECTED_INPUT_ORDER

    conversation = text[
        text.index("  conversation_variables:\n") : text.index(
            "  environment_variables:",
        )
    ]
    declared = re.findall(r"(?m)^ {4}name: ([A-Za-z0-9_]+)$", conversation)
    assert declared[:16] == [
        *reversed(_group("Current_Context")),
        *reversed(_group("History")),
        "System_Description",
        *reversed(_group("Tool_invocation")),
    ]

    for base in SHARD_GROUPS:
        refs = "".join(f"{{{{#conversation.{name}#}}}}" for name in _group(base))
        assert text.count(refs) == 3
        for index in range(1, 5):
            name = f"{base}_{index}"
            assert conversation.count(f"    name: {name}\n") == 1
            assert text.count(f"          variable: {name}\n") == 1
            assert re.search(
                rf"max_length: 233333\n(?:.*\n){{0,4}}[ ]{{10}}variable: {name}$",
                text,
                re.MULTILINE,
            )
