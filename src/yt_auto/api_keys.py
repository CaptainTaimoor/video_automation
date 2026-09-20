"""Several API keys per provider, tried in order, with retries.

A single key is a single point of failure: when it hits its daily cap or the
provider has a bad ten minutes, narration stops and the build misses its slot.

The order is deliberate and matters more than it looks:

    same key, a few times  ->  next key  ->  next provider  ->  Edge

A provider being briefly unwell is far more common than a key being dead, so
moving to the next key on the first hiccup burns through a whole rotation for
a blip that a second attempt would have survived. Only once a key has really
failed does the next one get a turn.

Keys live in ``secrets/api_keys.json``, which is inside the gitignored secrets
directory. Nothing here reads or writes ``.env``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# Tried in this order. Edge is not listed: it is the floor under all of them
# and takes no key.
PROVIDER_ORDER: tuple[str, ...] = ("deepgram", "openrouter", "inworld", "gemini")

# Shown under each provider on the page, so the choice is obvious without
# going back through a chat log. Deepgram leads because its free credit lasts
# years rather than days.
PROVIDER_NOTES: dict[str, str] = {
    "deepgram": "Aura voices. $200 free credit, no card - years at this volume. Tried first.",
    "openrouter": "Many models behind one key. Note: no text-to-speech models, so this is for script writing.",
    "inworld": "Warm, natural voices. Free tier is small - about 70 minutes.",
    "gemini": "Google. Also writes the scripts. Free tier is small for voice.",
}

DEFAULT_ATTEMPTS_PER_KEY = 3

# How long a key sits out after it has failed all its attempts, so one dead
# key does not get retried on every beat of the same build.
KEY_COOLDOWN_SECONDS = 600


def store_path(root: Path | None = None) -> Path:
    base = Path(root or Path.cwd())
    return base / "secrets" / "api_keys.json"


@dataclass
class ApiKey:
    """One key. ``label`` is what the page shows; the value is never shown whole."""

    value: str
    label: str = ""
    model: str = ""
    version: str = ""
    paused: bool = False
    failed_until: float = 0.0

    @property
    def masked(self) -> str:
        """Enough to tell two keys apart, not enough to use one."""
        raw = str(self.value or "")
        if len(raw) <= 8:
            return "*" * len(raw)
        return f"{raw[:4]}{'*' * max(4, len(raw) - 8)}{raw[-4:]}"

    def available(self, *, now: float | None = None) -> bool:
        return not self.paused and (now or time.time()) >= self.failed_until

    def as_public(self) -> dict[str, Any]:
        """What the dashboard may see. The value is deliberately absent."""
        return {
            "label": self.label,
            "masked": self.masked,
            "model": self.model,
            "version": self.version,
            "paused": self.paused,
            "resting": not self.available(),
        }

    def as_stored(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "model": self.model,
            "version": self.version,
            "paused": self.paused,
        }


@dataclass
class KeyStore:
    """Every provider's keys, in the order they should be tried."""

    providers: dict[str, list[ApiKey]] = field(default_factory=dict)

    # -- loading and saving ---------------------------------------------------

    @classmethod
    def load(cls, root: Path | None = None) -> "KeyStore":
        path = store_path(root)
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt store must not stop the bot narrating; it falls back to
            # the environment and, past that, to Edge.
            return cls()
        providers: dict[str, list[ApiKey]] = {}
        for provider, rows in (payload.get("providers") or {}).items():
            keys = [
                ApiKey(
                    value=str(row.get("value") or ""),
                    label=str(row.get("label") or ""),
                    model=str(row.get("model") or ""),
                    version=str(row.get("version") or ""),
                    paused=bool(row.get("paused")),
                )
                for row in rows or []
                if str(row.get("value") or "").strip()
            ]
            if keys:
                providers[str(provider)] = keys
        return cls(providers=providers)

    def save(self, root: Path | None = None) -> Path:
        path = store_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "providers": {
                provider: [key.as_stored() for key in keys]
                for provider, keys in self.providers.items()
            }
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    # -- editing --------------------------------------------------------------

    def add(self, provider: str, value: str, **fields: Any) -> ApiKey:
        key = ApiKey(value=str(value), **fields)
        self.providers.setdefault(str(provider), []).append(key)
        return key

    def remove(self, provider: str, index: int) -> bool:
        keys = self.providers.get(str(provider)) or []
        if 0 <= index < len(keys):
            keys.pop(index)
            return True
        return False

    def set_paused(self, provider: str, index: int, paused: bool) -> bool:
        keys = self.providers.get(str(provider)) or []
        if 0 <= index < len(keys):
            keys[index].paused = bool(paused)
            return True
        return False

    # -- reading --------------------------------------------------------------

    def keys_for(self, provider: str, *, now: float | None = None) -> list[ApiKey]:
        return [key for key in self.providers.get(str(provider)) or [] if key.available(now=now)]

    def ordered_providers(self, *, include_empty: bool = False) -> list[str]:
        """Configured providers, preferred order first, then any extras.

        ``include_empty`` lists every provider the bot knows how to use, even
        those with no key yet: the page needs a card for each, or there is
        nowhere to paste the first key for a provider you have not used before.
        The rotation itself asks for the configured ones only.
        """
        if include_empty:
            known = list(PROVIDER_ORDER)
        else:
            known = [name for name in PROVIDER_ORDER if self.providers.get(name)]
        extra = sorted(set(self.providers) - set(PROVIDER_ORDER))
        return known + extra

    def public_view(self) -> dict[str, Any]:
        """The shape the dashboard renders. Never contains a key value."""
        return {
            "providers": [
                {
                    "provider": provider,
                    "note": PROVIDER_NOTES.get(provider, ""),
                    "keys": [key.as_public() for key in self.providers.get(provider) or []],
                }
                for provider in self.ordered_providers(include_empty=True)
            ],
            "attempts_per_key": DEFAULT_ATTEMPTS_PER_KEY,
        }

    def merge_environment(self, env: dict[str, str] | None = None) -> int:
        """Adopt keys already in the environment, so nothing has to be retyped.

        Only adds what is missing, so a key edited on the page is never
        silently overwritten by a stale one in .env.
        """
        source = env if env is not None else os.environ
        added = 0
        for provider, variable in (
            ("deepgram", "DEEPGRAM_API_KEY"),
            ("openrouter", "OPENROUTER_API_KEY"),
            ("inworld", "INWORLD_API_KEY"),
            ("gemini", "GEMINI_API_KEY"),
        ):
            value = str(source.get(variable) or "").strip()
            if not value:
                continue
            if any(key.value == value for key in self.providers.get(provider) or []):
                continue
            self.add(provider, value, label="from .env")
            added += 1
        return added


