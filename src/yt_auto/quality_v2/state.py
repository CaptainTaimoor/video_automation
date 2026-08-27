from __future__ import annotations

"""Durable, standard-library state for resumable quality-v2 jobs.

The store is intentionally small: SQLite is enough for one Windows machine,
survives subprocess restarts, and avoids a new service or dependency.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds")


class RetryClass(str, Enum):
    TRANSIENT = "transient"
    QUOTA = "quota"
    CONTENT = "content"
    PERMANENT = "permanent"


def classify_failure(status_code: int | None = None, message: str = "") -> RetryClass:
    """Classify a provider failure without depending on a provider SDK."""
    text = str(message or "").lower()
    code = int(status_code or 0)
    if code == 429 or any(token in text for token in ("rate limit", "quota", "too many requests")):
        return RetryClass.QUOTA
    if code in {408, 409, 425, 500, 502, 503, 504} or any(
        token in text
        for token in ("timeout", "timed out", "connection", "temporarily", "service unavailable")
    ):
        return RetryClass.TRANSIENT
    if code in {400, 404, 413, 422} or any(
        token in text
        for token in ("schema", "invalid json", "semantic validation", "unsupported", "safety")
    ):
        return RetryClass.CONTENT
    return RetryClass.PERMANENT


def content_hash(*parts: object) -> str:
    """Stable cache key for a fully specified stage input."""
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CircuitDecision:
    allowed: bool
    state: str
    retry_at: datetime | None = None


class QualityStateStore:
    """Persistent state for checkpoints, provider health and poison inputs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), timeout=15)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "QualityStateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              channel_id TEXT NOT NULL,
              content_kind TEXT NOT NULL,
              state TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
              job_id TEXT NOT NULL,
              stage TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              state TEXT NOT NULL,
              artifact_path TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (job_id, stage)
            );
            CREATE TABLE IF NOT EXISTS provider_health (
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              stage TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'closed',
              open_until TEXT,
              last_error_class TEXT NOT NULL DEFAULT '',
              recent_results_json TEXT NOT NULL DEFAULT '[]',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (provider, model, stage)
            );
            CREATE TABLE IF NOT EXISTS failure_fingerprints (
              fingerprint TEXT PRIMARY KEY,
              failures INTEGER NOT NULL DEFAULT 0,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              quarantined_until TEXT,
              last_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              cache_key TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              artifact_path TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        # Keep local shadow databases forward-compatible while this branch is
        # iterated. SQLite has no IF NOT EXISTS form for ADD COLUMN.
        provider_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(provider_health)").fetchall()
        }
        if "recent_results_json" not in provider_columns:
            self._connection.execute(
                "ALTER TABLE provider_health ADD COLUMN recent_results_json TEXT NOT NULL DEFAULT '[]'"
            )
        self._connection.commit()

    @staticmethod
    def _json(value: Mapping[str, Any] | None) -> str:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def start_job(
        self,
        job_id: str,
        channel_id: str,
        content_kind: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        now = _iso()
        self._connection.execute(
            """INSERT INTO jobs(job_id, channel_id, content_kind, state, payload_json, created_at, updated_at)
               VALUES(?, ?, ?, 'queued', ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET state='queued', payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (job_id, channel_id, content_kind, self._json(payload), now, now),
        )
        self._connection.commit()

    def set_job_state(self, job_id: str, state: str) -> None:
        self._connection.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE job_id=?", (state, _iso(), job_id)
        )
        self._connection.commit()

    def checkpoint(
        self,
        job_id: str,
        stage: str,
        input_key: str,
        state: str,
        artifact_path: str | Path = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        now = _iso()
        self._connection.execute(
            """INSERT INTO checkpoints(job_id, stage, input_hash, state, artifact_path, payload_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id, stage) DO UPDATE SET input_hash=excluded.input_hash, state=excluded.state,
                 artifact_path=excluded.artifact_path, payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (job_id, stage, input_key, state, str(artifact_path), self._json(payload), now, now),
        )
        self._connection.commit()

    def checkpoint_for(self, job_id: str, stage: str, input_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM checkpoints WHERE job_id=? AND stage=? AND input_hash=?",
            (job_id, stage, input_key),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
        return result

    def cache_artifact(
        self,
        cache_key: str,
        kind: str,
        artifact_path: str | Path,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._connection.execute(
            """INSERT INTO artifacts(cache_key, kind, artifact_path, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET artifact_path=excluded.artifact_path,
                 metadata_json=excluded.metadata_json""",
            (cache_key, kind, str(artifact_path), self._json(metadata), _iso()),
        )
        self._connection.commit()

    def cached_artifact(self, cache_key: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM artifacts WHERE cache_key=?", (cache_key,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def circuit_decision(
        self, provider: str, model: str, stage: str, now: datetime | None = None
    ) -> CircuitDecision:
        now = now or _utc_now()
        row = self._connection.execute(
            "SELECT state, open_until FROM provider_health WHERE provider=? AND model=? AND stage=?",
            (provider, model, stage),
        ).fetchone()
        if not row or row["state"] == "closed":
            return CircuitDecision(True, "closed")
        retry_at = self._parse_time(row["open_until"])
        if retry_at and retry_at > now:
            return CircuitDecision(False, "open", retry_at)
        return CircuitDecision(True, "half_open", retry_at)

    def record_provider_result(
        self,
        provider: str,
        model: str,
        stage: str,
        success: bool,
        failure_class: RetryClass | str | None = None,
        now: datetime | None = None,
    ) -> CircuitDecision:
        now = now or _utc_now()
        row = self._connection.execute(
            "SELECT * FROM provider_health WHERE provider=? AND model=? AND stage=?",
            (provider, model, stage),
        ).fetchone()
        attempts = int(row["attempts"] if row else 0) + 1
        failures = int(row["failures"] if row else 0) + (0 if success else 1)
        consecutive = 0 if success else int(row["consecutive_failures"] if row else 0) + 1
        error_class = str(failure_class.value if isinstance(failure_class, RetryClass) else failure_class or "")
        try:
            recent_results = list(json.loads(row["recent_results_json"] or "[]")) if row else []
        except (TypeError, ValueError):
            recent_results = []
        recent_results = [1 if bool(value) else 0 for value in recent_results[-10:]]
        if success:
            recent_results.append(1)
        elif error_class in {RetryClass.TRANSIENT.value, RetryClass.QUOTA.value}:
            recent_results.append(0)
        recent_results = recent_results[-10:]
        state = "closed"
        open_until: datetime | None = None
        # Content/schema failures should route to another model or repair input,
        # not make the infrastructure circuit look unhealthy.
        if not success and error_class in {RetryClass.TRANSIENT.value, RetryClass.QUOTA.value}:
            failure_rate = recent_results.count(0) / max(len(recent_results), 1)
            if consecutive >= 3 or (len(recent_results) >= 10 and failure_rate >= 0.5):
                state = "open"
                exponent = min(consecutive - 3, 4)
                open_until = now + timedelta(seconds=min(900, 60 * (2**max(0, exponent))))
        self._connection.execute(
            """INSERT INTO provider_health(provider, model, stage, attempts, failures, consecutive_failures,
                                              state, open_until, last_error_class, recent_results_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, model, stage) DO UPDATE SET attempts=excluded.attempts,
                 failures=excluded.failures, consecutive_failures=excluded.consecutive_failures,
                 state=excluded.state, open_until=excluded.open_until,
                 last_error_class=excluded.last_error_class,
                 recent_results_json=excluded.recent_results_json, updated_at=excluded.updated_at""",
            (
                provider,
                model,
                stage,
                attempts,
                failures,
                consecutive,
                state,
                _iso(open_until) if open_until else None,
                error_class,
                json.dumps(recent_results, separators=(",", ":")),
                _iso(now),
            ),
        )
        self._connection.commit()
        return CircuitDecision(state != "open", state, open_until)

    def quarantine(
        self,
        fingerprint: str,
        reason: str,
        now: datetime | None = None,
        threshold: int = 2,
        hold: timedelta = timedelta(hours=12),
    ) -> bool:
        """Record deterministic failures; return whether this input is now held."""
        now = now or _utc_now()
        row = self._connection.execute(
            "SELECT failures, first_seen FROM failure_fingerprints WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        failures = int(row["failures"] if row else 0) + 1
        quarantined_until = now + hold if failures >= threshold else None
        self._connection.execute(
            """INSERT INTO failure_fingerprints(fingerprint, failures, first_seen, last_seen, quarantined_until, last_reason)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET failures=excluded.failures, last_seen=excluded.last_seen,
                 quarantined_until=excluded.quarantined_until, last_reason=excluded.last_reason""",
            (
                fingerprint,
                failures,
                row["first_seen"] if row else _iso(now),
                _iso(now),
                _iso(quarantined_until) if quarantined_until else None,
                str(reason)[:500],
            ),
        )
        self._connection.commit()
        return bool(quarantined_until)

    def is_quarantined(self, fingerprint: str, now: datetime | None = None) -> bool:
        row = self._connection.execute(
            "SELECT quarantined_until FROM failure_fingerprints WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if not row:
            return False
        until = self._parse_time(row["quarantined_until"])
        return bool(until and until > (now or _utc_now()))
