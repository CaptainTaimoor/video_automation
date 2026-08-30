# Quality V3 — free, human-first production runbook

This branch is the safer publishing path for the two channels. It is designed
for the current Windows machine (2 CPU cores, 12 GB RAM, GT 720M) and uses
network/free services only where they add value. A provider outage must result
in a slower retry or a held draft, never a low-quality upload.

## Route ladder

### Story and research

`config/settings.yaml` uses this bounded order:

1. Gemini free tier
2. Groq free tier
3. Cloudflare Workers AI (if configured)
4. OpenRouter `openrouter/free`
5. local Ollama

Blank credentials skip a route. Each failed route is recorded with a safe
provider/model/host summary, cooled down, and the next route is tried. The
curated history and Brain Lens fallback plans remain available when all model
routes are unavailable. Model output is accepted only after the deterministic
editorial checks; a model outage is not permission to publish a rejected draft.

### Visuals

- Ancient History: preferred Wikimedia Commons evidence spine first, then
  Wikimedia search, then licensed/cached archive candidates.
- Brain Lens: Pexels motion clips/photos first, with intent-specific queries;
  local stills are only a last-resort legacy path.
- Every selected asset is cached, rights-labelled, checked for resolution,
  deduplicated within the run and against recent media memory, and matched to
  its scene claim. V3 rejects local fact-card placeholders for Ancient Shorts.
- Free generative-video services are intentionally not in the primary route:
  their quotas and visual consistency are worse than licensed stills/clips on
  this hardware. Pillow/FFmpeg Ken Burns motion supplies lightweight movement.

### Narration and mix

Edge Neural TTS renders one continuous PCM narration stream per story. Channel
defaults are `+2%` for Ancient History and `-7%` for Brain Lens; override them
only after listening to a proof. Brain beat boundaries use light commas to
avoid the repeated, mechanical pauses caused by separate sentences. If Edge
is unavailable, the configured Kokoro, Piper, and offline fallbacks are tried
in order. The result is held if measured active pace, silence, level, or
coverage is outside the channel limits. Background music is disabled by
default so voice remains clean; it can be reintroduced only after a human
listening check.

## Quality gates

The build runs these gates before upload:

1. **Editorial:** complete sentences, a concrete hook, no repeated published
   angle, topic anchors in the narration, and caption-safe beat boundaries.
2. **Visual:** scene/claim intent, licensed source URL and label, resolution,
   duplicate/reuse checks, dark/black-frame checks, and final visual relevance.
3. **Audio:** continuous PCM analysis for active WPM, silence ratio, RMS/peak,
   clipping, and exact audio/video coverage. Narration is never hard-trimmed to
   fit a duration target.
4. **Render QA:** decode the final MP4, check PTS gaps, freezes, static runs,
   black frames, caption timing and readable caption length.
5. **Publishing:** only `quality_decision=pass` is upload-eligible. A held
   output is retained for review and is never silently promoted by a fallback.

The bot may therefore stop when all providers are down. That is intentional:
"never fails" means it never publishes a broken or visibly bad video.

## Recommended operation

1. Copy `.env.example` to `.env` and add only the free keys available to you.
2. Keep `YT_QUALITY_V3=1`, `YT_QUALITY_V2=1`,
   `YT_QUALITY_V2_ENFORCE=1`, and `YT_QUALITY_V2_DRAFTS=0` for production.
3. Run one dry-run Short per channel and inspect the MP4/contact sheet.
4. Run one dry-run long video per channel only after both Shorts pass.
5. Enable scheduled publishing only after the human review gate is accepted.

Example proof command (PowerShell):

```powershell
$env:PYTHONPATH = "src"
$env:YT_QUALITY_V3 = "1"
$env:YT_QUALITY_V2 = "1"
$env:YT_QUALITY_V2_ENFORCE = "1"
python run.py build --channel brain_lens --kind short --dry-run
```

Generated media belongs in `output/` and is ignored by Git. Only the source,
tests, configuration template and this runbook are committed to the branch.
