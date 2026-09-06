# Free API Growth Stack

Use free APIs as signal boosters, not as a replacement for retention. YouTube guidance says recommendations respond to viewer satisfaction, watch behavior, engagement, title/content relevance, and retention. The bot should use APIs to pick stronger topics, create sharper hooks, and improve visual relevance before upload.

## Current Priority Stack

1. Gemini API for creative direction
   - Use for topic scoring, title ranking, hook rewrites, script beat rewrites, and quality review.
   - Configure one authorized account with `GEMINI_API_KEY`; quota-key cycling is deliberately unsupported.
   - Keep Ollama fallback enabled so generation continues when Gemini is unavailable.

2. YouTube Analytics and Data API
   - Use actual channel results as the strongest feedback source.
   - Optimize for engaged-view rate, average view duration, title/thumbnail CTR, and repeat topic performance.
   - Avoid over-weighting raw views from very new or very low-sample videos.

3. Pexels and Pixabay
   - Use for stronger motion footage and stock visuals.
   - Cache API responses and downloaded assets for at least 24 hours to reduce quota pressure.
   - Prefer vertical/motion assets for Brain Lens; prefer artifact/map/documentary imagery for Ancient History.

4. Wikipedia/Wikimedia APIs
   - Use pageviews as a free topic-interest proxy.
   - Use Wikimedia Commons for real historical/artifact visuals when available.
   - Especially useful for Ancient History topics where factual trust matters.

5. GDELT
   - Use as a free trend radar for world-interest spikes and news-adjacent historical/psychology angles.
   - Best for "why this topic is suddenly relevant again" packaging.

6. Secondary AI APIs for fallback only
   - Groq can be useful for fast cheap/free title or hook scoring, but free limits vary by model.
   - Mistral's free API tier is mainly for evaluation/prototyping, not heavy publishing automation.
   - Hugging Face Inference Providers give small monthly free credits, useful for occasional model tests.
   - OpenRouter free models are useful for experiments, but daily free request limits are tight unless credits are added.

## Not Recommended As Primary

- Google Trends scraping libraries: useful but unofficial and fragile.
- Multiple API keys to bypass limits: risky; prefer caching, fallback models, and fewer high-value calls.
- Multiple accounts to bypass platform limits: avoid this; use legitimate failover, caching, and provider fallback.
- Free video-generation APIs: limits are too tight and output consistency is not reliable enough for scheduled publishing.

## Implementation Rule

For each planned video, the bot should generate multiple ideas but upload only the best one:

1. Create several topic candidates.
2. Score each for search relevance, novelty, emotional curiosity, evidence/visual availability, and past channel performance.
3. Rewrite the winning title and first hook.
4. Build the video.
5. Run a strict quality review.
6. Upload only if it passes.

## Sources

- Gemini API rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini API pricing/free tier: https://ai.google.dev/gemini-api/docs/pricing
- Gemini structured JSON output: https://ai.google.dev/api/generate-content
- YouTube performance FAQ: https://support.google.com/youtube/answer/141805
- YouTube thumbnail/title tips: https://support.google.com/youtube/answer/12340300
- YouTube engagement analytics: https://support.google.com/youtube/answer/9313698
- YouTube API quota: https://developers.google.com/youtube/v3/determine_quota_cost
- Pexels API limits: https://help.pexels.com/hc/en-us/articles/47677890260761-Is-the-Pexels-API-free-to-use
- Pixabay API docs: https://pixabay.com/api/docs/
- Wikimedia Pageviews API: https://wikitech.wikimedia.org/wiki/Analytics/AQS/Pageviews
- GDELT data/APIs: https://www.gdeltproject.org/data.html
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Mistral usage tiers: https://docs.mistral.ai/admin/user-management-finops/tier
- Hugging Face Inference Providers pricing: https://huggingface.co/docs/inference-providers/pricing
- OpenRouter limits: https://openrouter.ai/docs/api/reference/limits
