# -*- coding: utf-8 -*-
"""出站装配：body + plan + parsed → 查询 / inputs / 标记 / 附图。

query 标记约定：第一行恒为 [[cc_route:…]]；其余标记插在其后、正文之前。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from cache import (
    NEED_READ_NOTE,
    ReadCache,
    rehydrate_body_payloads,
    should_annotate_need_read,
)
from dify import upload_images
from parse import (
    TOOL_RESULT_PREFIX,
    build_dify_query,
    extract_images_from_last_user,
    fold_history_current_to_query,
    fold_messages_to_query,
    materialize_inputs,
    summarize_images,
)
from plan import Plan, ROUTE_TAG_HAIKU, ROUTE_TAG_OPUS
from tools import (
    TOOL_CONTINUE_MARKER,
    append_tools_reminder_to_query,
    decorate_tool_continue_query,
    inject_tools_into_inputs,
)

_ROUTE_HEAD_RE = re.compile(
    r"^({}|{})\s*\n?".format(re.escape(ROUTE_TAG_HAIKU), re.escape(ROUTE_TAG_OPUS)), re.I
)

STRUCT_MARKER = "[[cc_struct:on]]"

_AGENT_STATE_ORDER = ("pending", "result_ready", "result_carry")
_AGENT_STATE_MAX_ITEMS = 3
_AGENT_DESCRIPTION_MAX_CHARS = 96
_AGENT_SUCCESS_STATUSES = frozenset(("completed", "complete"))


def _short_agent_description(item: dict[str, Any]) -> str:
    description = item.get("description") or "未命名 Agent 委派"
    if not isinstance(description, str):
        description = str(description)
    description = re.sub(r"\s+", " ", description).strip() or "未命名 Agent 委派"
    if len(description) > _AGENT_DESCRIPTION_MAX_CHARS:
        description = description[: _AGENT_DESCRIPTION_MAX_CHARS - 1] + "…"
    return description


def _agent_status(item: dict[str, Any]) -> str:
    raw = str(item.get("status") or "unknown").strip().lower()
    return re.sub(r"[^a-z0-9_.-]+", "_", raw).strip("_")[:24] or "unknown"


def _agent_has_report(item: dict[str, Any]) -> bool:
    return _agent_status(item).replace("-", "_") in _AGENT_SUCCESS_STATUSES and bool(
        item.get("has_result")
    )


def _agent_task_lines(
    items: list[dict[str, Any]], *, include_status: bool = False
) -> str:
    shown = items[:_AGENT_STATE_MAX_ITEMS]
    lines = []
    for item in shown:
        label = _short_agent_description(item)
        if include_status:
            label += "（状态：{}）".format(_agent_status(item))
        lines.append("- {}".format(label))
    remaining = len(items) - len(shown)
    if remaining > 0:
        lines.append("- 另有 {} 个 Agent 任务".format(remaining))
    return "\n".join(lines)


def format_agent_lifecycle_block(lifecycle: Any) -> str:
    """将内部生命周期压成近端门槛；不带内部任务标识、输出文件或完整报告。"""
    if not isinstance(lifecycle, dict):
        return ""
    sections: list[str] = []
    for state in _AGENT_STATE_ORDER:
        raw_items = lifecycle.get(state)
        if not isinstance(raw_items, list):
            continue
        items = [item for item in raw_items if isinstance(item, dict)]
        if not items:
            continue
        all_have_reports = all(_agent_has_report(item) for item in items)
        tasks = _agent_task_lines(items, include_status=not all_have_reports)
        if state == "pending":
            sections.append(
                "[[cc_agents:pending]]\n"
                "仍有后台 Agent 未完成：\n{}\n"
                "不要用 Bash/Read/Glob 重复其文件或主题，也不要推进依赖其报告的提问、"
                "写入、判断或交付。只可做不重叠且不依赖结果的工作；否则说明仍在运行并结束本轮。".format(
                    tasks
                )
            )
        elif state == "result_ready":
            if all_have_reports:
                sections.append(
                    "[[cc_agents:result_ready]]\n"
                    "后台 Agent 完成通知已经到达：\n{}\n"
                    "完整报告位于「系统说明」的 <task-notification> 内 <result> 字段，"
                    "它不是用户输入。先整合并核对报告，再继续依赖步骤；"
                    "不要另读任务输出文件。".format(tasks)
                )
            else:
                sections.append(
                    "[[cc_agents:result_ready]]\n"
                    "后台 Agent 终止通知已经到达：\n{}\n"
                    "状态、结果或错误详情位于「系统说明」的 <task-notification>。"
                    "先核对终止原因；未成功任务不得按成功结果推进。".format(tasks)
                )
        elif state == "result_carry":
            if all_have_reports:
                sections.append(
                    "[[cc_agents:result_carry]]\n"
                    "后台 Agent 报告仍是当前工具续写链的证据：\n{}\n"
                    "结合「系统说明」中的 <result> 与本轮 tool result 后再继续，"
                    "不要遗忘委派结论。".format(tasks)
                )
            else:
                sections.append(
                    "[[cc_agents:result_carry]]\n"
                    "后台 Agent 终止通知仍在当前工具续写链中：\n{}\n"
                    "结合「系统说明」中的状态或错误详情与本轮 tool result，"
                    "再决定重试、降级或继续；不得假定任务成功。".format(tasks)
                )
    return "\n\n".join(sections)


def inject_marker_after_route(query: str, block: str) -> str:
    """block 插在 leading [[cc_route:…]] 之后；无 route 时前置；同块幂等。"""
    block = (block or "").strip()
    if not block:
        return query or ""
    q = query or ""
    if re.search(r"(?:^|\n){}(?=$|\n)".format(re.escape(block)), q):
        return q
    m = _ROUTE_HEAD_RE.match(q)
    if m:
        rest = q[m.end() :].lstrip("\n")
        parts = [m.group(1), block]
        if rest:
            parts.append(rest)
        return "\n".join(parts)
    if not q.strip():
        return block
    return block + "\n" + q.lstrip()


@dataclass
class Outbound:
    query: str
    dify_inputs: dict[str, str]
    query_user: str
    sparse: dict[str, str]
    need_read: bool
    cache_hits: list[str]
    cache_misses: list[str]
    query_user_chars: int
    tool_invocation_chars: int
    history_chars: int
    is_tool_continue: bool
    tool_structured: bool = False
    # 附图（attach_images_to_outbound 填充）
    dify_files: list = field(default_factory=list)
    image_failed: bool = False
    img_notes: list[str] = field(default_factory=list)
    image_b64_bytes: int = 0
    image_upload_status: str = "none"  # ok | failed | none | skipped
    has_images: bool = False
    image_count: int = 0


def prepare_text_outbound(
    *,
    body: dict[str, Any],
    plan: Plan,
    parsed: dict[str, Any],
    user_id: str,
    read_cache: ReadCache | None,
) -> Outbound:
    """文本侧装配：缓存重放 → 物化 → 工具注入 → query 组装与标记。"""
    sparse = dict(parsed.get("inputs") or {})
    query_user = parsed.get("query_user") or ""

    cache_hits: list[str] = []
    cache_misses: list[str] = []
    if read_cache is not None and not plan.is_sidecar_summary and not plan.is_placeholder:
        try:
            rh = rehydrate_body_payloads(
                query_user=query_user or "",
                tool_invocation=sparse.get("Tool_invocation") or "",
                history=sparse.get("History") or "",
                body=body,
                cache=read_cache,
                user_id=user_id,
            )
            query_user = rh.get("query_user") or query_user
            if "tool_invocation" in rh:
                sparse["Tool_invocation"] = rh["tool_invocation"] or ""
            if "history" in rh:
                sparse["History"] = rh["history"] or ""
            cache_hits = list(rh.get("hits") or [])
            cache_misses = list(rh.get("misses") or [])
        except Exception as e:
            print("[lan] read_cache skipped: {}".format(e))

    # recap / compact / haiku 子任务不接收 inputs，须用未去正文的历史折叠进 query；
    # opus 主枪则使用 History 中的工具结果引用，原文只在 Tool_invocation 保留一份。
    history_for_query = (
        parsed.get("history_full")
        if plan.query_mode == "history_current"
        else sparse.get("History")
    ) or ""
    dify_inputs = materialize_inputs(sparse, mode=plan.trim_mode)
    enable_tools = plan.enable_tools
    structured = plan.tool_structured
    if enable_tools:
        dify_inputs = inject_tools_into_inputs(
            dify_inputs, body.get("tools"), enabled=True, structured=structured
        )

    is_tool_continue_user = (query_user or "").strip().startswith(TOOL_RESULT_PREFIX)
    query_mode = plan.query_mode
    route_tag = plan.route_tag

    if query_mode == "title_fold":
        query = build_dify_query(route_tag, fold_messages_to_query(body))
    elif query_mode == "history_current":
        query = build_dify_query(
            route_tag, fold_history_current_to_query(history_for_query, query_user)
        )
    else:
        query = build_dify_query(
            route_tag, decorate_tool_continue_query(query_user, structured=structured)
        )
    query = append_tools_reminder_to_query(
        query, enabled=enable_tools, structured=structured
    )
    if structured:
        # 岚 if-else 据此路由到结构化出口分支
        query = inject_marker_after_route(query, STRUCT_MARKER)

    need_read = False
    if (
        not plan.is_sidecar_summary
        and query_mode == "opus_continue"
        and should_annotate_need_read(
            query_user or "",
            sparse.get("Tool_invocation") or "",
            is_tool_continue=is_tool_continue_user,
        )
    ):
        need_read = True
        query = inject_marker_after_route(query, NEED_READ_NOTE)

    if plan.is_main_window and plan.kind == "chat":
        agent_block = format_agent_lifecycle_block(parsed.get("agent_lifecycle"))
        if agent_block:
            query = inject_marker_after_route(query, agent_block)

    return Outbound(
        query=query,
        dify_inputs=dify_inputs,
        query_user=query_user,
        sparse=sparse,
        need_read=need_read,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        query_user_chars=len(query_user or ""),
        tool_invocation_chars=len(dify_inputs.get("Tool_invocation") or ""),
        history_chars=len(dify_inputs.get("History") or ""),
        is_tool_continue=TOOL_CONTINUE_MARKER in (query or ""),
        tool_structured=structured,
    )


def annotate_query_for_images(query: str, n_files: int) -> str:
    """声明本轮已附图及顺序；正文 [image] 占位改为带序号。"""
    if n_files <= 0:
        return query
    order_lines = [
        "  - Image #{} → Dify files[{}]（上传顺序第 {} 张）".format(i + 1, i, i + 1)
        for i in range(n_files)
    ]
    note = (
        "[[cc_images:{}]]\n"
        "本轮用户附图已通过 Dify files 多模态上传（专用于图，非文档）。\n"
        "顺序对应（与消息中 image 块出现顺序一致）：\n"
        "{}\n"
        "正文中的 [image] / [Image #N] 为占位；请按上表序号识图。"
    ).format(n_files, "\n".join(order_lines))
    q = query or ""
    n = [0]

    def _num(_m: re.Match[str]) -> str:
        n[0] += 1
        return "[image #{}]".format(n[0])

    if q:
        q = re.sub(r"\[image\]", _num, q, flags=re.I)
    return inject_marker_after_route(q, note)


def annotate_query_for_image_failure(query: str, notes: list[str] | None = None) -> str:
    detail = ""
    if notes:
        detail = "notes: " + "; ".join(str(n) for n in notes[:8]) + "\n"
    note = (
        "[[cc_images:failed]]\n"
        "本轮检测到用户附图，但未能准备 Dify files（上传失败或异常）。"
        "请勿假装已看到图片内容；可请用户重传或改用路径描述。\n"
        "{}".format(detail)
    ).rstrip()
    return inject_marker_after_route(query or "", note)


async def attach_images_to_outbound(
    outbound: Outbound,
    *,
    body: dict[str, Any],
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    user: str,
    is_sidecar: bool,
) -> Outbound:
    """抽图 → 上传（fail-open）→ 注解 query。"""
    if is_sidecar:
        outbound.image_upload_status = "skipped"
        return outbound

    try:
        images = extract_images_from_last_user(body, include_tool_result=True)
        stats0 = summarize_images(images)
        outbound.has_images = bool(stats0.get("has_images"))
        outbound.image_count = int(stats0.get("image_count") or 0)
        outbound.image_b64_bytes = int(stats0.get("b64_bytes") or 0)
        if not images:
            outbound.image_upload_status = "none"
            return outbound

        files, notes = await upload_images(
            images, base_url=base_url, api_key=api_key, user=user, client=client
        )
        outbound.dify_files = files or []
        outbound.img_notes = list(notes or [])
        if outbound.img_notes:
            print("[lan] images: {}".format("; ".join(outbound.img_notes)))

        if outbound.dify_files:
            outbound.query = annotate_query_for_images(outbound.query, len(outbound.dify_files))
            outbound.image_upload_status = "ok"
            print(
                "[lan] files→dify ×{} ids={}".format(
                    len(outbound.dify_files),
                    ",".join(
                        (f.get("upload_file_id") or f.get("url") or "?")[:12]
                        for f in outbound.dify_files
                        if isinstance(f, dict)
                    ),
                )
            )
        else:
            outbound.image_failed = True
            outbound.image_upload_status = "failed"
            outbound.query = annotate_query_for_image_failure(
                outbound.query, outbound.img_notes
            )
            print("[lan] images: present but files empty → [[cc_images:failed]]")
    except Exception as e:
        print("[lan] image upload skipped: {!r}".format(e))
        outbound.dify_files = []
        if outbound.has_images:
            outbound.image_failed = True
            outbound.image_upload_status = "failed"
            outbound.img_notes = list(outbound.img_notes) + [
                "exception:{} {!r}".format(type(e).__name__, e)
            ]
            outbound.query = annotate_query_for_image_failure(
                outbound.query, outbound.img_notes
            )
        else:
            outbound.image_upload_status = "none"
    return outbound


def outbound_log_extra(ob: Outbound) -> dict[str, Any]:
    from parse import sparse_inputs

    return {
        "parsed_inputs": {
            k: {"chars": len(v or ""), "head": (v or "")[:160]}
            for k, v in sparse_inputs(ob.dify_inputs).items()
        },
        "sparse_input_keys": list(ob.sparse.keys()),
        "dify_input_keys_nonempty": list(sparse_inputs(ob.dify_inputs).keys()),
        "dify_query": (ob.query or "")[:800],
        "history_chars": ob.history_chars,
        "query_user_chars": ob.query_user_chars,
        "tool_invocation_chars": ob.tool_invocation_chars,
        "read_cache_hits": (ob.cache_hits or [])[:8],
        "read_cache_misses": (ob.cache_misses or [])[:8],
        "need_read": ob.need_read,
        "tool_structured": ob.tool_structured,
        "current_user": (ob.query_user or "")[:200],
    }


def outbound_metrics_line(ob: Outbound) -> str:
    return (
        "[lan] text: query_user={} tool_inv={} history={} "
        "cache_hit={} cache_miss={} need_read={} images={} status={}".format(
            ob.query_user_chars,
            ob.tool_invocation_chars,
            ob.history_chars,
            len(ob.cache_hits or []),
            len(ob.cache_misses or []),
            "yes" if ob.need_read else "no",
            len(ob.dify_files or []),
            ob.image_upload_status,
        )
    )
