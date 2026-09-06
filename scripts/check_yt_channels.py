import sys
from pathlib import Path
sys.path.append("src")
from yt_auto.uploaders import YouTubeUploader
from yt_auto.pipeline import ShortsFactory

def check_channels():
    factory = ShortsFactory(Path("config/settings.yaml"))
    yt = YouTubeUploader()
    
    for channel_id in ["ancient_history", "brain_lens"]:
        channel = factory._channel(channel_id)
        print(f"\nChecking channel: {channel_id}")
        try:
            # We don't have a direct 'get_channel_info' but upload() triggers auth
            # I'll just check the token file for now or hit the API if I can.
            # Actually, I can use the existing 'factory.youtube.service' if it was public.
            # For now, let's just use the uploader to get info.
            service = yt._get_service(channel)
            res = service.channels().list(mine=True, part="snippet").execute()
            if res.get("items"):
                title = res["items"][0]["snippet"]["title"]
                handle = res["items"][0]["snippet"].get("customUrl", "unknown")
                print(f"  Title: {title}")
                print(f"  Handle: {handle}")
            else:
                print("  No channel found for this token.")
        except Exception as e:
            print(f"  Error: {e}")

if __name__ == "__main__":
    check_channels()
