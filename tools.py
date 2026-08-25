# -*- coding: utf-8 -*-
"""工具通道：Dify 不透传 Anthropic tools[]，代理自建三条出站通道。

入站：tools[] 压成目录 + 协议文本，注入 System_Description（+ query 尾注）。
出站解析（按优先级）：
1. 结构化外壳 —— 整包 {reply, tool_calls[]} 或 [[cc_tools_json]] 围栏（岚结构化分支）
2. 原文块 —— [[tool_use]] JSON 头 + [[raw:键]] 零转义长文本（大文件 Write 的根治通道）
3. JSON 抢救 —— 内联 JSON 严格解析失败后一趟定界修复（兜底旧形态）

执行始终在 Claude Code 本机；解析失败的块保留为可见正文，不产半调用。
"""
from __future__ import annotations

import json
import ntpath
import re
import uuid
from dataclasses import dataclass
from typing import Any

from parse import TOOL_RESULT_PREFIX

# 工具块开闭标记（大小写不敏感）
TOOL_MARKERS: tuple[tuple[str, str], ...] = (
    ("[[tool_use]]", "[[/tool_use]]"),
    ("<tool_call>", "</tool_call>"),
    ("<tool_use>", "</tool_use>"),
)
# terminal-tool：只在真实 Write/Edit 成功后由代理释放，不随工具枪展示。
AFTER_SUCCESS_OPEN = "[[after_success]]"
AFTER_SUCCESS_CLOSE = "[[/after_success]]"
TERMINAL_TOOL_NAMES = frozenset(("Write", "Edit"))

TOOLS_JSON_OPEN = "[[cc_tools_json]]"
TOOLS_JSON_CLOSE = "[[/cc_tools_json]]"
TOOLS_ON_MARKER = "[[cc_tools:on]]"
RAW_OPEN_PREFIX = "[[raw:"
RAW_CLOSE_PREFIX = "[[/raw:"
CODE_FENCE = "```"

# 协议标记的单一权威表。下面两个派生视图与 _PROTOCOL_RESIDUE_RE 各有不同覆盖面，
# 这是有理由的——截停要早（含代码围栏，因结构化围栏常被 ``` 包裹）、残留检测要宽
# （正则，能吃任意 raw 键名与开闭两向）、草案定位要准（子串比较，且排除
# after_success 自身）。原先三处各自枚举，同步全靠人记；现在改由此表派生。
# 表与正则的同步由 test_tools.py 的 sync 用例锁住。
_MARKER_PAIRS: tuple[tuple[str, str], ...] = TOOL_MARKERS + (
    (AFTER_SUCCESS_OPEN, AFTER_SUCCESS_CLOSE),
    (TOOLS_JSON_OPEN, TOOLS_JSON_CLOSE),
)

# 视图一：投机流式截停（半截前缀由 answer 的 holdback 兜住）
STREAM_CUT_MARKERS = tuple(open_m for open_m, _ in _MARKER_PAIRS) + (CODE_FENCE,)

# 视图二：终结草案开标记之后不得再出现的协议标记
_POST_DRAFT_FORBIDDEN = tuple(
    marker.lower() for pair in TOOL_MARKERS for marker in pair
) + (
    RAW_OPEN_PREFIX,
    RAW_CLOSE_PREFIX,
    TOOLS_JSON_OPEN.lower(),
    TOOLS_JSON_CLOSE.lower(),
)

TOOL_CONTINUE_MARKER = "[[cc_tool_continue]]"


@dataclass(frozen=True)
class AfterSuccessParse:
    """terminal 隐藏草案的严格解析结果。"""

    visible: str
    success: str = ""
    found: bool = False
    valid: bool = False
    reason: str = "none"

_ASK_HEADER_MAX = 12

# AskUserQuestion：目录提示与归一共用，防双写漂移
ASK_USER_QUESTION_SHAPE = {
    "required": ("question", "header", "options", "multiSelect"),
    "option_keys": ("label", "description", "preview"),
    "header_max": _ASK_HEADER_MAX,
    "catalog_hint": (
        "questions[].required: question,header,options,multiSelect; "
        "options[]: label,description only (forbid id/text/value on question; "
        "forbid value on option); header max {} chars".format(_ASK_HEADER_MAX)
    ),
}

# ── 协议文本 ─────────────────────────────────────────────────────────

