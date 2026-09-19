"""One-command pre-flight before going live.

Checks that this machine can actually publish -- dependencies, API keys,
OAuth tokens -- then optionally builds one video without uploading, so the
result can be eyed before anything reaches YouTube.

    python scripts/go_live.py              # checks + test build
    python scripts/go_live.py --skip-build # checks only (fast)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[warn]"


def _line(char: str = "-") -> None:
    print(char * 62)


def check_dependencies() -> list[str]:
    missing = []
    for module, hint in (
        ("feedparser", "requirements.txt"),
        ("moviepy.editor", "requirements.txt (needs moviepy 1.x)"),
        ("googleapiclient", "requirements.txt"),
        ("PIL", "requirements.txt (needs pillow 12+)"),
    ):
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module} ({exc.__class__.__name__}) - from {hint}")
    if missing:
        print(f"{BAD} Python packages missing:")
        for item in missing:
            print(f"        {item}")
        print()
        print("        Fix:  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        print("        (on Linux/macOS: ./.venv/bin/python -m pip install -r requirements.txt)")
        return ["dependencies"]
    print(f"{OK} Python packages installed")
    return []


def check_credentials_and_tokens() -> list[str]:
    from yt_auto.cli import _missing_credential_actions
    from yt_auto.pipeline import ShortsFactory

    # ShortsFactory takes the settings PATH and loads it itself; it also
    # applies .env, which the credential check depends on.
    factory = ShortsFactory(ROOT / "config" / "settings.yaml")
    config = factory.config

    problems: list[str] = []

    gaps = _missing_credential_actions(factory)
    blocking = [g for g in gaps if "stock image key" not in g]
    optional = [g for g in gaps if "stock image key" in g]

    if blocking:
        for gap in blocking:
            print(f"{BAD} {gap}")
        problems.append("credentials")
    else:
        print(f"{OK} API keys and client_secrets.json present")

    for gap in optional:
        print(f"{WARN} {gap}")

    for channel in config.channels:
        if not channel.youtube.upload_enabled:
            print(f"{WARN} {channel.id}: uploads disabled in settings.yaml")
            continue
        token = Path(channel.youtube.token_file)
        if token.exists():
            print(f"{OK} {channel.id}: authorized")
        else:
            print(f"{BAD} {channel.id}: not authorized")
            print(f"        Fix:  python run.py auth --channel {channel.id}")
            problems.append(f"auth:{channel.id}")

    return problems


def run_test_build(channel: str) -> bool:
    print()
    _line()
    print(f"Test build ({channel}, dry run -- nothing is uploaded)")
    print("This takes a few minutes.")
    _line()
    result = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "build",
         "--channel", channel, "--kind", "short", "--dry-run"],
        cwd=str(ROOT),
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-flight check before going live")
    parser.add_argument("--skip-build", action="store_true", help="checks only, no test build")
    parser.add_argument("--channel", default="ancient_history", help="channel for the test build")
    args = parser.parse_args()

    _line("=")
    print("GO LIVE PRE-FLIGHT")
    _line("=")

    problems = check_dependencies()
    if problems:
        print()
        print(f"{BAD} Install the packages first, then run this again.")
        return 1

    problems += check_credentials_and_tokens()

    if problems:
        print()
        _line()
        print(f"{BAD} Not ready yet. Fix the items marked [FAIL] above, then run this again.")
        _line()
        return 1

    print()
    print(f"{OK} All checks passed.")

    if args.skip_build:
        print()
        print("Skipped the test build. To run it:")
        print("   python scripts/go_live.py")
        return 0

    if not run_test_build(args.channel):
        print()
        _line()
        print(f"{BAD} The test build did not finish.")
        print("      Read the last error line above. Common causes:")
        print("        - no internet access to Wikimedia / Pexels")
        print("        - GEMINI_API_KEY rejected or out of quota")
        _line()
        return 1

    print()
    _line("=")
    print(f"{OK} READY. The test video built successfully.")
    print()
    print("Check it under  output\\  -- if it looks good, go live with:")
    print()
    print("   python run.py schedule --mode cron --upload")
    print()
    print("(or double-click START_BOT_AND_DASHBOARD.bat on Windows)")
    _line("=")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
