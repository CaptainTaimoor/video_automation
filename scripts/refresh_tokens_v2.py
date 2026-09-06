import json
from pathlib import Path

import requests

from fb_token_utils import required_env


def refresh_tokens_to_file() -> None:
    app_id = required_env("FB_APP_ID")
    app_secret = required_env("FB_APP_SECRET")
    short_user_token = required_env("FB_USER_ACCESS_TOKEN")

    response = requests.get(
        "https://graph.facebook.com/v21.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_user_token,
        },
        timeout=20,
    )
    response.raise_for_status()
    long_user_token = response.json().get("access_token")
    if not long_user_token:
        raise RuntimeError("Facebook did not return a long-lived user token")

    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={"access_token": long_user_token, "limit": 100},
        timeout=20,
    )
    response.raise_for_status()
    output = {
        page.get("name"): {"id": page.get("id"), "token": page.get("access_token")}
        for page in response.json().get("data", [])
        if page.get("name") and page.get("access_token")
    }
    destination = Path("fb_tokens_full.json")
    destination.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Saved {len(output)} page token record(s) to ignored file {destination}")


if __name__ == "__main__":
    refresh_tokens_to_file()
