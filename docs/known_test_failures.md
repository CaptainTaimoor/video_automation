# Known Test Failures

As of the `ready-queue` snapshot (`8e0d45c`, 2026-09-07) the suite reports
**306 passed, 5 failed**. All five failures are pre-existing and are *not*
environment or platform problems — they reproduce on a clean checkout.

## Cause

Commit `31d7f3c` ("Add Ready Queue pipeline and simplify the ops dashboard")
rewrote large parts of `src/yt_auto/pipeline.py` (+592 lines) and
`src/yt_auto/images.py`, changing Ancient-visual selection behaviour. Among
other changes it lowered:

```
ANCIENT_SHORT_MIN_VERIFIED_REAL_VISUALS: 8 -> 6
```

That commit updated only `tests/test_claim_card.py` and
`tests/test_ready_queue.py`. It left `tests/test_images_visual_pipeline.py`
and `tests/test_editorial_quality.py` untouched, so those still assert the
older behaviour.

## The failures

| Test | Expected | Actual |
|---|---|---|
| `test_ancient_short_preflight_requires_eight_unique_verified_real_sources` | complaint at <8 visuals | no complaint (threshold is now 6) |
| `test_ancient_continuity_fallbacks_are_polished_before_selection` | 14 prepared | 19 prepared |
| `test_ancient_continuity_reuses_safe_sources_from_caption_held_run` | 1 cached | 2 cached |
| `test_ancient_recovery_sorts_full_pool_before_candidate_limit` | 6 ordered | 2 ordered |
| `test_ancient_fetch_primes_broad_subject_archive_before_scene_queries` | — | `IndexError` in stubbed path |

## Deciding what to do

Each one needs a product decision, not a mechanical fix — the question is
whether the *current* behaviour is intended:

- If the lower thresholds are intended (fewer publishing gaps), update the
  tests to match the new constants.
- If 8 verified visuals is still the real quality bar, restore the constant
  and re-check why the count changed.

Do not "fix" these by loosening the assertions without deciding which
behaviour you actually want on the live channels.
