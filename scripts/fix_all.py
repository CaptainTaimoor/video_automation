"""
fix_all.py  -  Run this ONE script to fix everything.

What it does (automatically):
  1. Opens browser -> Log in as PERSONAL account -> hides all wrong videos
  2. Opens browser -> Log in as BRAIN LENS channel -> fixes future uploads
  3. Queues all videos into the backlog (scheduler uploads 1 per hour)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT             = Path(__file__).parent.parent
TOKEN_DIR        = ROOT / "secrets" / "tokens"
CLIENT_SECRETS   = ROOT / "client_secrets.json"
RUNS_FILE        = ROOT / "data" / "state" / "runs.jsonl"
BRAIN_LENS_TOKEN = TOKEN_DIR / "brain_lens_token.json"
PERSONAL_TOKEN   = TOKEN_DIR / "_personal_fix_token.json"

MANAGE_SCOPE  = ["https://www.googleapis.com/auth/youtube"]
UPLOAD_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def sep(msg: str = "") -> None:
    print("\n" + "=" * 60)
    if msg:
        print(f"  {msg}")
        print("=" * 60)


def oauth_flow(token_path: Path, scopes: list[str], hint: str) -> Credentials:
    """Run OAuth browser flow and save token. Returns credentials."""
    if token_path.exists():
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            creds = Credentials.from_authorized_user_info(data, scopes)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
                return creds
        except Exception:
            pass

    print(f"\nOpening browser... {hint}")
    input("Press ENTER to open browser -> ")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), scopes)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_channel_name(service) -> str:
    try:
        r = service.channels().list(part="snippet", mine=True).execute()
        return (r.get("items") or [{}])[0].get("snippet", {}).get("title", "Unknown")
    except Exception:
        return "Unknown"


def get_brain_lens_runs() -> list[dict]:
    runs = []
    seen_ids: set[str] = set()
    for line in RUNS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run.get("channel") != "brain_lens":
            continue
        vid = (run.get("youtube_id") or "").strip()
        run_dir = Path(run.get("run_dir", ""))
        video_path = run_dir / "short.mp4"
        if not video_path.exists():
            video_path = run_dir / "video.mp4"
        metadata_path = run_dir / "metadata.json"
        if vid and vid not in seen_ids:
            seen_ids.add(vid)
            runs.append({
                "youtube_id": vid,
                "run_dir": run_dir,
                "video_path": video_path,
                "metadata_path": metadata_path,
                "title": run.get("title", ""),
                "has_video": video_path.exists(),
                "has_metadata": metadata_path.exists(),
            })
    return runs


# ─────────────────────────────────────────────────────────────
# Step 1 - Hide all wrong videos on personal account
# ─────────────────────────────────────────────────────────────

def step1_hide_personal_videos(runs: list[dict]) -> None:
    sep("STEP 1 of 3: Hide wrong videos from your PERSONAL channel")
    print("""
A browser will open. Please:
  1. Log in as your PERSONAL account  (taimooramin1859)
  2. Click 'Allow' to grant YouTube management access
  3. Close the browser tab when you see the success page
""")

    creds = oauth_flow(PERSONAL_TOKEN, MANAGE_SCOPE,
                       hint="LOGIN AS: taimooramin1859 (personal account)")
    svc = build("youtube", "v3", credentials=creds)

    name = get_channel_name(svc)
    print(f"\nLogged in as: {name}")
    if "taimoor" not in name.lower() and "personal" not in name.lower() and "amin" not in name.lower():
        print(f"WARNING: Expected personal account but got '{name}'. Continuing anyway.")

    video_ids = [r["youtube_id"] for r in runs]
    total = len(video_ids)
    done = 0

    print(f"\nSetting {total} videos to PRIVATE...\n")
    for i, vid in enumerate(video_ids, 1):
        print(f"  [{i}/{total}] {vid}", end=" ... ", flush=True)
        try:
            svc.videos().update(
                part="status",
                body={"id": vid, "status": {"privacyStatus": "private"}},
            ).execute()
            print("hidden")
            done += 1
            time.sleep(0.35)
        except HttpError as exc:
            code = exc.resp.status
            if code == 404:
                print("not found (skipped)")
            else:
                print(f"failed (HTTP {code})")
        except Exception as exc:
            print(f"error: {exc}")

    print(f"\nStep 1 complete: {done}/{total} videos hidden on personal channel.")


# ─────────────────────────────────────────────────────────────
# Step 2 - Re-authenticate Brain Lens with the correct channel
# ─────────────────────────────────────────────────────────────

def step2_fix_brain_lens_auth() -> None:
    sep("STEP 2 of 3: Connect Brain Lens to the CORRECT YouTube channel")
    print("""
