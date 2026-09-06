import json, subprocess, time, shutil
from pathlib import Path

YTDLP = r"E:\yt_automation\.venv\Scripts\yt-dlp.exe"
FFMPEG = r"E:\yt_automation\.runtime\bin"
ROOT = Path(r"E:\yt_automation\data\state\competitor_analysis")
ranked = json.loads((ROOT/"ranked_competitors.json").read_text(encoding="utf-8"))
picks = {
  "ancient": ["HistoryMarche", "VoicesofthePast", "HistoryMatters", "Toldinstone"],
  "brain": ["charismaoncommand", "JimmyonRelationships", "HealthyGamerGG", "ImprovementPill"],
}
manifest = {"downloaded": [], "errors": []}

def download_set(niche, handle, videos):
    dest = ROOT/"samples"/niche/handle
    dest.mkdir(parents=True, exist_ok=True)
    thumbs = ROOT/"thumbs"/niche/handle
    thumbs.mkdir(parents=True, exist_ok=True)
    shorts = [v for v in videos if v.get("is_short")][:3]
    longs = [v for v in videos if not v.get("is_short")][:2]
    chosen = shorts + longs
    if not chosen:
        chosen = videos[:4]
    for v in chosen:
        vid = v["id"]
        print(f"DL {niche}/{handle}: {(v.get('title') or '')[:55]} ({v.get('views'):,} / {v.get('duration')}s)", flush=True)
        # thumbnail only via yt-dlp
        subprocess.run([YTDLP, "--ffmpeg-location", FFMPEG, "--skip-download", "--write-thumbnail", "--convert-thumbnails", "jpg",
                        "--output", str(thumbs/f"{vid}.%(ext)s"), "--no-warnings", "--quiet", v["url"]], timeout=90)
        mp4 = dest/f"{vid}.mp4"
        if mp4.exists() and mp4.stat().st_size > 100_000:
            print("  skip existing mp4", flush=True)
            manifest["downloaded"].append({"niche":niche,"handle":handle,**v,"path":str(mp4)})
            continue
        # clean partials
        for p in dest.glob(f"{vid}.*"):
            if p.suffix.lower() in {".mp4", ".webm", ".m4a", ".mkv"} and p.name != f"{vid}.mp4":
                try: p.unlink()
                except Exception: pass
        cmd = [YTDLP, "--ffmpeg-location", FFMPEG, "-f", "bv*[height<=720]+ba/b[height<=720]/w",
               "--merge-output-format", "mp4", "--write-info-json", "--write-auto-subs", "--sub-langs", "en.*,en",
               "--convert-subs", "srt", "--no-warnings", "--output", str(dest/f"{vid}.%(ext)s")]
        if not v.get("is_short") and (v.get("duration") or 0) > 120:
            cmd += ["--download-sections", "*0-90"]
        cmd.append(v["url"])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="ignore")
            if mp4.exists() and mp4.stat().st_size > 50_000:
                manifest["downloaded"].append({"niche":niche,"handle":handle,**v,"path":str(mp4)})
                print(f"  ok {mp4.stat().st_size//1024}KB", flush=True)
            else:
                # try single format fallback
                r2 = subprocess.run([YTDLP, "--ffmpeg-location", FFMPEG, "-f", "18/best[height<=480]/best",
                                     "--merge-output-format", "mp4", "--write-info-json", "--no-warnings",
                                     "--output", str(dest/f"{vid}.%(ext)s"), v["url"]],
                                    capture_output=True, text=True, timeout=240, encoding="utf-8", errors="ignore")
                if mp4.exists():
                    manifest["downloaded"].append({"niche":niche,"handle":handle,**v,"path":str(mp4)})
                    print("  ok fallback", flush=True)
                else:
                    err = (r.stderr or r.stdout or r2.stderr or "")[-400:]
                    manifest["errors"].append({"niche":niche,"handle":handle,"id":vid,"err":err})
                    print("  fail", err[:120], flush=True)
        except Exception as e:
            manifest["errors"].append({"niche":niche,"handle":handle,"id":vid,"err":str(e)})
            print("  exception", e, flush=True)
        time.sleep(0.3)

for niche, handles in picks.items():
    by_handle = {r["handle"]: r for r in ranked.get(niche, [])}
    for h in handles:
        row = by_handle.get(h)
        if not row:
            continue
        download_set(niche, h, row.get("top_videos") or [])

(ROOT/"download_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"DONE downloads={len(manifest['downloaded'])} errors={len(manifest['errors'])}")
