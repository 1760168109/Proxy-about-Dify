# -*- coding: utf-8 -*-
"""Read 正文缓存：对抗 Claude Code「Wasted call — file unchanged」不重传全文。

按 DIFY_USER_ID + 规范化路径分桶落盘；成功 Read 写入，Wasted call 时重放。
need_read 判定：本轮引用了文件路径但上下文尚无实质正文 → 提示先 Read。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from parse import (
    TOOL_RESULT_PREFIX,
    normalize_path,
    path_from_tool_input,
    text_from_content,
)
from persist import atomic_write_json, read_json_dict

_WASTED_MARKERS = (
    "Wasted call",
    "file unchanged since your last Read",
    "Refer to that earlier tool_result instead",
)

_CALLED_READ_RE = re.compile(
    r"Called the\s+Read\s+tool\s+with\s+the\s+following\s+input:\s*"
    r"(\{.*?\})\s*"
    r"Result of calling the\s+Read\s+tool:\s*",
    re.I | re.S,
)
# 与 parse.TOOL_USE_LINE_FMT / TOOL_RESULT_LINE_FMT 的折叠格式对应（变更须同步）
_TOOL_USE_TEXT_RE = re.compile(
    r"\[tool_use\]\s*name=(\S+)\s+id=([^\n]+)\n(\{[^\n]*\})", re.I
)
_TOOL_RESULT_TEXT_RE = re.compile(
    r"(\[tool_result\]\s*tool_use_id=([^\n]+)\n)(.*?)(?=\n\[tool_result\]|\n\[tool_use\]|\Z)",
    re.S | re.I,
)

_MIN_CACHE_CHARS = 40

_CONTEXT_PLACEHOLDER_PREFIXES = (
    "(body carried in Tool_invocation",
    "(current result carried in sys.query",
    "(superseded file state",
)

_CODE_DOC_EXTS = (
    "md|txt|py|js|ts|tsx|jsx|json|yaml|yml|toml|rs|go|java|c|h|cpp|hpp|cs|"
    "rb|php|css|html|htm|xml|sh|bash|ps1|sql|vue|svelte|kt|swift|r|ipynb|"
    "ini|cfg|conf|env|lock|gradle|cmake|makefile|dockerfile|gitignore|"
    "scss|less|sass|mjs|cjs|wasm|proto|graphql|gql|rst|tex|bib|"
    "png|jpg|jpeg|gif|webp|svg|pdf"
)
# @路径 / 绝对路径 / 相对路径（须分隔符+扩展名）；排除 email
_AT_PATH_RE = re.compile(
    r"@("
    r"(?:[A-Za-z]:[/\\]|[/\\])[^\s\]\"'<>]+"
    r"|[^\s\]\"'<>]*[/\\][^\s\]\"'<>]+"
    r"|[^\s\]\"'<>]+\.(" + _CODE_DOC_EXTS + r")"
    r")",
    re.I,
)
_WIN_ABS_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'<>]+")
_REL_PATH_RE = re.compile(
    r"(?:[\w.一-鿿\-]+[/\\])+[\w.一-鿿\-]+\.(?:" + _CODE_DOC_EXTS + r")",
    re.I,
)
_EMAIL_RE = re.compile(r"(?<![/\\])\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")

NEED_READ_MARKER = "[[cc_need_read]]"
NEED_READ_NOTE = (
    "[[cc_need_read]]\n"
    "本轮用户引用了工作区文件，但上下文尚无文件正文。"
    "须先调用 Read（file_path 用绝对路径）再作答；禁止凭记忆编造文件内容。"
)
REHYDRATE_PREFIX = "(rehydrated from proxy read_cache)\n"


def is_wasted_call(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    return any(m in t[:500] for m in _WASTED_MARKERS)


def _is_context_placeholder(text: str | None) -> bool:
    t = (text or "").strip()
    return any(t.startswith(prefix) for prefix in _CONTEXT_PLACEHOLDER_PREFIXES)


class ReadCache:
    """JSON 落盘：{ users: { user_id: { norm_path: {path, content, chars, updated_at} } } }"""

    def __init__(
        self,
        store_path: Path,
        *,
        max_entries_per_user: int = 40,
        max_chars_per_entry: int = 2_000_000,
        min_chars: int = _MIN_CACHE_CHARS,
    ) -> None:
        self.store_path = Path(store_path)
        self.max_entries_per_user = max_entries_per_user
        self.max_chars_per_entry = max_chars_per_entry
        self.min_chars = min_chars
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        self._data = read_json_dict(self.store_path, lambda: {"users": {}})
        return self._data

    def _save(self) -> None:
        atomic_write_json(self.store_path, self._load())

    def put(self, user_id: str, path: str, content: str) -> bool:
        if not user_id or not path:
            return False
        text = content if isinstance(content, str) else str(content or "")
        if (
            len(text) < self.min_chars
            or not text.strip()
            or is_wasted_call(text)
            or _is_context_placeholder(text)
        ):
            return False
        if len(text) > self.max_chars_per_entry:
            # 守则 15：宁可下一枪重读，也不回放砍尾正文——重放前缀不带「已截断」标记，
            # 模型会把残缺文件当作完整状态。超限即拒绝入缓存。
            return False
        key = normalize_path(path)
        if not key:
            return False
        data = self._load()
        bucket = data.setdefault("users", {}).setdefault(user_id, {})
        bucket[key] = {
            "path": path,
            "content": text,
            "chars": len(text),
            "updated_at": time.time(),
        }
        if len(bucket) > self.max_entries_per_user:
            ordered = sorted(
                bucket.items(),
                key=lambda kv: float(kv[1].get("updated_at") or 0),
                reverse=True,
            )
            data["users"][user_id] = dict(ordered[: self.max_entries_per_user])
        self._save()
        return True

    def has_mutation(self, user_id: str, path: str, mutation_id: str) -> bool:
        """判断某个 tool_use 是否已作用于当前缓存快照。"""
        if not user_id or not path or not mutation_id:
            return False
        key = normalize_path(path)
        ent = ((self._load().get("users") or {}).get(user_id) or {}).get(key)
        applied = ent.get("applied_mutations") if isinstance(ent, dict) else None
        return isinstance(applied, list) and mutation_id in applied

    def mark_mutation(self, user_id: str, path: str, mutation_id: str) -> None:
        """在快照内保留有限的 mutation id，避免历史重扫重复 Edit/Write。"""
        if not user_id or not path or not mutation_id:
            return
        key = normalize_path(path)
        if not key:
            return
        data = self._load()
        ent = ((data.get("users") or {}).get(user_id) or {}).get(key)
        if not isinstance(ent, dict):
            return
        applied = ent.setdefault("applied_mutations", [])
        if not isinstance(applied, list):
            applied = ent["applied_mutations"] = []
        if mutation_id not in applied:
            applied.append(mutation_id)
            del applied[:-32]
            self._save()

    def get(self, user_id: str, path: str) -> str | None:
        key = normalize_path(path)
        if not key:
            return None
        ent = ((self._load().get("users") or {}).get(user_id) or {}).get(key)
        if not isinstance(ent, dict):
            return None
        c = ent.get("content")
        return c if isinstance(c, str) and c.strip() else None

    def delete(self, user_id: str, path: str) -> bool:
        """文件状态已变化但无法可靠重建时，删除旧快照，绝不重放陈旧正文。"""
        key = normalize_path(path)
        if not user_id or not key:
            return False
        data = self._load()
        bucket = (data.get("users") or {}).get(user_id)
        if not isinstance(bucket, dict) or key not in bucket:
            return False
        del bucket[key]
        self._save()
        return True


def id_to_path_from_text(text: str) -> dict[str, str]:
    """折叠文本 [tool_use] name=Read id=…\\n{json} → id→path。"""
    out: dict[str, str] = {}
    if not text:
        return out
    for m in _TOOL_USE_TEXT_RE.finditer(text):
        if (m.group(1) or "").strip().lower() != "read":
            continue
        tid = (m.group(2) or "").strip()
        raw_input = m.group(3) or ""
        try:
            parsed_input = json.loads(raw_input)
        except Exception:
            parsed_input = None
        if isinstance(parsed_input, dict) and (
            parsed_input.get("offset") is not None
            or parsed_input.get("limit") is not None
        ):
            continue
        p = path_from_tool_input(parsed_input if isinstance(parsed_input, dict) else raw_input)
        if tid and p:
            out[tid] = p
    return out


def _ingest_folded_tool_text(
    text: str, cache: ReadCache, user_id: str, id_to_path: dict[str, str]
) -> None:
    if not text:
        return
    id_to_path.update(id_to_path_from_text(text))
    for m in _TOOL_RESULT_TEXT_RE.finditer(text):
        tid = (m.group(2) or "").strip()
        body = m.group(3) or ""
        if not body.strip() or is_wasted_call(body):
            continue
        path = id_to_path.get(tid) or ""
        if path:
            cache.put(user_id, path, body)


def _ingest_system_trace(text: str, cache: ReadCache, user_id: str) -> None:
    if not text:
        return
    for part in re.split(r"(?=Called the\s+Read\s+tool)", text, flags=re.I):
        if not re.search(r"Called the\s+Read\s+tool", part, re.I):
            continue
        m = _CALLED_READ_RE.search(part)
        if not m:
            m2 = re.search(
                r"following input:\s*(\{.*?\})\s*Result of calling", part, re.I | re.S
            )
            if not m2:
                continue
            path = path_from_tool_input(m2.group(1))
            rest = part[m2.end() :]
        else:
            path = path_from_tool_input(m.group(1))
            rest = part[m.end() :]
        body = re.split(r"\nCalled the\s+", rest, maxsplit=1)[0]
        if path and body.strip() and not is_wasted_call(body):
            cache.put(user_id, path, body)


def _apply_cached_mutation(
    cache: ReadCache,
    user_id: str,
    *,
    name: str,
    path: str,
    inp: dict[str, Any],
    mutation_id: str = "",
) -> None:
    """成功 Write 更新快照；成功 Edit 能精确应用则更新，否则使旧快照失效。"""
    if not path:
        return
    if mutation_id and cache.has_mutation(user_id, path, mutation_id):
        return

    def _mark() -> None:
        cache.mark_mutation(user_id, path, mutation_id)

    n = (name or "").lower()
    if n == "write":
        content = inp.get("content")
        # delete 不可省：put 在正文过短/为空/命中 wasted 时会拒绝写入，
        # 少了前置 delete，编辑前的旧快照就留在缓存里并被重放。下同。
        cache.delete(user_id, path)
        if isinstance(content, str):
            cache.put(user_id, path, content)
        _mark()
        return
    if n != "edit":
        return

    old = inp.get("old_string")
    new = inp.get("new_string")
    current = cache.get(user_id, path)
    if not isinstance(old, str) or not isinstance(new, str) or not current:
        cache.delete(user_id, path)
        _mark()
        return
    if old in current:
        updated = current.replace(old, new, -1 if inp.get("replace_all") else 1)
        cache.delete(user_id, path)
        cache.put(user_id, path, updated)
        _mark()
        return
    # 同一历史会被每枪重扫；已应用过的 Edit 应保持幂等。
    if new and new in current:
        _mark()
        return
    cache.delete(user_id, path)
    _mark()


def ingest_messages_into_cache(
    body: dict[str, Any], cache: ReadCache, user_id: str
) -> dict[str, str]:
    """扫描 messages：成功 Read 写入缓存；返回 tool_use_id → file_path。"""
    id_to_path: dict[str, str] = {}
    tool_meta: dict[str, tuple[str, str, dict[str, Any]]] = {}
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return id_to_path

    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        role = m.get("role") or ""

        if role == "system":
            t = content if isinstance(content, str) else text_from_content(content)
            if t:
                _ingest_system_trace(t, cache, user_id)
                id_to_path.update(id_to_path_from_text(t))
            continue

        if not isinstance(content, list):
            if isinstance(content, str) and content.strip():
                _ingest_folded_tool_text(content, cache, user_id, id_to_path)
                _ingest_system_trace(content, cache, user_id)
            continue

        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "tool_use":
                name = str(b.get("name") or "").strip()
                tid = str(b.get("id") or "")
                raw_inp = b.get("input")
                inp = raw_inp if isinstance(raw_inp, dict) else {}
                p = path_from_tool_input(inp)
                if tid:
                    tool_meta[tid] = (name, p, inp)
                    if (
                        name.lower() == "read"
                        and p
                        and inp.get("offset") is None
                        and inp.get("limit") is None
                    ):
                        id_to_path[tid] = p
            elif btype == "tool_result":
                tid = str(b.get("tool_use_id") or b.get("id") or "")
                name, path, inp = tool_meta.get(tid) or ("", id_to_path.get(tid) or "", {})
                if b.get("is_error"):
                    continue
                inner = b.get("content")
                if isinstance(inner, list) and all(
                    isinstance(x, dict) and x.get("type") == "image"
                    for x in inner
                    if isinstance(x, dict)
                ):
                    continue
                text = text_from_content(inner)
                if (
                    name.lower() == "read"
                    and path
                    and inp.get("offset") is None
                    and inp.get("limit") is None
                    and text
                    and not is_wasted_call(text)
                ):
                    cache.put(user_id, path, text)
                elif name.lower() in ("write", "edit"):
                    _apply_cached_mutation(
                        cache,
                        user_id,
                        name=name,
                        path=path,
                        inp=inp,
                        mutation_id=tid,
                    )

        blob = text_from_content(content)
        if blob:
            if "Called the Read" in blob:
                _ingest_system_trace(blob, cache, user_id)
            if "[tool_use]" in blob:
                _ingest_folded_tool_text(blob, cache, user_id, id_to_path)

    return id_to_path


def rehydrate_text(
    text: str,
    cache: ReadCache,
    user_id: str,
    id_to_path: dict[str, str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Wasted call tool_result → 缓存正文；返回 (new_text, hits, misses)。"""
    if not text:
        return text, [], []
    id_to_path = id_to_path or {}
    hits: list[str] = []
    misses: list[str] = []

    def repl(m: re.Match[str]) -> str:
        head, tid, body = m.group(1), m.group(2).strip(), m.group(3)
        if not is_wasted_call(body):
            return m.group(0)
        path = id_to_path.get(tid) or ""
        cached = cache.get(user_id, path) if path else None
        if not cached:
            misses.append(path or tid or "?")
            marker = (
                "\n[[cc_read_cache:miss]] path={}\n".format(path)
                if path
                else "\n[[cc_read_cache:miss]] tool_use_id={}\n".format(tid)
            )
            return head + body.rstrip() + marker
        hits.append(path or tid)
        return head + REHYDRATE_PREFIX + cached + "\n"

    new_text, n = _TOOL_RESULT_TEXT_RE.subn(repl, text)
    if n == 0 and is_wasted_call(text):
        if len(id_to_path) == 1:
            path = next(iter(id_to_path.values()))
            cached = cache.get(user_id, path)
            if cached:
                hits.append(path)
                return REHYDRATE_PREFIX + cached, hits, misses
        misses.append("(whole_text)")
        return text + "\n[[cc_read_cache:miss]]\n", hits, misses
    return new_text, hits, misses


