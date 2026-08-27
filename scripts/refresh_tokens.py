import requests

from fb_token_utils import required_env


def refresh_tokens() -> None:
    response = requests.get(
        "https://graph.facebook.com/v21.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": required_env("FB_APP_ID"),
            "client_secret": required_env("FB_APP_SECRET"),
            "fb_exchange_token": required_env("FB_USER_ACCESS_TOKEN"),
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
    pages = response.json().get("data", [])
    for page in pages:
        print(f"Name: {page.get('name')}, ID: {page.get('id')}, token_received={bool(page.get('access_token'))}")


if __name__ == "__main__":
    refresh_tokens()
