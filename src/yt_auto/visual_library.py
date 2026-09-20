"""A local store of footage that already passed the checks.

Downloading during a build is the slow part. On a modest line one clip takes
30-60 seconds and a Short needs about six, which is most of a scene's budget
spent waiting -- and when the budget runs out the scene falls back to a
generated card.

Anything that has already been downloaded and verified once is worth keeping.
The library holds those files with the metadata that proved them, so a later
build on a related subject reads from disk instead of asking a provider again.

It lives under ``.runtime`` (gitignored), is capped, and evicts the
least-recently-used file when it gets too big. Losing the cache costs speed,
never correctness: every entry is re-verifiable from its stored metadata.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB, as configured on the old box

# A term shorter than this matches too much to be worth indexing.
MIN_TERM_LENGTH = 4

_WORD = re.compile(r"[a-z0-9]{%d,}" % MIN_TERM_LENGTH)


def library_root(root: Path | None = None) -> Path:
    return Path(root or Path.cwd()) / ".runtime" / "visual_library"


def terms_of(*texts: str) -> set[str]:
    """The words worth indexing an asset under."""
    blob = " ".join(str(text or "") for text in texts).lower()
    return set(_WORD.findall(blob))


class VisualLibrary:
    """Reusable verified media, indexed by channel and subject words."""

    def __init__(self, root: Path | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.base = library_root(root)
        self.max_bytes = int(max_bytes)

    # -- index ---------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.base / "index.json"

    def _load_index(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"entries": {}}
        if not isinstance(payload.get("entries"), dict):
            return {"entries": {}}
        return payload

    def _save_index(self, payload: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # -- writing -------------------------------------------------------------

    def store(
        self,
        source_file: Path,
        *,
        channel_id: str,
        meta: dict[str, str],
        terms: Iterable[str] = (),
        now: float | None = None,
    ) -> Path | None:
        """Copy a verified asset in. Returns its path in the library.

        Storing is best-effort: a cache that cannot be written must never stop
        a build that has already produced the file it needs.
        """
        source = Path(source_file)
        if not source.exists() or not source.is_file():
            return None
        asset_id = str(meta.get("asset_id") or "").strip() or source.name
        key = f"{channel_id}:{asset_id}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:120]
        target = self.base / channel_id / f"{safe}{source.suffix.lower()}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
        except OSError:
            return None

        index = self._load_index()
        indexed = set(terms_of(*terms)) | terms_of(
            meta.get("asset_title", ""), meta.get("source_page", "")
        )
        index["entries"][key] = {
            "path": str(target),
            "channel": channel_id,
            "terms": sorted(indexed),
            "meta": dict(meta),
            "stored_at": now or time.time(),
            "used_at": now or time.time(),
        }
        try:
            self._save_index(index)
        except OSError:
            return None
        self.prune()
        return target

    # -- reading -------------------------------------------------------------

    def lookup(
        self,
        channel_id: str,
        terms: Iterable[str],
        *,
        limit: int = 8,
        now: float | None = None,
    ) -> list[tuple[Path, dict[str, str]]]:
        """Stored assets for this channel that share words with ``terms``.

        Ordered by how many words they share, so the closest match leads.
        """
        wanted = set(terms_of(*terms))
        if not wanted:
            return []
        index = self._load_index()
        scored: list[tuple[int, float, str, dict[str, Any]]] = []
        for key, entry in (index.get("entries") or {}).items():
            if str(entry.get("channel") or "") != channel_id:
                continue
            overlap = len(wanted & set(entry.get("terms") or ()))
            if not overlap:
                continue
            path = Path(str(entry.get("path") or ""))
            if not path.exists():
                continue
            scored.append((overlap, float(entry.get("used_at") or 0.0), key, entry))

        scored.sort(key=lambda row: (-row[0], -row[1]))
        chosen = scored[: max(0, int(limit))]
        if chosen:
            moment = now or time.time()
            for _, _, key, _ in chosen:
                index["entries"][key]["used_at"] = moment
            try:
                self._save_index(index)
            except OSError:
                pass
        return [(Path(entry["path"]), dict(entry.get("meta") or {})) for _, _, _, entry in chosen]

    # -- housekeeping --------------------------------------------------------

    def total_bytes(self) -> int:
        total = 0
        for entry in (self._load_index().get("entries") or {}).values():
            try:
                total += Path(str(entry.get("path") or "")).stat().st_size
            except OSError:
                continue
        return total

    def prune(self) -> int:
        """Drop least-recently-used files until the library fits. Returns count."""
        index = self._load_index()
        entries = index.get("entries") or {}
        sized: list[tuple[float, str, Path, int]] = []
        total = 0
        for key, entry in entries.items():
            path = Path(str(entry.get("path") or ""))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            sized.append((float(entry.get("used_at") or 0.0), key, path, size))

        if total <= self.max_bytes:
            return 0

        sized.sort(key=lambda row: row[0])  # oldest use first
        removed = 0
        for _, key, path, size in sized:
            if total <= self.max_bytes:
                break
            try:
                path.unlink()
            except OSError:
                pass
            entries.pop(key, None)
            total -= size
            removed += 1
        index["entries"] = entries
        try:
            self._save_index(index)
        except OSError:
            return removed
        return removed

    def stats(self) -> dict[str, Any]:
        """What the dashboard shows: how much is stored and how big it is."""
        entries = self._load_index().get("entries") or {}
        by_channel: dict[str, int] = {}
        for entry in entries.values():
            channel = str(entry.get("channel") or "unknown")
            by_channel[channel] = by_channel.get(channel, 0) + 1
        return {
            "files": len(entries),
            "bytes": self.total_bytes(),
            "max_bytes": self.max_bytes,
            "by_channel": by_channel,
        }