def rehydrate_body_payloads(
    *,
    query_user: str,
    tool_invocation: str,
    body: dict[str, Any],
    cache: ReadCache,
    user_id: str,
    history: str = "",
) -> dict[str, Any]:
    """先 ingest 成功读，再重放 query_user / Tool_invocation / History。"""
    # 先兼容旧式折叠文本，再以 Anthropic 结构化消息按时间重放；后者包含
    # Write/Edit，必须最后落地，避免旧 Read 正文反向覆盖新文件状态。
    id_to_path: dict[str, str] = {}
    for blob in (tool_invocation, history, query_user):
        if not blob:
            continue
        _ingest_folded_tool_text(blob, cache, user_id, id_to_path)
        _ingest_system_trace(blob, cache, user_id)
    id_to_path.update(ingest_messages_into_cache(body, cache, user_id))

    hits: list[str] = []
    misses: list[str] = []
    q2, h1, m1 = rehydrate_text(query_user or "", cache, user_id, id_to_path)
    t2, h2, m2 = rehydrate_text(tool_invocation or "", cache, user_id, id_to_path)
    hist2, h3, m3 = rehydrate_text(history or "", cache, user_id, id_to_path)
    hits.extend(h1 + h2 + h3)
    misses.extend(m1 + m2 + m3)
    return {
        "query_user": q2,
        "tool_invocation": t2,
        "history": hist2,
        "id_to_path": id_to_path,
        "hits": hits,
        "misses": misses,
    }


