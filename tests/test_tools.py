# -*- coding: utf-8 -*-
"""工具通道：三通道解析（原文块 / 结构化外壳 / JSON 抢救）与参数归一。"""
from __future__ import annotations

import json

from tools import (
    append_tools_reminder_to_query,
    decorate_tool_continue_query,
    extract_after_success,
    extract_structured_envelope,
    extract_tool_uses,
    find_stream_cut,
    format_tools_catalog,
    has_protocol_residue,
    inject_tools_into_inputs,
    is_terminal_tool_batch,
    normalize_tool_input,
    parse_after_success,
)


# ── 原文块：长文本零转义 ──


def test_raw_content_write_zero_escape():
    sample = """开写：

[[tool_use]]
{"name":"Write","input":{"file_path":"C:\\\\Users\\\\me\\\\Documents\\\\笔记\\\\原理.md"}}
[[raw:content]]
# 《艺术与错觉》原理笔记

为什么"笨拙"的风格在自己的时代被视为逼真？
路径示例：C:\\Users\\me\\Desktop（反斜杠原样）

```json
{"nested": "fence", "arr": [1, 2]}
```

最后一行。
[[/raw:content]]
[[/tool_use]]

写完了。"""
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1, tools
    assert tools[0]["name"] == "Write"
    assert tools[0]["input"]["file_path"].endswith("原理.md")
    c = tools[0]["input"]["content"]
    assert c.startswith("# 《艺术与错觉》原理笔记")
    assert '"笨拙"' in c
    assert "C:\\Users\\me\\Desktop" in c
    assert '{"nested": "fence", "arr": [1, 2]}' in c
    assert c.endswith("最后一行。")
    assert "[[raw:content]]" not in body
    assert "最后一行" not in body
    assert "开写" in body and "写完了" in body


def test_raw_edit_old_new_blocks():
    sample = (
        "[[tool_use]]\n"
        '{"name":"Edit","input":{"file_path":"C:\\\\a.md"}}\n'
        '[[raw:old_string]]\n第一行\n"旧"段落\n[[/raw:old_string]]\n'
        '[[raw:new_string]]\n第一行\n"新"段落\n\n尾行\n[[/raw:new_string]]\n'
        "[[/tool_use]]"
    )
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1, tools
    t = tools[0]
    assert t["name"] == "Edit"
    assert t["input"]["old_string"] == '第一行\n"旧"段落'
    assert t["input"]["new_string"] == '第一行\n"新"段落\n\n尾行'
    assert "[[raw:" not in body


def test_raw_missing_tool_close_still_ok():
    sample = (
        "[[tool_use]]\n"
        '{"name":"Write","input":{"file_path":"C:\\\\b.md"}}\n'
        "[[raw:content]]\nline1\nline2\n[[/raw:content]]\n"
    )
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1
    assert tools[0]["input"]["content"] == "line1\nline2"


def test_raw_unclosed_block_discarded():
    sample = (
        "[[tool_use]]\n"
        '{"name":"Write","input":{"file_path":"C:\\\\b.md"}}\n'
        "[[raw:content]]\n断流的内容……"
    )
    body, tools = extract_tool_uses(sample)
    assert tools == []
    assert "断流的内容" in body


def test_mixed_read_json_and_raw_write():
    sample = (
        "先读再写。\n\n"
        "[[tool_use]]\n"
        '{"name":"Read","input":{"file_path":"C:\\\\src\\\\a.md"}}\n'
        "[[/tool_use]]\n\n"
        "[[tool_use]]\n"
        '{"name":"Write","input":{"file_path":"C:\\\\out\\\\b.md"}}\n'
        "[[raw:content]]\n# out\nline2\n[[/raw:content]]\n"
        "[[/tool_use]]\n"
    )
    body, tools = extract_tool_uses(sample)
    assert [t["name"] for t in tools] == ["Read", "Write"]
    assert tools[1]["input"]["content"] == "# out\nline2"
    assert "[[tool_use]]" not in body


