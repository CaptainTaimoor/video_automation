"""The shared store of footage that already passed the checks.

Downloading during a build is the slow part. On a modest line one clip takes
30-60 seconds and a Short needs about six, which is most of a scene's budget
spent waiting -- and when the budget runs out the scene falls back to a
generated card, which is what put grey slides into published videos.

The library is the answer to that: anything downloaded and verified once is
kept with the metadata that proved it, so a later build on a related subject
reads a file instead of asking a provider again.

It lives wherever ``app.visuals.library_root`` points, which is normally a
mounted Google Drive rather than the local disk -- the point is that the
machine's own storage stays free while the collection grows past what a laptop
would hold.

The on-disk format is the one the collection already uses: ``catalog.csv`` for
what is in it and ``usage.csv`` for what has been used where. Both are plain
CSV on purpose. They are readable without this program, survive a rewrite of
it, and a half-written row costs one asset rather than the index.
"""

from __future__ import annotations

import csv
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Long enough that a viewer will not notice a repeat, short enough that the
# collection does not have to be enormous before the bot can run a full day.
DEFAULT_REUSE_COOLDOWN_DAYS = 15

# A term shorter than this matches too much to be worth indexing.
MIN_TERM_LENGTH = 4

_WORD = re.compile(r"[a-z0-9]{%d,}" % MIN_TERM_LENGTH)

CATALOG_FIELDS = (
    "file", "channel", "subject", "search_query", "tags", "kind", "source",
    "license", "artist", "asset_title", "description", "source_page", "url",
    "asset_id", "media_width", "media_height", "orientation",
    "perceptual_hash", "added_at",
)
USAGE_FIELDS = ("url", "file", "channel", "subject", "run", "used_at")


def terms_of(*texts: str) -> set[str]:
    """The words worth indexing an asset under."""
    blob = " ".join(str(text or "") for text in texts).lower()
    return set(_WORD.findall(blob))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


