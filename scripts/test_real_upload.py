import sys
from pathlib import Path
sys.path.append("src")
from yt_auto.pipeline import ShortsFactory

def test_upload():
    factory = ShortsFactory(Path("config/settings.yaml"))
    print("Attempting ONE backlog upload for Brain Lens...")
    # This calls replay_backlog_once which picks the newest pending item
    success = factory.replay_backlog_once("brain_lens")
    print(f"Upload success: {success}")

if __name__ == "__main__":
    test_upload()
