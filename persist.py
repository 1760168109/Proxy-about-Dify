# -*- coding: utf-8 -*-
"""本地状态落盘共用原语（sessions / meter / cache / terminal 共用）。

时间约定：`utc_now()` 供人读与展示；需要比较或排序的时间字段一律存浮点 epoch
（见 terminal 的 `created_epoch`、cache 的 `updated_at`）。例外是 sessions 的
LRU 键，它直接以 ISO 串排序——正确性依赖 utc_now 恒定宽度与恒定 +00:00 偏移，
改动 utc_now 的格式前须先迁移那一处。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """秒级 ISO UTC 时间戳。恒定宽度、恒定 +00:00 偏移，故字典序等于时序。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json_dict(
    path: Path, default_factory: Callable[[], dict[str, Any]] = dict
) -> dict[str, Any]:
    """读一份 JSON dict；缺失、空白、损坏或类型不符时用 default_factory() 兜底。

    容错口径集中在此一处：OSError（缺失与权限）、JSONDecodeError，以及
    UnicodeDecodeError——非 UTF-8 的损坏文件抛的是它，而它不属前两者，
    各存储自行 `except (OSError, JSONDecodeError)` 时会漏掉这一类。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return default_factory()
    if not raw.strip():
        return default_factory()
    try:
        data = json.loads(raw)
    except ValueError:
        return default_factory()
    return data if isinstance(data, dict) else default_factory()


def atomic_write_json(path: Path, data: Any) -> None:
    """tmp + replace 原子落盘：写盘中断不损坏原 JSON。父目录按需创建，
    故调用方无须自行 mkdir。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)