def test_after_success_is_hidden_but_raw_file_content_is_untouched():
    sample = (
        "[[tool_use]]\n"
        '{"name":"Write","input":{"file_path":"C:\\\\out.md"}}\n'
        "[[raw:content]]\n正文里的 [[after_success]] 只是文件内容\n[[/raw:content]]\n"
        "[[/tool_use]]\n"
        "[[after_success]]\n已写入 out.md。\n[[/after_success]]"
    )
    visible, tools = extract_tool_uses(sample)
    visible, success = extract_after_success(visible)
    assert visible == ""
    assert success == "已写入 out.md。"
    assert "[[after_success]]" in tools[0]["input"]["content"]
    assert is_terminal_tool_batch(tools)
    assert not is_terminal_tool_batch(
        [{"type": "tool_use", "id": "r", "name": "Read", "input": {}}]
    )


def test_after_success_requires_one_closed_block_at_response_end():
    cases = (
        "[[after_success]]半截",
        "[[after_success]]一[[/after_success]][[after_success]]二[[/after_success]]",
        "[[after_success]]完整[[/after_success]][[after_success]]又半截",
        "[[after_success]]完成[[/after_success]]\n尾部正文",
        "[[/after_success]]孤立闭标记",
    )
    for sample in cases:
        parsed = parse_after_success(sample)
        assert parsed.found and not parsed.valid
        assert parsed.success == ""
        assert "[[after_success]]" not in parsed.visible
        assert "[[/after_success]]" not in parsed.visible


def test_terminal_batch_rejects_same_mutation_target():
    tools = [
        {
            "type": "tool_use",
            "id": "w",
            "name": "Write",
            "input": {"file_path": r"C:\Work\same.md"},
        },
        {
            "type": "tool_use",
            "id": "e",
            "name": "Edit",
            "input": {"file_path": "c:/work/same.md"},
        },
    ]
    assert not is_terminal_tool_batch(tools)


def test_protocol_residue_includes_orphan_close_and_raw_markers():
    assert has_protocol_residue("[[/tool_use]]")
    assert has_protocol_residue("[[/tool_uses]]")
    assert has_protocol_residue("[[raw:content]]")
    assert not has_protocol_residue("普通正文")


def test_raw_block_crlf_normalized():
    sample = (
        "[[tool_use]]\r\n"
        '{"name":"Write","input":{"file_path":"C:\\\\c.md"}}\r\n'
        "[[raw:content]]\r\na\r\nb\r\n[[/raw:content]]\r\n"
        "[[/tool_use]]"
    )
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1
    assert tools[0]["input"]["content"] == "a\nb"


# ── JSON 抢救：裸换行 / CRLF / 未转义引号 / 非法转义 ──


def test_write_with_raw_newlines_in_content():
    sample = """已读取你的笔记。现在创建：

<tool_use>
{"name":"Write","input":{"file_path":"C:\\\\Users\\\\00\\\\Documents\\\\笔记\\\\导论笔记.md","content":"# 读书笔记

## 导论

理解：图式与漫画。

正文第二段。
"}}
</tool_use>
"""
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1, tools
    assert tools[0]["name"] == "Write"
    content = tools[0]["input"]["content"]
    assert "图式与漫画" in content and "正文第二段" in content and "\n" in content
    assert "<tool_use>" not in body


def test_write_crlf_and_lone_cr_normalize():
    for raw in ("a\r\nb", "a\rb"):
        sample = (
            "<tool_use>\n"
            '{"name":"Write","input":{"file_path":"C:\\\\a.md","content":"' + raw + '"}}\n'
            "</tool_use>"
        )
        body, tools = extract_tool_uses(sample)
        assert len(tools) == 1, tools
        assert tools[0]["input"]["content"] == "a\nb"


def test_write_unescaped_ascii_quotes_in_content():
    sample = (
        "[[tool_use]]\n"
        '{"name":"Write","input":{"file_path":"C:\\\\Users\\\\00\\\\a\\\\原理笔记.md",'
        '"content":"无法解释为什么"笨拙"的风格在自己的时代被视为逼真。\\n下一行。"}}\n'
        "[[/tool_use]]"
    )
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1, tools
    c = tools[0]["input"]["content"]
    assert "笨拙" in c and "逼真" in c
    assert "[[tool_use]]" not in body