def _has_path_hint(query_user: str) -> bool:
    q = query_user or ""
    if not q.strip():
        return False
    scrubbed = _EMAIL_RE.sub(" ", q)
    return bool(
        _AT_PATH_RE.search(scrubbed)
        or _WIN_ABS_RE.search(scrubbed)
        or _REL_PATH_RE.search(scrubbed)
    )


def _tool_body_has_substance(read_evidence: str, min_chars: int) -> bool:
    ev = (read_evidence or "").strip()
    if not ev:
        return False
    if REHYDRATE_PREFIX.strip() in ev:
        return True

    if "Result of calling the Read tool" in ev:
        for part in re.split(r"Result of calling the\s+Read\s+tool:\s*", ev, flags=re.I)[1:]:
            body = re.split(r"\nCalled the\s+", part, maxsplit=1)[0].strip()
            if (
                len(body) >= min_chars
                and not is_wasted_call(body)
                and not _is_context_placeholder(body)
            ):
                return True

    read_ids = {
        (match.group(2) or "").strip()
        for match in _TOOL_USE_TEXT_RE.finditer(ev)
        if (match.group(1) or "").strip().lower() == "read"
    }
    for match in _TOOL_RESULT_TEXT_RE.finditer(ev):
        tool_use_id = (match.group(2) or "").strip()
        body = (match.group(3) or "").strip()
        if (
            tool_use_id in read_ids
            and len(body) >= min_chars
            and not is_wasted_call(body)
            and not _is_context_placeholder(body)
        ):
            return True
    return False


def should_annotate_need_read(
    query_user: str,
    read_evidence: str,
    *,
    min_chars: int = _MIN_CACHE_CHARS,
) -> bool:
    """本轮像引用了文件、但上下文尚无实质正文 → 需要强制 Read 提示。

    `read_evidence` 是 `Tool_invocation` 与 `Current_Context` 拼起来的证据串，不是
    某一个 INPUT_KEYS 键。此参曾以键名 `tool_invocation` 命名，读码者据名断定
    `Current_Context` 不参与本判定——形参名指向了比实参更窄的东西。

    「是否工具续写」由 query_user 是否以 tool_result 前缀起头判定，就在下一行；
    不再由调用方另算一遍同一表达式传进来。
    """
    q = (query_user or "").strip()
    if not q or q.startswith(TOOL_RESULT_PREFIX):
        return False
    if not _has_path_hint(q):
        return False
    return not _tool_body_has_substance(read_evidence or "", min_chars)
