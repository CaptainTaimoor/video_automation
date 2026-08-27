from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def validate_ollama_generate_url(url: str) -> str:
    """Return a normalized Ollama generate URL only when it is loopback-local."""
    candidate = str(url or "").strip()
    try:
        parsed = urlparse(candidate)
        # Accessing port rejects malformed and out-of-range values.
        _ = parsed.port
    except (TypeError, ValueError):
        raise RuntimeError("Ollama URL is not a valid absolute URL") from None

    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise RuntimeError("Ollama URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(
            "Ollama URL must not contain credentials, query parameters, or fragments"
        )

    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise RuntimeError("Ollama URL must use a loopback host")

    if (parsed.path or "").rstrip("/") != "/api/generate":
        raise RuntimeError("Ollama URL must use the /api/generate path")
    return candidate.rstrip("/")
