# -*- coding: utf-8 -*-
"""出站装配：body + plan + parsed → 查询 / inputs / 标记 / 附图。

query 标记约定：第一行恒为 [[cc_route:…]]；其余标记插在其后、正文之前。
"""
from __future__ import annotations

import re
import sys
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
    build_dify_query,
    build_history_and_current,
    extract_images_from_last_user,
    fold_history_current_to_query,
    fold_messages_to_query,
    materialize_inputs,
    sparse_inputs,
    summarize_images,
)
from plan import Plan, ROUTE_TAG_HAIKU, ROUTE_TAG_OPUS
from tools import (
    TOOL_CONTINUE_MARKER,
    append_tools_reminder_to_query,
    decorate_tool_continue_query,
    inject_tools_into_inputs,
)
from unicode_wire import (
    DIFY_PERSISTED_VARIABLE_SIZE_LIMIT,
    WIRE_CLOSE,
    WIRE_OPEN,
    build_unicode_wire_note,
    encode_unicode_wire_payload,
    ensure_persisted_input_sizes,
)

_ROUTE_HEAD_RE = re.compile(
    r"^({}|{})\s*\n?".format(re.escape(ROUTE_TAG_HAIKU), re.escape(ROUTE_TAG_OPUS)), re.I
)

STRUCT_MARKER = "[[cc_struct:on]]"
IMAGES_MARKER_FAILED = "[[cc_images:failed]]"
IMAGES_MARKER_FMT = "[[cc_images:{}]]"
INPUT_SHARDS_MARKER = "[[cc_input_shards:on]]"
SHARDABLE_INPUT_KEYS = ("Tool_invocation", "Current_Context", "History")
# Hard rejection remains 204800. Shards target a lower size because the
# CPython string header can differ slightly between the proxy and Dify runtime.
DIFY_PERSISTED_SHARD_TARGET = 190_000

_AGENT_STATE_ORDER = ("pending", "result_ready", "result_carry")
_AGENT_STATE_MAX_ITEMS = 3
_AGENT_DESCRIPTION_MAX_CHARS = 96
_AGENT_SUCCESS_STATUSES = frozenset(("completed", "complete"))


class DifyInputLengthError(ValueError):
    """某个 Dify Start 输入超过应用配置的字符上限。"""

    def __init__(
        self,
        key: str,
        length: int,
        limit: int,
        *,
        lengths: dict[str, int],
        limits: dict[str, int],
    ) -> None:
        self.key = key
        self.length = int(length)
        self.limit = int(limit)
        self.lengths = dict(lengths)
        self.limits = dict(limits)
        super().__init__(
            "Dify input {!r} has {} characters; configured max_length is {}".format(
                key, length, limit
            )
        )


def ensure_dify_input_lengths(
    inputs: dict[str, str],
    limits: dict[str, int] | None,
) -> dict[str, int]:
    """按 Dify /parameters 的 max_length 做字符数预检；未知边界不猜。"""
    lengths = {key: len(value or "") for key, value in inputs.items()}
    known_limits = {
        key: int(limit)
        for key, limit in (limits or {}).items()
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
    }
    violations = [
        (key, length, known_limits[key])
        for key, length in lengths.items()
        if key in known_limits and length > known_limits[key]
    ]
    if violations:
        key, length, limit = max(
            violations,
            key=lambda item: (item[1] - item[2], item[1]),
        )
        raise DifyInputLengthError(
            key,
            length,
            limit,
            lengths=lengths,
            limits=known_limits,
        )
    return lengths


def _published_shard_slots(
    base_key: str, limits: dict[str, int] | None
) -> tuple[str, ...]:
    """Return published ``<base>_1...N`` fields in numeric order."""
    pattern = re.compile(r"^{}_(\d+)$".format(re.escape(base_key)))
    indexed: list[tuple[int, str]] = []
    for key, limit in (limits or {}).items():
        match = pattern.fullmatch(key)
        if (
            match
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and limit > 0
        ):
            indexed.append((int(match.group(1)), key))
    return tuple(key for _index, key in sorted(indexed))


