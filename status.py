# -*- coding: utf-8 -*-
"""Claude Code 动作状态辅助请求：固定指纹检测与本地短句生成。"""
from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Any

from parse import text_from_content

ACTION_STATUS_PROMPT_HEAD = (
    "Describe your most recent action in 3-5 words using present tense (-ing). "
    "Name the file or function, not the branch. Do not use tools."
)

_PREVIOUS_RE = re.compile(r'Previous:\s*"([^"]+)"\s*[—-]\s*say something NEW\.', re.I)


def is_action_status_request(last_user: str) -> bool:
    """只认当前 CC 固定文案；未知变体回落普通模型链。"""
    return ACTION_STATUS_PROMPT_HEAD in (last_user or "")


def _previous_status(last_user: str) -> str:
    match = _PREVIOUS_RE.search(last_user or "")
    return match.group(1).strip() if match else ""


def _path_label(raw: Any) -> str:
    value = str(raw or "").strip().strip('"')
    if not value:
        return ""
    path = PureWindowsPath(value)
    name = re.sub(r"\s+", "_", path.name)
    parent = re.sub(r"\s+", "_", path.parent.name)
    if name.lower() == "skill.md" and parent:
        return "{} {}".format(parent, name)
    return name


def _latest_tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    """只取最近一批已发起工具调用，不从 tool_result 文案猜测动作。"""
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        calls = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if calls:
            return calls
    return []


def _candidate_for_call(call: dict[str, Any]) -> str:
    name = str(call.get("name") or "").strip()
    raw_input = call.get("input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    label = _path_label(
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
    )
    if name == "Read" and label:
        return "Reading {} file".format(label)
    if name == "Write" and label:
        return "Writing {} file".format(label)
    if name == "Edit" and label:
        return "Editing {} file".format(label)
    if name == "Glob":
        return "Scanning workspace file paths"
    if name == "Grep":
        return "Searching workspace file contents"
    if name in ("Bash", "PowerShell"):
        return "Running workspace command now"
    if name == "Agent":
        return "Delegating focused review task"
    if name == "Skill":
        return "Loading requested workflow skill"
    tool_label = re.sub(r"\s+", "_", name) or "requested"
    return "Using {} tool now".format(tool_label)


def _valid_status(candidate: str) -> bool:
    words = candidate.split()
    return (
        3 <= len(words) <= 5
        and bool(re.fullmatch(r"[A-Za-z]+ing", words[0], flags=re.I))
    )


def build_action_status(body: dict[str, Any]) -> str:
    """依据最近工具事实生成 3–5 词状态，不调用模型。"""
    last_user = ""
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = text_from_content(message.get("content"))
            break
    previous = _previous_status(last_user).casefold()

    # CC 只把这句话用作 UI heartbeat，并非推理轮；本地据实生成可避免一次
    # 可计费的 Dify 调用，也杜绝状态文案意外触发工具副作用。
    candidates = [_candidate_for_call(call) for call in reversed(_latest_tool_calls(body))]
    candidates.extend(
        (
            "Reviewing current task evidence",
            "Summarizing current task findings",
        )
    )
    for candidate in candidates:
        if _valid_status(candidate) and candidate.casefold() != previous:
            return candidate
    return "Reviewing current task context"
