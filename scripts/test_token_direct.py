import os

import requests
from dotenv import load_dotenv


load_dotenv()
page_token = (os.getenv("FB_PAGE_TOKEN_ANCIENT_HISTORY") or "").strip()
page_id = (os.getenv("FB_PAGE_ID_ANCIENT_HISTORY") or "101382192681601").strip()
if not page_token:
    raise RuntimeError("FB_PAGE_TOKEN_ANCIENT_HISTORY is not configured")

url = f"https://graph.facebook.com/v21.0/{page_id}"
response = requests.get(
    url,
    params={"fields": "name,about", "access_token": page_token},
    timeout=20,
)
response.raise_for_status()
print(response.json())
