from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

from yt_auto.media_validation import validate_final_mp4
from yt_auto.video_builder import VideoBuilder


class MediaValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def _run_ffmpeg(self, *arguments: str) -> None:
        completed = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _make_timeline_fixture(self, path: Path, *, broken_audio: bool) -> None:
        arguments = [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x24304a:s=160x90:r=5:d=34.6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=34.6",
        ]
        if broken_audio:
            # Reproduce the production failure: 34.6s video/container timeline,
            # but only ~15.7s of AAC samples split across three PTS islands.
            arguments.extend(
                [
                    "-filter:a",
                    "aselect='lt(t,15.402667)+between(t,25.266312,25.372979)"
                    "+between(t,30.010188,30.202187)'",
                ]
            )
        arguments.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-t",
                "34.6",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(path),
            ]
        )
        self._run_ffmpeg(*arguments)

    def test_rejects_34_second_mp4_with_brain_lens_audio_holes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "broken.mp4"
            self._make_timeline_fixture(media_path, broken_audio=True)

            report = validate_final_mp4(
                media_path,
                expected_duration=34.6,
                ffmpeg_path=self.ffmpeg,
                timeout=120,
            )

            self.assertFalse(report.ok)
            self.assertAlmostEqual(report.video_decoded_duration, 34.6, delta=0.25)
            self.assertLess(report.audio_coverage_ratio, 0.50)
            self.assertGreaterEqual(report.audio_pts_gap_count, 2)
            self.assertGreater(report.audio_max_pts_gap, 4.0)
            self.assertGreaterEqual(report.audio_packet_pts_gap_count, 2)
            self.assertGreater(report.audio_max_packet_pts_gap, 4.0)
            self.assertTrue(
                any("audio decoded sample coverage" in reason for reason in report.errors),
                report.errors,
            )
            self.assertTrue(any("tail gap" in reason for reason in report.errors), report.errors)

    def test_accepts_complete_34_second_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "complete.mp4"
            self._make_timeline_fixture(media_path, broken_audio=False)

            report = validate_final_mp4(
                media_path,
                expected_duration=34.6,
                ffmpeg_path=self.ffmpeg,
                timeout=120,
            )

            self.assertTrue(report.ok, report.errors)
            self.assertGreaterEqual(report.audio_coverage_ratio, 0.985)
            self.assertEqual(report.audio_pts_gap_count, 0)
            self.assertEqual(report.audio_packet_pts_gap_count, 0)
            self.assertEqual(report.video_pts_gap_count, 0)
            self.assertEqual(report.video_pts_regression_count, 0)
            self.assertGreater(report.video_frame_count, 100)

    def test_rejects_video_frame_pts_holes_even_when_audio_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "video_pts_hole.mp4"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x90:r=10:d=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=4",
                "-filter:v",
                "select='not(between(n,10,12))'",
                "-fps_mode:v",
                "vfr",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-t",
                "4",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(media_path),
            )

            report = validate_final_mp4(
                media_path,
                expected_duration=4.0,
                ffmpeg_path=self.ffmpeg,
                timeout=120,
            )

            self.assertFalse(report.ok)
            self.assertGreaterEqual(report.video_pts_gap_count, 1)
            self.assertGreater(report.video_max_pts_gap, 0.2)
            self.assertTrue(
                any("video contains" in reason and "frame PTS gap" in reason for reason in report.errors),
                report.errors,
            )

    def test_two_stage_audio_render_loops_music_and_validates_final_mux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            video_track = work_dir / "video.mp4"
            first_segment = work_dir / "segment_0.mp4"
            second_segment = work_dir / "segment_1.mp4"
            concat_path = work_dir / "segments.txt"
            logo = work_dir / "logo.png"
            narration = work_dir / "narration.wav"
            music = work_dir / "music.wav"
            audio_track = work_dir / "audio.m4a"
            candidate = work_dir / "candidate.mp4"
            output = work_dir / "final.mp4"
            diagnostic = work_dir / "render.ffmpeg.log"

            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:r=5:d=2.0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(first_segment),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=0x24304a:s=160x90:r=5:d=2.0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(second_segment),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=520:sample_rate=48000:duration=4.0",
                str(narration),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=130:sample_rate=48000:duration=0.7",
                str(music),
            )

            concat_path.write_text(
                f"file '{first_segment.as_posix()}'\nfile '{second_segment.as_posix()}'\n",
                encoding="utf-8",
            )
            Image.new("RGBA", (24, 24), (255, 70, 70, 220)).save(logo)

            builder = VideoBuilder(1, 10, target_size=(160, 90))
            builder._render_fast_video_track(
                ffmpeg_path=self.ffmpeg,
                concat_path=concat_path,
                out_path=video_track,
                duration=4.0,
                ass_path=None,
                logo_path=logo,
                logo_width=18,
                logo_margin=4,
                diagnostic_path=diagnostic,
                timeout=120,
            )
            builder._render_fast_audio_track(
                ffmpeg_path=self.ffmpeg,
                narration_path=narration,
                music_path=music,
                out_path=audio_track,
                duration=4.0,
                music_volume=0.105,
                diagnostic_path=diagnostic,
                timeout=120,
            )
            report = builder._mux_and_validate_fast_render(
                ffmpeg_path=self.ffmpeg,
                video_track_path=video_track,
                audio_track_path=audio_track,
                candidate_path=candidate,
                out_path=output,
                duration=4.0,
                diagnostic_path=diagnostic,
                timeout=120,
            )

            self.assertTrue(report.ok, report.errors)
            self.assertTrue(output.exists())
            self.assertFalse(candidate.exists())
            self.assertGreaterEqual(report.audio_coverage_ratio, 0.985)
            self.assertEqual(report.audio_pts_gap_count, 0)
            self.assertEqual(report.audio_packet_pts_gap_count, 0)
            self.assertEqual(report.video_pts_gap_count, 0)
            self.assertTrue(output.with_suffix(".media_validation.json").exists())
            self.assertIn("final video-track render", diagnostic.read_text(encoding="utf-8"))
            self.assertIn("final audio-track render", diagnostic.read_text(encoding="utf-8"))

    def test_missing_file_returns_clear_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.mp4"
            report = validate_final_mp4(missing, ffmpeg_path=self.ffmpeg)
            self.assertFalse(report.ok)
            self.assertIn("does not exist", report.errors[0])

    def test_fast_short_build_runs_two_stage_render_and_writes_validation_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            first_image = work_dir / "first.jpg"
            second_image = work_dir / "second.jpg"
            narration = work_dir / "narration.wav"
            music_dir = work_dir / "music"
            music_dir.mkdir()
            music = music_dir / "bed.wav"
            output = work_dir / "short.mp4"
            subtitles = work_dir / "captions.srt"

            Image.effect_noise((640, 960), 90).convert("RGB").save(first_image, quality=92)
            Image.effect_noise((640, 960), 55).convert("RGB").save(second_image, quality=92)
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=520:sample_rate=48000:duration=4.0",
                str(narration),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=130:sample_rate=48000:duration=0.7",
                str(music),
            )

            builder = VideoBuilder(
                min_duration=1,
                max_duration=10,
                subtitle_enabled=True,
                target_size=(360, 640),
            )
            duration = builder.build(
                image_paths=[first_image, second_image],
                narration_path=narration,
                music_dir=music_dir,
                out_path=output,
                narration_text="One surprising signal changes everything. Watch what happens next.",
                subtitles_path=subtitles,
                title_text="The signal most people miss",
                narration_beats=[
                    "One surprising signal changes everything.",
                    "Watch what happens next.",
                ],
                scene_durations=[2.5, 1.5],
                target_size=(360, 640),
                duration_bounds=(1, 10),
                content_kind="short",
            )

            self.assertAlmostEqual(duration, 4.0, delta=0.1)
            self.assertTrue(output.exists())
            self.assertTrue(subtitles.exists())
            self.assertTrue(output.with_suffix(".ffmpeg.log").exists())
            report_path = output.with_suffix(".media_validation.json")
            self.assertTrue(report_path.exists())
            report = validate_final_mp4(
                output,
                expected_duration=duration,
                ffmpeg_path=self.ffmpeg,
                timeout=120,
            )
            self.assertTrue(report.ok, report.errors)
            self.assertGreaterEqual(report.audio_coverage_ratio, 0.985)


if __name__ == "__main__":
    unittest.main()
