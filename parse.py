# -*- coding: utf-8 -*-
"""入站折叠：Claude Code / Anthropic Messages 请求 → Dify inputs / query / History。

CC 请求的真实结构：
- system：content blocks 列表；身份壳与 Harness 丢弃，Memory/Environment 等分段入对应字段
- messages[user] 的 <system-reminder>：claudeMd / CLAUDE / MEMORY / currentDate
- 本轮 user 之后的 system 工具轨迹（CC 的 @ 预载）→ Current_Context
- 更早的 messages[role=system] 工具轨迹 → Tool_invocation；其余 → System_Description
- tool_use / tool_result 内容块 → Tool_invocation（正文唯一载体）；History 只留引用
- 同一路径出现更新的完整 Read / Write 后，旧文件快照标为 superseded
- 同一路径、同一 offset/limit 的局部 Read 重复时，只保留最新成功视图
- 不写入：SYSTEM / Harness / Query（sys.query 另传）
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

INPUT_KEYS = (
    "claudeMd",
    "Memory",
    "Environment",
    "Language",
    "Output_Style",
    "Context_management",
    "CLAUDE",
    "MEMORY",
    "currentDate",
    "Tool_invocation",
    "Current_Context",
    "System_Description",
    "History",
)

# 折叠文本协议（cache.py 的消费正则按此格式匹配，变更须同步）
TOOL_RESULT_PREFIX = "[tool_result]"
TOOL_USE_LINE_FMT = "[tool_use] name={} id={}\n{}"
TOOL_RESULT_LINE_FMT = TOOL_RESULT_PREFIX + " tool_use_id={}\n{}"

# ── content 原语 ─────────────────────────────────────────────────────


def text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" or btype is None:
                t = block.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
            elif btype == "tool_result":
                parts.append(TOOL_RESULT_PREFIX + "\n" + text_from_content(block.get("content")))
            elif btype == "tool_use":
                parts.append("[tool_use: {}]".format(block.get("name") or "?"))
            elif btype == "image":
                parts.append("[image]")
            elif btype == "thinking":
                t = block.get("thinking") or block.get("text") or ""
                if t:
                    parts.append("[thinking]\n" + str(t))
        return "\n".join(p for p in parts if p)
    return str(content)


def system_to_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        return text_from_content(system).strip()
    return str(system).strip()


# ── system / reminder 解析 ───────────────────────────────────────────

_BANNER_ANY = re.compile(r"(?im)^\s*Contents of .+?\.(md|MD)[^\n]*:?\s*$")
_OVERRIDE_BOILERPLATE = re.compile(
    r"(?is)Codebase and user instructions are shown below\..*?"
    r"MUST follow them exactly as written\.\s*"
)
_REMINDER_BOILERPLATE = re.compile(
    r"(?is)As you answer the user's questions, you can use the following context:\s*"
)
_REMINDER_FOOTER = re.compile(
    r"(?is)\n?\s*IMPORTANT:\s*this context may or may not be relevant.*$"
)

_SYS_SECTION_MAP = {
    "harness": "_drop",
    "session-specific guidance": "_drop",
    "session specific guidance": "_drop",
    "memory": "Memory",
    "environment": "Environment",
    "language": "Language",
    "output style": "Output_Style",
    "context management": "Context_management",
}

_TOOL_TRACE_SYSTEM_RE = re.compile(
    r"(?is)^\s*(?:"
    r"Called the\s+\S+\s+tool\b"
    r"|Result of calling the\s+\S+\s+tool\b"
    r"|Called the\s+.+\s+tool with the following input\b"
    r")"
)


def _strip_banners(text: str) -> str:
    if not text:
        return ""
    lines = [line for line in text.splitlines() if not _BANNER_ANY.match(line)]
    return _OVERRIDE_BOILERPLATE.sub("", "\n".join(lines)).strip()


def _strip_context_tail(s: str) -> str:
    """剥掉误并入正文的 currentDate 段与 IMPORTANT 脚注尾。"""
    s = re.split(r"(?im)\n#+\s*currentDate\b", s, maxsplit=1)[0]
    s = re.split(r"(?im)\nIMPORTANT:\s*this context may", s, maxsplit=1)[0]
    return s.strip()


def _normalize_header_title(title: str) -> str:
    t = title.strip()
    if ":" in t:
        t = t.split(":", 1)[0].strip()
    return re.sub(r"\s+", " ", t).lower()


def _split_by_hash_headers(text: str) -> list[tuple[str, str]]:
    """[(标题或''前言, 正文), …]"""
    if not text or not text.strip():
        return []
    chunks: list[tuple[str, list[str]]] = [("", [])]
    header_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    for line in text.splitlines():
        m = header_re.match(line.strip())
        if m:
            chunks.append((m.group(2).strip(), []))
        else:
            chunks[-1][1].append(line)
    return [(t, "\n".join(b).strip()) for t, b in chunks if t or "\n".join(b).strip()]


def is_cc_agent_identity(text: str) -> bool:
    """Claude Code 身份壳，不得进 claudeMd。"""
    if not text:
        return False
    head = text.lstrip()[:80]
    if head.startswith("You are Claude Code"):
        return True
    return "Anthropic's official CLI for Claude" in text[:200]


def _parse_system_layers(system_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not system_text.strip():
        return out
    for title, body in _split_by_hash_headers(system_text):
        if not title:
            continue
        key = _SYS_SECTION_MAP.get(_normalize_header_title(title))
        if key is None or key == "_drop":
            continue
        if body.strip().lower() in ("blank", "(blank)", "none", "n/a"):
            out[key] = ""
        else:
            out[key] = body.strip()
    return out


def _parse_reminder_inner(blob: str) -> dict[str, str]:
    """system-reminder 内部：claudeMd（全局）/ CLAUDE（项目）/ MEMORY / currentDate。"""
    out: dict[str, str] = {}
    if not blob:
        return out
    blob = _REMINDER_BOILERPLATE.sub("", blob)
    blob = _REMINDER_FOOTER.sub("", blob)

    collected_claude: list[str] = []
    collected_project: list[str] = []
    memory_parts: list[str] = []

    for part in re.split(r"(?im)(?=^Contents of .+)", blob):
        part = part.strip()
        if not part:
            continue
        first = part.splitlines()[0] if part.splitlines() else ""
        if not first.lower().startswith("contents of"):
            continue
        body = "\n".join(part.splitlines()[1:]).strip()
        if re.search(r"MEMORY\.md", first, re.I):
            body = re.sub(r"(?im)^#+\s*MEMORY\s*$", "", body).strip()
            body = _strip_context_tail(body)
            memory_parts.append(body)
        elif re.search(r"AGENTS\.md|agents[/\\]rules", first, re.I):
            collected_project.append(body)
        elif re.search(r"CLAUDE\.md", first, re.I):
            if re.search(r"private global|user's private", first, re.I):
                collected_claude.append(body)
            elif re.search(r"project instructions|checked into the codebase", first, re.I):
                collected_project.append(body)
            else:
                collected_claude.append(body)
        elif re.search(r"project instructions|checked into the codebase", first, re.I):
            collected_project.append(body)

    if not collected_claude and not collected_project:
        for title, body in _split_by_hash_headers(blob):
            nt = _normalize_header_title(title) if title else ""
            if nt in ("claudemd", "claude md", "claude.md") and body:
                cleaned = _strip_banners(_OVERRIDE_BOILERPLATE.sub("", body).strip())
                if cleaned and not is_cc_agent_identity(cleaned):
                    collected_claude.append(cleaned)

    current_date = ""
    m_date = re.search(r"(?im)^#+\s*currentDate\s*\n\s*(Today's date is[^\n]+)", blob)
    if m_date:
        current_date = m_date.group(1).strip()
    else:
        m2 = re.search(r"(?im)(Today's date is[^\n]+)", blob)
        if m2:
            current_date = m2.group(1).strip()

    claude_md = _strip_context_tail(_strip_banners("\n\n".join(x for x in collected_claude if x)))
    project = _strip_context_tail(_strip_banners("\n\n".join(x for x in collected_project if x)))
    memory = _strip_context_tail(_strip_banners("\n\n".join(x for x in memory_parts if x)))

    if claude_md:
        out["claudeMd"] = claude_md
    if project:
        out["CLAUDE"] = project
    if memory:
        out["MEMORY"] = memory
    if current_date:
        current_date = re.split(r"(?i)IMPORTANT:\s*this context may", current_date)[0].strip()
        out["currentDate"] = re.sub(r"(?im)^#+\s*currentDate\s*\n?", "", current_date).strip()
    return out


# ── 工具轨迹 / 历史 ──────────────────────────────────────────────────


def _extract_local_command_blocks(text: str) -> str:
    """提取需随历史保留的本地命令输出，排除 ``/branch`` 会话指针。

    普通 local-command stdout 可能是后续推理证据，故继续放入 Tool_invocation；
    ``/branch`` 输出只描述 CC 新旧 session UUID，既不是工具结果，也不影响任务语义。
    若把它持久化，每次 fork 都会向长工具历史追加一份无用且不稳定的控制信息。
    """
    parts = []
    for pat in (
        r"(?is)<local-command-caveat>.*?</local-command-caveat>",
        r"(?is)<local-command-stdout>.*?</local-command-stdout>",
    ):
        for m in re.finditer(pat, text):
            block = m.group(0).strip()
            # ``/branch`` 打印的是供 CC TUI 返回新旧会话的 session 指针，不是任务证据；
            # 若放进 Tool_invocation，每次分支都会向长历史追加一组无语义且不稳定的 UUID。
            if re.search(
                r"(?is)<local-command-stdout>\s*Branched conversation\.\s*"
                r"You are now in the new branch\b",
                block,
            ):
                continue
            parts.append(block)
    return "\n\n".join(parts)


def _is_tool_trace_system_text(text: str) -> bool:
    if not text or not text.strip():
        return False
    if _TOOL_TRACE_SYSTEM_RE.search(text):
        return True
    head = text.lstrip()[:240]
    return (
        "Called the " in head
        and " tool" in head
        and ("following input" in head or "Result of calling" in text[:800])
    ) or head.startswith("Result of calling the")


_SYSTEM_TRACE_CALL_RE = re.compile(
    r"(?im)^Called the\s+(?P<name>[^\r\n]+?)\s+tool with the following input:\s*"
)


def _system_trace_calls(text: str) -> list[dict[str, Any]]:
    """从 CC 的自然语言 system 轨迹中恢复工具名、输入与该次结果段。"""
    source = text or ""
    matches = list(_SYSTEM_TRACE_CALL_RE.finditer(source))
    calls: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for index, match in enumerate(matches):
        tail = source[match.end() :].lstrip()
        try:
            raw_input, _end = decoder.raw_decode(tail)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_input, dict):
            continue
        section_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(source)
        )
        calls.append(
            {
                "name": match.group("name").strip(),
                "input": raw_input,
                "section": source[match.start() : section_end],
            }
        )
    return calls


def _full_read_paths_from_system_traces(parts: list[str]) -> list[str]:
    """找出本轮 @ 预载中的完整 Read；它们可取代历史中的同路径快照。"""
    paths: list[str] = []
    seen: set[str] = set()
    for text in parts:
        for call in _system_trace_calls(text):
            if str(call.get("name") or "").casefold() != "read":
                continue
            inp = call.get("input") or {}
            if inp.get("offset") is not None or inp.get("limit") is not None:
                continue
            section = str(call.get("section") or "")
            if "Result of calling the Read tool:" not in section:
                continue
            if "Wasted call" in section or "file unchanged since your last Read" in section:
                continue
            path = path_from_tool_input(inp)
            normalized = normalize_path(path)
            if path and normalized not in seen:
                seen.add(normalized)
                paths.append(path)
    return paths


def path_from_tool_input(raw: Any) -> str:
    """从工具 input 取路径：dict 按别名顺序取；str 先试 JSON，再退回正则。"""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        for key in ("file_path", "path", "filePath", "filename"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return path_from_tool_input(obj)
        except Exception:
            pass
        m = re.search(r'file_path["\']?\s*[:=]\s*["\']([^"\']+)["\']', s, re.I)
        if m:
            return m.group(1).strip()
    return ""


def normalize_path(path: str) -> str:
    """跨平台可比的路径键：去引号、剥 @ 前缀、斜杠归一、折叠重复分隔符、casefold。

    UNC 的前导 `\\\\` 必须保留——它是路径身份的一部分，压成单斜杠会让
    `\\\\server\\share\\a.py` 与 `\\server\\share\\a.py` 混为一谈。read_cache 的键
    与「最新完整文件状态」的键同出此处，否则同一文件会在两套账里各算一个（守则 15）。
    """
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return ""
    if p.startswith("@"):
        p = p[1:]
    p = p.replace("/", "\\")
    if p.startswith("\\\\"):
        p = "\\\\" + re.sub(r"\\+", r"\\", p[2:])
    else:
        p = re.sub(r"\\+", r"\\", p)
    return p.casefold()


def _trailing_user_index(messages: list) -> int:
    """忽略 system 后，只有真正收尾的 user 才是本轮 query。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            return i
        if role == "assistant":
            return -1
    return -1


