# Brain Lens Presenter + Voice Upgrade Plan

## Research decision

- Use HeyGen as a trust layer, not as the whole video every time.
- Avoid full 8-10 minute talking-head videos; they usually feel static and hurt retention.
- Use local/open-source TTS only after sample testing. Prefer local HTTP-compatible engines so the bot can switch providers without code rewrites.
- Avoid Coqui XTTS v2 for commercial/monetized production unless licensing is reviewed carefully.

## Recommended production formats

### Shorts: premium presenter format

- 0-2s: cold hook with HeyGen close/medium shot.
- 2-24s: presenter + kinetic captions + 1-2 visual cutaways.
- 24-34s: emotional payoff, practical advice, loop ending.
- Use phrase captions: 2-4 words, never split relationship terms.

### Long videos: hybrid format

- 0-20s: HeyGen presenter intro.
- 20-120s: narration with diagrams, relationship examples, B-roll, text cards.
- Every 60-90s: return to presenter for a reset line.
- Final 20s: HeyGen presenter conclusion + subscribe line.

## Avatar strategy

### Do not use only the current close-up avatar

The current close-up is good for intimacy, but too much face-close framing can feel intense and repetitive.

### Create 3 Brain Lens presenters/looks

1. Calm Explainer — medium shot, soft studio, warm therapist vibe.
2. Dating Coach — slightly energetic, modern room, confident but respectful.
3. Myth Buster — desk/studio look, sharper tone for red flags and mistakes.

### Shot rules

- Shorts: use close-up only for the first hook or emotional punchline.
- Most clips: medium shot, chest-up, more background visible.
- Long videos: rotate medium shot + side angle + desk look.
- Avoid full-body unless using a studio/news layout.

## Voice strategy

### Test before switching production

Run:

```bat
E:\yt_automation\.venv\Scripts\python.exe E:\yt_automation\scripts\voice_lab.py
```

If a local Kokoro/Chatterbox server is running:

```bat
E:\yt_automation\.venv\Scripts\python.exe E:\yt_automation\scripts\voice_lab.py --http-url http://127.0.0.1:8880/v1/audio/speech --http-voice af_heart
```

### Production voice preference

1. HeyGen saved avatar voice for shorts with presenter.
2. Chatterbox or Kokoro local HTTP voice for long narration, if sample quality beats Edge.
3. Edge only as fallback.

## Immediate bot changes already applied

- Brain Lens captions now use tighter 4-word chunks.
- More relationship/psychology phrases are protected from bad subtitle splits.
- HeyGen overlay captions are smaller, cleaner, and less shouty.
- Optional local HTTP TTS backend added through environment variables.
- Voice lab script added for A/B testing voice quality before production switch.

## Next decision needed

Create these HeyGen looks next:

1. Brain Lens Medium Studio Explainer
2. Brain Lens Dating Coach Warm Room
3. Brain Lens Myth Buster Desk Presenter

Then we can rotate avatar by topic style.