def attempt_plan(
    store: KeyStore,
    *,
    attempts_per_key: int = DEFAULT_ATTEMPTS_PER_KEY,
    now: float | None = None,
) -> list[tuple[str, ApiKey, int]]:
    """Every (provider, key, attempt) in the order they should be tried.

    Spelling the whole rotation out as a list keeps the order testable without
    calling a provider, and makes it obvious that attempts on one key come
    before the next key rather than interleaved with it.
    """
    plan: list[tuple[str, ApiKey, int]] = []
    for provider in store.ordered_providers():
        for key in store.keys_for(provider, now=now):
            for attempt in range(1, max(1, int(attempts_per_key)) + 1):
                plan.append((provider, key, attempt))
    return plan


def render_with_rotation(
    store: KeyStore,
    render: Callable[[str, ApiKey, int], bool],
    *,
    fallback: Callable[[], bool] | None = None,
    attempts_per_key: int = DEFAULT_ATTEMPTS_PER_KEY,
    now: float | None = None,
    on_key_exhausted: Callable[[str, ApiKey], None] | None = None,
) -> tuple[bool, str]:
    """Try each key until one works, then fall back. Returns (ok, what_worked).

    ``render`` is given the provider, the key and which attempt this is, and
    returns True when it produced audio. Exceptions count as a failed attempt:
    a provider erroring is the ordinary case this exists for.
    """
    moment = now or time.time()
    for provider in store.ordered_providers():
        for key in store.keys_for(provider, now=moment):
            for attempt in range(1, max(1, int(attempts_per_key)) + 1):
                try:
                    if render(provider, key, attempt):
                        return True, f"{provider}:{key.label or key.masked}"
                except Exception:
                    pass
            # Every attempt on this key failed: rest it so the next beat of the
            # same build does not queue up behind it again.
            key.failed_until = moment + KEY_COOLDOWN_SECONDS
            if on_key_exhausted is not None:
                on_key_exhausted(provider, key)
    if fallback is not None and fallback():
        return True, "edge"
    return False, ""


def describe_plan(plan: Iterable[tuple[str, ApiKey, int]]) -> list[str]:
    """Readable rotation, for logs and for the page's "what happens next"."""
    return [f"{provider} {key.label or key.masked} try {attempt}" for provider, key, attempt in plan]
