# Viral Publisher Playbook

This bot is optimized for ethical YouTube growth signals: viewer choice, retention, satisfaction, and clear metadata. It cannot force the algorithm to boost a video, but it can consistently publish videos that are easier for YouTube to understand and easier for viewers to choose and finish.

## Research Rules

- Shorts discovery is driven by viewer personalization and video performance, especially whether viewers choose to watch, stick around, and show satisfaction signals.
- Titles should be accurate, concise, front-load the important subject, and avoid misleading curiosity gaps.
- Tags are secondary metadata; YouTube says title, thumbnail, and description are more important for discovery.
- Hashtags should stay directly relevant and focused. Over-tagging reduces relevance and can cause hashtags to be ignored.
- YouTube Data API limits are enforced in the publisher: title up to 100 characters, description up to 5,000 bytes, and tags within a 500-character budget.

Official references:

- YouTube Shorts search and discovery: https://support.google.com/youtube/answer/11914225?co=YOUTUBE._YTVideoType%3Dshorts
- YouTube title and thumbnail tips: https://support.google.com/youtube/answer/12340300
- YouTube video tags guidance: https://support.google.com/youtube/answer/146402
- YouTube hashtag policies: https://support.google.com/youtube/answer/6390658
- YouTube Data API video metadata limits: https://developers.google.com/youtube/v3/docs/videos

## What The Bot Now Does

- Builds searchable titles without hashtags, spam punctuation, or all-caps bait.
- Generates descriptions with the subject in the first line, key viewer takeaways, source/context links, search context, and focused hashtags.
- Keeps backend tags inside YouTube's 500-character tag budget.
- Scores SEO metadata before upload and includes SEO issues in the quality gate.
- Down-weights fake “viral” winners with tiny samples so 1-4 view videos no longer dominate decisions.
- Refreshes old backlog metadata with the new SEO system immediately before publishing.
- Adds report recommendations for publish hours, duration buckets, title terms, and retention issues.

## Current Channel Priorities

### Secrets of Time

- Keep testing the existing stronger slots: `23:50`, `07:50`, `13:50`, `11:50`, plus the newer `18:50` test.
- Bias Shorts toward the current stronger duration window: under `33s`, unless the topic needs more context.
- Repeat high-performing title contexts carefully: empires, evidence, collapse, Rome, Bronze Age, Nazca, Constantinople.
- Avoid stacked hype words like “shocking secret forbidden”; use one curiosity word plus a concrete subject.

### Brain Lens

- Keep publishing around `09:45`, `15:45`, `17:45`, `18:45`, and `21:45`.
- Bias toward `33-36s` for new Shorts; avoid drifting past `50s`.
- Prioritize topics with personal relevance and clear behavior payoff: attention, body language, burnout, overthinking, memory, bias, micro-expressions.
- Avoid medical overclaims. Use educational language, not diagnosis/cure language.

## Operating Loop

1. Refresh analytics:
   - `python run.py performance-report --channel ancient_history --days 60 --max-results 100`
   - `python run.py performance-report --channel brain_lens --days 60 --max-results 100`
2. Generate and upload on schedule:
   - `python run.py schedule --mode cron --upload`
3. Review reports in:
   - `data/state/ancient_history_performance_report.json`
   - `data/state/brain_lens_performance_report.json`
4. Use `recommendations` from each report to adjust schedules, title angles, and duration targets.

