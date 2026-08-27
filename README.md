# YouTube Shorts Automation (Free-First, Local)

This project builds automated YouTube Shorts and 8–10 minute videos for two niches/channels using free/local tools and authorized free API tiers where possible. This checkout is the isolated `quality-v2-free` rollout: scheduling and uploads are disabled by default so it cannot interfere with the existing live bot.

## What It Does

- Picks trend-aware topics per niche (Google Trends + Google News RSS + seed keywords).
- Generates script drafts from trend context.
- Routes script work across Gemini, Groq, Cloudflare Workers AI, OpenRouter's
  free router, and local Ollama with durable provider health cooldowns.
- Generates A/B title variants and auto-selects one using learned performance scores.
- Uses licensed real stock for Brain Lens and provenance-checked Wikimedia/archive material plus cited documentary diagrams for Ancient History.
- Rejects generated/fallback Brain Lens visuals and Ancient fact cards before rendering.
- Generates narration locally with the configured Kokoro voices.
- Auto-generates `.srt` subtitles and burns subtitles into the final video.
- Auto-generates a thumbnail image per video.
- Mixes narration + background music and renders vertical Shorts (1080x1920) or landscape long videos (1920x1080).
- Uploads to YouTube using each channel's configured privacy status.
- Syncs YouTube performance metrics and improves future style/title/term selection.
- Schedules automatic posting runs in `Asia/Karachi` timezone.
- Builds research-backed SEO metadata; see `docs/viral_publisher_playbook.md`.

## Important Notes

- Add only copyright-safe music files into `assets/music`.
- This app defaults to strict-safe content filtering (no explicit or policy-risk prompts).
- Final MP4 audio, video, captions, visual diversity, darkness and freeze checks
  must pass before an upload is eligible.
- Always use `--dry-run` for review builds. The scheduler can publish when it is
  explicitly started with `--upload` and the quality gate passes.
- If every AI provider is unavailable, deterministic templates remain available;
  weak results are held by the quality gate instead of being silently published.

## Setup

1. Install Python 3.10+.
2. Create/activate a virtual environment.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy env template:

```bash
copy .env.example .env
```

5. Put your YouTube OAuth client file at `client_secrets.json`.
6. Optional keys in `.env`:
   - `GEMINI_API_KEY` for the primary Gemini writer.
   - `GROQ_API_KEY`, `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`, or
     `OPENROUTER_API_KEY` for independent free-tier text fallbacks.
   - `YOUTUBE_API_KEY` for the optional YouTube comment-read API-key fallback.
   - `STABLE_HORDE_KEY` for AI image fallback.
   - `PIXABAY_API_KEY` for additional stock images.

## Free, Authorized AI Routing

`config/settings.yaml` uses a bounded provider order:

1. Gemini API
2. Groq
3. Cloudflare Workers AI
4. OpenRouter free models
5. Local Ollama

Leave a provider key blank to skip it. The router never rotates accounts or
uses a paid fallback; after three transient/quota failures the provider/model is
persistently opened and the next route is tried. Free quotas and model lists can
change, so OpenRouter intentionally stays last and Ollama is the local final
fallback.

Each build records sanitized provider attempts with a call purpose and whether
the response was actually consumed. `last_ai_provider` identifies the most
recent accepted AI-assisted task. The backward-compatible `script_provider`
fields are populated only when model-generated narration for the selected topic
passes the editorial gates; an AI title, score, or review never claims script
authorship. Invalid JSON or schema output is rejected and routed to the next
configured provider.

## Quality V2 free rollout

Use this folder separately from the live checkout. It stores its own history and
SQLite state under `data/state/`, both ignored by Git.

```bash
copy .env.example .env
set PYTHONPATH=src
python run.py quality-v2-import-history --runs-file E:\yt_automation\data\state\runs.jsonl
python run.py quality-v2-status
python run.py build --channel ancient_history --kind short --dry-run
```

`YT_QUALITY_V2=1` enables the new free-only gates in shadow mode. It adds:

- source-backed/repetition checks for scripts and visual manifests;
- persistent provider circuit breakers and deterministic failure quarantine;
- dialogue-first music ducking; and
- a tight 0.95–1.05 narration-tempo limit (failed timing must be rewritten, not rushed).

Review a small golden batch before changing `YT_QUALITY_V2_ENFORCE=1`. Keep
uploads disabled until that review is complete.

## Optional Local Model (Better Script Writer)

Install [Ollama](https://ollama.com), then pull a free model:

```bash
ollama pull llama3.2:3b
```

The app is already configured to use Ollama at:
- `http://localhost:11434/api/generate`

You can change provider/model in `config/settings.yaml`.

## Commands

```bash
python run.py init
python run.py channels

# build videos
python run.py build --channel ancient_history --kind short --dry-run
python run.py build --channel brain_lens --kind short --dry-run
python run.py build --channel ancient_history --kind video --dry-run
python run.py build --channel brain_lens --kind video --dry-run
python run.py build --channel ancient_history --upload

# scheduler
python run.py schedule --mode cron --upload

# feedback loop
python run.py feedback-sync --channel ancient_history --max-results 25
python run.py feedback-report --channel ancient_history
python run.py performance-report --channel ancient_history --days 60 --max-results 100
python run.py performance-report --channel brain_lens --days 60 --max-results 100
```

## Output Structure

Each run creates a timestamped folder in channel output:

- `topic.json`
- `narration.wav`
- `subtitles.srt`
- `thumbnail.jpg`
- `images/*.jpg`
- `short.mp4` or `video.mp4`
- `metadata.json`
- `sources.json`
- `short.media_validation.json` or `video.media_validation.json`
- `short.ffmpeg.log` or `video.ffmpeg.log`
- `frame_check/final_visual_qa.json`
- `frame_check/final_contact_sheet.jpg`

State and learning files:
- `data/state/runs.jsonl`
- `data/state/experiments.jsonl`
- `data/state/feedback_state.json`

## YouTube OAuth Flow

First upload run opens browser auth flow per channel and stores token:

- `secrets/tokens/ancient_history_token.json`
- `secrets/tokens/brain_lens_token.json`

## Free Resource Choices

- Trends: Google Trends (pytrends), Google News RSS.
- Images/video: Wikimedia Commons, Pexels, and Pixabay with source/license manifests; cited local diagrams are used only where the Ancient format allows them.
- TTS: local Kokoro voices configured per channel.
- Video Rendering: MoviePy + FFmpeg.
- Local Script Upgrade: Ollama local model (optional).
- Free hosted fallback: Hugging Face Inference router (optional key/quota).
- Upload + metrics: YouTube Data API v3.

## Regression Tests

```bash
set PYTHONPATH=src
python -m unittest discover -s tests -v
```

The suite includes adversarial silent-audio MP4s, timestamp holes, final-render
freeze/darkness checks, visual deduplication, source provenance, caption CPS and
phrase boundaries, editorial safety, metadata integrity and provider failover.

