from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_in_tz(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def slugify_text(value: str, max_len: int = 80) -> str:
    v = value.lower().strip()
    v = re.sub(r"[^a-z0-9\s-]", "", v)
    v = re.sub(r"\s+", "-", v)
    v = re.sub(r"-+", "-", v)
    return v[:max_len].strip("-") or "item"


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))
