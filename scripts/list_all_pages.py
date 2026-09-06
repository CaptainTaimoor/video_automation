import requests

from fb_token_utils import required_env


def list_all_fb_pages() -> None:
    response = requests.get(
        "https://graph.facebook.com/me/accounts",
        params={"access_token": required_env("FB_USER_ACCESS_TOKEN"), "limit": 100},
        timeout=20,
    )
    response.raise_for_status()
    accounts = response.json().get("data", [])
    print(f"Found {len(accounts)} pages.")
    for account in accounts:
        print(f"ID: {account.get('id')}, Name: {account.get('name')}")


if __name__ == "__main__":
    list_all_fb_pages()
