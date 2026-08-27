import json
import requests
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from yt_auto.models import ChannelConfig, TopicCandidate, ScriptWriterConfig, YouTubeConfig, FacebookConfig
from yt_auto.script_writer import ScriptWriter

def test_ollama():
    cfg = ScriptWriterConfig(
        provider="ollama",
        ollama_model="llama3.2:3b",
        ollama_url="http://localhost:11434/api/generate",
        timeout_seconds=120
    )
    writer = ScriptWriter(cfg)
    
    yt_cfg = YouTubeConfig(upload_enabled=False, client_secrets_file=Path("client_secrets.json"), token_file=Path("token.json"))
    fb_cfg = FacebookConfig(upload_enabled=False, page_id="12345")

    channel = ChannelConfig(
        id="brain_lens",
        display_name="Brain Lens",
        niche_description="Psychology facts and human behavior",
        seed_keywords=["psychology"],
        styles=["explainer"],
        schedule_times=[],
        voice_mode="mix",
        voices=[],
        hashtags=[],
        output_dir=Path("output/brain_lens"),
        youtube=yt_cfg,
        facebook=fb_cfg
    )
    
    topic = TopicCandidate(
        niche_id="brain_lens",
        style="explainer",
        trend_terms=["cognitive dissonance"],
        title="The psychology behind Cognitive Dissonance",
        subject="Cognitive Dissonance",
        hook="",
        narration="Template narration.",
        visual_captions=["Brain signal", "Decision making"],
        source_urls=[],
        image_queries=[],
        hashtags=["#psychology"],
        engagement_score=85.0
    )
    
    print("Testing Ollama Script Generation...")
    try:
        improved = writer.improve(channel, topic)
        print("\n--- IMPROVED NARRATION ---")
        print(improved.narration)
        print("\n--- BEATS ---")
        for i, beat in enumerate(improved.narration_beats):
            print(f"Beat {i+1}: {beat}")
            
        print("\nTesting Ollama Title Generation...")
        viral_title = writer.generate_viral_title(channel, topic)
        print(f"\nViral Title: {viral_title}")
        
    except Exception as e:
        print(f"Ollama test failed: {e}")

if __name__ == "__main__":
    test_ollama()
