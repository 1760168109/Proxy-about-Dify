# -*- coding: utf-8 -*-
"""Dify 持久变量 Unicode 线缆的可逆性与内存边界。"""
from __future__ import annotations

import sys

import pytest

from unicode_wire import (
    DIFY_PERSISTED_VARIABLE_SIZE_LIMIT,
    DifyPersistenceSizeError,
    UnicodeWireStreamDecoder,
    decode_unicode_wire_text,
    encode_unicode_wire_payload,
    encode_unicode_wire_text,
    ensure_persisted_input_sizes,
)


def test_wire_text_round_trip_preserves_non_bmp_and_literal_tokens():
    source = "灯💡与字面⟦U+01F4A1⟧"

    encoded, replacements, escaped_openers = encode_unicode_wire_text(source)

    assert encoded == "灯⟦U+01F4A1⟧与字面⟦⟦U+01F4A1⟧"
    assert replacements == 1
    assert escaped_openers == 1
    assert decode_unicode_wire_text(encoded) == source


def test_stream_decoder_round_trips_at_every_chunk_boundary():
    source = "前⟦⟦缀⟦U+01F4A1⟧后"
    expected = "前⟦缀💡后"

    for split in range(len(source) + 1):
        decoder = UnicodeWireStreamDecoder()
        actual = decoder.feed(source[:split])
        actual += decoder.feed(source[split:], final=True)
        assert actual == expected


def test_payload_activates_only_when_wire_reduces_python_memory():
    large = "x" * 52_000 + "💡"
    payload = encode_unicode_wire_payload(
        "继续检查字面⟦标记",
        {"Tool_invocation": large, "Current_Context": "短💡"},
    )

    assert payload.active is True
    assert payload.non_bmp_count == 1
    assert payload.codepoints == (0x1F4A1,)
    assert sys.getsizeof(payload.inputs["Tool_invocation"]) < sys.getsizeof(large)
    assert decode_unicode_wire_text(payload.inputs["Tool_invocation"]) == large
    # 另一个字段单独编码并不省内存；全局启用后仍须保证它和 query 可逆。
    assert payload.inputs["Current_Context"] == "短💡"
    assert decode_unicode_wire_text(payload.inputs["Current_Context"]) == "短💡"
    assert decode_unicode_wire_text(payload.query) == "继续检查字面⟦标记"


def test_payload_stays_inactive_when_encoding_does_not_reduce_memory():
    payload = encode_unicode_wire_payload("问💡", {"Current_Context": "短💡"})

    assert payload.active is False
    assert payload.query == "问💡"
    assert payload.inputs == {"Current_Context": "短💡"}


def test_persisted_size_check_accepts_wired_control_and_rejects_plain_bmp():
    plain = "汉" * 110_000
    with pytest.raises(DifyPersistenceSizeError) as caught:
        ensure_persisted_input_sizes({"Current_Context": plain})

    assert caught.value.size == sys.getsizeof(plain)
    assert caught.value.size > DIFY_PERSISTED_VARIABLE_SIZE_LIMIT

    source = "x" * 52_000 + "💡"
    wired = encode_unicode_wire_payload("继续", {"Tool_invocation": source})
    sizes = ensure_persisted_input_sizes(wired.inputs)
    assert sizes["Tool_invocation"] <= DIFY_PERSISTED_VARIABLE_SIZE_LIMIT


def test_decoder_preserves_invalid_or_incomplete_wire_literals():
    source = "a⟦U+000041⟧b⟦U+01F4"
    assert decode_unicode_wire_text(source) == source
