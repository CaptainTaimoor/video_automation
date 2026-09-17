# Competitor Analysis + Upload Plan (2026-09-05)

## Our baseline (current analytics)

| Channel | Total views (report) | Retention | Engaged-view rate | Best hours (local) | Best Short titles pattern |
|---|---:|---:|---:|---|---|
| **Secrets of Time** (`ancient_history`) | ~10.4k | **76.9%** (strong) | 45.5% | **00, 15, 10, 19, 8** | evidence / artifact / reframe |
| **Brain Lens** (`brain_lens`) | ~1.4k | **31.9%** (weak) | 23.9% | **10, 9, 5, 22, 2** | anxiety / signal / addictive |

Gap vs leaders (avg of each leader’s top sampled videos vs our best Short):
- Ancient vs `@HistoryMarche`: ~1400× (scale + packaging gap, not just topic)
- Brain vs `@charismaoncommand`: extreme scale gap; **retention is the first fix**, not more uploads

Artifacts saved under `data/state/competitor_analysis/` (ranked JSON, downloads, frames, thumbs).

---

## Best competitors found

### Secrets of Time niche

| Rank | Channel | Followers | Avg top-sample views | Format signal | Why it beats us |
|---:|---|---:|---:|---|---|
| 1 | **@HistoryMarche** | 1.32M | **1.40M** | **10/10 Shorts** (~51s avg) | Vertical map storytelling, question hooks, motion + labeled empires |
| 2 | **@VoicesofthePast** | 1.09M | **1.37M** | Long primary-source docs | Deep story authority for long-form DNA |
| 3 | **@HistoryMatters** | 1.92M | **1.14M** | Short animated “Why …?” explainers (~8–9 min + Shorts) | Clean question packaging |
| 4 | **@Toldinstone** | 648k | 149k | Archaeology / ruins evidence | Closest topic twin to our winning “artifact/evidence” titles |

Dropped as primary DNA: `@KnowledgeHub` (weak/unavailable in scout). Keep `@WeirdHistory` / `@SimpleHistory` as secondary inspiration only (brand mismatch vs evidence-led tone).

### Brain Lens niche

| Rank | Channel | Followers | Avg top-sample views | Format signal | Why it beats us |
|---:|---|---:|---:|---|---|
| 1 | **@charismaoncommand** | 7.11M | **2.26M** | Shorts-heavy social skills | Face + punchy How-to claim, viral social proof clips |
| 2 | **@JimmyonRelationships** | 1.48M | 204k | Dating/relationship coaching | Closest topic twin |
| 3 | **@HealthyGamerGG** | 3.45M | 125k | Therapist Shorts | Top title card + big captions + trust face |
| 4 | **@ImprovementPill** | 3.77M | 73k | **10/10 Shorts** graphic explainers | Clean listicle panels, high-contrast hooks |

`@Psych2Go` remains large but weaker short-sample average in this scrape; demoted from primary DNA.

---

## Visual analysis (from downloaded samples + frames)

### Ancient winners look like
- **Vertical 9:16 maps** with parchment texture, color-coded empires, large serif labels
- Mid-video **portrait callout cards** for named leaders
- Slow push/pan motion; information hierarchy is empire → city → person
- Titles: **How / Why / Could … ?** (~8–12 words), often ending in a concrete battle/place

### Brain winners look like
- **Face-first** or **flat graphic panel** (not busy stock collage)
- **On-screen title in second 0–1**
- Huge outlined captions mid-frame
- Titles: **How to … / Why …** with one emotional payoff
- Warm practical lighting (HealthyGamer) or bold flat color (ImprovementPill)

### What our bot should copy (safe / ethical)
- Packaging + pacing + structure — **not** their footage, logos, or scripts verbatim
- Ancient: more map/artifact clarity, fewer static stock loops
- Brain: stronger first-second text hook + caption readability (retention 31.9% is a first-2-seconds problem)

---

## Content rules to enforce

### Secrets of Time
1. Prefer titles with **evidence / artifact / map / battle / Why-How-Could**
2. Keep Shorts **50–59s** (competitors win near ~50–55s when visuals are strong)
3. First 2 seconds: place/year/claim on screen
4. Longs: Voices-of-the-Past style chapters + Toldinstone evidence objects

### Brain Lens
1. Temporary **quality > quantity** until retention ≥ ~55% and engaged-view rate ≥ ~40%
2. How/Why titles only for the next 14 days
3. Burned-in captions large; top claim card in first second
4. Ban weak recycled patterns; keep educational (no diagnosis language)

---

## New upload schedule (applied in `config/settings.yaml`)

> Scheduler jobs are registered at process start. **Gracefully restart the scheduler** to load new times (do not hard-kill mid-render). Builds will already pick up new viral DNA on the next job.

### Secrets of Time — 7 Shorts + 3 Longs / day (cap 10)
| Kind | Times |
|---|---|
| Shorts | **00:20, 08:20, 10:20, 12:50, 15:20, 17:50, 19:20** |
| Longs | **09:30, 16:30, 22:30** |

Removed weak overnight volume (`02:20`, `04:50`) that our analytics did not support.

### Brain Lens — 6 Shorts + 2 Longs / day (cap 8)
| Kind | Times |
|---|---|
| Shorts | **05:50, 09:20, 10:20, 14:20, 17:20, 22:20** |
| Longs | **11:00, 18:00** |

Aligned to our stronger hours; cut volume to recover retention.

Gap monitor: `max_upload_gap_hours: 4.0` for both (was 3.0) so recovery is less thrashy while quality gates hold weak builds.

---

## Bot config updates already made
- `viral_dna_channels` replaced with the four winners per niche above
- `data/state/*_viral_dna.json` refreshed from this analysis
- Schedule + daily caps updated as above

## 14-day operating loop
1. Restart scheduler once (graceful) to apply times
2. Watch dashboard: retention, upload gap, recovery category
3. Re-run `python run.py performance-report --channel ancient_history --days 28` and same for `brain_lens` after day 7
4. If Brain retention still <50%: drop to **4 Shorts/day** (09:20, 10:20, 17:20, 22:20)
5. Re-scout competitors monthly into `data/state/competitor_analysis/`

## Files
- Ranked scout: `data/state/competitor_analysis/ranked_competitors.json`
- Downloads: `data/state/competitor_analysis/samples/`
- Frames/thumbs: `data/state/competitor_analysis/frames/`, `thumbs/`
- Combined analysis: `data/state/competitor_analysis/content_visual_analysis.json`
