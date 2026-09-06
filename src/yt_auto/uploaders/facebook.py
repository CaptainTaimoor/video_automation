from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from yt_auto.models import ChannelConfig


class FacebookUploader:
    def __init__(self) -> None:
        self.app_id = os.getenv("FB_APP_ID")
        self.app_secret = os.getenv("FB_APP_SECRET")

    def _get_access_token(self, channel: ChannelConfig) -> str:
        env_var = getattr(channel.facebook, "access_token_env_var", "FB_PAGE_ACCESS_TOKEN")
        token = (os.getenv(env_var) or os.getenv("FB_PAGE_ACCESS_TOKEN") or "").strip()
        if not token:
            raise ValueError(f"Facebook access token missing. Set {env_var} in .env.")
        return token

    def upload(self, channel: ChannelConfig, video_path: Path, metadata: dict) -> str:
        """
        Uploads a video to a Facebook Page as a Reel using the Resumable Upload API.
        """
        token = self._get_access_token(channel)
        page_id = channel.facebook.page_id
        
        # 1. INITIALIZE (START)
        # We use the video_reels edge for Reels specifically as requested.
        start_url = f"https://graph.facebook.com/v21.0/{page_id}/video_reels"
        start_data = {
            "upload_phase": "start",
            "access_token": token
        }
        
        print(f"[{channel.id}] Initializing Facebook Reel upload...")
        r = requests.post(start_url, data=start_data)
        if not r.ok:
            raise RuntimeError(f"FB Initialize failed: {r.text}")
        
        start_res = r.json()
        video_id = start_res.get("video_id")
        if not video_id:
            raise RuntimeError(f"FB Initialize failed to return video_id: {start_res}")

        # 2. UPLOAD (TRANSFER)
        # We need to send the binary data to the video_id endpoint.
        upload_url = f"https://rupload.facebook.com/video-upload/v21.0/{video_id}"
        
        # We use the Ruquest library's ability to send files.
        # Header "Authorization: OAuth <token>" is often preferred for binary transfer.
        headers = {
            "Authorization": f"OAuth {token}",
            "file_size": str(video_path.stat().st_size),
            "offset": "0"
        }
        
        with open(video_path, "rb") as f:
            print(f"[{channel.id}] Transferring video data ({video_path.stat().st_size} bytes)...")
            r = requests.post(
                upload_url, 
                headers=headers, 
                data=f, 
                timeout=600
            )
            
        if not r.ok:
            raise RuntimeError(f"FB Transfer failed: {r.text}")

        # 3. FINISH (PUBLISH)
        finish_url = f"https://graph.facebook.com/v21.0/{page_id}/video_reels"
        finish_data = {
            "upload_phase": "finish",
            "access_token": token,
            "video_id": video_id,
            "description": metadata.get("description", ""),
            "title": metadata.get("title", ""),
            "video_state": "PUBLISHED"
        }
        
        print(f"[{channel.id}] Finalizing Facebook Reel...")
        # Finish can sometimes take a few retries if the video is still processing
        for attempt in range(5):
            r = requests.post(finish_url, data=finish_data)
            if r.ok:
                break
            # If it's a "video is still being processed" error, wait and retry
            if "processing" in r.text.lower():
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"FB Finish failed: {r.text}")
            
        print(f"[{channel.id}] Facebook Reel upload successful: {video_id}")
        return video_id
