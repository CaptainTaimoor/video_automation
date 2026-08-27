import os
from pathlib import Path
from dotenv import load_dotenv
from yt_auto.pipeline import ShortsFactory

def test_ollama_variety():
    load_dotenv()
    factory = ShortsFactory(Path("config/settings.yaml"))
    
    channels = ["ancient_history", "brain_lens"]
    
    print("--- Ollama Variety Test ---")
    for cid in channels:
        channel = factory._channel(cid)
        print(f"\n[Channel: {channel.display_name}]")
        
        # We'll simulate 2 separate runs to check for difference
        avoid_titles = set()
        for i in range(1, 3):
            print(f"  Attempt {i}: Generating topic and script...")
            topic = factory.topic_planner.plan(channel, avoid_titles=avoid_titles, content_kind="short")
            topic = factory.script_writer.improve(channel, topic, content_kind="short", avoid_titles=avoid_titles)
            
            print(f"    TITLE: {topic.title}")
            print(f"    HOOK: {topic.narration[:100]}...")
            
            avoid_titles.add(topic.title)
            # Check if headers are generic
            if topic.title.lower().startswith("the real story"):
                print("    WARNING: Generic title detected!")

if __name__ == "__main__":
    test_ollama_variety()
