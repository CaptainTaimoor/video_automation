from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from scripts.rebuild_editorial_audit_outputs import _reuse_exact_long_narration


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_wav(path: Path, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 44_100
    frames = int(rate * duration)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0\0\0" * frames)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


class ExactLongNarrationCacheTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[object, object, Path]:
        output_dir = root / "channel"
        source = output_dir / "video" / "2026-07-20" / "source"
        segment_dir = source / "narration_segments"
        segment_dir.mkdir(parents=True)
        beats = ["First exact beat.", "Second exact beat."]
        topic = SimpleNamespace(
            content_kind="video",
            title="Exact title",
            narration=" ".join(beats),
            narration_beats=beats,
        )
        _write_json(
            source / "topic.json",
            {
                "title": topic.title,
                "narration": topic.narration,
                "narration_beats": beats,
            },
        )
        _write_json(source / "metadata.json", {"voice": "kokoro-af_heart"})
        items = []
        for index in range(2):
            beat_path = segment_dir / f"beat_{index:03d}.wav"
            _write_wav(beat_path, 1.0)
            items.append(
                {
                    "index": index,
                    "path": str(beat_path),
                    "duration": 1.0,
                    "voice": "af_heart",
                    "speed": 0.98,
                    "text": beats[index],
                }
            )
        _write_json(
            segment_dir / "kokoro_manifest.json",
            {"engine": "kokoro", "items": items},
        )
        _write_json(
            source / "visual_timeline.json",
            [
                {"beat_index": 0, "start": 0.0, "end": 1.12},
                {"beat_index": 1, "start": 1.12, "end": 2.12},
            ],
        )
        _write_wav(source / "narration.wav", 2.12)
        factory = SimpleNamespace(
            _channel=lambda _channel_id: SimpleNamespace(
                output_dir=output_dir,
                voices=["kokoro-af_heart@0.98"],
                voice_mode="single",
                tts_backend="kokoro",
            ),
            _probe_audio_duration=_wav_duration,
        )
        return factory, topic, source

    def test_reuses_only_complete_exact_content_and_rewrites_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            factory, topic, source = self._fixture(root)
            target = root / "target"
            target.mkdir()

            reused = _reuse_exact_long_narration(factory, "brain_lens", topic, target)

            self.assertEqual(("kokoro-af_heart", [1.12, 1.0]), reused)
            self.assertTrue((target / "REUSED_EXACT_NARRATION.json").exists())
            manifest = json.loads(
                (target / "narration_segments" / "kokoro_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(
                all(str(target.resolve()) in item["path"] for item in manifest["items"])
            )
            self.assertEqual(str(source.resolve()), json.loads(
                (target / "REUSED_EXACT_NARRATION.json").read_text(encoding="utf-8")
            )["source_run"])

    def test_rejects_even_a_one_character_narration_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            factory, topic, _source = self._fixture(root)
            topic.narration += "!"
            target = root / "target"
            target.mkdir()

            self.assertIsNone(
                _reuse_exact_long_narration(factory, "brain_lens", topic, target)
            )
            self.assertFalse((target / "narration.wav").exists())


if __name__ == "__main__":
    unittest.main()
