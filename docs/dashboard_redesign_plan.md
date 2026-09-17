# Dashboard Redesign Plan (Automation Bot)
**Date:** 2026-09-06  
**Goal:** One clear screen that shows bot health + real YouTube numbers, so you know what to do next.

---

## 1) Why redesign?

Today the dashboard shows ops status (scheduler, gaps, backlog), but YouTube Analytics data is only partly used.
We already pull YouTube Analytics (views, watch time, retention, likes, comments, shares, subs gained, best hours).
The new dashboard must show that data clearly for both channels.

**Simple idea:**  
Left = Is the bot OK?  
Right = Are the videos growing / making money progress?

---

## 2) Who uses it / what they need

| Who | Needs in 10 seconds |
|---|---|
| You (admin) | Is bot posting? Any problem? Which channel is winning? |
| Growth | Best hours, top videos, retention |
| Money | Path to YouTube Partner Program (subs / Shorts views / watch hours) |

---

## 3) Design principles (simple)

1. **One home page** — no hunting
2. **Two channel tabs** — Secrets of Time / Brain Lens
3. **Big numbers first** — views, retention, uploads today
4. **Red / yellow / green** — problems obvious
5. **Mobile + LAN friendly** — phone on same Wi‑Fi
6. **No fake charts** — only real API / bot data
7. **Action line** — always one “do this next”

---

## Ready Queue + multi-page ops (2026-09)

The live dashboard is now a hash-routed multi-page app (still one HTML file):

Home · Queue · Channels · Analytics · Suggestions · Settings · Runs/Logs

Backend Ready Queue lives in `src/yt_auto/ready_queue.py` with scheduler hooks and
token-gated POST APIs. See [ready_queue_ops.md](ready_queue_ops.md).

---

## 4) Information architecture (pages)

### A) Home / Command Center
- Bot status strip: Scheduler · Watchdog · Dashboard · Guard
- Today uploads (Shorts / Longs) per channel
- Upload gap warning
- One recommended action
- Money checklist summary (Phase D)

### B) Channel deep page (one per channel)
Tabs inside channel:
1. **Growth** — views, retention, engaged rate, top/weak videos
2. **Timing** — best publish hours chart + current schedule
3. **Quality** — gate holds, fail reasons, claim-card / hook issues
4. **Money** — YPP progress + affiliates + Facebook
5. **Runs** — last builds/uploads with links

### C) System / Settings (small)
- Encoder, schedule caps, LAN URL, phase flags
- Buttons: Restart scheduler, Open controls folder (link text only)

---

## 5) Exact data we already have (use these)

From `performance-report` + YouTube Analytics API:
- Daily views / engaged views / watch minutes
- Average view duration + % viewed (retention)
- Likes, comments, shares, subscribers gained
- Per-video analytics + titles
- Best publish hours
- Short vs long summary
- Recommendations text

From bot state:
- Scheduler / watchdog heartbeats
- Backlog pending / failed / quality-skipped
- Upload continuity gap
- Today generated vs uploaded
- Quality gate streak + reasons
- Monetization checklist
- Latest short/long file status

**Gap to fill in redesign:**
- Show **subscriber total** (Studio / Channels API) if available — today we mostly have “subs gained in sample”
- Show **90-day Shorts views** and **12-month watch hours** as clear YPP bars
- Auto-refresh performance report nightly into dashboard (don’t require manual CLI)

---

## 6) Screen layouts (extensive but clear)

### 6.1 Top bar
- Brand: “YT Automation”
- Live pill: Scheduler OK / Warn / Down
- Updated time
- LAN hint: `http://YOUR-IP:8787`

### 6.2 KPI row (5 cards)
1. Scheduler health  
2. Uploads today (both channels)  
3. Worst upload gap  
4. Total tracked views (period)  
5. Money progress (e.g. 2/6 checklist)

### 6.3 Dual channel cards
Each card:
- Channel name + status color
- Today: Shorts uploaded / Longs uploaded / Held by quality
- Views (7d / 28d)  
- Retention %  
- Engaged-view rate  
- Best hour  
- Next scheduled job  
- Action sentence (“Fix 1 failed upload” / “Healthy”)

