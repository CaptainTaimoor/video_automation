# Start The Bot On A New PC

Everything in this repo is ready. You only need to put back 3 things that
were never saved to GitHub (on purpose — they are secrets).

## Step 1 — Get the code

```
git clone https://github.com/CaptainTaimoor/video_automation
cd video_automation
git checkout claude/tender-dirac-xqs1wh
```

## Step 2 — Install

- **Windows:** right-click `setup\INSTALL.bat` → Run as Administrator
- **Linux / Mac:** `bash setup/install.sh`

## Step 3 — Put back your 3 secrets

| What | Where it goes | How to get it again |
|---|---|---|
| API keys | `.env` | Re-copy from each website (see below) |
| YouTube app file | `client_secrets.json` | Google Cloud Console → your project → Credentials → download OAuth client |
| Channel logins | `secrets/tokens/` | Skip — Step 4 makes these fresh |

Keys to paste into `.env` (free tiers):
- `GEMINI_API_KEY` — aistudio.google.com  ← **most important, bot writes bad scripts without it**
- `PEXELS_API_KEY` — pexels.com/api
- `PIXABAY_API_KEY` — pixabay.com/api/docs

## Step 4 — Log in to YouTube (opens browser)

```
python run.py auth --channel ancient_history
python run.py auth --channel brain_lens
```

## Step 5 — Test, then go live

```
python run.py status
python run.py build --channel ancient_history --kind short --dry-run
```

If the test video looks good:

```
python run.py schedule --mode cron --upload
```

On Windows you can instead double-click `START_BOT_AND_DASHBOARD.bat`.

---

## If something fails

- **"not enough usable visuals"** → your keys are missing or the PC has no
  internet access to Wikimedia/Pexels.
- **"run auth"** in status → redo Step 4.
- Dashboard: http://localhost:8787

## Not in GitHub (copy from an old backup if you have one)

Voice models and face-enhancer weights (`piper` voices, `gfpgan/weights/*.pth`).
The bot still runs without them, using its normal voices.