TOOL_PROTOCOL_TEXT = """『工具协议 · Claude Code 本机执行 · 强制』
你可以通过工具访问本机文件系统与环境。Claude Code 只识别下方标记块；自然语言「我去读一下」不会触发工具、也不会弹出权限确认。

**硬性规则**
1. 凡要读文件、写文件、列目录、跑命令、搜索代码——必须输出 [[tool_use]] 块。工具执行前的自然语言只能简短说明下一步；严禁提前声称“已完成 / 已写入 / 已修改 / 验证通过”或交付最终结论。最稳妥的写法是从 [[tool_use]] 开始，本枪只输出工具块。
2. 用户给出绝对路径或 @ 路径时：优先 Read（已知文件）/ Glob·Grep（查找）；不要派 Agent 代读单文件。
3. 不要编造未读到的文件内容；没 Read 到就先调 Read。
4. 基本格式：块内一段合法 JSON（input 与 arguments 二选一，推荐 input）：

[[tool_use]]
{"name":"Read","input":{"file_path":"C:\\\\Users\\\\example\\\\project\\\\README.md"}}
[[/tool_use]]

5. **多行或含引号的长文本参数**（Write.content、Edit.old_string/new_string、Bash 多行脚本等）：不要写进 JSON——JSON 里省略该键，紧跟一个 [[raw:键名]] 原文块，内容零转义照抄（引号、反斜杠、换行、```围栏 都按原样写）：

[[tool_use]]
{"name":"Write","input":{"file_path":"C:\\\\Users\\\\example\\\\notes\\\\笔记.md"}}
[[raw:content]]
# 标题
正文任意多行："引号"、反斜杠 \\、代码围栏均无需转义。
[[/raw:content]]
[[/tool_use]]

Edit 同理用 [[raw:old_string]] 与 [[raw:new_string]] 两个原文块。唯一限制：原文中不得出现 [[/raw:键名]] 这一行本身（确有需要时退回 JSON 转义写法）。
6. name 必须是下列「可用工具」之一。先判断依赖：同一认知阶段中，路径、参数、必要性均已确定，且不存在数据、控制流、共享可变目标或副作用顺序依赖的调用，必须在本枪一次列全（多个块并行）；后项依赖前项结果或副作用时才拆枪。同一文件的 Write/Edit 应合并或串行，不得为了叙述进度串行。
7. 系统下一轮用 tool_result 返回后，你再依据真实结果给用户一次结论；结论用自然语言，不要再包 [[tool_use]]。
8. 若本枪全部调用仅为 Write/Edit、成功后无需解释工具输出且原任务即可结束，可在所有工具块后附**恰好一段、完整闭合且位于响应末尾**的 [[after_success]]…[[/after_success]]。它是简短的成功答复草案，代理只在全部工具明确成功后本地释放；失败、拒绝或未知结果仍交模型处理。Read/Glob/Grep/Bash/测试或仍需分析时严禁使用。
9. AskUserQuestion 的 questions[] **必须**用 question + header + options + multiSelect（禁止用 text 代替 question；option 只用 label/description，不要 id/value）。

『工具选用（省额度）』
- 读已知路径 → 只用 Read，不要 Agent。
- 列目录 / 搜文件名 → Glob，或通过 Bash 运行 PowerShell 命令。
- 宽泛探索若必须用 Agent → subagent_type=Explore（只读）；能本线程解决则不派生。
- 派 Agent 时：机械扫描可写 [[cc_route:haiku]]；深度分析 [[cc_route:opus]]。

示例 AskUserQuestion（字段名勿改）：
[[tool_use]]
{"name":"AskUserQuestion","input":{"questions":[{"question":"如何处理现有 CLAUDE.md？","header":"CLAUDE.md","multiSelect":false,"options":[{"label":"审视改进","description":"保留并针对性修改"},{"label":"保持不动","description":"跳过，继续其他设置"},{"label":"从头写","description":"丢弃现有内容重写"}]}]}}
[[/tool_use]]
"""

# 两份 query 尾注共享的依赖判据。四类依赖须与 TOOL_PROTOCOL_TEXT 第 6 条逐字一致：
# 「共享可变目标」正是「同一文件 Edit A + Edit A 须合并」的依据（守则 16），
# 而按约束居近，尾注才是最可能被遵守的那一份，不得比远处那份少一类。
_BATCH_RULE_NEAR = (
    "同一认知阶段里无数据、控制流、共享可变目标或副作用顺序依赖的调用一次列全；"
    "同一文件的变更须合并或串行。"
)

TOOL_QUERY_REMINDER = (
    "\n\n" + TOOLS_ON_MARKER + " 需要本机读盘/命令时，必须输出 [[tool_use]]...[[/tool_use]]；"
    "仅文字描述不会执行工具、也不会弹出权限确认。"
    "若本枪调用工具，工具前只能简述下一步，严禁提前声称完成或给最终结论；tool_result 返回后再确认。"
    + _BATCH_RULE_NEAR
    + "若全部调用仅为终结性的 Write/Edit，可在末尾附一段完整 [[after_success]] 成功答复草案；其他工具禁用。"
    "长文本参数（如 Write.content）用 [[raw:键名]]…[[/raw:键名]] 原文块，勿在 JSON 里转义长内容。"
    "AskUserQuestion 须 question+header+options+multiSelect（勿用 text 代替 question）。"
)