### 6.4 Analytics section (new — main redesign win)
For selected channel:
- **Line chart:** views per day (from analytics `top_days` / day rows)
- **Bar chart:** best publish hours
- **Table:** Top 10 videos (views, retention, likes, kind Short/Long)
- **Table:** Weak videos (to stop repeating)
- **Insight chips:** copy from `recommendations[]` in plain words

### 6.5 Money section
Progress bars:
- Subscribers → 1,000  
- Shorts views (90d) → 10,000,000  
- Long watch hours (12mo) → 4,000  
Checklist: affiliates filled? Facebook on? Short→long CTA on?

### 6.6 Ops / failures section
- Failed backlog items with real error text (click expand)
- Quality hold reasons (top 5)
- Recent runs list with YouTube link when uploaded

---

## 7) Visual design direction

Keep premium dark ops look, but make analytics readable:
- Fonts: keep Outfit + Fraunces (already branded)
- Color: teal = good, amber = warn, red = bad, gold = money/recommend
- Charts: simple SVG or lightweight Chart.js (no heavy framework required)
- Avoid clutter: max 1 chart + 1 table visible before scroll per section
- Mobile: stack cards; charts full width

---

## 8) Backend / API plan

### Keep
- `GET /api/status` as main payload

### Extend `/api/status` with richer `performance` block per channel:
```
performance: {
  period: {start, end},
  kpis: {views, engaged_rate, retention, watch_hours, subs_gained},
  daily: [{day, views, engaged_views, avg_pct}],
  hours: [{hour, avg_views, n}],
  top_videos: [...],
  weak_videos: [...],
  recommendations: [...],
  ypp: {subs_estimate, shorts_views_90d, watch_hours_12mo, path}
}
```

### New endpoints (optional Phase 2)
- `GET /api/channel/{id}/analytics?days=28`
- `POST /api/refresh-analytics` (runs performance-report safely)
- `GET /api/failures` (clean failed backlog details)

### Jobs
- Nightly auto `performance-report` for both channels → refresh JSON used by dashboard

---

## 9) Build phases (implementation order)

### Phase D1 — Data truth (1–2 days)
- Wire full performance JSON into API (hours, daily, recommendations, Short vs Long)
- Show real error text for failed backlog items
- Add YPP progress bars from available metrics

### Phase D2 — Analytics UI (2–3 days)
- Day views line chart
- Best-hours bar chart
- Top / weak video tables
- Plain-English insight chips

### Phase D3 — Money + actions (1–2 days)
- YPP checklist live
- Affiliate status
- “Do this next” smarter rules (failed upload > gap > quality streak > growth tip)

### Phase D4 — Polish (1 day)
- Mobile layout
- Empty states (“No analytics yet — run report”)
- Loading / stale data badges (“Analytics 2d old”)

---

## 10) Success criteria (when redesign is “done”)

1. You can see in **5 seconds** if bot is healthy  
2. You can see **which channel is growing** without opening YouTube Studio  
3. Best posting hours are visible as a chart  
4. Top videos and weak videos are listed with retention  
5. Money path to YPP is visible as progress bars  
6. Failed uploads show **why**, not just “failed=True”  
7. Works on phone via LAN  

---

## 11) What we will NOT do

- No fake demo numbers  
- No heavy React rewrite unless needed (can enhance current HTML dashboard first)  
- No TikTok analytics in v1  
- No editing schedules from UI in v1 (read-only first; controls stay in `controls/` folder)

---

## 12) Implementation status (2026-09-06)

**Shipped in current dashboard** (`scripts/live_dashboard.html` + `live_dashboard_server.py`):
- Full performance payload: daily views, best hours, KPIs, top/weak videos, recommendations, freshness
- Channel tabs: Growth · Timing · Money · Ops
- SVG charts: views-by-day line + best-hours bars
- YPP progress bars + money checklist
- Plain-English failed upload errors
- Watchdog / service-guard status strip
- Mobile stacking for cards, metrics, and tables

**Still optional later:**
- `POST /api/refresh-analytics` button
- Nightly auto `performance-report` job
- Lifetime subscriber total from Channels API (today uses period gain sample)
