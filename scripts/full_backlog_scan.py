import json
from pathlib import Path

def full_scan():
    root = Path("e:/yt_automation/output")
    channels = ["ancient_history", "brain_lens"]
    runs_file = Path("e:/yt_automation/data/state/runs.jsonl")
    
    logged_runs = {}
    if runs_file.exists():
        with open(runs_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    run = json.loads(line)
                    rd = run.get("run_dir")
                    if rd:
                        logged_runs[Path(rd).resolve()] = run
                except:
                    continue

    print(f"Logged runs in runs.jsonl: {len(logged_runs)}")

    for channel in channels:
        print(f"\nScanning channel: {channel}")
        channel_dir = root / channel / "short"
        if not channel_dir.exists():
            continue
            
        total_dirs = 0
        missing_from_log = 0
        missing_fb_in_log = 0
        
        for day_dir in channel_dir.iterdir():
            if not day_dir.is_dir() or day_dir.name == "test_unique_backlog":
                continue
            for run_dir in day_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                total_dirs += 1
                
                metadata_path = run_dir / "metadata.json"
                video_path = run_dir / "short.mp4"
                
                if not metadata_path.exists() or not video_path.exists():
                    continue
                
                resolved_run_dir = run_dir.resolve()
                if resolved_run_dir not in logged_runs:
                    missing_from_log += 1
                else:
                    run_data = logged_runs[resolved_run_dir]
                    fb_id = run_data.get("facebook_id") or run_data.get("facebook_video_id")
                    if not fb_id:
                        missing_fb_in_log += 1
        
        print(f"  Total Run Dirs found: {total_dirs}")
        print(f"  Runs NOT in runs.jsonl: {missing_from_log}")
        print(f"  Runs IN log but missing FB: {missing_fb_in_log}")

if __name__ == "__main__":
    full_scan()
