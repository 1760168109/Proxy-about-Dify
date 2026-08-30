# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from claude_hook import deliver_hook, install_hooks, merge_hook_settings


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_start_hook_forwards_additional_context():
    expected = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": "signed-marker",
        }
    }

    def opener(request, *, timeout):
        assert request.full_url.endswith("/hooks/subagent-start")
        assert timeout > 0
        return _Response(expected)

    result = deliver_hook(
        {
            "hook_event_name": "SubagentStart",
            "session_id": "parent",
            "agent_id": "agent",
        },
        opener=opener,
    )

    assert result == expected


def test_hook_network_failure_is_non_blocking(capsys):
    def broken_opener(_request, *, timeout):
        raise OSError("proxy is down")

    result = deliver_hook(
        {"hook_event_name": "SubagentStop"}, opener=broken_opener
    )

    assert result == {}
    assert "failed open" in capsys.readouterr().err


def test_settings_merge_preserves_existing_config_and_other_hooks():
    old_own_command = 'python "C:/proxy/claude_hook.py"'
    original = {
        "env": {"KEEP_SECRET": "unchanged"},
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "SubagentStart": [
                {"hooks": [{"type": "command", "command": "existing-start"}]},
                {"hooks": [{"type": "command", "command": old_own_command}]},
            ],
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "test"}]}
            ],
        },
    }

    hook_command = "python C:/proxy/claude_hook.py"
    merged = merge_hook_settings(original, command=hook_command)
    merged_twice = merge_hook_settings(merged, command=hook_command)

    assert merged["env"] == original["env"]
    assert merged["permissions"] == original["permissions"]
    assert merged["hooks"]["PostToolUse"] == original["hooks"]["PostToolUse"]
    start_commands = [
        item["command"]
        for entry in merged_twice["hooks"]["SubagentStart"]
        for item in entry.get("hooks", [])
    ]
    assert start_commands == ["existing-start", hook_command]
    stop_commands = [
        item["command"]
        for entry in merged_twice["hooks"]["SubagentStop"]
        for item in entry.get("hooks", [])
    ]
    assert stop_commands == [hook_command]


def test_settings_merge_removes_only_our_handler_from_mixed_matcher_group():
    original = {
        "hooks": {
            "SubagentStart": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python "C:/proxy/claude_hook.py"',
                        },
                        {"type": "command", "command": "keep-this-plugin"},
                    ],
                }
            ]
        }
    }

    merged = merge_hook_settings(original, command="python C:/proxy/claude_hook.py")

    start_entries = merged["hooks"]["SubagentStart"]
    assert start_entries[0] == {
        "matcher": ".*",
        "hooks": [
            {
                "type": "command",
                "command": "python C:/proxy/claude_hook.py",
                "timeout": 5,
            },
            {"type": "command", "command": "keep-this-plugin"},
        ],
    }


def test_settings_merge_preserves_same_filename_at_another_path():
    other = 'python "D:/another-tool/claude_hook.py" --lan-proxy-hook'
    current = 'python "C:/proxy/claude_hook.py" --lan-proxy-hook'
    original = {
        "hooks": {
            "SubagentStart": [
                {"hooks": [{"type": "command", "command": other}]},
            ]
        }
    }

    merged = merge_hook_settings(original, command=current)
    commands = [
        item["command"]
        for entry in merged["hooks"]["SubagentStart"]
        for item in entry.get("hooks", [])
    ]
    assert commands == [other, current]


def test_settings_merge_preserves_matcher_when_replacing_only_handler():
    current = 'python "C:/proxy/claude_hook.py" --lan-proxy-hook'
    original = {
        "hooks": {
            "SubagentStart": [
                {
                    "matcher": "Explore",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python "C:/proxy/claude_hook.py"',
                        }
                    ],
                }
            ]
        }
    }

    merged = merge_hook_settings(original, command=current)

    assert merged["hooks"]["SubagentStart"] == [
        {
            "matcher": "Explore",
            "hooks": [{"type": "command", "command": current, "timeout": 5}],
        }
    ]


def test_install_hooks_rejects_invalid_json_without_overwriting(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text("{broken", encoding="utf-8")

    try:
        install_hooks(settings)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid settings JSON must not be overwritten")

    assert settings.read_text(encoding="utf-8") == "{broken"
