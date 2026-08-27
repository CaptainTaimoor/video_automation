from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yt_auto.tts_engine import NarrationEngine


OUT_DIR = Path("output/voice_tests")
OUT_DIR.mkdir(parents=True, exist_ok=True)

samples = {
    "ancient_history": (
        ["en-GB-RyanNeural", "en-US-GuyNeural"],
        "Qin Shi Huang built a mausoleum guarded by the Terracotta Army. Thermopylae, Xerxes, Masada, and the Zealots still appear in ancient history.",
    ),
    "brain_lens": (
        ["en-US-AriaNeural", "en-US-JennyNeural", "en-GB-SoniaNeural"],
        "The first clue anxious attachment is taking over is not a thought. Dopamine, the amygdala, and rumination can shape your reaction.",
    ),
}

for channel, (voices, text) in samples.items():
    out_path = OUT_DIR / f"{channel}.mp3"
    voice = NarrationEngine(voices, "single").synthesize(text, out_path)
    print(f"{channel}: {voice} -> {out_path}")
