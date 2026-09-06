import json
import os
from pathlib import Path

def backfill_used_topics():
    state_dir = Path("data/state")
    runs_file = state_dir / "runs.jsonl"
    used_topics_file = state_dir / "used_topics.txt"
    
    used = set()
    if used_topics_file.exists():
        used = {line.strip().lower() for line in used_topics_file.read_text(encoding="utf-8").splitlines() if line.strip()}
    
    if runs_file.exists():
        for line in runs_file.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try:
                run = json.loads(line)
                title = run.get("title", "").strip().lower()
                if title: used.add(title)
                subject = run.get("subject", "").strip().lower()
                if subject: used.add(subject)
            except: continue
            
    # Also check existing output directories for metadata.json
    output_root = Path("output")
    if output_root.exists():
        for meta_path in output_root.rglob("metadata.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = meta.get("title", "").strip().lower()
                if title: used.add(title)
            except: continue
            
    with open(used_topics_file, "w", encoding="utf-8") as f:
        for item in sorted(used):
            f.write(f"{item}\n")
    
    print(f"Backfilled {len(used)} topics/phrases into used_topics.txt")

if __name__ == "__main__":
    backfill_used_topics()
