# Free local visual-library research and rollout plan

## Decision

Use an **on-demand curated cache**, not a bulk archive mirror. Keep SQLite as the
authoritative catalog and use its FTS5 text search first. The current bot already
records source URL, provider asset ID, license, creator, dimensions and perceptual
hashes; preserving that work is lighter and safer than adding a vector server.

Add `sentence-transformers/all-MiniLM-L6-v2` (384-dimensional text embeddings)
only after the approved local collection grows beyond roughly 10,000 assets or
FTS retrieval measurably misses relevant scenes. LanceDB is the preferred optional
embedded vector layer because it runs from a local directory and needs no daemon.
Do not run Qdrant, Chroma, a GPU vision model, or an archive crawler on this PC.

## Approved source ladder

1. **Existing approved local cache** — fastest and immune to provider outages;
   still pass scene relevance, freshness, rights, resolution and pHash checks.
2. **Wikimedia Commons** — best primary history source. Use `imageinfo` with
   `extmetadata`, keep the file page, creator, license name/URL and attribution.
3. **Library of Congress** — JSON API requires no key and includes film/video,
   photographs and streaming derivatives. Accept only records whose item-level
   rights statement permits reuse; LOC does not own every item.
4. **Europeana** — Search/Record APIs require a free key. Only admit Tier 4/open
   reuse objects and preserve each record's rights URI.
5. **Smithsonian Open Access** — admit only explicitly CC0-designated assets.
6. **NASA Image and Video Library** — useful for history-of-science episodes.
   Acknowledge NASA, reject marked third-party media, avoid endorsement/logo use,
   and flag identifiable people for rights review.
7. **Internet Archive** — discovery source only. Admit an item only when explicit
   metadata supplies an allowed public-domain or Creative Commons license; being
   downloadable from archive.org is not proof of reuse rights.
8. **Pexels/Pixabay** — primary motion suppliers for Brain Lens. Cache search
   responses for 24 hours, keep attribution data, download selected media locally,
   and never systematically mirror either library.

Primary references:

- Wikimedia reuse and metadata:
  https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia/en
  and https://www.mediawiki.org/wiki/API:Imageinfo
- Library of Congress APIs and rights:
  https://www.loc.gov/apis/json-and-yaml/ and
  https://www.loc.gov/collections/guide-records/about-this-collection/rights-and-access/
- Europeana APIs and reuse:
  https://www.europeana.eu/en/apis and
  https://www.europeana.eu/eu/stories/learn-how-to-reuse-europes-digital-cultural-heritage
- Smithsonian Open Access: https://www.si.edu/openaccess/faq
- NASA media guidelines: https://www.nasa.gov/nasa-brand-center/images-and-media/
- Internet Archive developer APIs: https://archive.org/developers/
- Pexels API: https://www.pexels.com/api/documentation/
- Pixabay API: https://pixabay.com/api/docs/
- LanceDB embedded search: https://docs.lancedb.com/quickstart
- MiniLM embeddings: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2

## Automatic rights policy

Auto-admit only Public Domain/PDM, CC0, `No restrictions`, CC BY, Pexels License,
or Pixabay License with a verified source page. Reject unknown, all-rights-reserved,
CC-NC and CC-ND. Put CC BY-SA in review rather than silently mixing share-alike
terms into a standard YouTube upload. Never infer a license from the host domain.

Every catalog row needs:

- stable provider ID, canonical media URL and source-page URL;
- local path, SHA-256 and perceptual hash;
- provider, media type, width, height, duration, FPS and orientation;
- title, description, tags, subject, location, period and scene-action terms;
- license name/URI, creator, attribution text and verification timestamp;
- blur/darkness/motion/relevance measurements;
- use count, last-used timestamp, episode ID and rejection reason.

## Retrieval and edit algorithm

1. Convert each narration beat into concrete entities, place/period, action and
   shot intent. Search exact entity terms before broad concepts.
2. Apply hard filters before ranking: allowed rights, verified source page,
   adequate resolution/orientation, decodable file, no watermark/placeholder,
   no generated card, and no child/unsafe mismatch.
3. Retrieve local FTS candidates, then query providers only for missing coverage.
4. Rank scene match first, then real motion, technical quality, visual novelty and
   low prior-use count. A pHash near-duplicate never becomes a new candidate.
5. Distribute all unused qualified assets before recycling. Reuse requires at
   least three different intervening physical shots; a returning video starts at
   a different audited offset.
6. If a provider is down, continue local -> next provider. If real coverage is
   still inadequate, hold the video for retry. Never hide the failure with a
   blank frame, generated fact card or unrelated image.

## Resource and storage limits

- One downloader and one renderer at a time; no continuously running AI service.
- Download only selected assets, with a configurable 25-50 GB cache ceiling.
- Retain small thumbnails and metadata after eviction; redownload by canonical ID.
- Use WAL-mode SQLite, batched writes and a daily integrity/license recheck queue.
- Pre-compute MiniLM vectors once per asset only if the optional semantic phase is
  enabled. Text vectors are much cheaper than CLIP frame embeddings.

## Publishing throughput

The requested minimum schedule is 5 longs plus 10 Shorts per channel per day.
Generation slots are staggered and every slot is fail-closed; the bot must not
replace a failed build with near-identical filler. YouTube documents a separate,
account-dependent daily channel upload limit, and its spam/monetization rules now
explicitly reject automated high-volume, minimally changed content. If either
channel starts producing interchangeable videos or hits its channel limit, the
scheduler must build a reviewed backlog instead of flooding uploads.

References:

- https://support.google.com/youtube/answer/57407
- https://support.google.com/youtube/answer/2801973
- https://support.google.com/youtube/answer/1311392
- https://developers.google.com/youtube/v3/docs/videos/insert

## Rollout

1. **Implemented now:** global three-shot reuse cooldown, early full-library
   distribution, generated-graphic upload hold, final timeline repeat gate, and
   staggered 5-long/10-Short per-channel schedule.
2. **Next safe increment:** add LOC/Europeana/Smithsonian adapters and persist
   approved downloads in the existing SQLite state/catalog.
3. **After real usage data:** enable MiniLM + LanceDB only if FTS retrieval recall
   is demonstrably insufficient. Keep SQLite as provenance/rights authority.