def _fits_shard(
    value: str,
    *,
    char_limit: int | None,
    size_limit: int = DIFY_PERSISTED_VARIABLE_SIZE_LIMIT,
) -> bool:
    return (
        (char_limit is None or len(value) <= char_limit)
        and sys.getsizeof(value) <= size_limit
    )


def _largest_fitting_prefix(value: str, *, char_limit: int | None) -> int:
    """Largest prefix that passes both the form and persistence boundaries."""
    high = len(value) if char_limit is None else min(len(value), char_limit)
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if _fits_shard(
            value[:middle],
            char_limit=char_limit,
            size_limit=DIFY_PERSISTED_SHARD_TARGET,
        ):
            low = middle
        else:
            high = middle - 1
    return low


def _wire_safe_cut(value: str, cut: int) -> int:
    """Avoid putting a reversible Unicode wire token across two fields."""
    if cut <= 0 or cut >= len(value):
        return cut
    if value[cut - 1 : cut + 1] == WIRE_OPEN + WIRE_OPEN:
        return cut - 1
    opener = value.rfind(WIRE_OPEN, 0, cut)
    closer = value.rfind(WIRE_CLOSE, 0, cut)
    if opener > closer and value.startswith("U+", opener + 1):
        closing = value.find(WIRE_CLOSE, opener + 1)
        if closing >= cut:
            return opener
    return cut


def _split_across_slots(
    value: str,
    slots: tuple[str, ...],
    limits: dict[str, int],
) -> list[str] | None:
    remaining = value
    chunks: list[str] = []
    for slot in slots:
        if not remaining:
            break
        cut = _largest_fitting_prefix(remaining, char_limit=limits.get(slot))
        if cut < len(remaining):
            cut = _wire_safe_cut(remaining, cut)
        if cut <= 0:
            return None
        chunk = remaining[:cut]
        if not _fits_shard(
            chunk,
            char_limit=limits.get(slot),
            size_limit=DIFY_PERSISTED_SHARD_TARGET,
        ):
            return None
        chunks.append(chunk)
        remaining = remaining[cut:]
    if remaining or "".join(chunks) != value:
        return None
    return chunks


