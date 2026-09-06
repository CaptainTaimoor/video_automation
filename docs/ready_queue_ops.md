# Ready Queue Ops

Prep and render videos ahead of schedule; upload only at slot time (or immediately for gap rescue). Classic build-at-slot remains the default until you enable Ready Queue.

## Enable / disable

1. Open the dashboard (`http://127.0.0.1:8787/`).
2. Go to **Ready Queue** → tap **Turn ON** or **Turn OFF**.
3. Tap **Prep today** after enabling.
4. On **Analytics**, tap **Refresh numbers**.

No token prompt. No JSON editing in the UI. Keys stay in `.env` on the PC.

## What runs when ON

| Job | When | Behavior |
|---|---|---|
| `ready_queue_day_pack` | ~05:30 daily | Creates `data/day_packs/{channel}/{date}/plan.json` + planned items in `data/state/{channel}_ready_queue.json` |
| `ready_queue_worker` | every ~4 min | Dry-run renders next buffer item (Ancient ~2–3 Shorts ahead, Brain ~1–2) |
| Cron slot jobs | schedule times | Prefer upload of a `rendered` file; if none, classic build fallback |
| Upload gap monitor | interval | If gap overdue and a ready Short exists → publish it before emergency rebuild |
| Nightly performance report | ~03:15 | Runs `performance-report` per channel |
| Pack cleanup | ~04:10 | Deletes day_pack folders older than 2 days |

## Dashboard pages

- **Home** — health, Ready Queue on/off summary, recommended action  
- **Queue** — toggle, filters, force render/upload, cancel, retry, day pack  
- **Channels** — existing Growth/Timing/Money/Ops panels  
- **Analytics** — per-channel deep analytics + viral DNA + refresh  
- **Suggestions** — report tips + queue urgency + money todos  
- **Settings** — flags, schedule edits (backs up `settings.yaml`), key presence (masked), restart scheduler  
- **Runs / Logs** — `runs.jsonl`, active builds, log tails  

All **POST** APIs require header `X-Dashboard-Token: <YT_DASHBOARD_TOKEN>` when the token is set.

## Safety rules

- Quality `held_quality` items never auto-upload (UI force-upload also blocked).  
- Topic burn still follows pipeline rules (rendered/uploaded quality-pass paths).  
- Brain Lens Facebook backlog must **not** block Ready Queue YouTube publishes.  
- Secrets are never returned by the API — only configured/missing.  

## Manual checks

```bash
python -m unittest tests.test_ready_queue
python run.py schedule --mode cron --upload
```

After enabling Ready Queue, restart the scheduler (`controls/RESTART_SCHEDULER.bat` or Settings → Restart scheduler) so jobs pick up the flag and any schedule edits.
