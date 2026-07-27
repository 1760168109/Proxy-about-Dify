# -*- coding: utf-8 -*-
"""判枪：一次扫 body，定出本枪的全部策略。

route（haiku/opus/local）· kind（chat/title/recap/compact/placeholder）·
tools · 续主会话 · inputs 物化档 · 结构化出口 · 出站流式形态。

优先级：旁路（title/recap/compact）→ 子代理 → testandlife → model 名 → 默认 opus。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from parse import strip_reminders, system_to_text, text_from_content

Route = Literal["haiku", "opus", "local"]
GunKind = Literal["placeholder", "title", "recap", "compact", "chat"]
QueryMode = Literal["title_fold", "history_current", "opus_continue"]

# query 首行标记，供岚 if-else「包含」匹配；改动须同步岚工作流
ROUTE_TAG_HAIKU = "[[cc_route:haiku]]"
ROUTE_TAG_OPUS = "[[cc_route:opus]]"

# 代理认领的模型别名（路由判定与 /v1/models 发现共用）
MODEL_ALIASES = ("alan", "dify-lan", "lan", "岚")
DEFAULT_MODEL = "dify-lan"

SIDECAR_KINDS = ("title", "recap", "compact")

# 联调口令：强制 haiku（长口令防误触）
SMOKE_HAIKU_TOKEN = "testandlife"

# ── 旁路指纹（CC 文案变更时须同步） ──────────────────────────────────

_TITLE_MARKERS = (
    "Generate a concise, sentence-case title",
    'single "title" field',
    "single \"title\" field",
    "Return JSON with a single",
)
_RECAP_MARKERS = (
    "The user stepped away and is coming back",
    "Recap in under 40 words",
    "Lead with the overall goal and current task",
)
_COMPACT_MARKERS = (
    "CRITICAL: Respond with TEXT ONLY",
    "Do NOT call any tools",
    "Primary Request and Intent",
    "after compaction",
    "continue to apply after compaction",
    "Your summary should include the following sections",
    "Key Technical Concepts",
    "preserved verbatim in the summary",
)
_SIDECAR_EXTRA_MARKERS = (
    "compact the conversation",
    "Summarize this conversation",
)

_PLACEHOLDER_AGENT_TASK_RE = re.compile(
    r"(?is)^\s*===\s*SYSTEM CONTEXT\s*===\s*sys\s*"
    r"===\s*CONVERSATION HISTORY\s*===\s*hist\s*"
    r"===\s*USER MESSAGE\s*===\s*msg\s*$"
)

# ── 子代理指纹 ───────────────────────────────────────────────────────

_SUBAGENT_SYSTEM_MARKERS = (
    "You are a Claude agent, built on Anthropic's Claude Agent SDK",
    "You are an agent for Claude Code",
    "Claude Agent SDK",
    "built on Anthropic's Claude Agent SDK",
)
_SUBAGENT_AUX_MARKERS = (
    "You are already the dedicated agent for this task",
    "do not re-delegate your entire assignment",
    "Messages from the agent that launched you",
    "the caller will relay this to the user",
    "Agent threads always have their cwd reset",
)
_MAIN_SESSION_MARKERS = (
    "You are Claude Code, Anthropic's official CLI for Claude",
    'You are an interactive agent that helps users according to your "Output Style"',
)
_EXPLICIT_ROUTE_RE = re.compile(
    "{}|{}".format(re.escape(ROUTE_TAG_HAIKU), re.escape(ROUTE_TAG_OPUS)), re.I
)

_SUBAGENT_FAST_MARKERS = (
    "directory tree", "directory structure", "file tree", "list all files",
    "list all subdirectories", "list the full", "recursive list",
    "ls -r", "ls -R", "tree ", "find ", "glob ",
    "扫描目录", "目录结构", "完整结构", "列出所有", "递归列出", "列目录", "文件树",
    "dir /s", "Get-ChildItem -Recurse",
)
_SUBAGENT_HEAVY_MARKERS = (
    "analyze", "analysis", "architecture", "review", "investigate", "research",
    "design", "evaluate", "compare", "summarize findings",
    "分析", "评估", "审查", "架构", "对比", "方案", "内化", "提炼", "诊断", "根因",
    "code review", "adversarial",
)


@dataclass
class Plan:
    """单次 POST /v1/messages 的完整策略快照。"""

    model: str
    stream: bool
    route: str
    route_tag: str
    route_reasons: list[str] = field(default_factory=list)
    cc_model: str = ""
    is_subagent: bool = False
    kind: GunKind = "chat"
    trim_mode: str | None = None
    enable_tools: bool = False
    attach_main: bool = False
    tool_structured: bool = False
    query_mode: QueryMode = "opus_continue"
    is_main_window: bool = True

    @property
    def is_placeholder(self) -> bool:
        return self.kind == "placeholder"

    @property
    def is_sidecar_summary(self) -> bool:
        return self.kind in SIDECAR_KINDS

    @property
    def bill(self) -> bool:
        return self.kind != "placeholder"

    def log_extra(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "route_tag": self.route_tag,
            "route_reasons": list(self.route_reasons),
            "cc_model": self.cc_model,
            "stream": self.stream,
            "is_subagent": self.is_subagent,
            "gun_kind": self.kind,
            "trim_mode": self.trim_mode,
            "enable_tools": self.enable_tools,
            "attach_main": self.attach_main,
            "tool_structured": self.tool_structured,
            "query_mode": self.query_mode,
            "is_main_window": self.is_main_window,
        }


# ── 检测器 ───────────────────────────────────────────────────────────


def _last_user_text(body: dict[str, Any]) -> str:
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return text_from_content(m.get("content"))
    return ""


def _request_blob(body: dict[str, Any]) -> str:
    parts = [system_to_text(body.get("system"))]
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            parts.append(text_from_content(m.get("content")))
    return "\n".join(parts)


def is_title_generation(body: dict[str, Any]) -> bool:
    system = system_to_text(body.get("system"))
    if any(m in system for m in _TITLE_MARKERS):
        return True
    return any(m in _request_blob(body) for m in _TITLE_MARKERS)


def is_recap_generation(body: dict[str, Any]) -> bool:
    last_user = _last_user_text(body)
    if any(m in last_user for m in _RECAP_MARKERS):
        return True
    blob = system_to_text(body.get("system")) + "\n" + last_user
    return any(m in blob for m in _RECAP_MARKERS)


def is_compact_generation(body: dict[str, Any]) -> bool:
    last_user = _last_user_text(body)
    if not last_user:
        return False
    # 多特征命中，防正文偶含 "Primary Request" 误触
    hits = sum(1 for m in _COMPACT_MARKERS if m in last_user)
    if hits >= 2:
        return True
    return (
        "CRITICAL: Respond with TEXT ONLY" in last_user
        and "Primary Request and Intent" in last_user
    )


def is_placeholder_agent_task(body: dict[str, Any]) -> bool:
    """CC 子代理偶发把未填占位模板（sys/hist/msg）当任务发来 → 本地短路。"""
    core = strip_reminders(_last_user_text(body)).strip()
    if _PLACEHOLDER_AGENT_TASK_RE.match(core):
        return True
    return (
        "=== SYSTEM CONTEXT ===" in core
        and "=== USER MESSAGE ===" in core
        and bool(re.search(r"(?m)^\s*msg\s*$", core))
        and len(core) < 200
    )


def is_subagent_session(body: dict[str, Any]) -> bool:
    """Agent 工具拉起的子代理会话（多信号打分，降低文案微调漏判）。"""
    system = system_to_text(body.get("system"))
    if not system:
        return False
    head = system.lstrip()[:1200]

    if head.startswith("You are Claude Code"):
        return False
    if any(m in head[:200] for m in _MAIN_SESSION_MARKERS):
        if not any(m in head for m in _SUBAGENT_SYSTEM_MARKERS):
            return False
    if any(m in head for m in _SUBAGENT_SYSTEM_MARKERS):
        return True

    score = 0
    if any(m in system for m in _SUBAGENT_AUX_MARKERS):
        score += 2
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        names = {(t.get("name") if isinstance(t, dict) else None) for t in tools}
        names.discard(None)
        # 主会话常带 Agent 且工具很多；子代理常为精简集且无 Agent
        if "Agent" not in names and 5 <= len(names) <= 20:
            score += 1
        if "Agent" in names and len(names) >= 25:
            score -= 2
    return score >= 2


def _subagent_task_text(body: dict[str, Any]) -> str:
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        raw = text_from_content(m.get("content"))
        if not raw:
            continue
        cleaned = strip_reminders(raw).strip()
        return cleaned or raw.strip()
    return ""


def _classify_subagent_route(body: dict[str, Any]) -> tuple[str, list[str]]:
    """只看委派任务正文（不看 SDK 模板句）：显式标记 → 机械 haiku → 默认 opus。"""
    reasons = ["cc_subagent_session"]
    task = _subagent_task_text(body)
    blob = task + "\n" + system_to_text(body.get("system"))[:400]
    m_ex = _EXPLICIT_ROUTE_RE.search(blob)
    if m_ex:
        r = "haiku" if "haiku" in m_ex.group(0).lower() else "opus"
        reasons.append("subagent_explicit_route_" + r)
        return r, reasons

    low = (task or "").lower()
    if not low.strip():
        reasons.append("subagent_default_opus_empty_task")
        return "opus", reasons
    if any(k.lower() in low for k in _SUBAGENT_HEAVY_MARKERS):
        reasons.append("subagent_heavy_analysis")
        return "opus", reasons
    if any(k.lower() in low for k in _SUBAGENT_FAST_MARKERS):
        reasons.append("subagent_mechanical_fast")
        return "haiku", reasons
    reasons.append("subagent_default_opus")
    return "opus", reasons


# ── 主入口 ───────────────────────────────────────────────────────────


def build_plan(
    body: dict[str, Any],
    *,
    accept_sse: bool = False,
    tool_structured: bool = False,
) -> Plan:
    """一次扫 body 组装策略；main 不得再重扫旁路指纹。

    流式裁决：body.stream 优先，省略看 Accept 头，皆无回 JSON
    （Anthropic 约定，勿默认 SSE——事故见经验.md 守则 1）。
    """
    model = body.get("model")
    if not isinstance(model, str) or not model:
        model = DEFAULT_MODEL
    stream = bool(body.get("stream")) if "stream" in body else bool(accept_sse)

    is_title = is_title_generation(body)
    is_recap = is_recap_generation(body)
    is_compact = is_compact_generation(body)

    if is_placeholder_agent_task(body):
        return Plan(
            model=model,
            stream=stream,
            route="local",
            route_tag="",
            route_reasons=["placeholder_agent_task"],
            cc_model=model,
            kind="placeholder",
            is_main_window=False,
        )

    mlow = model.strip().lower()
    reasons: list[str] = []
    subagent = is_subagent_session(body)
    blob_low = ""

    if is_title or is_recap or is_compact or any(
        m.lower() in (blob_low := _request_blob(body).lower())
        for m in _SIDECAR_EXTRA_MARKERS
    ):
        route = "haiku"
        reasons.append("sidecar_fast_task")
        if is_compact:
            reasons.append("compact_job")
        elif is_recap:
            reasons.append("recap_job")
        elif is_title:
            reasons.append("title_job")
        else:
            reasons.append("sidecar_extra_marker")
    elif subagent:
        route, sub_reasons = _classify_subagent_route(body)
        reasons.extend(sub_reasons)
    elif SMOKE_HAIKU_TOKEN in (blob_low or _request_blob(body).lower()):
        route = "haiku"
        reasons.append("smoke_token_testandlife")
    elif "haiku" in mlow:
        route = "haiku"
        reasons.append("model_name_haiku")
    elif mlow in MODEL_ALIASES or "opus" in mlow:
        route = "opus"
        reasons.append("model_name_opus_or_alan")
    elif "sonnet" in mlow:
        route = "opus"
        reasons.append("model_name_sonnet_as_opus")
    else:
        route = "opus"
        reasons.append("default_opus")

    if is_title:
        kind: GunKind = "title"
    elif is_compact:
        kind = "compact"
    elif is_recap:
        kind = "recap"
    else:
        kind = "chat"
    is_sidecar = kind in SIDECAR_KINDS

    trim_mode = None
    if route == "haiku":
        trim_mode = "empty" if is_sidecar else "strip"

    enable_tools = (
        isinstance(body.get("tools"), list)
        and len(body.get("tools") or []) > 0
        and not is_sidecar
    )

    if route == "haiku" and is_title:
        query_mode: QueryMode = "title_fold"
    elif route == "haiku":
        query_mode = "history_current"
    else:
        query_mode = "opus_continue"

    return Plan(
        model=model,
        stream=stream,
        route=route,
        route_tag=ROUTE_TAG_HAIKU if route == "haiku" else ROUTE_TAG_OPUS,
        route_reasons=reasons,
        cc_model=model,
        is_subagent=subagent,
        kind=kind,
        trim_mode=trim_mode,
        enable_tools=enable_tools,
        attach_main=(route == "opus" and not subagent),
        tool_structured=bool(tool_structured and enable_tools and route == "opus"),
        query_mode=query_mode,
        is_main_window=(not subagent) and (not is_sidecar),
    )