def shard_oversized_inputs(
    inputs: dict[str, str],
    limits: dict[str, int] | None,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Use only published shard fields; never repurpose a semantic input.

    The canonical field carries ordinary values. If it crosses either boundary,
    it is cleared and its exact wire text moves to numbered fields. Every
    published but unused shard is still sent as ``""`` so an older conversation
    cannot leak a tail from the previous turn.
    """
    known_limits = {
        key: int(limit)
        for key, limit in (limits or {}).items()
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
    }
    result = dict(inputs)
    layouts: dict[str, tuple[str, ...]] = {}
    for base_key in SHARDABLE_INPUT_KEYS:
        slots = _published_shard_slots(base_key, known_limits)
        for slot in slots:
            result[slot] = ""

        value = result.get(base_key) or ""
        if _fits_shard(value, char_limit=known_limits.get(base_key)):
            continue
        if not slots:
            continue

        chunks = _split_across_slots(value, slots, known_limits)
        if chunks is None:
            continue
        result[base_key] = ""
        used = slots[: len(chunks)]
        for slot, chunk in zip(used, chunks):
            result[slot] = chunk
        layouts[base_key] = used
    return result, layouts


def format_input_shards_block(layouts: dict[str, tuple[str, ...]]) -> str:
    if not layouts:
        return ""
    lines = [
        INPUT_SHARDS_MARKER,
        "以下输入字段是同名逻辑内容的无损连续分片；须按所列变量顺序阅读，"
        "分片边界不表示语义边界：",
    ]
    for base_key in SHARDABLE_INPUT_KEYS:
        slots = layouts.get(base_key)
        if slots:
            lines.append("- {}: {}".format(base_key, " -> ".join(slots)))
    return "\n".join(lines)


def input_shard_capacity_hint(
    key: str, limits: dict[str, int] | None
) -> str:
    if key not in SHARDABLE_INPUT_KEYS:
        return ""
    slots = _published_shard_slots(key, limits)
    if slots:
        state = "当前已发布 {} 个分片槽，但总容量仍不足".format(len(slots))
    else:
        state = "本次 /parameters 参数快照中未发现该字段的编号分片槽"
    return (
        "{}。请在 Dify 开始节点增加 {}_1…N，在全部 LLM 提示中按编号引用，"
        "并重新发布应用；代理会自动发现、无损分片并清空未用槽。"
    ).format(state, key)


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
    current_context_chars: int
    history_chars: int
    is_tool_continue: bool
    tool_structured: bool = False
    unicode_wire_active: bool = False
    unicode_wire_non_bmp_count: int = 0
    unicode_wire_escaped_openers: int = 0
    unicode_wire_codepoints: tuple[int, ...] = ()
    input_char_lengths: dict[str, int] = field(default_factory=dict)
    input_char_limits: dict[str, int] = field(default_factory=dict)
    input_limits_source: str = "unavailable"
    persisted_input_sizes: dict[str, int] = field(default_factory=dict)
    input_shards: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 附图（attach_images_to_outbound 填充）
    dify_files: list = field(default_factory=list)
    image_failed: bool = False
    img_notes: list[str] = field(default_factory=list)
    image_mapping: list[dict[str, Any]] = field(default_factory=list)
    image_b64_bytes: int = 0
    image_upload_status: str = "none"  # ok | partial | failed | none | skipped
    has_images: bool = False
    image_count: int = 0


def prepare_text_outbound(
    *,
    body: dict[str, Any],
    plan: Plan,
    parsed: dict[str, Any],
    user_id: str,
    read_cache: ReadCache | None,
    input_char_limits: dict[str, int] | None = None,
    input_limits_source: str = "unavailable",
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
    if plan.query_mode == "history_current":
        history_for_query, _current, _notes = build_history_and_current(
            parsed.get("conversation_messages") or []
        )
    else:
        history_for_query = sparse.get("History") or ""
    dify_inputs = materialize_inputs(sparse, mode=plan.trim_mode)
    enable_tools = plan.enable_tools
    structured = plan.tool_structured
    if enable_tools:
        dify_inputs = inject_tools_into_inputs(
            dify_inputs, body.get("tools"), enabled=True, structured=structured
        )

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
    # 判「模型手上有没有正文」须看真正发出去的 inputs，不是物化前的 sparse：strip 档会丢掉
    # Current_Context / Tool_invocation，看 sparse 会认定「有正文」而咽下提示，于是那枪既
    # 没有正文、也不知道正文可以要。旁路枪例外——它恒 13 键空串，本就不该收 Read 指令。
    read_evidence = "\n\n".join(
        value
        for value in (
            dify_inputs.get("Tool_invocation") or "",
            dify_inputs.get("Current_Context") or "",
        )
        if value
    )
    if not plan.is_sidecar_summary and should_annotate_need_read(
        query_user or "", read_evidence
    ):
        need_read = True
        query = inject_marker_after_route(query, NEED_READ_NOTE)

    if plan.is_main_window and plan.kind == "chat":
        agent_block = format_agent_lifecycle_block(parsed.get("agent_lifecycle"))
        if agent_block:
            query = inject_marker_after_route(query, agent_block)

    # Start 表单与下一轮 conversation 恢复是两道不同边界：先对确实节省
    # CPython 内存的非 BMP 输入做可逆表示，再分别校验字符数与持久变量内存。
    wire = encode_unicode_wire_payload(query, dify_inputs)
    query = wire.query
    dify_inputs = wire.inputs
    if wire.active:
        query = inject_marker_after_route(query, build_unicode_wire_note(wire.codepoints))
    logical_char_lengths = {
        key: len(dify_inputs.get(key) or "") for key in SHARDABLE_INPUT_KEYS
    }
    dify_inputs, input_shards = shard_oversized_inputs(
        dify_inputs, input_char_limits
    )
    if input_shards:
        query = inject_marker_after_route(
            query, format_input_shards_block(input_shards)
        )
    input_char_lengths = ensure_dify_input_lengths(dify_inputs, input_char_limits)
    persisted_sizes = ensure_persisted_input_sizes(dify_inputs)

    return Outbound(
        query=query,
        dify_inputs=dify_inputs,
        query_user=query_user,
        sparse=sparse,
        need_read=need_read,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        query_user_chars=len(query_user or ""),
        tool_invocation_chars=logical_char_lengths["Tool_invocation"],
        current_context_chars=logical_char_lengths["Current_Context"],
        history_chars=logical_char_lengths["History"],
        is_tool_continue=TOOL_CONTINUE_MARKER in (query or ""),
        tool_structured=structured,
        unicode_wire_active=wire.active,
        unicode_wire_non_bmp_count=wire.non_bmp_count,
        unicode_wire_escaped_openers=wire.escaped_openers,
        unicode_wire_codepoints=wire.codepoints,
        input_char_lengths=input_char_lengths,
        input_char_limits=dict(input_char_limits or {}),
        input_limits_source=input_limits_source,
        persisted_input_sizes=persisted_sizes,
        input_shards=input_shards,
    )


def annotate_query_for_images(
    query: str,
    mapping: list[dict[str, Any]] | None = None,
) -> str:
    """声明原图到 Dify files 的映射，保留失败/去重图的原始索引。"""
    entries = [item for item in (mapping or []) if isinstance(item, dict)]
    entries = sorted(
        (
            item
            for item in entries
            if isinstance(item.get("source_index"), int)
            and item.get("source_index") >= 0
        ),
        key=lambda item: int(item["source_index"]),
    )
    if not entries:
        return query
    failed = any(item.get("status") not in {"ok", "dedup"} for item in entries)
    file_count = (
        max(
            (
                int(item["file_index"])
                for item in entries
                if item.get("status") in {"ok", "dedup"}
                and isinstance(item.get("file_index"), int)
            ),
            default=-1,
        )
        + 1
    )
    marker = IMAGES_MARKER_FAILED if failed else IMAGES_MARKER_FMT.format(file_count)
    order_lines = [
        (
            "  - Image #{} → Dify files[{}]（上传顺序第 {} 张）".format(
                int(item["source_index"]) + 1,
                int(item["file_index"]),
                int(item["file_index"]) + 1,
            )
            if item.get("status") in {"ok", "dedup"}
            and isinstance(item.get("file_index"), int)
            else "  - Image #{} 不可用（{}）".format(
                int(item["source_index"]) + 1,
                str(item.get("status") or "failed"),
            )
        )
        for item in entries
    ]
    transport_note = (
        "本轮部分附图未能通过 Dify files 上传，缺失图不可见。\n"
        if failed
        else "本轮用户附图已通过 Dify files 多模态上传（专用于图，非文档）。\n"
    )
    note = (
        "{}\n"
        "{}"
        "顺序对应（与消息中 image 块出现顺序一致）：\n"
        "{}\n"
        "正文中的 [image] / [Image #N] 为占位；请按上表序号识图。"
    ).format(marker, transport_note, "\n".join(order_lines))
    q = query or ""
    n = [0]
    by_source = {int(item["source_index"]): item for item in entries}

    def _num(_m: re.Match[str]) -> str:
        source_index = n[0]
        n[0] += 1
        item = by_source.get(source_index)
        if item is not None and item.get("status") not in {"ok", "dedup"}:
            return "[image #{} unavailable]".format(n[0])
        return "[image #{}]".format(n[0])

    if q:
        q = re.sub(r"\[image\]", _num, q, flags=re.I)
    return inject_marker_after_route(q, note)


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

        files, notes, mapping = await upload_images(
            images, base_url=base_url, api_key=api_key, user=user, client=client
        )
        outbound.dify_files = files or []
        outbound.img_notes = list(notes or [])
        outbound.image_mapping = list(mapping or [])
        if outbound.img_notes:
            print("[lan] images: {}".format("; ".join(outbound.img_notes)))

        if outbound.dify_files:
            outbound.query = annotate_query_for_images(
                outbound.query, outbound.image_mapping
            )
            outbound.image_failed = any(
                item.get("status") not in {"ok", "dedup"}
                for item in outbound.image_mapping
            )
            outbound.image_upload_status = (
                "partial" if outbound.image_failed else "ok"
            )
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
            if not outbound.image_mapping:
                outbound.image_mapping = [
                    {"source_index": i, "status": "failed", "reason": "no_files"}
                    for i in range(outbound.image_count)
                ]
            outbound.query = annotate_query_for_images(
                outbound.query, outbound.image_mapping
            )
            print("[lan] images: present but files empty → [[cc_images:failed]]")
    except Exception as e:
        print("[lan] image upload skipped: {!r}".format(e))
        outbound.dify_files = []
        if outbound.has_images:
            outbound.image_failed = True
            outbound.image_upload_status = "failed"
            outbound.image_mapping = [
                {"source_index": i, "status": "failed", "reason": "exception"}
                for i in range(outbound.image_count)
            ]
            outbound.img_notes = list(outbound.img_notes) + [
                "exception:{} {!r}".format(type(e).__name__, e)
            ]
            outbound.query = annotate_query_for_images(
                outbound.query, outbound.image_mapping
            )
        else:
            outbound.image_upload_status = "none"
    return outbound


def outbound_log_extra(ob: Outbound) -> dict[str, Any]:
    nonempty = sparse_inputs(ob.dify_inputs)
    return {
        "parsed_inputs": {
            k: {
                "chars": len(v or ""),
                "max_length": ob.input_char_limits.get(k),
                "head": (v or "")[:160],
            }
            for k, v in nonempty.items()
        },
        "sparse_input_keys": list(ob.sparse.keys()),
        "dify_input_keys_nonempty": list(nonempty.keys()),
        "dify_query": (ob.query or "")[:800],
        "history_chars": ob.history_chars,
        "query_user_chars": ob.query_user_chars,
        "tool_invocation_chars": ob.tool_invocation_chars,
        "current_context_chars": ob.current_context_chars,
        "input_char_lengths": dict(ob.input_char_lengths),
        "input_char_limits": dict(ob.input_char_limits),
        "input_limits_source": ob.input_limits_source,
        "persisted_input_sizes": dict(ob.persisted_input_sizes),
        "input_shards": {
            key: list(fields) for key, fields in ob.input_shards.items()
        },
        "unicode_wire_active": ob.unicode_wire_active,
        "unicode_wire_non_bmp_count": ob.unicode_wire_non_bmp_count,
        "unicode_wire_escaped_openers": ob.unicode_wire_escaped_openers,
        "unicode_wire_codepoints": [
            "U+{:06X}".format(codepoint) for codepoint in ob.unicode_wire_codepoints
        ],
        "read_cache_hits": (ob.cache_hits or [])[:8],
        "read_cache_misses": (ob.cache_misses or [])[:8],
        "need_read": ob.need_read,
        "tool_structured": ob.tool_structured,
        "current_user": (ob.query_user or "")[:200],
    }


def outbound_metrics_line(ob: Outbound) -> str:
    max_input = max(
        ob.input_char_lengths.items(), key=lambda item: item[1], default=("-", 0)
    )
    max_persisted = max(
        ob.persisted_input_sizes.items(), key=lambda item: item[1], default=("-", 0)
    )
    return (
        "text: query_user={} tool_inv={} current_ctx={} history={} "
        "max_var={}={} limit={} limit_src={} persist={}={} shards={} wire={}×{} "
        "cache_hit={} cache_miss={} need_read={} images={} status={}".format(
            ob.query_user_chars,
            ob.tool_invocation_chars,
            ob.current_context_chars,
            ob.history_chars,
            max_input[0],
            max_input[1],
            ob.input_char_limits.get(max_input[0], "-"),
            ob.input_limits_source,
            max_persisted[0],
            max_persisted[1],
            ",".join(
                "{}×{}".format(key, len(fields))
                for key, fields in ob.input_shards.items()
            )
            or "off",
            "on" if ob.unicode_wire_active else "off",
            ob.unicode_wire_non_bmp_count,
            len(ob.cache_hits or []),
            len(ob.cache_misses or []),
            "yes" if ob.need_read else "no",
            len(ob.dify_files or []),
            ob.image_upload_status,
        )
    )
