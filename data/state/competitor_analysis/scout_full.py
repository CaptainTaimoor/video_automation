import json, subprocess, time, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

YTDLP = r"E:\yt_automation\.venv\Scripts\yt-dlp.exe"
OUT = Path(r"E:\yt_automation\data\state\competitor_analysis")

# Best-fit competitors vs our niches (not just biggest overall)
CANDIDATES = {
  "ancient": [
    "https://www.youtube.com/@Toldinstone",
    "https://www.youtube.com/@VoicesofthePast",
    "https://www.youtube.com/@WeirdHistory",
    "https://www.youtube.com/@SimpleHistory",
    "https://www.youtube.com/@HistoryMatters",
    "https://www.youtube.com/@SeeUinHistory",
    "https://www.youtube.com/@HistoryMarche",
    "https://www.youtube.com/@EpicHistoryTV",
  ],
  "brain": [
    "https://www.youtube.com/@Psych2Go",
    "https://www.youtube.com/@HealthyGamerGG",
    "https://www.youtube.com/@ImprovementPill",
    "https://www.youtube.com/@charismaoncommand",
    "https://www.youtube.com/@JimmyonRelationships",
    "https://www.youtube.com/@TheBehaviorPanel",
    "https://www.youtube.com/@TheSchoolOfLife",
    "https://www.youtube.com/@HowToADHD",
  ],
}

def run(cmd, timeout=90):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="ignore")
    return r.stdout or ""

def list_ids(channel_url, end=40):
    # Prefer shorts tab then videos
    ids = []
    for tab in ["/shorts", "/videos"]:
        out = run([YTDLP, "--flat-playlist", "--playlist-end", str(end), "--print", "%(id)s", "--no-warnings", "--quiet", channel_url + tab], 120)
        for line in out.splitlines():
            vid = line.strip()
            if re.fullmatch(r"[\w-]{6,}", vid) and vid not in ids:
                ids.append(vid)
        if len(ids) >= end:
            break
    return ids[:end]

def meta_for(vid):
    out = run([
        YTDLP, "--skip-download", "--no-warnings", "--quiet",
        "--print", "%(id)s|||%(title)s|||%(view_count)s|||%(like_count)s|||%(comment_count)s|||%(duration)s|||%(upload_date)s|||%(channel)s|||%(channel_follower_count)s|||%(webpage_url)s|||%(thumbnail)s|||%(description).180s",
        f"https://www.youtube.com/watch?v={vid}",
    ], 60)
    line = next((l for l in out.splitlines() if "|||" in l), "")
    if not line:
        return None
    p = line.split("|||")
    def num(i):
        try: return int(float(p[i]))
        except Exception: return 0
    return {
        "id": p[0],
        "title": p[1] if len(p)>1 else "",
        "views": num(2),
        "likes": num(3),
        "comments": num(4),
        "duration": num(5),
        "upload_date": p[6] if len(p)>6 else "",
        "channel": p[7] if len(p)>7 else "",
        "followers": num(8),
        "url": p[9] if len(p)>9 else f"https://youtu.be/{vid}",
        "thumbnail": p[10] if len(p)>10 else "",
        "description_snip": p[11] if len(p)>11 else "",
        "is_short": num(5) > 0 and num(5) <= 60,
    }

results = {"ancient": [], "brain": []}
for niche, urls in CANDIDATES.items():
    for url in urls:
        handle = url.rstrip("/").split("@")[-1]
        print(f"\n=== {niche} :: {handle} ===", flush=True)
        ids = list_ids(url, 30)
        print(f"  ids={len(ids)}", flush=True)
        videos = []
        # fetch meta for first 18 ids (mix of recent + later sort by views)
        for vid in ids[:18]:
            try:
                m = meta_for(vid)
                if m and m["views"] > 0:
                    videos.append(m)
            except Exception as e:
                print("  meta fail", vid, e)
            time.sleep(0.15)
        videos.sort(key=lambda v: v["views"], reverse=True)
        top = videos[:10]
        shorts = [v for v in top if v["is_short"]]
        longs = [v for v in top if not v["is_short"]]
        avg = int(sum(v["views"] for v in top)/len(top)) if top else 0
        row = {
            "handle": handle,
            "url": url,
            "followers": top[0]["followers"] if top else 0,
            "sample": len(top),
            "avg_top_views": avg,
            "max_views": top[0]["views"] if top else 0,
            "shorts_in_top10": len(shorts),
            "longs_in_top10": len(longs),
            "avg_short_duration": int(sum(v["duration"] for v in shorts)/len(shorts)) if shorts else None,
            "avg_long_duration": int(sum(v["duration"] for v in longs)/len(longs)) if longs else None,
            "top_videos": top,
        }
        results[niche].append(row)
        (OUT/niche/f"{handle}.json").write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  avg_top={avg:,} max={row['max_views']:,} shorts={row['shorts_in_top10']} longs={row['longs_in_top10']} followers={row['followers']:,}", flush=True)

# Rank
for niche in results:
    results[niche].sort(key=lambda r: (r["avg_top_views"], r["followers"]), reverse=True)

(OUT/"ranked_competitors.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print("\n==== RANKED ====")
for niche, rows in results.items():
    print(f"\n[{niche}]")
    for i,r in enumerate(rows[:6],1):
        print(f" {i}. @{r['handle']} avg={r['avg_top_views']:,} max={r['max_views']:,} shorts={r['shorts_in_top10']} foll={r['followers']:,}")
