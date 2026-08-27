import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import imageio_ffmpeg

from yt_auto.video_builder import VideoBuilder


class QualityV2VideoAudioTests(unittest.TestCase):
    def test_v2_fast_audio_track_uses_dialogue_ducking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration.wav"
            music = root / "music.mp3"
            narration.touch()
            music.touch()
            builder = VideoBuilder(min_duration=18, max_duration=36)
            with patch.dict(os.environ, {"YT_QUALITY_V2": "1"}), patch.object(
                builder, "_run_ffmpeg_logged"
            ) as run_ffmpeg:
                builder._render_fast_audio_track(
                    ffmpeg_path="ffmpeg",
                    narration_path=narration,
                    music_path=music,
                    out_path=root / "audio.m4a",
                    duration=30.0,
                    music_volume=0.105,
                    diagnostic_path=root / "audio.log",
                    timeout=30,
                )
            command = run_ffmpeg.call_args.args[0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("sidechaincompress", graph)
            self.assertIn("volume=0.080", graph)
            self.assertIn("loudnorm=I=-16.5:TP=-1.0", graph)

    def test_v2_ducking_graph_is_accepted_by_real_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration.wav"
            music = root / "music.mp3"
            narration.touch()
            music.touch()
            builder = VideoBuilder(min_duration=1, max_duration=4)
            with patch.dict(os.environ, {"YT_QUALITY_V2": "1"}), patch.object(
                builder, "_run_ffmpeg_logged"
            ) as run_ffmpeg:
                builder._render_fast_audio_track(
                    ffmpeg_path="ffmpeg",
                    narration_path=narration,
                    music_path=music,
                    out_path=root / "audio.m4a",
                    duration=2.0,
                    music_volume=0.105,
                    diagnostic_path=root / "audio.log",
                    timeout=30,
                )
            generated = run_ffmpeg.call_args.args[0]
            graph = generated[generated.index("-filter_complex") + 1]
            completed = subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:duration=2",
                    "-filter_complex",
                    graph,
                    "-map",
                    "[aout]",
                    "-f",
                    "null",
                    "NUL" if os.name == "nt" else "/dev/null",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_v2_refuses_large_renderer_tempo_change(self):
        builder = VideoBuilder(min_duration=18, max_duration=36)
        with patch.dict(os.environ, {"YT_QUALITY_V2": "1"}):
            with self.assertRaises(RuntimeError):
                builder._fast_voice_filters(duration=30.0, voice_tempo=1.12)
