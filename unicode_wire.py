# -*- coding: utf-8 -*-
"""Dify 持久变量的非 BMP 无损表示与返回解码。

存在理由（2026-08-25 隔离双轮实测）：Dify Start 表单接受了
``"x" * 52000 + "💡"``，但同一 conversation 的下一轮在恢复变量时以
``208064 > 204800`` 拒绝；可逆表示为 ``⟦U+01F4A1⟧`` 后占用 104078，
两轮均成功。原因是 CPython 按字符串最高码点选择整串存储宽度，一个非 BMP
字符可能扩大整段文本的内存，而 ``max_length`` 与 HTTP 请求体大小都解释不了它。

因此本模块是一层跨轮持久化兼容协议，不是普通文本美化或通用压缩。删除或替换前
必须按《架构.md》“线缆层变更门槛”复跑真实 Dify 双轮对照，并保持
``tests/test_unicode_wire.py`` 与 ``tests/test_precepts.py`` 的端点契约通过。
"""
from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from typing import Any

WIRE_OPEN = "⟦"
WIRE_CLOSE = "⟧"
WIRE_MARKER = "[[cc_unicode_wire:on]]"
_WIRE_PREFIX = "U+"
_WIRE_HEX_DIGITS = 6
_HEX = frozenset("0123456789abcdefABCDEF")

# Dify conversation/environment variable 恢复路径的实测 CPython 内存上限。
# 此处不能改用 len、UTF-8 长度或 HTTP body 大小；三者对应不同边界。
DIFY_PERSISTED_VARIABLE_SIZE_LIMIT = 204_800


class DifyPersistenceSizeError(ValueError):
    """最终 input 会使下一轮 Dify conversation 恢复失败。"""

    def __init__(
        self,
        key: str,
        size: int,
        limit: int = DIFY_PERSISTED_VARIABLE_SIZE_LIMIT,
        *,
        sizes: dict[str, int] | None = None,
    ) -> None:
        self.key = key
        self.size = int(size)
        self.limit = int(limit)
        self.sizes = dict(sizes or {key: self.size})
        super().__init__(
            "Dify persisted input {!r} occupies {} bytes in Python memory; "
            "the restore limit is {}".format(key, size, limit)
        )


@dataclass(frozen=True)
class UnicodeWirePayload:
    query: str
    inputs: dict[str, str]
    active: bool
    non_bmp_count: int
    escaped_openers: int
    codepoints: tuple[int, ...]


@dataclass(frozen=True)
class _WireCandidate:
    output: str
    original: str
    replacements: int
    escaped_openers: int


def _codepoint_token(codepoint: int) -> str:
    return "{}{}{:06X}{}".format(WIRE_OPEN, _WIRE_PREFIX, codepoint, WIRE_CLOSE)


def encode_unicode_wire_text(text: str) -> tuple[str, int, int]:
    """返回（编码文本、非 BMP 替换数、字面左括号转义数）。"""
    parts: list[str] = []
    replacements = 0
    escaped_openers = 0
    for char in text or "":
        if char == WIRE_OPEN:
            parts.append(WIRE_OPEN + WIRE_OPEN)
            escaped_openers += 1
        elif ord(char) > 0xFFFF:
            parts.append(_codepoint_token(ord(char)))
            replacements += 1
        else:
            parts.append(char)
    return "".join(parts), replacements, escaped_openers


def _escape_wire_openers(text: str) -> tuple[str, int]:
    source = text or ""
    count = source.count(WIRE_OPEN)
    return source.replace(WIRE_OPEN, WIRE_OPEN + WIRE_OPEN), count


def encode_unicode_wire_payload(
    query: str,
    inputs: dict[str, Any] | None,
) -> UnicodeWirePayload:
    """逐变量选择更省内存的表示；启用后统一转义左括号以保证可逆。"""
    normalized = {
        str(key): value if isinstance(value, str) else str(value or "")
        for key, value in (inputs or {}).items()
    }
    candidates: dict[str, _WireCandidate] = {}
    active = False
    for key, value in normalized.items():
        encoded, replacements, encoded_openers = encode_unicode_wire_text(value)
        escaped, escaped_openers = _escape_wire_openers(value)
        use_encoded = bool(replacements) and sys.getsizeof(encoded) < sys.getsizeof(
            escaped
        )
        candidates[key] = _WireCandidate(
            output=encoded if use_encoded else escaped,
            original=value,
            replacements=replacements if use_encoded else 0,
            escaped_openers=encoded_openers if use_encoded else escaped_openers,
        )
        active = active or use_encoded

    if not active:
        return UnicodeWirePayload(
            query=query or "",
            inputs=normalized,
            active=False,
            non_bmp_count=0,
            escaped_openers=0,
            codepoints=(),
        )

    encoded_query, query_openers = _escape_wire_openers(query or "")
    codepoints: list[int] = []
    seen: set[int] = set()
    for candidate in candidates.values():
        if not candidate.replacements:
            continue
        for char in candidate.original:
            codepoint = ord(char)
            if codepoint > 0xFFFF and codepoint not in seen:
                seen.add(codepoint)
                codepoints.append(codepoint)

    return UnicodeWirePayload(
        query=encoded_query,
        inputs={key: candidate.output for key, candidate in candidates.items()},
        active=True,
        non_bmp_count=sum(item.replacements for item in candidates.values()),
        escaped_openers=query_openers
        + sum(item.escaped_openers for item in candidates.values()),
        codepoints=tuple(codepoints),
    )