def test_json_content_windows_backslash_repair():
    sample = (
        "[[tool_use]]\n"
        '{"name":"Write","input":{"file_path":"C:\\\\c.md","content":"见 C:\\Users\\me 目录\n第二行"}}\n'
        "[[/tool_use]]"
    )
    body, tools = extract_tool_uses(sample)
    assert len(tools) == 1, tools
    c = tools[0]["input"]["content"]
    assert "C:\\Users\\me" in c and "第二行" in c


def test_write_nested_braces_and_escaped_quotes():
    sample = """<tool_use>
{"name":"Write","input":{"file_path":"C:\\\\cfg.json","content":"prefix
{a:1}
suffix
"}}
</tool_use>"""
    body, tools = extract_tool_uses(sample)
    assert tools[0]["input"]["content"] == "prefix\n{a:1}\nsuffix\n"

    sample2 = r"""[[tool_use]]
{"name":"Write","input":{"file_path":"C:\\q.md","content":"say \"hi\" please"}}
[[/tool_use]]"""
    body2, tools2 = extract_tool_uses(sample2)
    assert tools2[0]["input"]["content"] == 'say "hi" please'


def test_invalid_or_markerless_input_no_crash():
    body, tools = extract_tool_uses("前言\n\n<tool_use>\n{not valid json\n后文\n")
    assert isinstance(tools, list) and isinstance(body, str)
    body2, tools2 = extract_tool_uses("hello <tool_use> no brace </tool_use> world")
    assert tools2 == [] and "hello" in body2


def test_body_strips_payload_completely():
    sample = """说明文字。

<tool_use>
{"name":"Write","input":{"file_path":"C:\\\\Users\\\\00\\\\note.md","content":"# 标题
段落一
段落二"}}
</tool_use>

结束语。
"""
    body, tools = extract_tool_uses(sample)
    assert tools[0]["input"]["content"] == "# 标题\n段落一\n段落二"
    assert "<tool_use>" not in body and "file_path" not in body
    assert "段落一" not in body
    assert "说明文字" in body and "结束语" in body


# ── 结构化外壳 ──


def test_envelope_bare_object():
    payload = {
        "reply": "我把两份笔记写好了。",
        "tool_calls": [
            {"name": "Write", "input": {"file_path": "C:\\a.md", "content": '第一行\n"引号"\n'}},
            {"name": "Write", "input": {"file_path": "C:\\b.md", "content": "short"}},
        ],
    }
    env = extract_structured_envelope(json.dumps(payload, ensure_ascii=False))
    assert env is not None
    body, tools = env
    assert body == "我把两份笔记写好了。"
    assert [t["name"] for t in tools] == ["Write", "Write"]
    assert tools[0]["input"]["content"] == '第一行\n"引号"\n'
    assert tools[0]["type"] == "tool_use" and tools[0]["id"].startswith("toolu_")


def test_envelope_code_fenced_json():
    env = extract_structured_envelope('```json\n{"reply":"好","tool_calls":[]}\n```')
    assert env is not None
    body, tools = env
    assert body == "好" and tools == []


def test_envelope_fenced_in_prose():
    text = (
        "先说结论……\n\n[[cc_tools_json]]\n"
        '{"tool_calls":[{"name":"Read","input":{"file_path":"C:\\\\x.md"}}]}\n'
        "[[/cc_tools_json]]\n收尾。"
    )
    env = extract_structured_envelope(text)
    assert env is not None
    body, tools = env
    assert "先说结论" in body and "收尾" in body
    assert "[[cc_tools_json]]" not in body
    assert tools[0]["name"] == "Read"


def test_envelope_none_for_plain_or_other_json():
    assert extract_structured_envelope("普通回答。") is None
    assert extract_structured_envelope('{"title":"t","isNewTopic":true}') is None
    assert extract_structured_envelope("") is None


# ── 协议注入 ──


