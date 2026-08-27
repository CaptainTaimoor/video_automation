"""
channel_analyzer.py

Viral DNA Channel Cloner — analyzes top YouTube videos from a reference channel
to extract hook patterns, pacing, tone, and video ideas. Saves results as a
viral_dna.json file that the script writer and topic researcher use to write
scripts in the same proven style.

Usage (via CLI):
    python run.py analyze --channel brain_lens
    python run.py analyze --channel ancient_history

Output:
    data/state/{channel_id}_viral_dna.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from yt_auto.network_policy import validate_ollama_generate_url


class ChannelAnalyzer:
    """Fetches top video transcripts from a YouTube channel and extracts
    Viral DNA: hook patterns, pacing, tone, and fresh video ideas."""

    def __init__(self, ollama_url: str, ollama_model: str, timeout: int = 120) -> None:
        self.ollama_url   = validate_ollama_generate_url(ollama_url)
        self.ollama_model = ollama_model
        self.timeout      = timeout
        self.session      = requests.Session()
        self.session.headers.update({"User-Agent": "yt-auto/1.0"})

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def analyze(self, channel_url: str, state_dir: Path, channel_id: str,
                max_videos: int = 15) -> dict:
        """Full pipeline: fetch transcripts → extract DNA → save & return."""
        print(f"[{channel_id}] Analyzing channel: {channel_url}")
        print(f"[{channel_id}] Fetching top {max_videos} video transcripts with yt-dlp...")

        transcripts = self._fetch_transcripts(channel_url, max_videos)
        if not transcripts:
            print(f"[{channel_id}] WARNING: No transcripts fetched. Using fallback DNA.")
            return self._fallback_dna(channel_id)

        print(f"[{channel_id}] Got {len(transcripts)} transcripts. Extracting DNA via Ollama...")
        dna = self._extract_dna_with_ollama(transcripts, channel_url, channel_id)

        out_path = Path(state_dir) / f"{channel_id}_viral_dna.json"
        out_path.write_text(json.dumps(dna, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{channel_id}] Viral DNA saved to: {out_path}")
        return dna

    @staticmethod
    def load(state_dir: Path, channel_id: str) -> Optional[dict]:
        """Load existing DNA file if present."""
        path = Path(state_dir) / f"{channel_id}_viral_dna.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────
    # Transcript fetching via yt-dlp
    # ─────────────────────────────────────────────────────────────────

    def _yt_dlp_path(self) -> str:
        """Find the yt-dlp executable, favoring the venv/Scripts folder if on Windows."""
        bin_dir = Path(sys.executable).parent
        ext = ".exe" if os.name == "nt" else ""
        local_path = bin_dir / f"yt-dlp{ext}"
        if local_path.exists():
            return str(local_path)
        return "yt-dlp"

    def _fetch_transcripts(self, channel_url: str, max_videos: int) -> list[dict]:
        """Use yt-dlp to get transcripts and metadata for the top N videos."""
        transcripts = []
        ytdlp = self._yt_dlp_path()

        try:
            # Step 1: Get video URLs from channel (sorted by view count via playlist)
            cmd_list = [
                ytdlp,
                "--flat-playlist",
                "--playlist-end", str(max_videos * 3),  # overfetch, filter later
                "--print", "%(id)s|||%(title)s|||%(view_count)s",
                "--no-warnings",
                "--quiet",
                channel_url,
            ]
            result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=60)
            lines = [l.strip() for l in result.stdout.splitlines() if "|||" in l]

            # Sort by view count (highest first), take top N
            entries = []
            for line in lines:
                parts = line.split("|||")
                if len(parts) >= 2:
                    vid_id = parts[0].strip()
                    title  = parts[1].strip()
                    try:
                        views = int(parts[2].strip()) if len(parts) > 2 else 0
                    except (ValueError, IndexError):
                        views = 0
                    if vid_id:
                        entries.append((views, vid_id, title))

            entries.sort(key=lambda x: x[0], reverse=True)
            top_entries = entries[:max_videos]

            if not top_entries:
                print("  yt-dlp returned no entries.")
                return []

            print(f"  Found {len(top_entries)} videos. Fetching subtitles...")

            # Step 2: Fetch subtitle/transcript for each video
            for i, (views, vid_id, title) in enumerate(top_entries, 1):
                url = f"https://www.youtube.com/watch?v={vid_id}"
                print(f"  [{i}/{len(top_entries)}] {title[:60]}...", end=" ", flush=True)
                text = self._get_transcript(url)
                if text:
                    print(f"({len(text.split())} words)")
                    transcripts.append({
                        "video_id": vid_id,
                        "title":    title,
                        "views":    views,
                        "url":      url,
                        "text":     text[:3000],  # cap to avoid overwhelming Ollama
                    })
                else:
                    print("no transcript")
                time.sleep(0.5)

        except subprocess.TimeoutExpired:
            print("  yt-dlp timed out.")
        except FileNotFoundError:
            print("  yt-dlp not found. Install: pip install yt-dlp")
        except Exception as exc:
            print(f"  Error fetching transcripts: {exc}")

        return transcripts

    def _get_transcript(self, url: str) -> str:
        """Download auto-generated subtitles for one video, return plain text."""
        import tempfile, os
        ytdlp = self._yt_dlp_path()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                ytdlp,
                "--write-auto-subs",
                "--sub-lang", "en",
                "--sub-format", "vtt",
                "--skip-download",
                "--output", os.path.join(tmp, "%(id)s"),
                "--no-warnings",
                "--quiet",
                url,
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30)
                # Find the downloaded vtt file
                for f in Path(tmp).glob("*.vtt"):
                    raw = f.read_text(encoding="utf-8", errors="ignore")
                    return self._vtt_to_text(raw)
            except Exception:
                pass
        return ""

    def _vtt_to_text(self, vtt: str) -> str:
        """Convert VTT subtitle file to clean plain text."""
        lines = []
        seen: set[str] = set()
        for line in vtt.splitlines():
            line = line.strip()
            # Skip header lines, timestamps, and empty lines
            if not line or "-->" in line or line.startswith("WEBVTT") or line.isdigit():
                continue
            # Strip HTML tags
            clean = re.sub(r"<[^>]+>", "", line).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean and clean not in seen:
                seen.add(clean)
                lines.append(clean)
        return " ".join(lines)

    # ─────────────────────────────────────────────────────────────────
    # DNA Extraction via Ollama
    # ─────────────────────────────────────────────────────────────────

    def _extract_dna_with_ollama(self, transcripts: list[dict],
                                  channel_url: str, channel_id: str) -> dict:
        """Send transcript samples to Ollama and extract structured DNA."""

        # Build a compact summary of the transcripts
        samples = []
        for t in transcripts[:8]:  # limit to 8 to fit in context
            excerpt = " ".join(t["text"].split()[:200])
            samples.append(f'TITLE: "{t["title"]}"\nOPENING: {excerpt}')

        samples_text = "\n\n---\n\n".join(samples)

        # Also get the list of titles for idea generation
        titles = [t["title"] for t in transcripts]

        prompt = f"""You are a viral YouTube content strategist analyzing a successful channel.