class VisualLibrary:
    """Verified media on shared storage, indexed by channel and subject words."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        reuse_cooldown_days: int = DEFAULT_REUSE_COOLDOWN_DAYS,
    ) -> None:
        self.base = Path(root) if root else Path.cwd() / ".runtime" / "visual_library"
        self.reuse_cooldown_days = max(0, int(reuse_cooldown_days))
        self._catalog: list[dict[str, str]] | None = None
        self._used_at: dict[str, datetime] | None = None

    # -- paths ---------------------------------------------------------------

    @property
    def catalog_path(self) -> Path:
        return self.base / "catalog.csv"

    @property
    def usage_path(self) -> Path:
        return self.base / "usage.csv"

    @property
    def available(self) -> bool:
        """Whether the store is actually reachable right now.

        A mounted drive can disappear between builds, and a missing library is
        a slower build rather than a broken one, so every caller checks this
        instead of handling an exception.
        """
        try:
            return self.catalog_path.is_file()
        except OSError:
            return False

    # -- reading -------------------------------------------------------------

    def catalog(self, refresh: bool = False) -> list[dict[str, str]]:
        if self._catalog is not None and not refresh:
            return self._catalog
        rows: list[dict[str, str]] = []
        try:
            with self.catalog_path.open("r", encoding="utf-8", errors="replace",
                                        newline="") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("file") or "").strip():
                        rows.append(row)
        except (OSError, csv.Error):
            rows = []
        self._catalog = rows
        return rows

    def used_at(self, refresh: bool = False) -> dict[str, datetime]:
        """The last time each file was used, newest wins."""
        if self._used_at is not None and not refresh:
            return self._used_at
        seen: dict[str, datetime] = {}
        try:
            with self.usage_path.open("r", encoding="utf-8", errors="replace",
                                      newline="") as handle:
                for row in csv.DictReader(handle):
                    key = str(row.get("file") or "").strip()
                    stamp = _parse_time(row.get("used_at", ""))
                    if not key or stamp is None:
                        continue
                    if key not in seen or stamp > seen[key]:
                        seen[key] = stamp
        except (OSError, csv.Error):
            seen = {}
        self._used_at = seen
        return seen

    def rested(self, file_name: str, *, now: datetime | None = None) -> bool:
        """Whether this asset has been out of use long enough to reuse."""
        if self.reuse_cooldown_days <= 0:
            return True
        last = self.used_at().get(str(file_name or "").strip())
        if last is None:
            return True
        return (now or _now()) - last >= timedelta(days=self.reuse_cooldown_days)

    # -- lookup --------------------------------------------------------------

    def lookup(
        self,
        channel_id: str,
        *texts: str,
        kind: str = "",
        limit: int = 12,
        exclude_files: Iterable[str] = (),
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Assets for this subject, best match first, rested ones only.

        Ranked by how many subject words an asset shares with the request, so
        a Tikal build prefers a Tikal photograph over a generic Maya one but
        still finds the generic one when that is all there is.
        """
        if not self.available:
            return []
        wanted = terms_of(*texts)
        skip = {str(name).strip() for name in exclude_files if str(name).strip()}
        scored: list[tuple[int, float, dict[str, str]]] = []
        for row in self.catalog():
            if channel_id and str(row.get("channel") or "").strip() != channel_id:
                continue
            if kind and str(row.get("kind") or "").strip() != kind:
                continue
            name = str(row.get("file") or "").strip()
            if not name or name in skip:
                continue
            if not self.rested(name, now=now):
                continue
            path = self.base / name
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            filed_under = terms_of(row.get("subject", ""), row.get("tags", ""),
                                   row.get("asset_title", ""), row.get("search_query", ""))
            # What a vision model saw in the frame. A match here is worth
            # more than a match on the search words, because it is about the
            # picture rather than about how the picture was found: a scene
            # about carved stelae should get the stela, not the jungle skyline
            # that was also filed under Tikal.
            shows = terms_of(row.get("description", ""))
            overlap = len(wanted & filed_under) + 2 * len(wanted & shows)
            if wanted and not overlap:
                continue
            # A newer asset wins a tie: the collection grows towards what the
            # channels actually cover.
            added = _parse_time(row.get("added_at", ""))
            scored.append((overlap, added.timestamp() if added else 0.0, row))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        out: list[dict[str, Any]] = []
        for _, _, row in scored[:max(0, int(limit))]:
            entry = dict(row)
            entry["path"] = self.base / str(row.get("file") or "")
            out.append(entry)
        return out

    # -- writing -------------------------------------------------------------

    def _append(self, path: Path, fields: tuple[str, ...], row: dict[str, Any]) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields,
                                        extrasaction="ignore")
                if fresh:
                    writer.writeheader()
                writer.writerow({key: row.get(key, "") for key in fields})
            return True
        except OSError:
            return False

    def record_use(self, entry: dict[str, Any], run_dir: Path | str = "") -> bool:
        """Note that this asset went into a build, so it can rest afterwards."""
        name = str(entry.get("file") or "").strip()
        if not name:
            return False
        ok = self._append(self.usage_path, USAGE_FIELDS, {
            "url": entry.get("url", ""),
            "file": name,
            "channel": entry.get("channel", ""),
            "subject": entry.get("subject", ""),
            "run": str(run_dir or ""),
            "used_at": _now().isoformat(timespec="seconds"),
        })
        if ok and self._used_at is not None:
            self._used_at[name] = _now()
        return ok

    def store(
        self,
        source_path: Path,
        *,
        channel_id: str,
        subject: str,
        meta: dict[str, Any],
        kind: str = "image",
        search_query: str = "",
    ) -> str:
        """Copy a verified asset in and catalogue it. Returns its library name."""
        if not str(channel_id or "").strip():
            return ""
        try:
            if not Path(source_path).is_file():
                return ""
        except OSError:
            return ""
        slug = re.sub(r"[^a-z0-9]+", "-", str(subject or "misc").lower()).strip("-")[:48]
        slug = slug or "misc"
        suffix = Path(source_path).suffix.lower() or ".jpg"
        stem = re.sub(r"[^a-f0-9]", "", str(meta.get("perceptual_hash") or ""))[:14]
        if not stem:
            stem = "%x" % abs(hash((str(meta.get("url") or ""), time.time())))
        name = f"{channel_id}/{slug}/{stem}{suffix}"
        target = self.base / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(Path(source_path).read_bytes())
        except OSError:
            return ""
        row = {
            "file": name,
            "channel": channel_id,
            "subject": subject,
            "search_query": search_query,
            "tags": " ".join(sorted(terms_of(subject, search_query,
                                             meta.get("asset_title", "")))),
            "kind": kind,
            "source": meta.get("source", ""),
            "license": meta.get("license", ""),
            "artist": meta.get("artist", ""),
            "asset_title": meta.get("asset_title", ""),
            "description": meta.get("description", ""),
            "source_page": meta.get("source_page", ""),
            "url": meta.get("url", ""),
            "asset_id": meta.get("asset_id", ""),
            "media_width": meta.get("media_width", ""),
            "media_height": meta.get("media_height", ""),
            "orientation": meta.get("orientation", ""),
            "perceptual_hash": meta.get("perceptual_hash", ""),
            "added_at": _now().isoformat(timespec="seconds"),
        }
        if not self._append(self.catalog_path, CATALOG_FIELDS, row):
            return ""
        if self._catalog is not None:
            self._catalog.append(row)
        return name

    def update_descriptions(self, captions: dict[str, tuple[str, list[str]]]) -> int:
        """Write vision-model descriptions and tags into the catalogue.

        The whole file is rewritten through a temporary copy and swapped in,
        so a drive that drops mid-write leaves the old catalogue intact rather
        than a truncated one.
        """
        if not captions:
            return 0
        rows = self.catalog(refresh=True)
        if not rows:
            return 0
        fields = list(rows[0].keys())
        for name in CATALOG_FIELDS:
            if name not in fields:
                fields.append(name)
        changed = 0
        for row in rows:
            key = str(row.get("file") or "").strip()
            if key not in captions:
                continue
            description, tags = captions[key]
            row["description"] = description
            merged = set(str(row.get("tags") or "").split())
            merged.update(terms_of(" ".join(tags)))
            row["tags"] = " ".join(sorted(merged))
            changed += 1
        if not changed:
            return 0
        tmp = self.catalog_path.with_suffix(".csv.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in fields})
            os.replace(tmp, self.catalog_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return 0
        self._catalog = rows
        return changed

    def descriptions_for(self, channel_id: str, *texts: str, limit: int = 12) -> list[str]:
        """What the library can show for a subject, for the script writer to read."""
        out = []
        for hit in self.lookup(channel_id, *texts, limit=limit * 3):
            text = str(hit.get("description") or "").strip()
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def has_asset(self, url: str = "", perceptual_hash: str = "") -> bool:
        """Whether this exact asset is already held, to avoid fetching twice."""
        url = str(url or "").strip()
        digest = str(perceptual_hash or "").strip()
        if not url and not digest:
            return False
        for row in self.catalog():
            if url and str(row.get("url") or "").strip() == url:
                return True
            if digest and str(row.get("perceptual_hash") or "").strip() == digest:
                return True
        return False

    # -- reporting -----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        rows = self.catalog()
        by_channel: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for row in rows:
            channel = str(row.get("channel") or "?")
            by_channel[channel] = by_channel.get(channel, 0) + 1
            kind = str(row.get("kind") or "?")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        now = _now()
        resting = sum(1 for row in rows if not self.rested(str(row.get("file") or ""), now=now))
        return {
            "root": str(self.base),
            "available": self.available,
            "assets": len(rows),
            "by_channel": by_channel,
            "by_kind": by_kind,
            "resting": resting,
            "reuse_cooldown_days": self.reuse_cooldown_days,
        }
