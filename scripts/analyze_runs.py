import json
from pathlib import Path

def analyze_backlog():
    runs_file = Path("data/state/runs.jsonl")
    if not runs_file.exists():
        print("runs.jsonl not found")
        return

    ancient_history_total = 0
    ancient_history_no_fb = 0
    brain_lens_total = 0
    brain_lens_no_fb = 0

    with open(runs_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                run = json.loads(line)
                channel = run.get("channel")
                yt_id = run.get("youtube_id")
                fb_id = run.get("facebook_id") or run.get("facebook_video_id")
                
                if channel == "ancient_history":
                    ancient_history_total += 1
                    if yt_id and not fb_id:
                        ancient_history_no_fb += 1
                elif channel == "brain_lens":
                    brain_lens_total += 1
                    if yt_id and not fb_id:
                        brain_lens_no_fb += 1
            except:
                continue

    print(f"Ancient History: Total={ancient_history_total}, Missing FB={ancient_history_no_fb}")
    print(f"Brain Lens: Total={brain_lens_total}, Missing FB={brain_lens_no_fb}")

if __name__ == "__main__":
    analyze_backlog()
