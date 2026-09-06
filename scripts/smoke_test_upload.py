import os
import requests
from pathlib import Path
from dotenv import load_dotenv
import sys

# Add src to sys.path
sys.path.append("src")

from yt_auto.pipeline import ShortsFactory
from yt_auto.models import TopicCandidate

def test_full_upload():
    load_dotenv()
    factory = ShortsFactory(Path("config/settings.yaml"))
    
    # Test with Brain Lens (it had a successful connection test)
    channel = factory._channel("brain_lens")
    
    # Use a small existing video from the output for a real smoke test
    video_path = None
    search_dirs = [
        factory.config.app.output_root / "brain_lens" / "short",
        factory.config.app.output_root / "ancient_history" / "short"
    ]
    
    for sd in search_dirs:
        if sd.exists():
            for p in sd.rglob("*.mp4"):
                if p.stat().st_size > 0:
                    video_path = p
                    break
        if video_path:
            break
            
    if not video_path:
        print("No existing video found for smoke test. Skipping real upload.")
        return

    print(f"Using video for test: {video_path}")
    
    metadata = {
        "title": "API Test (Please Delete)",
        "description": "Verification test for Direct Facebook App + YouTube integration. #test",
        "tags": ["test", "api"]
    }
    
    print("--- Starting Smoke Test Upload ---")
    
    # 1. Facebook Upload
    print(f"[{channel.id}] Testing Facebook upload...")
    try:
        fb_id = factory.facebook.upload(channel, video_path, metadata)
        print(f"[{channel.id}] Facebook Upload SUCCESS! Video ID: {fb_id}")
    except Exception as exc:
        print(f"[{channel.id}] Facebook Upload FAILED: {exc}")

    # 2. YouTube Upload (Private)
    print(f"[{channel.id}] Testing YouTube upload (Private)...")
    try:
        # Correct signature: upload(channel, video_path, metadata, privacy_status, thumbnail_path=None, is_short=False)
        yt_id = factory.youtube.upload(
            channel=channel, 
            video_path=video_path, 
            metadata=metadata, 
            privacy_status="private",
            is_short=True
        )
        print(f"[{channel.id}] YouTube Upload SUCCESS! Video ID: {yt_id}")
    except Exception as exc:
        print(f"[{channel.id}] YouTube Upload FAILED: {exc}")

if __name__ == "__main__":
    test_full_upload()
