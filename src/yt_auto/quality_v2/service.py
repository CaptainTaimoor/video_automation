from __future__ import annotations

"""Integration layer for the v2 quality controls.

It reads only durable manifests and never writes to legacy state.  The active
pipeline can run it in shadow mode first, then turn on enforcement once the
imported corpus and golden samples look correct.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

from .editorial import Claim, CorpusMemory, EditorialGate, EditorialReport, StoryBrief
from .state import QualityStateStore, content_hash
from .visuals import (
    AssetRecord,
    RightsStatus,
    SceneVisualPlan,
    VisualHistory,
    canonical_asset_id,
    deterministic_fallback_visual,
    validate_rights,
    validate_scene_coverage,
)


class QualityV2Service:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = QualityStateStore(self.state_dir / "quality_v2.sqlite")
        self.memory_path = self.state_dir / "quality_v2_corpus.json"
        self.memory = self._load_memory()
        self.gate = EditorialGate()

    def close(self) -> None:
        self.store.close()

    def _load_memory(self) -> CorpusMemory:
        try:
            raw = json.loads(self.memory_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return CorpusMemory(
            titles=[str(value) for value in raw.get("titles", []) if str(value).strip()],
            narrations=[str(value) for value in raw.get("narrations", []) if str(value).strip()],
        )

    def _save_memory(self) -> None:
        temp = self.memory_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(self.memory), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.memory_path)

    @staticmethod
    def _topic_text(topic: Any, field: str) -> str:
        return str(getattr(topic, field, "") or "")

    def claims_for_topic(self, topic: Any) -> tuple[Claim, ...]:
        sources = tuple(str(url) for url in (getattr(topic, "source_urls", []) or []) if str(url).strip())
        beats = list(getattr(topic, "narration_beats", []) or []) or [self._topic_text(topic, "narration")]
        claims: list[Claim] = []
        for index, beat in enumerate(beats):
            text = str(beat or "").strip()
            if not text:
                continue
            claim_id = hashlib.sha256(f"{index}:{text}".encode("utf-8")).hexdigest()[:16]
            claims.append(Claim(claim_id, text, sources, "approved" if sources else "unverified"))
        return tuple(claims)

    def brief_for_topic(self, topic: Any, claims: tuple[Claim, ...]) -> StoryBrief:
        beats = list(getattr(topic, "narration_beats", []) or [])
        hook = str(getattr(topic, "hook", "") or (beats[0] if beats else ""))
        payoff = str(beats[-1] if beats else self._topic_text(topic, "narration"))
        return StoryBrief(
            question=self._topic_text(topic, "title"),
            hook=hook,
            payoff=payoff,
            claim_ids=tuple(claim.claim_id for claim in claims),
            visual_promises=tuple(str(item) for item in (getattr(topic, "visual_captions", []) or [])[:8]),
        )

    def review_topic(self, topic: Any, history_required: bool = False) -> EditorialReport:
        claims = self.claims_for_topic(topic)
        brief = self.brief_for_topic(topic, claims)
        return self.gate.review(
            title=self._topic_text(topic, "title"),
            narration=self._topic_text(topic, "narration"),
            corpus=self.memory,
            claims=claims,
            brief=brief,
            content_kind=self._topic_text(topic, "content_kind") or "short",
            history_required=history_required,
        )

    def review_visual_sources(
        self,
        topic: Any,
        sources: list[dict[str, Any]],
        recent_urls: set[str] | None = None,
    ) -> dict[str, Any]:
        """Check manifest rights, same-episode reuse, and fallback coverage.

        The legacy fetcher already performs semantic visual relevance checks.  This
        complements it with small, deterministic checks that are safe to run on
        every candidate and require no vision model or GPU.
        """
        known_urls = {str(value).strip() for value in (recent_urls or set()) if str(value).strip()}
        previous = VisualHistory(
            AssetRecord(asset_id=url, source_url=url, rights="published")
            for url in known_urls
            if url.startswith(("http://", "https://"))
        )
        episode_ids: set[str] = set()
        issues: list[str] = []
        warnings: list[str] = []
        plans: list[SceneVisualPlan] = []
        subject = self._topic_text(topic, "subject") or self._topic_text(topic, "title")
        beats = list(getattr(topic, "narration_beats", []) or [])

        for index, raw_source in enumerate(sources):
            source = raw_source if isinstance(raw_source, dict) else {}
            source_type = str(source.get("source") or "unknown").strip().lower()
            source_url = str(source.get("url") or "").strip()
            asset_id = str(source.get("asset_id") or source_url or f"scene-{index + 1}").strip()
            rights = str(source.get("license") or "").strip()
            record = AssetRecord(
                asset_id=asset_id,
                source_url=source_url,
                rights=rights,
                license_url=str(source.get("license_url") or source.get("source_page") or ""),
                creator=str(source.get("artist") or ""),
                checksum=str(source.get("checksum") or ""),
                perceptual_hash=str(source.get("perceptual_hash") or ""),
                category=source_type,
            )
            local_or_generated = source_type.startswith("local_") or source_type in {
                "stable_horde",
                "pollinations_ai",
            }
            if local_or_generated:
                warnings.append(f"scene {index + 1} uses {source_type}; fallback provenance should be reviewed")
            else:
                rights_status = validate_rights(record)
                if rights_status == RightsStatus.REJECTED:
                    issues.append(f"scene {index + 1} lacks a usable visual rights record")
                elif rights_status == RightsStatus.REVIEW:
                    warnings.append(f"scene {index + 1} uses a share-alike visual license")

            if source_url and source_url in known_urls:
                issues.append(f"scene {index + 1} repeats a recently published visual")
            reuse = previous.evaluate(record, episode_ids)
            if not reuse.allowed:
                issues.append(f"scene {index + 1} {reuse.reason}")
            episode_ids.add(canonical_asset_id(record))

            beat_text = str(beats[index % len(beats)] if beats else "")
            plans.append(
                SceneVisualPlan(
                    beat_id=str(source.get("scene_index") or index + 1),
                    primary_asset_id=asset_id if source_url or source.get("asset_id") else "",
                    fallback_kind=deterministic_fallback_visual(subject, beat_text),
                    duration_seconds=1.0,
                    visual_purpose=str(source.get("asset_title") or beat_text)[:160],
                )
            )

        issues.extend(validate_scene_coverage(plans))
        return {
            "approved": not issues,
            "issues": list(dict.fromkeys(issues)),
            "warnings": list(dict.fromkeys(warnings)),
            "asset_count": len(sources),
            "recent_asset_count": len(known_urls),
        }

    def record_approved(self, topic: Any) -> None:
        title = self._topic_text(topic, "title")
        narration = self._topic_text(topic, "narration")
        self.memory.add(title, narration)
        self._save_memory()
        self.store.cache_artifact(
            content_hash("approved-script", title, narration),
            "approved_script",
            "corpus",
            {"title": title, "words": len(narration.split())},
        )

    def import_history(self, runs_path: Path) -> dict[str, int]:
        """Import only successfully uploaded topics from a legacy run log."""
        runs_path = Path(runs_path)
        imported = 0
        skipped = 0
        seen_dirs: set[str] = set()
        try:
            lines = runs_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"imported": 0, "skipped": 0, "missing": 1}
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not row.get("uploaded"):
                continue
            run_dir = str(row.get("run_dir") or "")
            if not run_dir or run_dir in seen_dirs:
                continue
            seen_dirs.add(run_dir)
            topic_path = Path(run_dir) / "topic.json"
            try:
                topic = json.loads(topic_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                skipped += 1
                continue
            title = str(topic.get("title") or row.get("title") or "")
            narration = str(topic.get("narration") or "")
            if not title or not narration:
                skipped += 1
                continue
            self.memory.add(title, narration)
            imported += 1
        self._save_memory()
        return {"imported": imported, "skipped": skipped, "missing": 0}

    def status(self) -> dict[str, int]:
        return {"titles": len(self.memory.titles), "narrations": len(self.memory.narrations)}
