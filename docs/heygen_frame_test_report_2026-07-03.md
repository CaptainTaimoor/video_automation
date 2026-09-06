# HeyGen Frame Test Report - 2026-07-03

## What was tested
- Direct Playwright/HeyGen browser flow using uploaded `BL Medium Studio` look group.
- Exact frame path: `BL Medium Studio > Look 01`.
- Stable group path: `BL Medium Studio`.
- Extension/render-download capture path.

## Result
- HeyGen group page showed 10 uploaded photo looks.
- Exact frame selection by index can select the card, but HeyGen opens the editor with an extra blank Scene 2 selected.
- Script paste then fails because the selected scene has `No Avatar`.
- Because Look 01 failed this structural UI step, the other new uploaded photo looks are not safe to enable yet; they use the same HeyGen photo-look flow.
- Stable `BL Medium Studio` flow renders and downloads successfully.

## Successful proof
- Rendered/downloaded file: `E:\yt_automation\output\brain_lens\short\2026-07-03\20260703_143859\short.mp4`
- Quality score: `88/100` before latest duration fix.
- Main issue was duration `30.73s`; scripts were extended after this test to target `33-36s`, which should remove the -12 duration penalty.

## Production decision
- Production remains on stable `BL Medium Studio` only.
- Exact uploaded frames are not enabled until the blank Scene 2 HeyGen UI issue is fixed and each frame passes paste + render + download.
