# Recovery notes — what the lost PC (`E:\yt_automation`, branch `master`) had

Reconstructed 2026-09-20 by reading the full "Project analysis and growth suggestions"
session. The old machine is gone; none of this was pushed to GitHub. This file is the
record of what existed, so it can be rebuilt without re-reading the chat.

Old test suite: **734 passed, 1 skipped, 192 subtests**. This repo: **369**.

## Modules that exist there and not here

| File | Size / purpose |
|---|---|
| `pipeline_quality.py` | 2037 lines. The upload gate. |
| `pipeline_visuals.py` | 1850 lines. Visual selection + curated episode packs. |
| `pipeline_presenter.py` | 781 lines. |
| `pipeline_reporting.py` | 705 lines. Holds `_gate_code_version()`. |
| `pipeline_backlog.py` | 539 lines. |
| `pipeline_metadata.py` | 443 lines. |
| `short_composer.py` | `ShortComposerMixin` — composes a Short, repairs against the gate. |
| `brain_hooks.py` | 130 lines. One shared definition of a good Brain Lens opener. |
| `upload_plan.py` | +176. Daily plan: random count in range, times from analytics. |
| `publish_spacing.py` | +113. 30-minute minimum gap, checked against YouTube at upload. |
| `api_keys.py` | +367. Multi-key store with rotation. |
| `dashboard_views.py` | +513. Lives in `scripts/`, not `src/yt_auto/`. |

`pipeline.py` there is 1900 lines of orchestration only — the 7945-line original was split
into the six mixins above (commit `0b05a5d`, verified as a pure move: all 110 method
bodies byte-identical, same 116 public members).

## Key constants

| Setting | Value |
|---|---|
| Brain Lens Short words | 122–142 (`_BRAIN_SHORT_MIN_WORDS` / `_MAX_WORDS`) |
| Recovery floor | 114 (`_BRAIN_SHORT_RECOVERY_MIN_WORDS`), under `YT_CONTINUITY_RECOVERY` |
| Max visible characters, any Short | 1000 |
| Slot retry limit | 8/day, 20 min apart (`BACKFILL_MAX_ATTEMPTS` / `_COOLDOWN_MINUTES`) |
| Publish spacing | 30 minutes, never moves a video earlier |
| Long video | 4–6 min, ~620 words (was a hard 1250-word floor forcing 9 min) |
| Caption speed | 19.5 CPS, gate and renderer locked together by a test |
| Upload ranges | Ancient 6–8 long / 10–12 short · Brain Lens 5–7 long / 8–10 short |
| Best hours | Ancient 21:00, 19:00, 17:00, 20:00 · Brain Lens 21:00, 17:00, 23:00 |

## Bugs found and fixed there (each cost real uploads)

| Bug | Effect |
|---|---|
| Four disagreeing Brain Lens rule lists | 99 of 111 builds failed. `"reply"` vs `"replies"`; `friendship to romance` had no entry |
| Caption splitter scored mistakes backwards | One Machu Picchu fact failed 581 times in 36h |
| Caption speed 19.5 build vs 17.0 approve | Every finished Ancient Short rejected |
| Headline looked up as a Wikipedia page | "no source" → same dead topic 12× → build fails |
| Wikipedia rate-limiting (generic User-Agent) | 429 read as "topic has no source" |
| Video packs counted photos only | Brain Lens clip packs thrown away |
| Recovery mode permanently on | Pompeii uploaded 12 times |
| Two uploader classes, patched the wrong one | `publish_at` TypeError killed 2 finished longs |
| Upload-gap monitor | Killed a long at segment 49 of 49 |
| 05:30 plan refresh saw uploaded slots as empty | Duplicate videos at the same minute |
| Dashboard re-rendered every 3s | Destroyed the button mid-click; "Retry all" looked dead |
| `_require_token` always returned True | Every LAN device could POST control actions |
| Preflight floor 6 vs reuse threshold 8 | 30% of videos shipped 2 fake text-on-colour cards |
| OpenRouter switched to raw PCM | 18 builds failed on unreadable audio; fixed by wrapping a WAV header |

## Decisions worth keeping

- **OpenRouter has no TTS.** 437 models, zero speech. Its only audio models are Lyria (music).
- **Deepgram Aura-1** — $200 credit ≈ 54 months, no card. Ranked best.
- **Openverse** — no API key, hundreds of commercial-use images for every failing subject.
  Museum APIs (Met, Smithsonian) are thin for archaeology.
- **Voice**: user picked Gemini **Iapetus**. Cloning another creator's voice was declined;
  cloning the user's own was offered instead.
- Fallback order: Deepgram → Inworld → Edge (1.7s). Edge must sit directly behind tier 1;
  Piper/Kokoro in between cost 104s/48s and risk missing a slot.

## Still unknown

The 3 files hidden behind "Show 3 more" in one message were `script_writer.py`,
`pipeline_reporting.py` and `test_brain_hooks.py` — all since identified. No remaining gaps
in the narrative, but the **actual diffs are unrecoverable**: the session's diff viewer
fetches them from the dead machine and returns "No changes to show".
