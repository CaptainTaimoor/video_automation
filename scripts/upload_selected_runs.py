from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from yt_auto.pipeline import ShortsFactory
from yt_auto.uploaders.youtube import UploadLimitExceededError
from yt_auto.utils import write_json


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video_path(run_dir: Path, metadata: dict) -> Path:
    named = metadata.get("video_file")
    candidates = [run_dir / str(named)] if named else []
    candidates.extend((run_dir / "short.mp4", run_dir / "video.mp4"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No rendered MP4 found in {run_dir}")


def _thumbnail_path(run_dir: Path, metadata: dict) -> Path | None:
    named = metadata.get("thumbnail_file")
    candidates = [run_dir / str(named)] if named else []
    candidates.extend((run_dir / "thumbnail.jpg", run_dir / "thumbnail.png"))
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _load_manifest(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError("Upload manifest must contain a non-empty 'items' list")
    return [row for row in rows if isinstance(row, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotently upload explicitly selected completed runs to YouTube.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file)
    factory = ShortsFactory(args.config)
    items = _load_manifest(args.manifest)

    prepared: list[tuple[dict, object, Path, Path, Path | None, dict]] = []
    for item in items:
        run_dir = Path(str(item["run_dir"])).resolve()
        metadata_path = run_dir / "metadata.json"
        receipt_path = run_dir / "youtube_upload_receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
        if receipt.get("youtube_id"):
            print(json.dumps({"status": "already_uploaded", "run_dir": str(run_dir), **receipt}, ensure_ascii=False))
            continue

        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        channel = factory._channel(str(item["channel"]))
        if not channel.youtube or not channel.youtube.upload_enabled:
            raise RuntimeError(f"YouTube upload is disabled for {channel.id} in {args.config}")

        video_path = _video_path(run_dir, metadata)
        thumbnail_path = _thumbnail_path(run_dir, metadata)
        upload_metadata = deepcopy(metadata)
        old_title = str(upload_metadata.get("title") or "")
        upload_title = str(item.get("upload_title") or old_title).strip()
        upload_metadata["title"] = upload_title
        description = str(upload_metadata.get("description") or "")
        if old_title and upload_title != old_title:
            upload_metadata["description"] = description.replace(old_title, upload_title, 1)

        prepared.append((item, channel, run_dir, video_path, thumbnail_path, upload_metadata))
        print(json.dumps({
            "status": "ready",
            "channel": channel.id,
            "privacy": channel.youtube.privacy_status,
            "title": upload_title,
            "video": str(video_path),
            "bytes": video_path.stat().st_size,
        }, ensure_ascii=False))

    if args.check_only:
        return 0

    validated: set[str] = set()
    for _item, channel, run_dir, video_path, thumbnail_path, upload_metadata in prepared:
        receipt_path = run_dir / "youtube_upload_receipt.json"
        try:
            if channel.id not in validated:
                factory.youtube.validate_token(channel)
                validated.add(channel.id)
            video_id = factory.youtube.upload(
                channel=channel,
                video_path=video_path,
                metadata=upload_metadata,
                privacy_status=channel.youtube.privacy_status,
                thumbnail_path=thumbnail_path,
                is_short=str(upload_metadata.get("content_kind") or "").lower() == "short",
            )
            if not video_id:
                raise RuntimeError("YouTube returned no video id")

            uploaded_at = datetime.now(timezone.utc).isoformat()
            receipt = {
                "status": "uploaded",
                "youtube_id": video_id,
                "youtube_url": f"https://youtu.be/{video_id}",
                "channel": channel.id,
                "privacy": channel.youtube.privacy_status,
                "title": upload_metadata["title"],
                "video_sha256": _sha256(video_path),
                "uploaded_at": uploaded_at,
            }
            write_json(receipt_path, receipt)

            metadata_path = run_dir / "metadata.json"
            stored_metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            stored_metadata.update({
                "title": upload_metadata["title"],
                "description": upload_metadata.get("description", stored_metadata.get("description", "")),
                "youtube_video_id": video_id,
                "youtube_url": receipt["youtube_url"],
                "youtube_privacy": channel.youtube.privacy_status,
                "youtube_uploaded_at": uploaded_at,
            })
            write_json(metadata_path, stored_metadata)
            print(json.dumps(receipt, ensure_ascii=False))
        except UploadLimitExceededError as exc:
            write_json(receipt_path, {
                "status": "pending_quota_reset",
                "channel": channel.id,
                "title": upload_metadata["title"],
                "error": str(exc),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            print(json.dumps({"status": "pending_quota_reset", "run_dir": str(run_dir), "error": str(exc)}, ensure_ascii=False))
            return 75
        except Exception as exc:
            write_json(receipt_path, {
                "status": "failed",
                "channel": channel.id,
                "title": upload_metadata["title"],
                "error": str(exc),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            print(json.dumps({"status": "failed", "run_dir": str(run_dir), "error": str(exc)}, ensure_ascii=False))
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
