from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from yt_auto.models import ChannelConfig
from yt_auto.seo import fit_youtube_tags
from yt_auto.utils import ensure_dir


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


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
        if not channel.youtube or not channel.youtube.upload_enabled:
            return

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
    ) -> str:
        service = self._service_for_channel(channel)

        body = {
            "snippet": {
                "title": (metadata.get("title") or "Untitled Video")[:100],
                "description": (metadata.get("description") or "Automated upload")[:5000],
                "tags": fit_youtube_tags(metadata.get("tags", [])),
                "categoryId": metadata.get("category_id", "24"),
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

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
                if "EOF occurred in violation of protocol" in str(e):
                    error_count += 1
                    if error_count > 10:
                        raise
                    time.sleep(2)
                else:
                    raise

        video_id = str(response.get("id", ""))
        if is_short and thumbnail_path is not None and thumbnail_path.exists():
            print(f"Thumbnail upload skipped for {channel.id}: YouTube Shorts do not support uploaded custom thumbnails same way as standard videos.")
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

    def list_video_comments(
        self,
        channel: ChannelConfig,
        video_id: str,
        max_results: int = 30,
        order: str = "time",
    ) -> List[dict]:
        service = self._service_for_channel(channel)

        from googleapiclient.errors import HttpError

        comments: List[dict] = []
        page_token = None
        safe_limit = max(1, min(int(max_results), 100))
        used_api_key_fallback = False
        while len(comments) < safe_limit:
            try:
                request = service.commentThreads().list(
                    part="snippet,replies",
                    videoId=video_id,
                    maxResults=min(100, safe_limit - len(comments)),
                    order=order,
                    textFormat="plainText",
                    pageToken=page_token,
                )
                data = request.execute()
            except HttpError as exc:
                reason = ""
                try:
                    payload = json.loads(exc.content.decode("utf-8"))
                    reason = payload.get("error", {}).get("errors", [{}])[0].get("reason", "")
                except Exception:
                    reason = str(exc)
                api_key = (
                    os.getenv("YOUTUBE_API_KEY")
                    or os.getenv("GOOGLE_API_KEY")
                )
                if (
                    not used_api_key_fallback
                    and api_key
                    and reason in {"insufficientPermissions", "forbidden"}
                ):
                    service = build("youtube", "v3", developerKey=api_key)
                    comments = []
                    page_token = None
                    used_api_key_fallback = True
                    continue
                if reason in {"commentsDisabled", "videoNotFound", "forbidden"}:
                    return []
                raise

            for item in data.get("items", []):
                top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                text = top.get("textDisplay") or top.get("textOriginal") or ""
                if not text:
                    continue
                comments.append(
                    {
                        "comment_id": item.get("snippet", {}).get("topLevelComment", {}).get("id", ""),
                        "video_id": video_id,
                        "author": top.get("authorDisplayName", ""),
                        "text": text,
                        "like_count": int(top.get("likeCount", 0)),
                        "published_at": top.get("publishedAt", ""),
                        "updated_at": top.get("updatedAt", ""),
                        "reply_count": int(item.get("snippet", {}).get("totalReplyCount", 0)),
                    }
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return comments[:safe_limit]

    def video_details(self, channel: ChannelConfig, video_ids: List[str]) -> Dict[str, dict]:
        service = self._service_for_channel(channel)
        out: Dict[str, dict] = {}
        ids = [video_id for video_id in video_ids if video_id]
        for idx in range(0, len(ids), 50):
            chunk = ids[idx:idx + 50]
            if not chunk:
                continue
            data = service.videos().list(
                part="snippet,statistics,contentDetails",
                id=",".join(chunk),
            ).execute()
            for item in data.get("items", []):
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                out[item.get("id", "")] = {
                    "video_id": item.get("id"),
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                }
        return out

    def analytics_report(
        self,
        channel: ChannelConfig,
        start_date: str,
        end_date: str,
        metrics: str,
        dimensions: str | None = None,
        filters: str | None = None,
        sort: str | None = None,
        max_results: int | None = None,
    ) -> dict:
        creds = self._load_saved_credentials(channel.youtube.token_file)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            channel.youtube.token_file.write_text(creds.to_json(), encoding="utf-8")
        if not creds or not creds.valid:
            self._service_for_channel(channel)
            creds = self._load_saved_credentials(channel.youtube.token_file)
        if not creds or not creds.valid:
            raise UploadAuthError(f"OAuth token for {channel.id} is not valid. Re-authorize: python run.py auth --channel {channel.id}")

        service = build("youtubeAnalytics", "v2", credentials=creds)
        request = {
            "ids": "channel==MINE",
            "startDate": start_date,
            "endDate": end_date,
            "metrics": metrics,
        }
        if dimensions:
            request["dimensions"] = dimensions
        if filters:
            request["filters"] = filters
        if sort:
            request["sort"] = sort
        if max_results:
            request["maxResults"] = int(max_results)
        return service.reports().query(**request).execute()
