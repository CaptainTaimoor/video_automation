# YouTube Shorts Automation (Free-First, Local)

This project builds and uploads automated YouTube Shorts and 8–10 minute videos for two niches/channels using free/local tools and authorized API tiers where possible.

## What It Does

- Picks trend-aware topics per niche (Google Trends + Google News RSS + seed keywords).
- Generates script drafts from trend context.
- Routes script work across Gemini, a Hugging Face Qwen fallback, an optional
  authorized OpenAI-compatible gateway, and local Ollama with health cooldowns.
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

**Windows live box / one-click:** run `setup\INSTALL.bat` as Administrator. It installs Python/Node if needed, creates `.venv`, installs dependencies, configures Windows auto-start, and opens LAN dashboard access. Use the `controls\` folder to enable/disable autostart and LAN.

**Linux / macOS (development and builds):** run `bash setup/install.sh`. It checks the
Python version, creates `.venv`, installs dependencies, scaffolds `.env` and creates the
runtime directories. It does not install auto-start services or touch the firewall —
those steps are Windows-only and belong to the publishing box.

Manual setup:

1. Install Python 3.10+.
2. Create/activate a virtual environment.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy env template:

```bash
copy .env.example .env      # Windows
cp .env.example .env        # Linux/macOS
```

5. Put your YouTube OAuth client file at `client_secrets.json`.
6. Optional keys in `.env`:
   - `GEMINI_API_KEY` for the primary Gemini writer.
   - `HF_API_KEY` for the Hugging Face Qwen free-tier fallback.
   - `YOUTUBE_API_KEY` for the optional YouTube comment-read API-key fallback.
   - `STABLE_HORDE_KEY` for AI image fallback.
   - `PIXABAY_API_KEY` for additional stock images.

## Free, Authorized AI Routing

`config/settings.yaml` uses a bounded provider order:

1. Gemini API
2. Hugging Face's OpenAI-compatible router with `Qwen/Qwen3-8B`
3. Local Ollama

You may point `AI_GATEWAY_URL`, `AI_GATEWAY_MODEL`, and `AI_GATEWAY_API_KEY`
at another OpenAI-compatible service you are authorized to use, after adding its
exact hostname to `openai_compatible_allowed_hosts`. Set the URL and model
together; an environment override always uses `AI_GATEWAY_API_KEY` and never the
fixed Hugging Face profile's `HF_API_KEY`. Every gateway must use HTTPS. Generic
loopback gateways are deliberately rejected; local inference uses an Ollama
`/api/generate` URL restricted to `localhost` or a loopback IP. 9Router is not
integrated, and this project does not automate account farming, free-trial
cycling, browser-cookie clearing, or unofficial OAuth token relays.

Each build records sanitized provider attempts with a call purpose and whether
the response was actually consumed. `last_ai_provider` identifies the most
recent accepted AI-assisted task. The backward-compatible `script_provider`
fields are populated only when model-generated narration for the selected topic
passes the editorial gates; an AI title, score, or review never claims script
authorship. Invalid JSON or schema output is rejected and routed to the next
configured provider.

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

Install the test extras once, then run the suite from the project root:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

`conftest.py` puts `src/` on `sys.path`, so pytest needs no `PYTHONPATH` export and
no package install. The stdlib runner does not read `conftest.py`, so it still needs
the path set explicitly:

```bash
set PYTHONPATH=src            # Windows
PYTHONPATH=src python -m unittest discover -s tests -v   # Linux/macOS
```

The suite includes adversarial silent-audio MP4s, timestamp holes, final-render
freeze/darkness checks, visual deduplication, source provenance, caption CPS and
phrase boundaries, editorial safety, metadata integrity and provider failover.