def _tools_def() -> list:
    return [
        {
            "name": "Write",
            "description": "write file",
            "input_schema": {
                "required": ["file_path", "content"],
                "properties": {"file_path": {}, "content": {}},
            },
        }
    ]


def _agent_tool_def() -> dict:
    return {
        "name": "Agent",
        "description": "A" * 300 + "AGENT_DESCRIPTION_TAIL",
        "input_schema": {
            "required": ["description", "prompt", "subagent_type"],
            "properties": {
                "description": {},
                "prompt": {},
                "subagent_type": {},
                "run_in_background": {},
            },
        },
    }


def test_agent_catalog_keeps_lifecycle_semantics_outside_description_limit():
    other = {
        "name": "Other",
        "description": "B" * 300 + "OTHER_DESCRIPTION_TAIL",
        "input_schema": {"properties": {}},
    }
    catalog = format_tools_catalog([_agent_tool_def(), other])

    assert "AGENT_DESCRIPTION_TAIL" not in catalog
    assert "OTHER_DESCRIPTION_TAIL" not in catalog
    assert "B" * 159 + "…" in catalog
    assert "Agent 默认异步" in catalog
    assert "run_in_background:false" in catalog
    assert "父窗口" in catalog and "重复" in catalog
    assert "完成通知" in catalog and "<result>" in catalog
    assert "终止前" in catalog
    assert "定点补缺或核验" in catalog


def test_agent_catalog_semantics_match_text_and_structured_protocols():
    tools = [_agent_tool_def()]
    text_sd = inject_tools_into_inputs({}, tools, enabled=True)["System_Description"]
    struct_sd = inject_tools_into_inputs(
        {}, tools, enabled=True, structured=True
    )["System_Description"]

    for phrase in (
        "Agent 默认异步",
        "run_in_background:false",
        "父窗口",
        "完成通知",
        "<result>",
    ):
        assert phrase in text_sd
        assert phrase in struct_sd


def test_protocol_injection_text_mode():
    inputs = inject_tools_into_inputs({}, _tools_def(), enabled=True)
    sd = inputs["System_Description"]
    assert "[[raw:content]]" in sd and "可用工具" in sd
    q = append_tools_reminder_to_query("问题", enabled=True)
    assert "[[cc_tools:on]]" in q and "[[raw:" in q
    assert "严禁提前声称完成" in q and "tool_result 返回后再确认" in q
    assert "严禁提前声称“已完成" in sd
    assert "同一认知阶段" in sd and "不得为了叙述进度串行" in sd
    assert "[[after_success]]" in sd and "全部工具明确成功后本地释放" in sd
    assert "副作用顺序依赖" in q and "同一文件" in q
    assert "终结性的 Write/Edit" in q


def test_protocol_injection_struct_mode():
    inputs = inject_tools_into_inputs({}, _tools_def(), enabled=True, structured=True)
    sd = inputs["System_Description"]
    assert "tool_calls" in sd and "结构化出口" in sd and "可用工具" in sd
    q = append_tools_reminder_to_query("问题", enabled=True, structured=True)
    assert "tool_calls" in q and "[[cc_tools:on]]" in q
    assert "reply 必须为空" in q and "tool_result 返回后再给结论" in q
    assert "副作用顺序依赖" in q and "同一文件" in q
    assert "只要 tool_calls 非空，reply 必须为空字符串" in sd


def test_tool_continue_requires_one_verified_conclusion():
    q = decorate_tool_continue_query(
        "[tool_result] tool_use_id=toolu_x\nFile created successfully"
    )
    assert q.startswith("[[cc_tool_continue]]")
    assert "只给一次经结果验证的结论" in q
    assert "工具执行前的完成声明不是执行事实" in q

    qs = decorate_tool_continue_query(
        "[tool_result] tool_use_id=toolu_x\nFile created successfully",
        structured=True,
    )
    assert "reply 置空" in qs
    assert "只给一次经结果验证的结论" in qs


# ── 归一 ──


