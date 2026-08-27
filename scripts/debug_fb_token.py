import requests

from fb_token_utils import required_env


def debug_fb_token() -> None:
    app_id = required_env("FB_APP_ID")
    app_secret = required_env("FB_APP_SECRET")
    input_token = required_env("FB_USER_ACCESS_TOKEN")
    response = requests.get(
        "https://graph.facebook.com/debug_token",
        params={"input_token": input_token, "access_token": f"{app_id}|{app_secret}"},
        timeout=20,
    )
    response.raise_for_status()
    info = response.json().get("data", {})
    print("--- Token Debug Info ---")
    print(f"Type: {info.get('type')}")
    print(f"Is Valid: {info.get('is_valid')}")
    print(f"Expires At: {info.get('expires_at')}")
    print(f"Scopes: {info.get('scopes')}")

    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={"access_token": input_token, "limit": 100},
        timeout=20,
    )
    response.raise_for_status()
    print("\n--- Associated Accounts (Pages) ---")
    for account in response.json().get("data", []):
        print(
            f"Name: {account.get('name')}, ID: {account.get('id')}, "
            f"token_received={bool(account.get('access_token'))}"
        )


if __name__ == "__main__":
    debug_fb_token()