Channel URL: {channel_url}

Here are transcript excerpts from the top {len(transcripts)} videos on this channel:

{samples_text}

Analyze these videos deeply and return ONLY a valid JSON object with these exact keys:

{{
  "hook_patterns": ["pattern 1", "pattern 2", "pattern 3", "pattern 4", "pattern 5"],
  "tone": "one paragraph describing the speaking tone, energy level, and voice personality",
  "pacing": "one sentence describing sentence length, rhythm, and how fast information is delivered",
  "structure": "one paragraph describing how videos are structured: hook → body → CTA",
  "emotion_triggers": ["trigger1", "trigger2", "trigger3"],
  "forbidden": ["thing to avoid 1", "thing to avoid 2"],
  "video_ideas": [
    {{"title": "catchy video title idea 1", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 2", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 3", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 4", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 5", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 6", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 7", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 8", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 9", "angle": "the fresh angle or hook to take"}},
    {{"title": "catchy video title idea 10", "angle": "the fresh angle or hook to take"}}
  ],
  "script_instructions": "2-3 sentences of specific writing instructions to replicate this channel's style WITHOUT copying — rephrase topics in your own original words"
}}

CRITICAL RULES:
- video_ideas must be ORIGINAL topic angles inspired by the channel's STYLE, not copies of their exact titles
- Each idea should be a fresh take the channel hasn't done, in the same proven style
- Return ONLY the JSON, no commentary before or after"""

        try:
            raw = self._ollama(prompt)
            # Extract JSON from response
            match = re.search(r'\{[\s\S]+\}', raw)
            if match:
                dna = json.loads(match.group())
                dna["source_channel"] = channel_url
                dna["analyzed_titles"] = titles[:20]
                dna["analyzed_at"] = time.strftime("%Y-%m-%d")
                return dna
        except Exception as exc:
            print(f"  Ollama extraction error: {exc}. Using fallback.")

        return self._fallback_dna(channel_id)

    def _ollama(self, prompt: str) -> str:
        payload = {
            "model": self.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "top_p": 0.9,
                "num_predict": 1200,
            },
        }
        endpoint = validate_ollama_generate_url(self.ollama_url)
        r = requests.post(
            endpoint,
            json=payload,
            timeout=self.timeout,
            allow_redirects=False,
        )
        r.raise_for_status()
        return str(r.json().get("response", "")).strip()

    def _fallback_dna(self, channel_id: str) -> dict:
        """Return sane defaults if analysis fails."""
        if channel_id == "brain_lens":
            return {
                "hook_patterns": [
                    "Start with a counterintuitive fact about how the brain works",
                    "Open with a relatable everyday scenario that hides a deep psychology concept",
                    "Lead with a shocking statistic about human behavior",
                    "Start with a question that makes the viewer doubt themselves",
                    "Open by naming a feeling everyone has but few can explain",
                ],
                "tone": "Calm, authoritative, empathetic. Speaks like a smart friend who happens to be a psychologist. Never preachy.",
                "pacing": "Short punchy sentences. 8-12 words each. Fast delivery with clear pauses at key reveals.",
                "structure": "Hook (shocking fact) → Science explanation → Real-life application → Practical tip → Subscribe CTA",
                "emotion_triggers": ["curiosity", "validation", "self-awareness", "surprise"],
                "forbidden": ["have you ever wondered", "did you know", "in today's video"],
                "video_ideas": [
                    {"title": "Why Successful People Still Feel Like Frauds", "angle": "Impostor syndrome in high achievers"},
                    {"title": "Your Brain Is Lying to You About Your Memories", "angle": "Memory reconstruction vs replay"},
                    {"title": "The Hidden Cost of Being a People Pleaser", "angle": "Fawn response and identity loss"},
                    {"title": "Why You Can't Stop Scrolling Even When You're Bored", "angle": "Variable reward and dopamine loops"},
                    {"title": "The Psychology of Why You Procrastinate", "angle": "Emotional avoidance not laziness"},
                ],
                "script_instructions": "Write in short, punchy sentences. Start every script with a bold counterintuitive statement. Build from personal relatable observation to scientific explanation to practical takeaway. Never moralize.",
                "source_channel": "fallback",
                "analyzed_at": time.strftime("%Y-%m-%d"),
            }
        # ancient_history fallback
        return {
            "hook_patterns": [
                "Open with the shocking outcome before revealing the cause",
                "Start with a modern parallel to an ancient event",
                "Lead with a specific sensory detail (smell, sound, sight) from history",
                "Open with a common myth and immediately challenge it with evidence",
                "Start with a precise date and location to ground the viewer instantly",
            ],
            "tone": "Dramatic yet scholarly. Like a documentary narrator who respects the audience's intelligence. Confident, specific, never sensational.",
            "pacing": "Medium-length sentences with occasional very short punches for emphasis. Builds tension before reveals.",
            "structure": "Hook (shocking detail) → Context setting → Rising stakes → Reveal/consequence → Legacy/why it matters → CTA",
            "emotion_triggers": ["awe", "curiosity", "dread", "wonder"],
            "forbidden": ["let's travel back in time", "welcome to our channel", "don't forget to like and subscribe"],
            "video_ideas": [
                {"title": "The Night Rome Almost Ceased to Exist", "angle": "The Gallic sack of Rome in 390 BC"},
                {"title": "The Forgotten Disease That Ended an Empire", "angle": "Antonine Plague impact on Rome"},
                {"title": "The Architect Who Changed the Ancient World Forever", "angle": "Imhotep and pyramid innovation"},
                {"title": "When Soldiers Refused to Go Home", "angle": "The mutiny that changed Alexander's campaign"},
                {"title": "The Trade Route That Built and Destroyed Civilizations", "angle": "Silk Road collapse and pandemic spread"},
            ],
            "script_instructions": "Use specific dates, names, and sensory details. Build dramatic tension through precise historical facts. End each scene with a consequence that propels to the next. Never vague — always specific.",
            "source_channel": "fallback",
            "analyzed_at": time.strftime("%Y-%m-%d"),
        }
