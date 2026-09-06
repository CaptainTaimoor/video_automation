import requests

from fb_token_utils import required_env


def list_all_fb_pages_detailed() -> None:
    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={
            "access_token": required_env("FB_USER_ACCESS_TOKEN"),
            "limit": 100,
            "fields": "name,id,category,about",
        },
        timeout=20,
    )
    response.raise_for_status()
    accounts = response.json().get("data", [])
    print(f"Found {len(accounts)} pages.")
    for account in accounts:
        about = account.get("about") or "No about text"
        print(
            f"ID: {account.get('id')}, Name: {account.get('name')}, "
            f"Category: {account.get('category')}"
        )
        print(f"  About: {about[:60]}")


if __name__ == "__main__":
    list_all_fb_pages_detailed()