def build_unicode_wire_note(
    codepoints: tuple[int, ...], *, max_items: int = 24
) -> str:
    shown = codepoints[: max(0, int(max_items))]
    labels = [
        "U+{:06X}={}".format(
            codepoint,
            unicodedata.name(chr(codepoint), "UNKNOWN"),
        )
        for codepoint in shown
    ]
    remaining = len(codepoints) - len(shown)
    if remaining > 0:
        labels.append("另有 {} 个码点（按 U+ 值理解）".format(remaining))
    return (
        "{}\n"
        "上下文中的 ⟦U+hhhhhh⟧ 是对应 Unicode 码点的无损线缆表示。"
        "理解时按码点还原；答复若需复现该字符，仍输出同一线缆表示，"
        "代理会在返回前还原。⟦⟦ 表示字面量左括号 ⟦。\n"
        "本轮码点：{}"
    ).format(WIRE_MARKER, "；".join(labels) if labels else "无")


class UnicodeWireStreamDecoder:
    """跨 SSE 分片识别线缆 token，保留不完整或非法的字面文本。"""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str, *, final: bool = False) -> str:
        source = self._buffer + (chunk or "")
        out: list[str] = []
        index = 0
        length = len(source)
        token_length = 1 + len(_WIRE_PREFIX) + _WIRE_HEX_DIGITS + 1

        while index < length:
            if source[index] != WIRE_OPEN:
                out.append(source[index])
                index += 1
                continue

            remaining = length - index
            if remaining == 1:
                if final:
                    out.append(WIRE_OPEN)
                    index += 1
                break
            if source[index + 1] == WIRE_OPEN:
                out.append(WIRE_OPEN)
                index += 2
                continue
            if source[index + 1] != "U":
                out.append(WIRE_OPEN)
                index += 1
                continue
            if remaining == 2:
                if final:
                    out.append(WIRE_OPEN)
                    index += 1
                break
            if source[index + 2] != "+":
                out.append(WIRE_OPEN)
                index += 1
                continue

            if remaining < token_length:
                available_hex = source[index + 3 :]
                if all(char in _HEX for char in available_hex):
                    if final:
                        out.append(WIRE_OPEN)
                        index += 1
                    break
                out.append(WIRE_OPEN)
                index += 1
                continue

            hex_text = source[index + 3 : index + 3 + _WIRE_HEX_DIGITS]
            close_index = index + token_length - 1
            if all(char in _HEX for char in hex_text) and source[close_index] == WIRE_CLOSE:
                codepoint = int(hex_text, 16)
                if 0x10000 <= codepoint <= 0x10FFFF:
                    out.append(chr(codepoint))
                    index += token_length
                    continue
            out.append(WIRE_OPEN)
            index += 1

        self._buffer = source[index:]
        if final and self._buffer:
            out.append(self._buffer)
            self._buffer = ""
        return "".join(out)


def decode_unicode_wire_text(text: str) -> str:
    return UnicodeWireStreamDecoder().feed(text or "", final=True)


def decode_unicode_wire_value(value: Any) -> Any:
    if isinstance(value, str):
        return decode_unicode_wire_text(value)
    if isinstance(value, list):
        return [decode_unicode_wire_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(decode_unicode_wire_value(item) for item in value)
    if isinstance(value, dict):
        return {
            decode_unicode_wire_value(key): decode_unicode_wire_value(item)
            for key, item in value.items()
        }
    return value


def persisted_input_sizes(inputs: dict[str, str] | None) -> dict[str, int]:
    return {key: sys.getsizeof(value) for key, value in (inputs or {}).items()}


def ensure_persisted_input_sizes(
    inputs: dict[str, str] | None,
    limit: int = DIFY_PERSISTED_VARIABLE_SIZE_LIMIT,
) -> dict[str, int]:
    """预防本轮成功后，同一 Dify conversation 在下一轮恢复失败。"""
    sizes = persisted_input_sizes(inputs)
    oversized = [(key, size) for key, size in sizes.items() if size > limit]
    if oversized:
        key, size = max(oversized, key=lambda item: item[1])
        raise DifyPersistenceSizeError(key, size, limit, sizes=sizes)
    return sizes