TOOL_PROTOCOL_TEXT_STRUCT = """『工具协议 · 结构化出口 · Claude Code 本机执行 · 强制』
本枪走结构化出口：你的**整个回答**须是一个 JSON 对象（系统已用 schema 约束输出）：
{"reply": "给用户的正文（Markdown；无话可说给空串）", "tool_calls": [{"name": "工具名", "input": {…该工具参数…}}]}

规则：
1. 凡要读文件、写文件、列目录、跑命令——把调用放进 tool_calls 数组；只要 tool_calls 非空，reply 必须为空字符串，严禁在工具执行前声称完成。tool_result 返回后的结论枪才令 tool_calls 为空、在 reply 给一次结论。禁止在 reply 里写 [[tool_use]] 标记或参数 JSON。
2. name 必须来自下方「可用工具」；input 键名严格按目录 required/props（AskUserQuestion 用 question+header+options+multiSelect）。
3. 先判断依赖；同一认知阶段中路径、参数、必要性均已确定，且不存在数据、控制流、共享可变目标或副作用顺序依赖的调用一次列全（数组多项，系统并行执行）。后项依赖前项结果或副作用时才拆枪；同一文件的 Write/Edit 应合并或串行。
4. 长文本参数（如 Write.content）作为 JSON 字符串值完整写入 input，保证合法转义；格式由结构化通道兜底，你负责内容完整。
5. reply 与 tool_calls 必须一致：说要写文件，tool_calls 里就必须有对应调用。
"""

TOOL_QUERY_REMINDER_STRUCT = (
    "\n\n" + TOOLS_ON_MARKER + " 本枪为结构化出口：整个回答 = {\"reply\", \"tool_calls\"} 单个 JSON 对象；"
    "需要本机操作时把调用放进 tool_calls，且 reply 必须为空；tool_result 返回后再给结论。"
    + _BATCH_RULE_NEAR
    + "勿在 reply 里写 [[tool_use]] 标记。"
)

# ── 目录 ─────────────────────────────────────────────────────────────

_TOOL_CATALOG_PRIORITY = (
    "Read", "Glob", "Grep", "Bash", "PowerShell",
    "Edit", "Write", "AskUserQuestion", "Agent",
)

# 易错工具钉死关键形状（全量 schema 近 100KB，不可整包灌入）
_CATALOG_NEST_HINTS: dict[str, str] = {
    "AskUserQuestion": ASK_USER_QUESTION_SHAPE["catalog_hint"],
    "Read": "required: file_path (absolute path; not path/file)",
    "Edit": "required: file_path,old_string,new_string (not path/file; not old_str/new_str); 多行值用 [[raw:old_string]]/[[raw:new_string]] 原文块",
    "Write": "required: file_path,content (not path/file); 多行 content 用 [[raw:content]] 原文块",
    "Bash": "required: command (not cmd/script); 多行脚本用 [[raw:command]] 原文块",
    "Grep": "required: pattern (not regex/query); path optional (not directory/dir)",
    "Glob": "required: pattern (not glob/g); path optional (not directory)",
    "Agent": (
        "Agent 默认异步；下阶段依赖报告时传 run_in_background:false。"
        "异步终止前，父窗口不得以 Bash/Read/Glob 重复其范围（不是独立并行），"
        "只做不重叠且不依赖报告的工作。完成通知到达后先整合 <result>；"
        "不全量重做，只按报告定点补缺或核验，再推进依赖阶段或交付"
    ),
}


def _catalog_nest_hint(name: str, schema: dict[str, Any]) -> str:
    key = (name or "").strip()
    if key in _CATALOG_NEST_HINTS:
        return _CATALOG_NEST_HINTS[key]
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return ""
    bits: list[str] = []
    for pname, pschema in list(props.items())[:4]:
        if not isinstance(pschema, dict):
            continue
        items = pschema.get("items")
        if not isinstance(items, dict):
            continue
        ireq = items.get("required")
        if isinstance(ireq, list) and ireq:
            bits.append(
                "{}.[].required: {}".format(pname, ",".join(str(x) for x in ireq[:8]))
            )
    return "; ".join(bits[:2])


