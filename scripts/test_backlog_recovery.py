import sys
from pathlib import Path
sys.path.append("src")
from yt_auto.pipeline import ShortsFactory

def test_backlog():
    factory = ShortsFactory(Path("config/settings.yaml"))
    
    print("--- Ancient History ---")
    backlog_ah = factory.initialize_backlog("ancient_history", rebuild=True)
    print(f"Total Backlog Items: {len(backlog_ah)}")
    pending_ah = [it for it in backlog_ah if it.get("status") == "pending"]
    print(f"Pending Items: {len(pending_ah)}")

    print("\n--- Brain Lens ---")
    backlog_bl = factory.initialize_backlog("brain_lens", rebuild=True)
    print(f"Total Backlog Items: {len(backlog_bl)}")
    pending_bl = [it for it in backlog_bl if it.get("status") == "pending"]
    print(f"Pending Items: {len(pending_bl)}")

if __name__ == "__main__":
    test_backlog()
