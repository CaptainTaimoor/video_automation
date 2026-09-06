from __future__ import annotations

import os

from dotenv import load_dotenv


def required_env(name: str) -> str:
    load_dotenv()
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured in the environment")
    return value