def _current_tool_result_ids(messages: list) -> set[str]:
    """当前 user 的结果正文由 sys.query 承载，Tool_invocation 只留引用。"""
    i = _trailing_user_index(messages)
    if i < 0:
        return set()
    content = messages[i].get("content")
    if not isinstance(content, list):
        return set()
    return {
        str(b.get("tool_use_id") or b.get("id") or "")
        for b in content
        if isinstance(b, dict)
        and b.get("type") == "tool_result"
        and (b.get("tool_use_id") or b.get("id"))
    }


_AGENT_ASYNC_LAUNCH_TEXT = "Async agent launched successfully"
_AGENT_NOTIFICATION_HEADER = "[SYSTEM NOTIFICATION - NOT USER INPUT]"
_AGENT_NOTIFICATION_RE = re.compile(
    r"(?is)<task-notification\b[^>]*>(.*?)</task-notification>"
)
_AGENT_TERMINAL_STATUSES = frozenset(
    (
        "completed",
        "complete",
        "failed",
        "error",
        "cancelled",
        "canceled",
        "killed",
        "stopped",
        "terminated",
        "timed_out",
        "timeout",
    )
)


def _notification_tag(block: str, tag: str) -> str:
    match = re.search(
        r"(?is)<{0}\b[^>]*>\s*(.*?)\s*</{0}>".format(re.escape(tag)),
        block or "",
    )
    return match.group(1).strip() if match else ""


