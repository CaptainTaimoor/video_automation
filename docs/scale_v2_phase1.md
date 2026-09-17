# Scale v2 — Phase 1

Isolated worktree branch for higher daily volume without touching the live `master` bot.

## Worktree

- Path: `E:\yt_automation_scale_v2`
- Branch: `feature/scale-v2`
- Live bot stays on `E:\yt_automation` (`master`). Do not stop/restart it for this work.

## Phase 1 schedule (Asia/Karachi)

Per channel (settings on this branch only):

- Shorts: **9** timed slots (~8–10 target)
- Longs: **3** timed slots (~2–3 target)
- `daily_upload_cap`: **12** (room for Phase 1; not the later 14–18 / 7–8 push)

Do **not** enable the larger Phase 2 cadence yet (YouTube risk + weak PC).

## API keys

Copy from `.env.example` into a local `.env` (never commit secrets):

```text
GROQ_API_KEY=...          # preferred after Gemini — PASTE KEY HERE (missing on this PC as of Phase 1 QA)
MISTRAL_API_KEY=...       # optional
GEMINI_API_KEY=...        # existing
HF_API_KEY=...            # existing Hugging Face gateway
```

Provider order: `gemini → groq → openai_compatible (HF) → ollama`.

Worktree `.env` may inherit missing keys from the sibling live `E:\yt_automation\.env`
(without overriding local values). `GROQ_API_KEY` must still be pasted by the operator;
routing already skips Groq cleanly when the key is empty.

Ollama: prefer `qwen2.5:7b`. Full script drafts are refused on `llama3.2:3b` (3B remains OK only for tiny JSON repair/scoring).

Gemini paid / higher quotas are **not** required for Phase 1.

## Shared assets (music)

Channel music lives under `assets/music/<channel_id>/`. Worktrees should junction or
symlink to the live checkout:

```powershell
cmd /c mklink /J E:\yt_automation_scale_v2\assets\music E:\yt_automation\assets\music
```

Or set `YT_MUSIC_DIR=E:\yt_automation\assets\music`. The pipeline also falls back to the
sibling live music folder automatically when the worktree copy is empty.

## Shadow dry-runs (no upload, no live-bot restart)

From the worktree, with the main venv Python if needed:

```powershell
cd E:\yt_automation_scale_v2
$env:PYTHONPATH = "E:\yt_automation_scale_v2\src"
# Optional: point state/output under the worktree to avoid colliding with live runs
# $env:YT_STATE_DIR = "E:\yt_automation_scale_v2\data\state"

E:\yt_automation\.venv\Scripts\python.exe run.py build --channel ancient_history --kind short --dry-run
E:\yt_automation\.venv\Scripts\python.exe run.py build --channel ancient_history --kind video --dry-run
E:\yt_automation\.venv\Scripts\python.exe run.py build --channel brain_lens --kind short --dry-run
E:\yt_automation\.venv\Scripts\python.exe run.py build --channel brain_lens --kind video --dry-run
```

Always use `--dry-run`. Check `data/state/active_builds/` on the live tree before starting; wait if that channel already has an active build.

## P0 behaviour shipped here

1. **Caption CPS auto-repair** — shorts reflow / slight narration stretch toward 16.5 CPS before hard abort at 17.
2. **Failed topics not burned** — caption/render failures go to `failed_topics.jsonl`; `used_topics` only after upload or quality-pass ready build.
3. **Groq routing** — first-class provider + allowed hosts `api.groq.com` / `api.mistral.ai`.
4. **Concurrent builds** — scheduler semaphore max **2**, with optional psutil free-RAM check.
5. **Phase 1 schedule** — 9 shorts + 3 longs per channel in `config/settings.yaml`.

## P1 QA follow-ups (this pass)

1. **Music mix gate** — worktree junctions `assets/music` → live checkout; pipeline falls back to sibling `yt_automation/assets/music` / `YT_MUSIC_DIR`. Quality review now verifies real audio files.
2. **Brain Lens deterministic fallback** — AI provider repair attempted first; only **one** template last-resort (disable with `YT_ALLOW_DETERMINISTIC_BRAIN_FALLBACK=0`).
3. **Ancient short pool** — visual-ready subjects expanded; session caption rejects no longer exhaust into continuity-only as quickly.
4. **Short caption reflow** — wider 6-word budget + readable clause continuations aligned between render and quality review.
5. **Long skeleton variation** — mild subject-seeded chapter rotation (not full Phase 1.5 uniqueness).

## Explicitly deferred (next passes)

- Wikidata topic factory (design stub only)
- MoviePy → FFmpeg rewrite for longs
- Piper as default TTS
- Oracle Cloud
- Unique long-form structure overhaul (Phase 1.5)
- Stopping / restarting the live master bot
- Operator must paste `GROQ_API_KEY` for stronger Brain Lens AI drafts (key absent on this PC)
