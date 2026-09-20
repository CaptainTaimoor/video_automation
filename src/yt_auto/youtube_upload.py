from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from yt_auto.models import ChannelConfig
from yt_auto.utils import ensure_dir


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]



def _rfc3339_utc(moment: datetime) -> str:
    """YouTube wants UTC with a trailing Z; a naive time is read as local."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UploadLimitExceededError(RuntimeError):
    pass


class UploadAuthError(RuntimeError):
    pass


def _is_upload_limit_error_message(message: str) -> bool:
    text = (message or "").lower()
    return "uploadlimitexceeded" in text or "exceeded the number of videos they may upload" in text


def _is_invalid_grant_message(message: str) -> bool:
    text = (message or "").lower()
    return "invalid_grant" in text or "expired or revoked" in text


class YouTubeUploader:
    def __init__(self) -> None:
        self._services: Dict[str, object] = {}

    def _load_saved_credentials(self, token_file: Path):
        if not token_file.exists():
            return None
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
        except Exception:
            return None

        # Users sometimes place a copy of client_secrets.json into the token path.
        # That file has an "installed" block, not an authorized-user refresh token.
        if isinstance(data, dict) and ("installed" in data or "web" in data):
            return None

        try:
            creds = Credentials.from_authorized_user_info(data, SCOPES)
        except Exception:
            return None
        if creds and not creds.has_scopes(SCOPES):
            return None
        return creds

    def validate_token(self, channel: ChannelConfig) -> None:
        token_file = channel.youtube.token_file
        creds = self._load_saved_credentials(token_file)
        if not creds:
            raise UploadAuthError(f"Missing or invalid token for {channel.id}. Run: python run.py auth --channel {channel.id}")

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                if _is_invalid_grant_message(str(exc)):
                    try:
                        token_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise UploadAuthError(
                        f"OAuth token for {channel.id} was expired/revoked. Re-authorize: python run.py auth --channel {channel.id}"
                    ) from exc
                raise
            token_file.write_text(creds.to_json(), encoding="utf-8")

        if not creds.valid:
            raise UploadAuthError(f"OAuth token for {channel.id} is not valid. Re-authorize: python run.py auth --channel {channel.id}")

    def _obtain_credentials(self, channel: ChannelConfig):
        flow = InstalledAppFlow.from_client_secrets_file(
            str(channel.youtube.client_secrets_file),
            SCOPES,
        )
        creds = flow.run_local_server(port=0)
        ensure_dir(channel.youtube.token_file.parent)
        channel.youtube.token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _service_for_channel(self, channel: ChannelConfig):
        if channel.id in self._services:
            return self._services[channel.id]

        token_file = channel.youtube.token_file
        creds = self._load_saved_credentials(token_file)

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                if _is_invalid_grant_message(str(exc)):
                    try:
                        token_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise UploadAuthError(
                        f"OAuth token for {channel.id} was expired/revoked. Re-authorize: python run.py auth --channel {channel.id}"
                    ) from exc
                raise
            token_file.write_text(creds.to_json(), encoding="utf-8")

        if not creds or not creds.valid:
            print(f"[{channel.id}] OAuth authorization required. A browser window will open.")
            creds = self._obtain_credentials(channel)

        ensure_dir(token_file.parent)
        token_file.write_text(creds.to_json(), encoding="utf-8")

        service = build("youtube", "v3", credentials=creds)
        self._services[channel.id] = service
        return service

    def authorize(self, channel: ChannelConfig) -> dict:
        service = self._service_for_channel(channel)
        data = service.channels().list(part="snippet", mine=True).execute()
        items = data.get("items", [])
        if not items:
            return {"title": "", "channel_id": ""}
        item = items[0]
        return {
            "title": item.get("snippet", {}).get("title", ""),
            "channel_id": item.get("id", ""),
        }

    def upload(
        self,
        channel: ChannelConfig,
        video_path: Path,
        metadata: dict,
        privacy_status: str,
        thumbnail_path: Path | None = None,
        is_short: bool = False,
        publish_at: datetime | None = None,
    ) -> str:
        service = self._service_for_channel(channel)

        body = {
            "snippet": {
                "title": metadata["title"][:100],
                "description": metadata["description"][:5000],
                "tags": metadata.get("tags", [])[:30],
                "categoryId": metadata.get("category_id", "24"),
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        # A scheduled publish must be uploaded private; YouTube flips it public
        # itself at the given time, so the machine can be busy or off by then.
        # Both uploader classes in this repo take this argument: patching only
        # the one that looked current is how two finished videos were lost to
        # an unexpected-keyword error.
        if publish_at is not None:
            body["status"]["privacyStatus"] = "private"
            body["status"]["publishAt"] = _rfc3339_utc(publish_at)

        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)

        import time
        import http.client
        import ssl
        from googleapiclient.errors import HttpError, ResumableUploadError
        
        response = None
        error_count = 0
        while response is None:
            try:
                _, response = request.next_chunk()
                error_count = 0
            except HttpError as e:
                if _is_upload_limit_error_message(str(e)):
                    raise UploadLimitExceededError(str(e)) from e
                if e.resp.status in [500, 502, 503, 504]:
                    error_count += 1
                    if error_count > 5:
                        raise
                    time.sleep(2 ** error_count)
                else:
                    raise
            except ResumableUploadError as e:
                if _is_upload_limit_error_message(str(e)):
                    raise UploadLimitExceededError(str(e)) from e
                status = getattr(getattr(e, "resp", None), "status", None)
                if status in [500, 502, 503, 504]:
                    error_count += 1
                    if error_count > 5:
                        raise
                    time.sleep(2 ** error_count)
                else:
                    raise
            except (http.client.HTTPException, ssl.SSLError, ConnectionError) as e:
                error_count += 1
                if error_count > 10:
                    raise
                time.sleep(1 + (error_count * 2))
            except Exception as e:
                # Catch-all for other bizarre socket/SSL disconnects
                if "EOF occurred in violation of protocol" in str(e):
                    error_count += 1
                    if error_count > 10:
                        raise
                    time.sleep(2)
                else:
                    raise

        video_id = str(response.get("id", ""))
        if is_short and thumbnail_path is not None and thumbnail_path.exists():
            print(f"Thumbnail upload skipped for {channel.id}: YouTube Shorts do not support uploaded custom thumbnails the same way as standard videos.")
        elif video_id and thumbnail_path is not None and thumbnail_path.exists():
            thumb_media = MediaFileUpload(str(thumbnail_path))
            try:
                service.thumbnails().set(videoId=video_id, media_body=thumb_media).execute()
            except Exception as exc:
                print(f"Thumbnail upload skipped for {channel.id}: {exc}")

        return video_id

    def list_recent_videos(self, channel: ChannelConfig, max_results: int = 25) -> List[dict]:
        service = self._service_for_channel(channel)

        channel_data = service.channels().list(part="contentDetails", mine=True).execute()
        items = channel_data.get("items", [])
        if not items:
            return []

        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl_data = service.playlistItems().list(
            part="contentDetails,snippet",
            playlistId=uploads_id,
            maxResults=max_results,
        ).execute()

        ids = []
        for it in pl_data.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)

        if not ids:
            return []

        detail = service.videos().list(part="snippet,statistics", id=",".join(ids)).execute()
        out = []
        for it in detail.get("items", []):
            stats = it.get("statistics", {})
            snip = it.get("snippet", {})
            out.append(
                {
                    "video_id": it.get("id"),
                    "title": snip.get("title", ""),
                    "published_at": snip.get("publishedAt", ""),
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                }
            )
        return out
