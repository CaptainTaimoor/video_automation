
import os
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from yt_auto.pipeline import ShortsFactory
from yt_auto.config import load_config

def main():
    config_path = Path("config/settings.yaml")
    factory = ShortsFactory(config_path)
    
    for channel_id in ["ancient_history", "brain_lens"]:
        print(f"--- Recovering backlog for {channel_id} ---")
        items = factory.initialize_backlog(channel_id)
        print(f"Backlog recovered: {len(items)} items.")
        if items:
            print(f"Top item: {items[0]['run_dir']} -> {items[0]['video_path']}")

if __name__ == "__main__":
    main()