def _is_terminal_agent_status(status: str) -> bool:
    normalized = re.sub(r"[\s-]+", "_", (status or "").strip().lower())
    return normalized in _AGENT_TERMINAL_STATUSES


def _empty_agent_lifecycle() -> dict[str, list[dict[str, Any]]]:
    return {
        "pending": [],
        "result_ready": [],
        "result_carry": [],
    }


def _user_notification_blocks(
    message: dict[str, Any],
) -> list[tuple[int | None, str]]:
    """返回 CC 后台通知候选；``None`` 表示整条 string user 消息。

    CC 首次投递使用 ephemeral text block，下一枪会把同一伪 user 消息携带为
    plain string。string 形态只收完整的 ``<system-reminder>`` 包装，避免把
    普通用户正文中引用的一小段通知当成控制消息。
    """
    content = message.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        if (
            re.fullmatch(r"(?is)<system-reminder\b[^>]*>.*</system-reminder>", stripped)
            and _AGENT_NOTIFICATION_HEADER in stripped
            and "<task-notification" in stripped.lower()
        ):
            return [(None, content)]
        return []
    if not isinstance(content, list):
        return []

    candidates: list[tuple[int | None, str]] = []
    is_mixed_message = len(content) > 1
    for block_index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        cache_control = block.get("cache_control")
        is_ephemeral = (
            isinstance(cache_control, dict)
            and cache_control.get("type") == "ephemeral"
        )
        if not is_ephemeral and not is_mixed_message:
            continue
        text = block.get("text")
        if (
            not isinstance(text, str)
            or _AGENT_NOTIFICATION_HEADER not in text
            or "<task-notification" not in text.lower()
        ):
            continue
        candidates.append((block_index, text))
    return candidates


def _assistant_has_tool_use(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )


