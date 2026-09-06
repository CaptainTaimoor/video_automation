import os
import requests
from dotenv import load_dotenv

def verify_fb_connections():
    load_dotenv()
    
    pages = {
        "Secrets of Time": os.getenv("FB_PAGE_TOKEN_ANCIENT_HISTORY"),
        "Brain Lens": os.getenv("FB_PAGE_TOKEN_BRAIN_LENS")
    }

    print("--- Facebook Connectivity Test ---")
    for name, token in pages.items():
        if not token:
            print(f"[{name}] Token missing in .env")
            continue
            
        # Get basic page info
        url = f"https://graph.facebook.com/v21.0/me?fields=name,id,about,fan_count&access_token={token}"
        r = requests.get(url)
        data = r.json()
        
        if "error" in data:
            print(f"[{name}] Connection FAILED: {data['error'].get('message')}")
        else:
            print(f"[{name}] Connection SUCCESS!")
            print(f"      ID: {data.get('id')}")
            print(f"      Fan Count: {data.get('fan_count')}")
            print(f"      About: {data.get('about')[:50] if data.get('about') else 'No about text'}")

if __name__ == "__main__":
    verify_fb_connections()
