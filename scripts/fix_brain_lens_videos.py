"""
fix_brain_lens_videos.py

Step 1: Delete the old brain_lens token and re-authenticate the PERSONAL account
        with full YouTube management scope, then set all wrongly-uploaded videos
        to PRIVATE or DELETE them.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TOKEN_FILE        = Path(__file__).parent.parent / "secrets" / "tokens" / "brain_lens_token.json"
PERSONAL_TOKEN    = Path(__file__).parent.parent / "secrets" / "tokens" / "personal_fix_token.json"
CLIENT_SECRETS    = Path(__file__).parent.parent / "client_secrets.json"
RUNS_FILE         = Path(__file__).parent.parent / "data" / "state" / "runs.jsonl"

# Full management scope needed to update video privacy
FULL_SCOPES = ["https://www.googleapis.com/auth/youtube"]


def get_or_create_personal_credentials() -> Credentials:
    """Get credentials for the personal account with full YouTube management scope."""
    if PERSONAL_TOKEN.exists():
        try:
            data = json.loads(PERSONAL_TOKEN.read_text(encoding="utf-8"))
            creds = Credentials.from_authorized_user_info(data, FULL_SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                PERSONAL_TOKEN.write_text(creds.to_json(), encoding="utf-8")
                return creds
        except Exception:
            pass

    # Need fresh OAuth flow
    if not CLIENT_SECRETS.exists():
        print(f"ERROR: client_secrets.json not found at {CLIENT_SECRETS}")
        sys.exit(1)

    print("\nA browser window will open.")
    print("LOG IN WITH YOUR PERSONAL ACCOUNT (taimooramin1859)")
    print("to grant permission to manage your YouTube videos.\n")
    input("Press Enter to open the browser...")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), FULL_SCOPES)
    creds = flow.run_local_server(port=0)
    PERSONAL_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_brain_lens_video_ids() -> list[str]:
    ids = []
    for line in RUNS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run.get("channel") == "brain_lens":
            vid = (run.get("youtube_id") or "").strip()
            if vid:
                ids.append(vid)
    return list(dict.fromkeys(ids))


def set_videos_private(service, video_ids: list[str]) -> list[str]:
    total = len(video_ids)
    success = 0
    failed = []

    for i, vid_id in enumerate(video_ids, 1):
        print(f"[{i}/{total}] Setting private: {vid_id} ...", end=" ", flush=True)
        try:
            service.videos().update(
                part="status",
                body={
                    "id": vid_id,
                    "status": {"privacyStatus": "private"},
                },
            ).execute()
            print("OK")
            success += 1
            time.sleep(0.4)
        except HttpError as exc:
            code = exc.resp.status
            if code == 404:
                print("NOT FOUND (already deleted or not on this account)")
            elif code == 403:
                print(f"FORBIDDEN - {exc}")
                failed.append(vid_id)
            else:
                print(f"HTTP {code}: {exc}")
                failed.append(vid_id)
        except Exception as exc:
            print(f"ERROR: {exc}")
            failed.append(vid_id)

    print(f"\nDone! {success}/{total} videos set to PRIVATE.")
    return failed


def main() -> None:
    print("=" * 60)
    print("Brain Lens Video Privacy Fixer")
    print("=" * 60)

    video_ids = get_brain_lens_video_ids()
    print(f"Found {len(video_ids)} Brain Lens videos uploaded to personal account.\n")

    creds = get_or_create_personal_credentials()
    service = build("youtube", "v3", credentials=creds)

    try:
        me = service.channels().list(part="snippet", mine=True).execute()
        ch = (me.get("items") or [{}])[0].get("snippet", {})
        print(f"Logged in as: {ch.get('title', 'Unknown')}")
        print(f"These {len(video_ids)} videos will be set to PRIVATE.\n")
    except Exception as exc:
        print(f"Could not verify account: {exc}")

    confirm = input(f"Set ALL {len(video_ids)} videos to PRIVATE on personal account? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    failed = set_videos_private(service, video_ids)

    if failed:
        print(f"\nFailed for {len(failed)} videos:")
        for v in failed:
            print(f"  https://youtube.com/watch?v={v}")

    print("\n" + "=" * 60)
    print("NEXT STEPS:")
    print("=" * 60)
    print("1. Delete the old Brain Lens token:")
    print(f"   del \"{TOKEN_FILE}\"")
    print("")
    print("2. Re-authenticate with the CORRECT Brain Lens channel:")
    print("   .venv\\Scripts\\python run.py auth --channel brain_lens")
    print("   --> Log in with: @Brainlens-1 account (NOT personal)")
    print("")
    print("3. Re-upload all videos to the correct channel:")
    print("   .venv\\Scripts\\python scripts\\reupload_brain_lens.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