def _agent_lifecycle_context(
    messages: list,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[int, dict[int | None, str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """返回生命周期、须提升的通知 blocks、可信通知摘要与 Agent 调用。

    只有能和已有 Agent tool-use-id 配对的通知才被提升出 user 正文；这样
    Claude Code 的控制通知不会被当作用户话语，同时用户粘贴的仿造 XML 也不会
    获得系统输入权限。
    """
    lifecycle = _empty_agent_lifecycle()
    promoted_notifications: dict[int, dict[int | None, str]] = {}
    if not isinstance(messages, list) or not messages:
        return lifecycle, promoted_notifications, [], []

    agent_calls: dict[str, dict[str, Any]] = {}
    agent_call_summaries: list[dict[str, Any]] = []
    assistant_tool_indices: dict[str, int] = {}
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_use_id = str(block.get("id") or "").strip()
            if not tool_use_id:
                continue
            assistant_tool_indices[tool_use_id] = message_index
            if block.get("name") != "Agent":
                continue
            raw_input = block.get("input")
            agent_input = raw_input if isinstance(raw_input, dict) else {}
            description = agent_input.get("description") or ""
            if not isinstance(description, str):
                description = str(description)
            agent_call_summaries.append(
                {
                    "tool_use_id": tool_use_id,
                    "description": description.strip(),
                    "message_index": message_index,
                }
            )
            agent_calls[tool_use_id] = {
                "description": description.strip(),
                "call_index": message_index,
            }

    if not agent_calls:
        return lifecycle, promoted_notifications, [], agent_call_summaries

    async_launches: dict[str, int] = {}
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or block.get("id") or "").strip()
            call = agent_calls.get(tool_use_id)
            if (
                not call
                or bool(block.get("is_error"))
                or message_index <= int(call["call_index"])
            ):
                continue
            result_text = text_from_content(block.get("content"))
            if _AGENT_ASYNC_LAUNCH_TEXT.casefold() in result_text.casefold():
                async_launches[tool_use_id] = message_index

    notifications: dict[str, dict[str, Any]] = {}
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            system_text = text_from_content(message.get("content"))
            notification_candidates: list[tuple[int | None, str]] = (
                [(None, system_text)]
                if _AGENT_NOTIFICATION_HEADER in system_text
                else []
            )
        elif role == "user":
            notification_candidates = list(_user_notification_blocks(message))
        else:
            continue

        for block_index, notification_message in notification_candidates:
            matched_agent = False
            for notification_match in _AGENT_NOTIFICATION_RE.finditer(
                notification_message
            ):
                notification_text = notification_match.group(1)
                tool_use_id = _notification_tag(notification_text, "tool-use-id")
                call = agent_calls.get(tool_use_id)
                if not call or message_index <= int(call["call_index"]):
                    continue
                status = _notification_tag(notification_text, "status")
                # tool-use-id 与消息链中真实 Agent 调用配对，决定通知能否提升为
                # 系统输入；task-id 只用于和 SubagentStart/Stop 档案关联，不能
                # 单独授予身份、会话 CID 或控制消息权限。
                notifications[tool_use_id] = {
                    "tool_use_id": tool_use_id,
                    "agent_id": _notification_tag(notification_text, "task-id"),
                    "status": status,
                    "message_index": message_index,
                    "has_result": bool(_notification_tag(notification_text, "result")),
                    "summary": _notification_tag(notification_text, "summary"),
                    "result": _notification_tag(notification_text, "result"),
                }
                matched_agent = True
            if role == "user" and matched_agent:
                promoted_notifications.setdefault(message_index, {})[
                    block_index
                ] = notification_message

    current_result_ids = _current_tool_result_ids(messages)
    assistant_message_indices = [
        i
        for i, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]

    for tool_use_id, call in agent_calls.items():
        notification = notifications.get(tool_use_id)
        if not notification or not _is_terminal_agent_status(notification["status"]):
            if tool_use_id in async_launches:
                lifecycle["pending"].append(
                    {
                        "tool_use_id": tool_use_id,
                        "description": call["description"],
                        "status": "pending",
                        "message_index": async_launches[tool_use_id],
                    }
                )
            continue

        notification_index = int(notification["message_index"])
        carried_result = False
        for current_id in current_result_ids:
            source_index = assistant_tool_indices.get(current_id, -1)
            if source_index <= notification_index:
                continue
            closed_before_source = any(
                isinstance(messages[i], dict)
                and messages[i].get("role") == "assistant"
                and not _assistant_has_tool_use(messages[i])
                for i in range(notification_index + 1, source_index)
            )
            if not closed_before_source:
                carried_result = True
                break
        item = {
            "tool_use_id": tool_use_id,
            "agent_id": notification.get("agent_id") or "",
            "description": call["description"],
            "status": notification["status"],
            "message_index": notification_index,
            "has_result": bool(notification["has_result"]),
        }
        if carried_result:
            lifecycle["result_carry"].append(item)
        elif not any(i > notification_index for i in assistant_message_indices):
            lifecycle["result_ready"].append(item)

    trusted_notifications = sorted(
        (dict(item) for item in notifications.values()),
        key=lambda item: int(item.get("message_index") or 0),
    )
    return (
        lifecycle,
        promoted_notifications,
        trusted_notifications,
        agent_call_summaries,
    )


def extract_agent_lifecycle(messages: list) -> dict[str, list[dict[str, Any]]]:
    """兼容调用面：返回消息链中的 Agent 生命周期。"""
    lifecycle, _promoted, _trusted, _calls = _agent_lifecycle_context(messages)
    return lifecycle


def _tool_trace_index(messages: list) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    order = 0
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if not isinstance(block, dict):
                continue
            order += 1
            btype = block.get("type")
            if btype == "tool_use":
                tid = str(block.get("id") or "")
                if not tid:
                    continue
                raw_input = block.get("input")
                inp = raw_input if isinstance(raw_input, dict) else {}
                calls[tid] = {
                    "id": tid,
                    "name": str(block.get("name") or "?"),
                    "input": inp,
                    "path": path_from_tool_input(inp),
                    "order": order,
                }
            elif btype == "tool_result":
                tid = str(block.get("tool_use_id") or block.get("id") or "")
                if not tid:
                    continue
                results[tid] = {
                    "id": tid,
                    "content": text_from_content(block.get("content")),
                    "is_error": bool(block.get("is_error")),
                    "order": order,
                }
    return calls, results


def _latest_full_file_states(
    calls: dict[str, dict[str, Any]], results: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """完整 Read / 成功 Write 才能取代旧快照；Edit 只是基线之上的增量。"""
    latest: dict[str, dict[str, Any]] = {}
    for tid, result in sorted(results.items(), key=lambda item: item[1]["order"]):
        call = calls.get(tid) or {}
        name = str(call.get("name") or "").lower()
        path = str(call.get("path") or "")
        if result.get("is_error") or not path or name not in ("read", "write"):
            continue
        if name == "read":
            inp = call.get("input") or {}
            body = str(result.get("content") or "")
            if inp.get("offset") is not None or inp.get("limit") is not None:
                continue
            if _is_wasted_read_body(body):
                continue
        latest[normalize_path(path)] = {
            "id": tid,
            "name": call.get("name") or "?",
            "path": path,
            "order": result["order"],
        }
    return latest


def _is_wasted_read_body(body: str) -> bool:
    """A Wasted-call response is not a newer snapshot of the requested view."""
    head = (body or "")[:500]
    return "Wasted call" in head or "file unchanged since your last Read" in head


def _read_view_option(value: Any) -> Any:
    """Make a Read range option safe and stable as a dictionary key."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _read_view_key(call: dict[str, Any]) -> tuple[str, Any, Any] | None:
    """Return the exact identity of one Read path/range view."""
    path = normalize_path(str(call.get("path") or ""))
    if not path:
        return None
    inp = call.get("input") or {}
    return (
        path,
        _read_view_option(inp.get("offset")),
        _read_view_option(inp.get("limit")),
    )


def _latest_read_views(
    calls: dict[str, dict[str, Any]], results: dict[str, dict[str, Any]]
) -> dict[tuple[str, Any, Any], dict[str, Any]]:
    """Find the latest successful result for every exact path/range Read view."""
    latest: dict[tuple[str, Any, Any], dict[str, Any]] = {}
    for tid, result in sorted(results.items(), key=lambda item: item[1]["order"]):
        call = calls.get(tid) or {}
        if str(call.get("name") or "").lower() != "read":
            continue
        body = str(result.get("content") or "")
        if result.get("is_error") or not body or _is_wasted_read_body(body):
            continue
        key = _read_view_key(call)
        if key is None:
            continue
        inp = call.get("input") or {}
        latest[key] = {
            "id": tid,
            "name": call.get("name") or "Read",
            "path": call.get("path") or "",
            "offset": inp.get("offset"),
            "limit": inp.get("limit"),
            "order": result["order"],
        }
    return latest


def _superseded_note(latest: dict[str, Any]) -> str:
    label = latest.get("label") or "file state"
    return "(superseded {}; latest={} id={} path={})".format(
        label,
        latest.get("name") or "?", latest.get("id") or "?", latest.get("path") or "?"
    )


def _compact_superseded_input(
    call: dict[str, Any],
    result: dict[str, Any] | None,
    latest: dict[str, Any] | None,
) -> dict[str, Any]:
    inp = dict(call.get("input") or {})
    if not latest or not result or int(latest.get("order") or 0) <= int(result.get("order") or 0):
        return inp
    name = str(call.get("name") or "").lower()
    note = _superseded_note(latest)
    if name == "write" and "content" in inp:
        inp["content"] = note
    elif name == "edit":
        if "old_string" in inp:
            inp["old_string"] = note
        if "new_string" in inp:
            inp["new_string"] = note
    return inp


def _extract_tool_blocks_from_messages(
    messages: list,
    *,
    current_full_read_paths: list[str] | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """工具原文的唯一载体；当前结果在 query，旧快照按最新文件状态折叠。"""
    calls, results = _tool_trace_index(messages)
    latest_states = _latest_full_file_states(calls, results)
    latest_views = _latest_read_views(calls, results)
    external_order = max(
        [int(x.get("order") or 0) for x in calls.values()]
        + [int(x.get("order") or 0) for x in results.values()]
        + [0]
    )
    for index, path in enumerate(current_full_read_paths or [], start=1):
        normalized = normalize_path(path)
        if not normalized:
            continue
        latest_states[normalized] = {
            "id": "current-context:{}".format(index),
            "name": "Current_Context",
            "path": path,
            "order": external_order + index,
        }
    current_ids = _current_tool_result_ids(messages)
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "tool_use":
                    tid = str(b.get("id") or "")
                    call = calls.get(tid) or {
                        "name": b.get("name") or "?",
                        "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                        "path": path_from_tool_input(b.get("input")),
                    }
                    latest = latest_states.get(normalize_path(str(call.get("path") or "")))
                    raw_in = _compact_superseded_input(call, results.get(tid), latest)
                    try:
                        inp = json.dumps(raw_in, ensure_ascii=False) if raw_in is not None else ""
                    except Exception:
                        inp = str(raw_in)
                    parts.append(
                        TOOL_USE_LINE_FMT.format(
                            b.get("name") or "?", tid, inp
                        )
                    )
                elif btype == "tool_result":
                    tid = str(b.get("tool_use_id") or b.get("id") or "")
                    call = calls.get(tid) or {}
                    body = text_from_content(b.get("content"))
                    if tid in current_ids:
                        body = "(current result carried in sys.query)"
                    elif str(call.get("name") or "").lower() == "read":
                        result_order = int((results.get(tid) or {}).get("order") or 0)
                        candidates: list[dict[str, Any]] = []
                        latest = latest_states.get(
                            normalize_path(str(call.get("path") or ""))
                        )
                        if (
                            latest
                            and latest.get("id") != tid
                            and int(latest.get("order") or 0) > result_order
                        ):
                            file_state = dict(latest)
                            file_state["label"] = "file state"
                            candidates.append(file_state)
                        view = latest_views.get(_read_view_key(call))
                        if (
                            view
                            and view.get("id") != tid
                            and int(view.get("order") or 0) > result_order
                        ):
                            read_view = dict(view)
                            read_view["label"] = "read view"
                            candidates.append(read_view)
                        if candidates:
                            body = _superseded_note(
                                max(
                                    candidates,
                                    key=lambda item: int(item.get("order") or 0),
                                )
                            )
                    parts.append(
                        TOOL_RESULT_LINE_FMT.format(
                            tid,
                            body,
                        )
                    )
                elif btype == "text":
                    loc = _extract_local_command_blocks(b.get("text") or "")
                    if loc:
                        parts.append(loc)
        elif isinstance(content, str):
            loc = _extract_local_command_blocks(content)
            if loc:
                parts.append(loc)
    return "\n\n".join(parts).strip(), calls


_SYSTEM_REMINDER_RE = re.compile(r"(?is)<system-reminder>.*?</system-reminder>")

# CC 把本地命令序列化为三个 XML 元素。这里匹配完整元素而非只匹配标签，
# 以免命令名和参数漏进下一轮；是否真正删除仍由下方函数限定为 ``/branch``。
_COMMAND_SHELL_BLOCK_RE = re.compile(
    r"(?is)"
    r"<command-name\b[^>]*>.*?</command-name>"
    r"|<command-message\b[^>]*>.*?</command-message>"
    r"|<command-args\b[^>]*>.*?</command-args>"
)


def _strip_branch_command_shell(text: str) -> str:
    """只剥除 Claude Code ``/branch`` 包络，保留同轮真实用户文字。

    CC 会把 ``command-*`` 三块与用户紧随其后的输入合并为同一条 user message；
    事故请求因此同时含 ``/branch`` 控制壳和「继续」。模型只应收到后者。
    这里同时校验 command-name 与 command-message，未知/自定义 slash command
    一律原样放行，避免代理替 Claude Code 猜测其是否属于模型任务。
    """
    if not text:
        return ""
    name = re.search(
        r"(?is)<command-name\b[^>]*>\s*(.*?)\s*</command-name>", text
    )
    message = re.search(
        r"(?is)<command-message\b[^>]*>\s*(.*?)\s*</command-message>", text
    )
    # 同时核对两个字段和精确的内建命令；自定义 slash command 可能就是模型任务，
    # 因而必须 fail-open、原样保留。
    if not name or not message:
        return text
    if name.group(1).lstrip("/").casefold() != "branch":
        return text
    if message.group(1).lstrip("/").casefold() != "branch":
        return text
    return _COMMAND_SHELL_BLOCK_RE.sub("", text)


def strip_reminders(text: str) -> str:
    """剥除 <system-reminder> 块（plan 的指纹判定与此处折叠共用）。"""
    return _SYSTEM_REMINDER_RE.sub("", text or "")


def _user_query_after_reminders(user_text: str) -> str:
    if not user_text:
        return ""
    # 可信 Agent 通知已在 parse_payload 中从 conversation_messages 移走；仍留在
    # 用户正文里的同形 XML 没有匹配真实 Agent call，必须保留为普通用户文字。
    def remove_context_reminder(match: re.Match[str]) -> str:
        block = match.group(0)
        if (
            _AGENT_NOTIFICATION_HEADER in block
            and "<task-notification" in block.lower()
        ):
            return block
        return ""

    without = _SYSTEM_REMINDER_RE.sub(remove_context_reminder, user_text)
    without = re.sub(r"(?is)<local-command-caveat>.*?</local-command-caveat>", "", without)
    without = re.sub(r"(?is)<local-command-stdout>.*?</local-command-stdout>", "", without)
    # branch 创建新的 CC session，XML 包络属于控制信息；与它同轮出现的剩余文字
    # （通常是「继续」）才是真正的用户请求。
    return _strip_branch_command_shell(without).strip()


def _assistant_for_history(content: Any) -> str:
    """History 里的 assistant：可见正文 + 工具名；thinking 整段不进。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content).strip()
    parts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
        elif btype == "tool_use":
            tools.append(str(block.get("name") or "?"))
    out = "\n".join(parts).strip()
    if tools:
        out = (out + "\n" if out else "") + "[called tools: {}]".format(", ".join(tools))
    return out.strip()


def _tool_result_history_ref(
    block: dict[str, Any], calls: dict[str, dict[str, Any]] | None = None
) -> str:
    tid = str(block.get("tool_use_id") or block.get("id") or "")
    call = (calls or {}).get(tid) or {}
    detail = []
    if call.get("name"):
        detail.append("name={}".format(call["name"]))
    if call.get("path"):
        detail.append("path={}".format(call["path"]))
    suffix = "; " + " ".join(detail) if detail else ""
    return TOOL_RESULT_LINE_FMT.format(
        tid, "(body carried in Tool_invocation{})".format(suffix)
    )


def _user_for_history_or_current(
    content: Any,
    *,
    compact_tool_results: bool = False,
    calls: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, bool]:
    """返回 (text, is_tool_result_only)；tool_result 轮以 [tool_result] 起头。"""
    if content is None:
        return "", False
    if isinstance(content, str):
        q = _user_query_after_reminders(content)
        if not q and content and not re.search(r"(?is)<system-reminder>", content):
            q = content.strip()
        return q, False
    if not isinstance(content, list):
        return str(content).strip(), False

    tool_parts: list[str] = []
    text_parts: list[str] = []
    has_image = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            if compact_tool_results:
                tool_parts.append(_tool_result_history_ref(block, calls))
            else:
                tool_parts.append(
                    TOOL_RESULT_LINE_FMT.format(
                        block.get("tool_use_id") or block.get("id") or "",
                        text_from_content(block.get("content")),
                    )
                )
        elif btype == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                text_parts.append(t)
        elif btype == "image":
            has_image = True

    if tool_parts and not text_parts:
        return "\n\n".join(tool_parts).strip(), True

    blob = "\n".join(text_parts)
    q = _user_query_after_reminders(blob)
    if not q and blob and not re.search(r"(?is)<system-reminder>", blob):
        q = blob.strip()
    if has_image:
        q = (q + "\n[image]") if q else "[image]"
    if tool_parts:
        q = (q + "\n\n" if q else "") + "\n\n".join(tool_parts)
    return (q or "").strip(), bool(tool_parts) and not text_parts


def build_history_and_current(
    messages: list,
    *,
    compact_tool_results: bool = False,
    calls: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, list[str]]:
    """本轮之前的 user/assistant → History；最后一条 user → 本轮 query。"""
    notes: list[str] = []
    turns: list[tuple[str, str]] = []

    current_user_idx = _trailing_user_index(messages)

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant":
            text = _assistant_for_history(m.get("content"))
            if text:
                turns.append(("assistant", text))
            continue
        if role == "user":
            q, is_tr = _user_for_history_or_current(
                m.get("content"),
                compact_tool_results=compact_tool_results and i != current_user_idx,
                calls=calls,
            )
            if q:
                turns.append(("user", q))
                if is_tr:
                    notes.append("user_turn_tool_result")

    current = ""
    if turns and turns[-1][0] == "user":
        current = turns[-1][1]
        prior = turns[:-1]
    else:
        prior = turns
        notes.append("no_trailing_user_turn")

    if current.startswith(TOOL_RESULT_PREFIX):
        notes.append("current_is_tool_result_continue")

    if not prior:
        return "", current, notes
    history = "\n\n".join("{}:\n{}".format(r, t) for r, t in prior)
    notes.append("history_turns={}".format(len(prior)))
    return history, current, notes


# ── 主入口 ───────────────────────────────────────────────────────────


def sparse_inputs(inputs: dict[str, str] | None) -> dict[str, str]:
    """只保留非空字段（日志 / 诊断用）。"""
    out: dict[str, str] = {}
    for k, v in (inputs or {}).items():
        if not isinstance(v, str):
            v = str(v or "")
        if v.strip():
            out[k] = v
    return out


def parse_payload(body: dict[str, Any]) -> dict[str, Any]:
    """稀疏解析：只产出有内容的键；补空/清空由 materialize_inputs 决定。"""
    inputs: dict[str, str] = {}
    notes: list[str] = []

    system_text = system_to_text(body.get("system"))
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    (
        agent_lifecycle,
        promoted_agent_notifications,
        trusted_agent_notifications,
        agent_calls,
    ) = _agent_lifecycle_context(messages)
    conversation_messages: list[Any] = []
    for message_index, message in enumerate(messages):
        notification_blocks = promoted_agent_notifications.get(message_index)
        if not notification_blocks or not isinstance(message, dict):
            conversation_messages.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str) and None in notification_blocks:
            continue
        if not isinstance(content, list):
            conversation_messages.append(message)
            continue
        remaining_blocks = [
            block
            for block_index, block in enumerate(content)
            if block_index not in notification_blocks
        ]
        if remaining_blocks:
            filtered_message = dict(message)
            filtered_message["content"] = remaining_blocks
            conversation_messages.append(filtered_message)

    for k, v in _parse_system_layers(system_text).items():
        if v:
            inputs[k] = v

    # system 消息：本轮 user 后的 @ 预载 → Current_Context；更早的工具轨迹留在历史状态。
    system_msg_parts: list[str] = []
    tool_trace_from_system: list[str] = []
    current_context_parts: list[str] = []
    trailing_user_index = _trailing_user_index(messages)
    for message_index, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        notification_blocks = promoted_agent_notifications.get(message_index)
        if notification_blocks:
            message_texts = [
                notification_blocks[block_index]
                for block_index in sorted(notification_blocks)
            ]
        elif m.get("role") == "system":
            message_texts = [text_from_content(m.get("content"))]
        else:
            continue
        for text in message_texts:
            if not text or is_cc_agent_identity(text):
                continue
            if _is_tool_trace_system_text(text):
                if (
                    m.get("role") == "system"
                    and trailing_user_index >= 0
                    and message_index > trailing_user_index
                ):
                    current_context_parts.append(text.strip())
                else:
                    tool_trace_from_system.append(text.strip())
            else:
                system_msg_parts.append(text)
    if system_msg_parts:
        # 同一通知可能在 CC 序列化转换处以等值载体重现；系统说明只保留一份。
        inputs["System_Description"] = "\n\n".join(
            dict.fromkeys(part.strip() for part in system_msg_parts if part.strip())
        ).strip()
    if current_context_parts:
        inputs["Current_Context"] = "\n\n".join(current_context_parts).strip()

    # reminder 字段（优先含 system-reminder 的 user）
    reminder_src: list[str] = []
    for m in conversation_messages:
        if isinstance(m, dict) and m.get("role") == "user":
            t = text_from_content(m.get("content"))
            if re.search(r"(?is)<system-reminder>", t):
                reminder_src.append(t)
    if not reminder_src:
        for m in conversation_messages:
            if isinstance(m, dict) and m.get("role") == "user":
                reminder_src.append(text_from_content(m.get("content")))
    for block in re.findall(
        r"<system-reminder>\s*(.*?)\s*</system-reminder>",
        "\n\n".join(reminder_src),
        flags=re.I | re.S,
    ):
        for k, v in _parse_reminder_inner(block).items():
            if v:
                inputs[k] = v

    # 工具轨迹
    tool_parts: list[str] = []
    current_full_read_paths = _full_read_paths_from_system_traces(current_context_parts)
    structured, tool_calls = _extract_tool_blocks_from_messages(
        conversation_messages,
        current_full_read_paths=current_full_read_paths,
    )
    if structured:
        tool_parts.append(structured)
    tool_parts.extend(tool_trace_from_system)
    if tool_parts:
        inputs["Tool_invocation"] = "\n\n".join(tool_parts).strip()

    history, current, hnotes = build_history_and_current(
        conversation_messages, compact_tool_results=True, calls=tool_calls
    )
    notes.extend(hnotes)
    notes.append("current_context_parts={}".format(len(current_context_parts)))
    notes.append("current_context_full_reads={}".format(len(current_full_read_paths)))
    notes.append(
        "agent_promoted_notifications={}".format(
            sum(len(blocks) for blocks in promoted_agent_notifications.values())
        )
    )
    notes.extend(
        "agent_{}={}".format(state, len(agent_lifecycle[state]))
        for state in ("pending", "result_ready", "result_carry")
    )
    if history:
        inputs["History"] = history
    query_user = current if current else " "
    if not current:
        notes.append("query_empty_placeholder")

    cleaned = sparse_inputs(inputs)
    if is_cc_agent_identity(cleaned.get("claudeMd") or ""):
        # 主路径（_parse_reminder_inner 的 "Contents of" 分支）不查身份壳，此处补齐。
        cleaned.pop("claudeMd", None)

    notes.append("sparse_keys={}".format(len(cleaned)))
    return {
        "inputs": cleaned,
        "query_user": query_user,
        "notes": notes,
        "history_chars": len(history or ""),
        # 旁路枪要的「未去正文的完整历史」在这里只交出原料，由消费端按需折叠：
        # 主枪用不到它，不该为一份自己不发送的几百 KB 字符串付代价。
        "conversation_messages": conversation_messages,
        "current_user": current,
        "agent_lifecycle": agent_lifecycle,
        "agent_notifications": trusted_agent_notifications,
        "agent_calls": agent_calls,
    }


def materialize_inputs(
    inputs: dict[str, str] | None,
    *,
    mode: str | None = None,
) -> dict[str, str]:
    """稀疏 → 送 Dify 的 inputs。

    - empty（title/recap/compact）：全键 ""，清会话变量
    - strip（其它 haiku）：丢全部 INPUT_KEYS；工具协议稍后可写回 System_Description
    - None（主对话 / opus 子代理）：全键快照，空串用于覆盖上轮会话变量
    """
    src = {k: (v if isinstance(v, str) else str(v or "")) for k, v in (inputs or {}).items()}
    m = (mode or "").lower() if mode else None
    if m == "empty":
        return {k: "" for k in INPUT_KEYS}
    if m == "strip":
        return {k: v for k, v in src.items() if k not in INPUT_KEYS and v.strip()}
    return {k: src.get(k) or "" for k in INPUT_KEYS}


# ── query 折叠 ───────────────────────────────────────────────────────


def fold_messages_to_query(body: dict[str, Any]) -> str:
    """title 枪：system + 历史 + 本轮整包折叠为单条 query。"""
    system_text = system_to_text(body.get("system"))
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    history = list(messages)
    current_user = ""
    if history:
        last = history[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            current_user = text_from_content(last.get("content")).strip()
            history = history[:-1]

    conv_lines: list[str] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        text = text_from_content(msg.get("content")).strip()
        if text:
            conv_lines.append("{}:\n{}".format(msg.get("role") or "unknown", text))

    parts: list[str] = []
    if system_text:
        parts.append("[system]\n{}".format(system_text))
    if conv_lines:
        parts.append("[conversation]\n{}".format("\n\n".join(conv_lines)))
    if current_user:
        parts.append("[current user]\n{}".format(current_user))
    elif not conv_lines and not system_text:
        parts.append("[current user]\n")
    return "\n\n---\n\n".join(parts)


def fold_history_current_to_query(history: str, current: str) -> str:
    """recap/compact 等旁路：历史 + 本轮；不含 top-level system（防 Harness 灌入）。"""
    parts: list[str] = []
    if (history or "").strip():
        parts.append("[conversation]\n{}".format(history.strip()))
    if (current or "").strip():
        parts.append("[current user]\n{}".format(current.strip()))
    return "\n\n---\n\n".join(parts)


def build_dify_query(route_tag: str, query_user: str) -> str:
    q = (query_user or "").strip()
    tag = (route_tag or "").strip()
    if tag:
        return "{}\n{}".format(tag, q) if q else tag
    return q


# ── 图抽取（上传在 dify.py） ─────────────────────────────────────────


def _image_dict_from_source(src: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(src, dict):
        return None
    st = (src.get("type") or "").lower()
    media = src.get("media_type") or src.get("mediaType") or "image/png"
    if st == "base64":
        data = src.get("data") or ""
        if not isinstance(data, str) or not data.strip():
            return None
        return {"kind": "base64", "media_type": str(media), "data": data.strip()}
    if st in ("url", "external"):
        url = src.get("url") or ""
        if url:
            return {"kind": "url", "media_type": str(media), "url": str(url)}
    return None


def extract_images_from_content(
    content: Any,
    *,
    include_tool_result: bool = True,
) -> list[dict[str, Any]]:
    """image 块；include_tool_result 时递归 tool_result 内嵌图（续写枪识图）。"""
    out: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "image":
            img = _image_dict_from_source(block.get("source") or {})
            if img:
                out.append(img)
        elif include_tool_result and btype == "tool_result":
            out.extend(
                extract_images_from_content(block.get("content"), include_tool_result=True)
            )
    return out


def extract_images_from_last_user(
    body: dict[str, Any],
    *,
    include_tool_result: bool = True,
) -> list[dict[str, Any]]:
    """仅最后一条 user 的图（本轮附图 + tool_result 内嵌图）。"""
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return []
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return extract_images_from_content(
                m.get("content"), include_tool_result=include_tool_result
            )
    return []


def image_b64_byte_len(img: dict[str, Any]) -> int:
    if not isinstance(img, dict) or img.get("kind") != "base64":
        return 0
    data = str(img.get("data") or "")
    if not data:
        return 0
    try:
        raw = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
        return len(base64.b64decode(raw, validate=False)) if raw else 0
    except Exception:
        return 0


def summarize_images(images: list[dict[str, Any]]) -> dict[str, Any]:
    b64_bytes = sum(image_b64_byte_len(im) for im in images or [])
    n = len(images or [])
    return {"image_count": n, "b64_bytes": b64_bytes, "has_images": n > 0}