def format_tools_catalog(tools: Any, *, max_tools: int = 40, desc_len: int = 160) -> str:
    """Anthropic tools[] → 短目录。"""
    if not isinstance(tools, list) or not tools:
        return ""
    items: list[tuple[int, dict[str, Any]]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            pri = _TOOL_CATALOG_PRIORITY.index(name.strip())
        except ValueError:
            pri = 100
        items.append((pri, t))
    items.sort(key=lambda x: (x[0], str(x[1].get("name") or "")))

    lines: list[str] = ["『可用工具』（Claude Code 本机；调用须用上方工具协议）"]
    n = 0
    for _, t in items:
        name = t.get("name")
        n += 1
        if n > max_tools:
            lines.append("- … 其余工具已省略，仍可按同名调用")
            break
        desc = t.get("description") or ""
        if not isinstance(desc, str):
            desc = str(desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > desc_len:
            desc = desc[: desc_len - 1] + "…"
        schema = t.get("input_schema") or t.get("parameters") or {}
        props: list[str] = []
        required: list = []
        if isinstance(schema, dict):
            required = schema.get("required") or []
            if not isinstance(required, list):
                required = []
            p = schema.get("properties")
            if isinstance(p, dict):
                props = list(p.keys())
        extra = []
        if required:
            extra.append("required: " + ",".join(str(x) for x in required[:12]))
        if props:
            prop_s = ",".join(props[:16])
            if len(props) > 16:
                prop_s += ",…"
            extra.append("props: " + prop_s)
        nest = _catalog_nest_hint(str(name), schema if isinstance(schema, dict) else {})
        if nest:
            extra.append(nest)
        tail = (" | " + "; ".join(extra)) if extra else ""
        lines.append("- **{}**: {}{}".format(name, desc, tail))
    if n == 0:
        return ""
    return "\n".join(lines)


def inject_tools_into_inputs(
    inputs: dict[str, str],
    tools: Any,
    *,
    enabled: bool,
    structured: bool = False,
) -> dict[str, str]:
    """协议 + 目录追加到 System_Description（不新增 Start 变量）。"""
    if not enabled:
        return inputs
    catalog = format_tools_catalog(tools)
    if not catalog:
        return inputs
    out = dict(inputs)
    proto = TOOL_PROTOCOL_TEXT_STRUCT if structured else TOOL_PROTOCOL_TEXT
    block = proto.rstrip() + "\n\n" + catalog
    prev = (out.get("System_Description") or "").strip()
    out["System_Description"] = (prev + "\n\n" + block).strip() if prev else block
    return out


def append_tools_reminder_to_query(
    query: str,
    *,
    enabled: bool,
    structured: bool = False,
) -> str:
    """约束居近：工具提醒贴在 sys.query 末尾。"""
    if not enabled:
        return query
    q = query or ""
    if TOOLS_ON_MARKER in q:
        return q
    return q.rstrip() + (TOOL_QUERY_REMINDER_STRUCT if structured else TOOL_QUERY_REMINDER)


def decorate_tool_continue_query(query_user: str, *, structured: bool = False) -> str:
    """工具续写枪加壳，避免模型把 tool_result 当新问题重答开场。"""
    q = (query_user or "").strip()
    if not q.startswith(TOOL_RESULT_PREFIX):
        return q
    tail = (
        "3. 若仍需工具，把调用放进 tool_calls 且 reply 置空；否则 tool_calls 给空数组，只给一次经结果验证的结论。\n"
        "4. History 中工具执行前的完成声明不是执行事实，不要逐字复述；以本轮 tool_result 为准。\n\n"
        if structured
        else "3. 若仍需工具，本枪只继续输出 [[tool_use]]；否则只给一次经结果验证的结论。\n"
        "4. History 中工具执行前的完成声明不是执行事实，不要逐字复述；以本轮 tool_result 为准。\n\n"
    )
    return (
        TOOL_CONTINUE_MARKER + "\n"
        "本轮是 Claude Code 工具回传（接上一条 assistant，同一用户问题）。\n"
        "要求：\n"
        "1. 续写完成原任务，不要重做已在上一条完成的开场（如重复完整识图描述）；\n"
        "2. 结合 History 里最近一条 assistant 与下方 tool_result；\n" + tail + q
    )


# ── input 归一 ───────────────────────────────────────────────────────


def toolu_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _alias_str_key(
    out: dict[str, Any],
    target: str,
    alts: tuple[str, ...],
    *,
    drop_alts: bool = True,
) -> None:
    cur = out.get(target)
    if not (isinstance(cur, str) and cur.strip()):
        for alt in alts:
            v = out.get(alt)
            if isinstance(v, str) and v.strip():
                out[target] = v.strip()
                break
    if drop_alts and target in out:
        for alt in alts:
            if alt != target:
                out.pop(alt, None)


def _short_header(text: str, max_len: int | None = None) -> str:
    if max_len is None:
        max_len = int(ASK_USER_QUESTION_SHAPE["header_max"])
    s = re.sub(r"\s+", " ", (text or "").strip()).strip("？?。.!！")
    if not s:
        return "Question"
    return s if len(s) <= max_len else s[:max_len]


def _normalize_ask_user_question(inp: dict[str, Any]) -> dict[str, Any]:
    out = dict(inp)
    qs = out.get("questions")
    if not isinstance(qs, list):
        return out
    new_qs: list[dict[str, Any]] = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        q2: dict[str, Any] = dict(q)

        question = q2.get("question")
        if not (isinstance(question, str) and question.strip()):
            for alt in ("text", "prompt", "content", "message", "q"):
                v = q2.get(alt)
                if isinstance(v, str) and v.strip():
                    question = v.strip()
                    break
            if isinstance(question, str) and question.strip():
                q2["question"] = question.strip()
        for alt in ("text", "prompt", "content", "message", "q", "id"):
            q2.pop(alt, None)

        header = q2.get("header")
        if not (isinstance(header, str) and header.strip()):
            for alt in ("title", "tag", "chip", "label"):
                v = q2.get(alt)
                if isinstance(v, str) and v.strip():
                    header = v.strip()
                    break
            if not (isinstance(header, str) and header.strip()):
                header = _short_header(str(q2.get("question") or ""))
        q2["header"] = _short_header(str(header))
        for alt in ("title", "tag", "chip"):
            q2.pop(alt, None)
        if "label" in q2 and "question" in q2:
            q2.pop("label", None)

        ms = q2.get("multiSelect")
        if isinstance(ms, bool):
            q2["multiSelect"] = ms
        elif isinstance(ms, str) and ms.strip().lower() in ("true", "1", "yes"):
            q2["multiSelect"] = True
        else:
            q2["multiSelect"] = False

        opts = q2.get("options")
        if isinstance(opts, list):
            new_opts: list[dict[str, Any]] = []
            for o in opts:
                if not isinstance(o, dict):
                    continue
                label = o.get("label")
                if not (isinstance(label, str) and label.strip()):
                    for alt in ("value", "name", "text", "title"):
                        v = o.get(alt)
                        if isinstance(v, str) and v.strip():
                            label = v.strip()
                            break
                if not (isinstance(label, str) and label.strip()):
                    continue
                desc = o.get("description")
                if not (isinstance(desc, str) and desc.strip()):
                    for alt in ("desc", "detail", "help"):
                        v = o.get(alt)
                        if isinstance(v, str) and v.strip():
                            desc = v.strip()
                            break
                if not (isinstance(desc, str) and desc.strip()):
                    desc = label.strip()
                o2: dict[str, Any] = {"label": label.strip(), "description": desc.strip()}
                preview = o.get("preview")
                if isinstance(preview, str) and preview.strip():
                    o2["preview"] = preview
                new_opts.append(o2)
            q2["options"] = new_opts

        allowed_q = set(ASK_USER_QUESTION_SHAPE["required"])
        new_qs.append({k: v for k, v in q2.items() if k in allowed_q})

    out["questions"] = new_qs
    return {k: out[k] for k in ("questions", "answers", "annotations", "metadata") if k in out}


def normalize_tool_input(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    """模型常见错形 → CC schema 可过检的形状（保守别名，不猜业务内容）。"""
    if not isinstance(inp, dict):
        return {}
    n = (name or "").strip()
    if n == "AskUserQuestion":
        return _normalize_ask_user_question(inp)
    out = dict(inp)
    if n in ("Read", "Edit", "Write"):
        _alias_str_key(out, "file_path", ("path", "file", "filepath", "filename", "filePath"))
        if n == "Edit":
            _alias_str_key(out, "old_string", ("old_str", "oldString"))
            _alias_str_key(out, "new_string", ("new_str", "newString"))
        return out
    if n == "Bash":
        _alias_str_key(out, "command", ("cmd", "script"))
        return out
    if n == "Grep":
        _alias_str_key(out, "pattern", ("regex", "query"))
        _alias_str_key(out, "path", ("directory", "dir"))
        return out
    if n == "Glob":
        _alias_str_key(out, "pattern", ("glob", "g"))
        _alias_str_key(out, "path", ("directory",))
        return out
    return out


def tool_use_from_obj(
    obj: Any,
    extra_fields: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """{name, input} → Anthropic tool_use 块；原文块字段覆盖 JSON 同名键。"""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str) or not name.strip():
        return None
    inp = obj.get("input")
    if inp is None:
        inp = obj.get("arguments") or obj.get("parameters") or {}
    if not isinstance(inp, dict):
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except json.JSONDecodeError:
                inp = {"value": inp}
        else:
            inp = {"value": inp}
    if extra_fields:
        inp = dict(inp) if isinstance(inp, dict) else {}
        inp.update(extra_fields)
    name_s = name.strip()
    return {
        "type": "tool_use",
        "id": toolu_id(),
        "name": name_s,
        "input": normalize_tool_input(name_s, inp if isinstance(inp, dict) else {}),
    }


# ── JSON 抢救扫描 ────────────────────────────────────────────────────


def _is_hex4(text: str, pos: int) -> bool:
    seg = text[pos : pos + 4]
    return len(seg) == 4 and all(c in "0123456789abcdefABCDEF" for c in seg)


def _looks_like_json_string_end(text: str, quote_idx: int) -> bool:
    """quote_idx 处的引号是否像字符串真正结束（其后接 , } ] : 或 EOF）。"""
    k = quote_idx + 1
    n = len(text)
    while k < n and text[k] in " \t\r\n":
        k += 1
    if k >= n:
        return True
    return text[k] in ",}]:"


def _scan_json_object_repaired(text: str, start: int) -> tuple[str, int] | None:
    """从 { 起一趟扫描：定界 + 串内修复。

    - 串内 CRLF / 裸换行 → \\n；\\t 与控制符转义
    - 未转义引号（后接汉字等字面量）→ \\"
    - 非法转义（裸写 C:\\Users 的 \\U 等）→ 反斜杠按字面量 \\\\
    返回 (repaired_json_text, 原文 end_idx) 或 None。
    """
    if start >= len(text) or text[start] != "{":
        return None
    out: list[str] = []
    depth = 0
    i = start
    n = len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                nxt = text[i + 1] if i + 1 < n else ""
                if nxt in "\"\\/bfnrt":
                    out.append(ch)
                    out.append(nxt)
                    i += 2
                    continue
                if nxt == "u" and _is_hex4(text, i + 2):
                    out.append(text[i : i + 6])
                    i += 6
                    continue
                out.append("\\\\")
                i += 1
                continue
            if ch == '"':
                if _looks_like_json_string_end(text, i):
                    out.append(ch)
                    in_str = False
                else:
                    out.append('\\"')
                i += 1
                continue
            if ch == "\r":
                out.append("\\n")
                i += 2 if i + 1 < n and text[i + 1] == "\n" else 1
                continue
            if ch == "\n":
                out.append("\\n")
                i += 1
                continue
            if ch == "\t":
                out.append("\\t")
                i += 1
                continue
            o = ord(ch)
            if o < 0x20:
                out.append("\\u%04x" % o)
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
        i += 1
    return None


def raw_decode_tool_json(text: str, start: int) -> tuple[Any, int] | None:
    """严格 raw_decode → 失败则修复重试。"""
    dec = json.JSONDecoder()
    try:
        obj, end_idx = dec.raw_decode(text, start)
        return obj, end_idx
    except json.JSONDecodeError:
        pass
    scanned = _scan_json_object_repaired(text, start)
    if scanned is None:
        return None
    repaired, end_idx = scanned
    try:
        return json.loads(repaired), end_idx
    except json.JSONDecodeError:
        return None


# ── 原文块 ───────────────────────────────────────────────────────────

_RAW_OPEN_RE = re.compile(r"\[\[raw:([A-Za-z_][A-Za-z0-9_]*)\]\]", re.I)


def _scan_raw_field_blocks(text: str, pos: int) -> tuple[dict[str, str], int, bool]:
    """JSON 头之后连续的 [[raw:键]]…[[/raw:键]] 原文块。

    返回 (fields, end_pos, ok)；ok=False = 开而未闭（流截断），整块应弃置为正文。
    payload 剥除紧邻围栏的单个换行；CRLF / 孤立 CR 归一为 \\n；其余零改动。
    """
    fields: dict[str, str] = {}
    i = pos
    n = len(text)
    low = text.lower()
    while True:
        j = i
        while j < n and text[j].isspace():
            j += 1
        m = _RAW_OPEN_RE.match(text, j)
        if not m:
            return fields, i, True
        key = m.group(1)
        close = "{}{}]]".format(RAW_CLOSE_PREFIX, key.lower())
        start = m.end()
        if text.startswith("\r\n", start):
            start += 2
        elif start < n and text[start] == "\n":
            start += 1
        idx = low.find(close, start)
        if idx < 0:
            return fields, i, False
        payload = text[start:idx]
        if payload.endswith("\r\n"):
            payload = payload[:-2]
        elif payload.endswith("\n"):
            payload = payload[:-1]
        fields[key] = payload.replace("\r\n", "\n").replace("\r", "\n")
        i = idx + len(close)


def _extract_marked_json_blocks(
    text: str,
    open_m: str,
    close_m: str,
    tools_out: list[dict[str, Any]],
) -> str:
    """open … {json} [raw块…] … close → tool_use；失败保留原文并前进。"""
    if not text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    open_l = open_m.lower()
    close_l = close_m.lower()
    while i < n:
        rel = text[i:].lower().find(open_l)
        if rel < 0:
            out.append(text[i:])
            break
        out.append(text[i : i + rel])
        start_open = i + rel
        after_open = start_open + len(open_m)
        j = after_open
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] != "{":
            out.append(text[start_open:after_open])
            i = after_open
            continue
        decoded = raw_decode_tool_json(text, j)
        if decoded is None:
            out.append(text[start_open:after_open])
            i = after_open
            continue
        obj, end_idx = decoded
        raw_fields, end_idx, raw_ok = _scan_raw_field_blocks(text, end_idx)
        if not raw_ok:
            out.append(text[start_open:after_open])
            i = after_open
            continue
        k = end_idx
        while k < n and text[k].isspace():
            k += 1
        if text[k : k + len(close_m)].lower() != close_l:
            # 无闭合：模型常漏，仍认块
            parsed = tool_use_from_obj(obj, raw_fields)
            if parsed:
                tools_out.append(parsed)
                out.append("\n")
                i = end_idx
            else:
                out.append(text[start_open:after_open])
                i = after_open
            continue
        parsed = tool_use_from_obj(obj, raw_fields)
        if parsed:
            tools_out.append(parsed)
            out.append("\n")
        else:
            out.append(text[start_open : k + len(close_m)])
        i = k + len(close_m)
    return "".join(out)


def extract_tool_uses(text: str) -> tuple[str, list[dict[str, Any]]]:
    """模型正文 → (去标记正文, tool_use 块列表)。"""
    if not text:
        return "", []
    tools_out: list[dict[str, Any]] = []
    cleaned = text
    for open_m, close_m in TOOL_MARKERS:
        cleaned = _extract_marked_json_blocks(cleaned, open_m, close_m, tools_out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, tools_out


def _strip_after_success_fragments(source: str) -> str:
    """无效隐藏协议也不外泄；保留标记之外的可见正文。"""
    low = source.lower()
    open_l = AFTER_SUCCESS_OPEN.lower()
    close_l = AFTER_SUCCESS_CLOSE.lower()
    out: list[str] = []
    pos = 0
    while pos < len(source):
        start = low.find(open_l, pos)
        stray_close = low.find(close_l, pos)
        if stray_close >= 0 and (start < 0 or stray_close < start):
            out.append(source[pos:stray_close])
            pos = stray_close + len(AFTER_SUCCESS_CLOSE)
            continue
        if start < 0:
            out.append(source[pos:])
            break
        out.append(source[pos:start])
        end = low.find(close_l, start + len(AFTER_SUCCESS_OPEN))
        if end < 0:
            break
        pos = end + len(AFTER_SUCCESS_CLOSE)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def parse_after_success(text: str) -> AfterSuccessParse:
    """只认一个完整、非空且位于响应末尾的 terminal 草案。"""
    source = text or ""
    if not source:
        return AfterSuccessParse(visible="")
    low = source.lower()
    open_l = AFTER_SUCCESS_OPEN.lower()
    close_l = AFTER_SUCCESS_CLOSE.lower()
    opens = [m.start() for m in re.finditer(re.escape(open_l), low)]
    closes = [m.start() for m in re.finditer(re.escape(close_l), low)]
    found = bool(opens or closes)
    if not found:
        return AfterSuccessParse(visible=source)

    reason = "invalid_marker_count"
    if len(opens) == 1 and len(closes) == 1:
        start = opens[0]
        inner_start = start + len(AFTER_SUCCESS_OPEN)
        end = closes[0]
        if end < inner_start:
            reason = "close_before_open"
        elif source[end + len(AFTER_SUCCESS_CLOSE) :].strip():
            reason = "not_at_response_end"
        else:
            success = source[inner_start:end].strip()
            if success:
                visible = re.sub(r"\n{3,}", "\n\n", source[:start]).strip()
                return AfterSuccessParse(
                    visible=visible,
                    success=success,
                    found=True,
                    valid=True,
                    reason="valid",
                )
            reason = "empty_success"
    elif len(opens) == 1 and not closes:
        reason = "unclosed"
    elif closes and not opens:
        reason = "orphan_close"

    return AfterSuccessParse(
        visible=_strip_after_success_fragments(source),
        found=True,
        valid=False,
        reason=reason,
    )


def extract_after_success(text: str) -> tuple[str, str]:
    """兼容调用面：无效契约不返回成功草案。"""
    parsed = parse_after_success(text)
    return parsed.visible, parsed.success if parsed.valid else ""


# 视图三：残留检测。覆盖面须不小于 _MARKER_PAIRS——它额外吃任意 raw 键名，
# 故不从表机械派生；两者的同步由 test_tools.py 的 sync 用例锁住。
_PROTOCOL_RESIDUE_RE = re.compile(
    r"(?i)(?:"
    r"\[\[\s*/?\s*(?:tool_uses?|raw(?::[^\]\r\n]+)?|after_success|cc_tools_json)\s*\]\]"
    r"|</?\s*(?:tool_uses?|tool_call)\s*>"
    r")"
)


def has_protocol_residue(text: str) -> bool:
    """terminal 资格须拒绝任意未被消费的工具协议残片。"""
    return bool(_PROTOCOL_RESIDUE_RE.search(text or ""))


def terminal_draft_follows_tools(source: str) -> bool:
    """最后一个草案开标记之后不得再出现工具协议标记。"""
    low = (source or "").lower()
    start = low.rfind(AFTER_SUCCESS_OPEN.lower())
    if start < 0:
        return True
    tail = low[start + len(AFTER_SUCCESS_OPEN) :]
    return not any(marker in tail for marker in _POST_DRAFT_FORBIDDEN)


def is_terminal_tool_batch(tool_uses: list[dict[str, Any]]) -> bool:
    """终结批次只含 Write/Edit，且已知的变更目标互不冲突。"""
    if not tool_uses:
        return False
    seen_paths: set[str] = set()
    for tool in tool_uses:
        if not isinstance(tool, dict):
            return False
        if str(tool.get("name") or "") not in TERMINAL_TOOL_NAMES:
            return False
        inp = tool.get("input")
        path = inp.get("file_path") if isinstance(inp, dict) else None
        if isinstance(path, str) and path.strip():
            normalized = ntpath.normcase(ntpath.normpath(path.strip().replace("/", "\\")))
            if normalized in seen_paths:
                return False
            seen_paths.add(normalized)
    return True


# ── 结构化外壳 ───────────────────────────────────────────────────────


def tool_uses_from_calls(calls: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(calls, list):
        return out
    for c in calls:
        tu = tool_use_from_obj(c)
        if tu:
            out.append(tu)
    return out


def _strip_code_fence(s: str) -> str:
    """剥掉 ```json … ``` 一层围栏（prompt 型结构化输出的常见包装）。"""
    if not s.startswith("```"):
        return s
    s2 = re.sub(r"^```[A-Za-z]*[ \t]*\r?\n", "", s)
    s2 = re.sub(r"\r?\n```[ \t]*$", "", s2)
    return s2.strip()


def extract_structured_envelope(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    """整包 {reply, tool_calls[]} 或 [[cc_tools_json]] 围栏 → (正文, tool_use 列表)。

    判定收紧到「顶层含 tool_calls 键」，普通 JSON 答复不误判；不命中返回 None。
    """
    s = (text or "").strip()
    if not s:
        return None
    low = s.lower()
    o = low.find(TOOLS_JSON_OPEN)
    if o >= 0:
        c = low.find(TOOLS_JSON_CLOSE, o)
        inner = s[o + len(TOOLS_JSON_OPEN) : (c if c >= 0 else len(s))]
        body = s[:o] + (s[c + len(TOOLS_JSON_CLOSE) :] if c >= 0 else "")
        inner = _strip_code_fence(inner.strip())
        ja = inner.find("[")
        jo = inner.find("{")
        calls: Any = None
        if ja >= 0 and (jo < 0 or ja < jo):
            try:
                calls = json.loads(inner[ja:])
            except json.JSONDecodeError:
                calls = None
        elif jo >= 0:
            decoded = raw_decode_tool_json(inner, jo)
            if decoded is not None and isinstance(decoded[0], dict):
                calls = decoded[0].get("tool_calls")
        if calls is None:
            return None
        return body.strip(), tool_uses_from_calls(calls)

    s2 = _strip_code_fence(s)
    if not s2.startswith("{"):
        return None
    decoded = raw_decode_tool_json(s2, 0)
    if decoded is None:
        return None
    obj, end_idx = decoded
    if not isinstance(obj, dict) or "tool_calls" not in obj:
        return None
    reply = obj.get("reply")
    if not isinstance(reply, str):
        reply = ""
    tail = s2[end_idx:].strip()
    body = (reply + ("\n\n" + tail if tail else "")).strip()
    return body, tool_uses_from_calls(obj.get("tool_calls"))


def find_stream_cut(buf_lower: str) -> int:
    """投机流式：正文中最早的工具标记位置；无则 -1。"""
    cut = -1
    for m in STREAM_CUT_MARKERS:
        i = buf_lower.find(m)
        if i >= 0 and (cut < 0 or i < cut):
            cut = i
    return cut