A browser will open. Please:
  1. Log in as your BRAIN LENS account  (@Brainlens-1)
  2. Click 'Allow' to grant YouTube upload access
  3. Close the browser tab when you see the success page
""")

    # Delete the bad token first
    if BRAIN_LENS_TOKEN.exists():
        BRAIN_LENS_TOKEN.unlink()
        print("Removed old (wrong) token.\n")

    creds = oauth_flow(BRAIN_LENS_TOKEN, UPLOAD_SCOPES,
                       hint="LOGIN AS: Brain Lens channel (@Brainlens-1)")
    svc = build("youtube", "v3", credentials=creds)

    name = get_channel_name(svc)
    print(f"\nConnected channel: {name}")
    print("Step 2 complete: Brain Lens is now authenticated correctly.")


# ─────────────────────────────────────────────────────────────
# Step 3 - Add all videos to the backlog (scheduler uploads 1/hr)
# ─────────────────────────────────────────────────────────────

def step3_populate_backlog(runs: list[dict]) -> None:
    sep("STEP 3 of 3: Queue videos into the backlog (uploads 1/hour automatically)")

    uploadable = [r for r in runs if r["has_video"] and r["has_metadata"]]
    print(f"Found {len(uploadable)} videos with local files ready to queue.\n")

    if not uploadable:
        print("No local video files found. Nothing to queue.")
        return

    backlog_path = ROOT / "data" / "state" / "brain_lens_backlog.json"

    # Load existing backlog
    existing = {}
    if backlog_path.exists():
        try:
            data = json.loads(backlog_path.read_text(encoding="utf-8"))
            for item in data.get("items", []):
                existing[item.get("run_dir", "")] = item
        except Exception:
            pass

    queued = 0
    skipped = 0
    for run in uploadable:
        run_dir_str = str(run["run_dir"])
        metadata = json.loads(run["metadata_path"].read_text(encoding="utf-8"))
        title = metadata.get("title") or run["title"] or "Brain Lens Video"

        if run_dir_str in existing:
            existing_item = existing[run_dir_str]
            # Reset to pending so it gets re-uploaded to the correct channel
            existing_item["youtube_id"] = None  # Clear wrong-account ID
            existing_item["status"] = "pending"
            existing_item["attempts"] = 0
            existing_item["error"] = None
            queued += 1
        else:
            # New backlog entry
            existing[run_dir_str] = {
                "run_dir": run_dir_str,
                "video_path": str(run["video_path"]),
                "metadata_path": str(run["metadata_path"]),
                "title": title,
                "status": "pending",
                "attempts": 0,
                "youtube_id": None,
                "facebook_id": None,
                "error": None,
            }
            queued += 1

    # Save updated backlog
    items = list(existing.values())
    backlog_path.write_text(
        json.dumps({"channel": "brain_lens", "items": items}, indent=2),
        encoding="utf-8"
    )

    print(f"Queued {queued} videos into the backlog.")
    print(f"The scheduler will upload 1 video per hour automatically.")
    print(f"At 1/hour, all {queued} videos will be uploaded in ~{queued} hours.")



# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    sep("Brain Lens Full Fix Script")
    print("""
This script will:
  1. Hide wrong videos from your personal YouTube channel
  2. Re-connect Brain Lens to the correct YouTube channel
  3. Queue all videos into the backlog (scheduler uploads 1/hour)

You only need to log into 2 browser windows when asked.
Everything else is automatic.
""")
    input("Press ENTER to begin -> ")

    runs = get_brain_lens_runs()
    print(f"Found {len(runs)} Brain Lens video entries in the run log.\n")

    step1_hide_personal_videos(runs)
    step2_fix_brain_lens_auth()
    step3_populate_backlog(runs)

    sep("ALL DONE!")
    print("""
Summary:
  - Brain Lens videos hidden from personal channel
  - Brain Lens is now connected to the correct YouTube channel
  - All videos queued into backlog (uploads 1/hour automatically)

Start the scheduler to begin uploading:
  python run.py schedule --upload
""")


if __name__ == "__main__":
    main()
