#!/usr/bin/env bash
# Linux/macOS counterpart to INSTALL.bat.
#
# Sets up a working development/build environment: virtualenv, dependencies,
# .env scaffold and the runtime directories. It deliberately does NOT install
# auto-start services or open firewall ports -- those parts of INSTALL.bat are
# Windows-specific and belong to the live publishing box.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"

echo "============================================================"
echo " YT Automation - Linux/macOS setup"
echo " Project: $ROOT"
echo "============================================================"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: '$PY' not found. Install Python 3.10+ (or set PYTHON=/path/to/python3)." >&2
  exit 1
fi

# The codebase uses PEP 604 unions (`float | None`) at runtime, so 3.10 is the floor.
"$PY" - <<'PYCHECK'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"ERROR: Python 3.10+ required, found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} OK")
PYCHECK

if [ ! -d .venv ]; then
  echo "[1/4] Creating .venv ..."
  "$PY" -m venv .venv
else
  echo "[1/4] Reusing existing .venv"
fi

echo "[2/4] Installing dependencies ..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# ffmpeg ships with imageio-ffmpeg; the code prefers that binary over PATH.
./.venv/bin/python - <<'PYFFMPEG'
try:
    import imageio_ffmpeg
    print(f"ffmpeg: {imageio_ffmpeg.get_ffmpeg_exe()}")
except Exception as exc:  # pragma: no cover - diagnostic only
    print(f"WARNING: bundled ffmpeg unavailable ({exc}); install ffmpeg on PATH")
PYFFMPEG

echo "[3/4] Ensuring .env ..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "      created .env from .env.example - fill in your API keys"
else
  echo "      .env already present, left untouched"
fi

echo "[4/4] Creating runtime directories ..."
./.venv/bin/python run.py init

cat <<'DONE'

============================================================
 Setup complete.

 Activate:   source .venv/bin/activate
 Channels:   python run.py channels
 Dry run:    python run.py build --channel ancient_history --kind short --dry-run
 Tests:      python -m pytest tests/ -q

 Still needed for uploads (copy from the old machine, never commit):
   - .env                    API keys
   - client_secrets.json     YouTube OAuth client
   - secrets/tokens/*.json   authorized channel tokens

 Local-only model weights are not in git (piper voices, gfpgan weights).
============================================================
DONE