def test_normalize_ask_user_question():
    raw = {
        "questions": [
            {
                "text": "选？",
                "options": [
                    {"value": "a", "label": "A", "description": "da"},
                    {"value": "b", "label": "B", "description": "db"},
                ],
            }
        ]
    }
    out = normalize_tool_input("AskUserQuestion", raw)
    q = out["questions"][0]
    assert q["question"] == "选？"
    assert "header" in q and q["multiSelect"] is False
    assert all(set(o.keys()) <= {"label", "description", "preview"} for o in q["options"])


def test_normalize_aliases():
    e = normalize_tool_input("Edit", {"path": r"C:\a.py", "old_string": "x", "new_string": "y"})
    assert e.get("file_path", "").endswith("a.py")
    b = normalize_tool_input("Bash", {"cmd": "echo hi"})
    assert b.get("command") == "echo hi"
    g = normalize_tool_input("Grep", {"regex": "foo", "directory": "."})
    assert g.get("pattern") == "foo" and g.get("path") == "."


def test_find_stream_cut():
    assert find_stream_cut("你好 [[tool_use]] x".lower()) == 3
    assert find_stream_cut("纯文本没有标记".lower()) == -1
    assert find_stream_cut("<tool_call> 早于 [[tool_use]]".lower()) == 0


def test_protocol_marker_views_stay_in_sync_with_the_table():
    """三处标记视图共享 _MARKER_PAIRS；残留正则的覆盖面须不小于表。

    截停要早、残留检测要宽、草案定位要准——三者覆盖面本就不同，故不能机械同形。
    本用例锁住的是「表里新增一个标记而某个视图漏掉」这种静默失配。
    """
    from tools import (
        AFTER_SUCCESS_CLOSE,
        AFTER_SUCCESS_OPEN,
        CODE_FENCE,
        RAW_CLOSE_PREFIX,
        RAW_OPEN_PREFIX,
        STREAM_CUT_MARKERS,
        TOOL_MARKERS,
        TOOLS_JSON_CLOSE,
        TOOLS_JSON_OPEN,
        has_protocol_residue,
        terminal_draft_follows_tools,
    )

    # 残留检测：表中每个字面标记、以及带任意键名的 raw 块，都必须被认出
    residue_cases = [marker for pair in TOOL_MARKERS for marker in pair]
    residue_cases += [
        AFTER_SUCCESS_OPEN,
        AFTER_SUCCESS_CLOSE,
        TOOLS_JSON_OPEN,
        TOOLS_JSON_CLOSE,
        RAW_OPEN_PREFIX + "content]]",
        RAW_CLOSE_PREFIX + "content]]",
        RAW_OPEN_PREFIX + "new_string]]",
    ]
    for marker in residue_cases:
        assert has_protocol_residue("正文 {} 尾".format(marker)), marker
    assert not has_protocol_residue("纯正文，没有任何协议标记。")

    # 截停视图：每个开标记与代码围栏都要能触发，闭标记不单独触发
    for open_marker, _close in TOOL_MARKERS:
        assert open_marker in STREAM_CUT_MARKERS
    assert AFTER_SUCCESS_OPEN in STREAM_CUT_MARKERS
    assert TOOLS_JSON_OPEN in STREAM_CUT_MARKERS
    assert CODE_FENCE in STREAM_CUT_MARKERS
    assert AFTER_SUCCESS_CLOSE not in STREAM_CUT_MARKERS

    # 草案定位视图：草案之后出现任一协议标记即失格；after_success 自身不算
    for marker in (
        TOOL_MARKERS[0][0],
        TOOL_MARKERS[0][1],
        RAW_OPEN_PREFIX + "content]]",
        TOOLS_JSON_OPEN,
    ):
        assert not terminal_draft_follows_tools(
            "{}草案{}\n{}".format(AFTER_SUCCESS_OPEN, AFTER_SUCCESS_CLOSE, marker)
        ), marker
    assert terminal_draft_follows_tools(
        "{}\n{{}}\n{}\n{}草案{}".format(
            TOOL_MARKERS[0][0],
            TOOL_MARKERS[0][1],
            AFTER_SUCCESS_OPEN,
            AFTER_SUCCESS_CLOSE,
        )
    )
