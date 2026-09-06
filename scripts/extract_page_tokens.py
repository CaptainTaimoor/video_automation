import json
from pathlib import Path

import requests

from fb_token_utils import required_env


def extract_page_tokens() -> None:
    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={"access_token": required_env("FB_USER_ACCESS_TOKEN"), "limit": 100},
        timeout=20,
    )
    response.raise_for_status()
    target_pages = {"Secrets of Time", "Brain Lens"}
    mapping = {
        account["name"]: {"id": account.get("id"), "token": account.get("access_token")}
        for account in response.json().get("data", [])
        if account.get("name") in target_pages and account.get("access_token")
    }
    destination = Path("fb_tokens_full.json")
    destination.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"Saved {len(mapping)} target page token record(s) to ignored file {destination}")


if __name__ == "__main__":
    extract_page_tokens()
