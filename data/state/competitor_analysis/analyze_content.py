import json, subprocess, statistics
from pathlib import Path
from collections import Counter

ROOT = Path(r"E:\yt_automation\data\state\competitor_analysis")
FF = r"E:\yt_automation\.runtime\bin\ffmpeg.exe"
FRAMES = ROOT/"frames"
FRAMES.mkdir(exist_ok=True)
ranked = json.loads((ROOT/"ranked_competitors.json").read_text(encoding="utf-8"))
manifest = json.loads((ROOT/"download_manifest.json").read_text(encoding="utf-8"))

for item in manifest.get("downloaded", []):
    mp4 = Path(item["path"])
    if not mp4.exists():
        continue
    outdir = FRAMES/item["niche"]/item["handle"]/item["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    for t, name in [(1,"t01"), (3,"t03"), (8,"t08")]:
        out = outdir/f"{name}.jpg"
        if out.exists() and out.stat().st_size > 1000:
            continue
        subprocess.run([FF, "-y", "-ss", str(t), "-i", str(mp4), "-frames:v", "1", "-q:v", "3", str(out)],
                       capture_output=True, timeout=30)

analysis = {"niches": {}, "vs_our_channels": {}}

def title_features(title: str):
    t = title or ""
    words = t.split()
    return {"len_words": len(words), "has_number": any(ch.isdigit() for ch in t), "has_question": "?" in t}

for niche, rows in ranked.items():
    niche_report = {"channels": [], "title_patterns": Counter(), "short_durations": [], "long_durations": [], "top_hooks": []}
    for row in rows[:6]:
        feats = [title_features(v["title"]) for v in row.get("top_videos", [])]
        shorts = [v for v in row.get("top_videos", []) if v.get("is_short")]
        longs = [v for v in row.get("top_videos", []) if not v.get("is_short")]
        for v in row.get("top_videos", [])[:6]:
            t = v["title"]; low = t.lower()
            if "?" in t: niche_report["title_patterns"]["question"] += 1
            if any(ch.isdigit() for ch in t): niche_report["title_patterns"]["number"] += 1
            if low.startswith("how"): niche_report["title_patterns"]["how"] += 1
            if low.startswith("why"): niche_report["title_patterns"]["why"] += 1
            if ":" in t: niche_report["title_patterns"]["colon"] += 1
            niche_report["top_hooks"].append({"channel": row["handle"], "title": t, "views": v["views"], "duration": v["duration"], "is_short": v["is_short"]})
        niche_report["short_durations"] += [v["duration"] for v in shorts]
        niche_report["long_durations"] += [v["duration"] for v in longs]
        niche_report["channels"].append({
            "handle": row["handle"], "avg_top_views": row["avg_top_views"], "max_views": row["max_views"],
            "followers": row["followers"], "shorts_in_top10": row["shorts_in_top10"], "longs_in_top10": row["longs_in_top10"],
            "avg_short_duration": row.get("avg_short_duration"), "avg_long_duration": row.get("avg_long_duration"),
            "sample_titles": [v["title"] for v in row.get("top_videos", [])[:5]],
            "avg_title_words": round(statistics.mean([f["len_words"] for f in feats]),1) if feats else 0,
        })
    niche_report["top_hooks"] = sorted(niche_report["top_hooks"], key=lambda x: x["views"], reverse=True)[:12]
    niche_report["title_patterns"] = dict(niche_report["title_patterns"])
    niche_report["median_short_duration"] = int(statistics.median(niche_report["short_durations"])) if niche_report["short_durations"] else None
    niche_report["median_long_duration"] = int(statistics.median(niche_report["long_durations"])) if niche_report["long_durations"] else None
    analysis["niches"][niche] = niche_report

for cid, niche in [("ancient_history","ancient"), ("brain_lens","brain")]:
    p = Path(rf"E:\yt_automation\data\state\{cid}_performance_report.json")
    if not p.exists(): continue
    d = json.loads(p.read_text(encoding="utf-8-sig"))
    a = d.get("analytics_summary") or {}
    top = d.get("top_videos") or []
    leader = (ranked.get(niche) or [{}])[0]
    our_best = (top[0].get("views") if top else 1) or 1
    analysis["vs_our_channels"][cid] = {
        "our_total_views": a.get("total_views"),
        "our_retention_pct": a.get("average_view_percentage"),
        "our_avg_view_duration": a.get("average_view_duration"),
        "our_engaged_view_rate": a.get("engaged_view_rate"),
        "our_best_hours": [h.get("key") for h in (d.get("best_publish_hours") or [])[:5]],
        "our_top_titles": [v.get("title") for v in top[:5]],
        "competitor_leader": leader.get("handle"),
        "competitor_leader_avg_views": leader.get("avg_top_views"),
        "gap_vs_our_best_video": round((leader.get("avg_top_views") or 1) / max(our_best,1), 1),
    }

visual = []
for mp4 in sorted((ROOT/"samples").rglob("*.mp4")):
    infos = list(mp4.parent.glob(f"{mp4.stem}*.info.json"))
    meta = {}
    if infos:
        try: meta = json.loads(infos[0].read_text(encoding="utf-8"))
        except Exception: meta = {}
    w = meta.get("width") or (meta.get("formats") or [{}])[-1].get("width")
    h = meta.get("height") or (meta.get("formats") or [{}])[-1].get("height")
    # prefer requested format dims
    for f in reversed(meta.get("formats") or []):
        if f.get("vcodec") not in (None, "none") and f.get("width") and f.get("height"):
            w, h = f.get("width"), f.get("height")
            break
    visual.append({
        "file": str(mp4.relative_to(ROOT)).replace("\\","/"),
        "channel": mp4.parent.name,
        "niche": mp4.parent.parent.name,
        "width": w, "height": h,
        "vertical": bool(w and h and h > w),
        "title": meta.get("title") or mp4.stem,
        "view_count": meta.get("view_count"),
        "duration": meta.get("duration"),
        "fps": meta.get("fps"),
    })

analysis["visual_samples"] = visual
analysis["frame_count"] = len(list(FRAMES.rglob("t01.jpg")))
(ROOT/"content_visual_analysis.json").write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
print("OK niches", {k: len(v['channels']) for k,v in analysis['niches'].items()})
print("visual", len(visual), "frames", analysis["frame_count"])
for cid, row in analysis["vs_our_channels"].items():
    print(cid, "retention", row["our_retention_pct"], "best_hours", row["our_best_hours"], "gap", row["gap_vs_our_best_video"], "x vs", row["competitor_leader"])
