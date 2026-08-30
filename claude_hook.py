# -*- coding: utf-8 -*-
"""Claude Code SubagentStart/Stop command hook → lan 本地端点。

这个脚本运行在 Claude Code 的 hook 子进程中：它只负责把事件转发给本地
代理，并把代理返回的身份 marker 交还给 Claude Code；网络故障必须让 hook
放行，不能因为代理暂时不可用而阻断 Claude Code 自己的子代理生命周期。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from persist import atomic_write_json

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

HOOK_BASE_URL = (
    os.getenv("LAN_HOOK_BASE_URL") or "http://127.0.0.1:7272"
).rstrip("/")
HOOK_TIMEOUT_SECONDS = 3.0
HOOK_EVENTS = ("SubagentStart", "SubagentStop")
HOOK_OWNER_FLAG = "--lan-proxy-hook"
_HOOK_SCRIPT_RE = re.compile(
    r"(?i)(?:\"([^\"]*claude_hook\.py)\"|([^\s\"]*claude_hook\.py))"
)


def _endpoint(event: str) -> str | None:
    if event == "SubagentStart":
        return "/hooks/subagent-start"
    if event == "SubagentStop":
        return "/hooks/subagent-stop"
    return None


def _hook_command(script_path: Path = Path(__file__).resolve()) -> str:
    # Claude Code command hooks run through a shell. Windows paths may contain spaces;
    # shlex.quote is POSIX-shaped, so use explicit double quotes and escape the rare
    # embedded quote rather than relying on platform-dependent quoting.
    quoted = str(script_path).replace("\\", "/").replace('"', '\\"')
    return 'python "{}" {}'.format(quoted, HOOK_OWNER_FLAG)


def _hook_script_path(command: Any) -> str:
    match = _HOOK_SCRIPT_RE.search(str(command or ""))
    if not match:
        return ""
    raw = match.group(1) or match.group(2) or ""
    return raw.replace("\\", "/").rstrip("/").casefold()


def _is_our_hook_command(candidate: Any, installed_command: str) -> bool:
    """只认当前安装目标的完整脚本路径；不以文件名或 owner flag 单独认领。"""
    text = str(candidate or "")
    candidate_path = _hook_script_path(text)
    installed_path = _hook_script_path(installed_command)
    return bool(candidate_path and installed_path and candidate_path == installed_path)


def merge_hook_settings(
    settings: dict[str, Any], *, command: str | None = None
) -> dict[str, Any]:
    """保留全部既有配置，只幂等替换本项目的两个 command hook。"""
    # settings.json 可能同时承载用户的 permissions、env 和其它插件 hook；
    # 安装器只能识别并替换自己留下的 claude_hook.py 项，不能按事件整体覆盖。
    result = dict(settings)
    existing_hooks = result.get("hooks")
    hooks = dict(existing_hooks) if isinstance(existing_hooks, dict) else {}
    hook_command = command or _hook_command()

    for event in HOOK_EVENTS:
        raw_entries = hooks.get(event)
        entries = list(raw_entries) if isinstance(raw_entries, list) else []
        kept: list[Any] = []
        replacement_added = False
        for entry in entries:
            nested = entry.get("hooks") if isinstance(entry, dict) else None
            nested_hooks = nested if isinstance(nested, list) else []
            owns_entry = any(
                isinstance(item, dict)
                and _is_our_hook_command(item.get("command"), hook_command)
                for item in nested_hooks
            )
            if not owns_entry:
                kept.append(entry)
                continue

            # 一个 matcher 组可能同时承载用户/插件的多个 handlers；保留组级
            # matcher、其它 handlers 和原有位置，只替换本代理自己的 handler。
            new_hooks: list[Any] = []
            for item in nested_hooks:
                is_ours = (
                    isinstance(item, dict)
                    and _is_our_hook_command(item.get("command"), hook_command)
                )
                if is_ours:
                    if not replacement_added:
                        new_hooks.append(
                            {
                                "type": "command",
                                "command": hook_command,
                                "timeout": 5,
                            }
                        )
                        replacement_added = True
                    continue
                new_hooks.append(item)
            if new_hooks:
                kept_entry = dict(entry)
                kept_entry["hooks"] = new_hooks
                kept.append(kept_entry)
            else:
                # 只有本代理 handler 的旧组也要保留 matcher / 其它组级字段。
                kept_entry = dict(entry)
                kept_entry["hooks"] = [
                    {
                        "type": "command",
                        "command": hook_command,
                        "timeout": 5,
                    }
                ]
                kept.append(kept_entry)
        if not replacement_added:
            kept.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "timeout": 5,
                        }
                    ]
                }
            )
        hooks[event] = kept
    result["hooks"] = hooks
    return result


def install_hooks(settings_path: Path) -> dict[str, Any]:
    # 先合并再原子写回，保证重复运行 install.ps1 不会不断追加重复 hook，
    # 也不会在写入中断时留下半份 settings.json。
    path = Path(settings_path)
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if raw.strip():
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Claude settings root must be a JSON object")
        else:
            data = {}
    else:
        data = {}
    merged = merge_hook_settings(data)
    atomic_write_json(path, merged)
    return merged


def deliver_hook(
    payload: dict[str, Any],
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """转发 hook；网络/代理故障返回空 JSON，让 Claude Code fail-open。"""
    # Stop 事件只需要落档，不向 CC 注入额外上下文；Start 事件才返回
    # additionalContext。这样报告档案不会被误当成子代理提示词。
    event = str(payload.get("hook_event_name") or "")
    endpoint = _endpoint(event)
    if endpoint is None:
        return {}
    headers = {"Content-Type": "application/json"}
    admin_token = (os.getenv("ADMIN_TOKEN") or "").strip()
    if admin_token:
        headers["x-api-key"] = admin_token
    request = urllib.request.Request(
        HOOK_BASE_URL + endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=HOOK_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
        result = json.loads(raw)
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        print("[lan-hook] {} failed open: {}".format(event, exc), file=sys.stderr)
        return {}
    if not isinstance(result, dict):
        return {}
    return result if event == "SubagentStart" else {}


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--install":
        settings_path = (
            Path(sys.argv[2])
            if len(sys.argv) >= 3
            else Path.home() / ".claude" / "settings.json"
        )
        try:
            install_hooks(settings_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print("[lan-hook] install failed: {}".format(exc), file=sys.stderr)
            return 1
        print("[lan-hook] installed SubagentStart / SubagentStop")
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, UnicodeError) as exc:
        print("[lan-hook] invalid stdin failed open: {}".format(exc), file=sys.stderr)
        print("{}")
        return 0
    result = deliver_hook(payload if isinstance(payload, dict) else {})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
