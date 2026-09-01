from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import imageio_ffmpeg
import numpy as np
from moviepy.editor import VideoFileClip
from PIL import Image, ImageDraw

from yt_auto.images import HybridMediaFetcher
from yt_auto.subtitles import SubtitleSegment
from yt_auto.video_builder import VideoBuilder


class FastStillAnimationTests(unittest.TestCase):
    def test_quality_v3_enables_builder_quality_gates(self) -> None:
        with patch.dict(
            "os.environ",
            {"YT_QUALITY_V3": "yes", "YT_QUALITY_V2": "0"},
            clear=True,
        ):
            self.assertTrue(VideoBuilder._quality_v2_enabled())
        with patch.dict(
            "os.environ",
            {"YT_QUALITY_V3": "0", "YT_QUALITY_V2": "true"},
            clear=True,
        ):
            self.assertTrue(VideoBuilder._quality_v2_enabled())

    def test_fast_filters_keep_video_natural_and_still_motion_one_way(self) -> None:
        builder = VideoBuilder(1, 10, target_size=(180, 320))

        video_filter = builder._fast_segment_visual_filter(
            Path("source.mp4"),
            6.0,
            is_hook=True,
            source_offset=0.25,
        )
        still_filter = builder._fast_segment_visual_filter(
            Path("source.jpg"),
            6.0,
            is_hook=True,
            source_offset=0.25,
        )

        self.assertNotIn("zoompan", video_filter)
        self.assertIn("zoompan", still_filter)
        self.assertIn("min(on/", still_filter)
        for oscillation in ("abs(", "mod(", "sin(", "cos("):
            self.assertNotIn(oscillation, still_filter)

    def test_long_vertical_still_scales_motion_with_beat_duration(self) -> None:
        builder = VideoBuilder(1, 10, target_size=(180, 320))

        short_filter = builder._fast_segment_visual_filter(
            Path("source.jpg"),
            3.0,
            is_hook=False,
            source_offset=0.25,
        )
        long_filter = builder._fast_segment_visual_filter(
            Path("source.jpg"),
            8.0,
            is_hook=False,
            source_offset=0.25,
        )

        self.assertIn("+0.075*", short_filter)
        self.assertIn("+0.180*", long_filter)

    def test_landscape_still_hook_keeps_visible_motion_after_hd_scaling(self) -> None:
        builder = VideoBuilder(180, 600, target_size=(1920, 1080))

        hook_filter = builder._fast_segment_visual_filter(
            Path("wide_aerial.jpg"),
            5.8,
            is_hook=True,
            source_offset=0.25,
        )

        self.assertIn("+0.180*", hook_filter)

    def test_audited_motion_offset_skips_a_known_static_clip_opening(self) -> None:
        builder = VideoBuilder(1, 10, target_size=(180, 320))

        self.assertEqual(
            4.10,
            builder._source_motion_offset(
                Path("raw_08_adult-couple-plannin.mp4"),
                3,
                base=0.25,
                step=0.55,
                modulo=6,
            ),
        )
        self.assertAlmostEqual(
            1.9,
            builder._source_motion_offset(
                Path("other.mp4"),
                3,
                base=0.25,
                step=0.55,
                modulo=6,
            ),
        )

    def test_fast_segment_render_failure_is_not_hidden_by_a_static_fallback(self) -> None:
        builder = VideoBuilder(1, 10, target_size=(180, 320))
        with patch(
            "yt_auto.video_builder.subprocess.run",
            return_value=Mock(returncode=1, stderr=b"synthetic decoder failure"),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "synthetic decoder failure"):
                builder._render_fast_segment(
                    "ffmpeg",
                    Path("broken.mp4"),
                    Path("out.mp4"),
                    duration=3.0,
                    is_hook=False,
                )

        self.assertEqual(1, run.call_count)

    def test_long_visual_plan_intercuts_neighboring_b_roll_with_short_shots(self) -> None:
        builder = VideoBuilder(180, 240, target_size=(320, 180))
        images = [Path(f"scene_{index:02d}.jpg") for index in range(20)]
        beats = [
            SubtitleSegment(index * 10.0, (index + 1) * 10.0, f"Beat {index}")
            for index in range(20)
        ]

        segments, planned_images, beat_indices = builder._beat_aligned_visual_plan(
            image_paths=images,
            beat_segments=beats,
            story_beats=[beat.text for beat in beats],
            duration=200.0,
        )

        self.assertEqual(40, len(segments))
        self.assertTrue(all(segment.end - segment.start <= 7.5 for segment in segments))
        self.assertTrue(
            all(left.resolve() != right.resolve() for left, right in zip(planned_images, planned_images[1:]))
        )
        last_seen = {}
        for position, image_path in enumerate(planned_images):
            key = image_path.resolve()
            if key in last_seen:
                self.assertGreaterEqual(position - last_seen[key] - 1, 3)
            last_seen[key] = position
        for beat_index in range(20):
            first_for_beat = beat_indices.index(beat_index)
            self.assertEqual(images[beat_index], planned_images[first_for_beat])

        # The old local-neighbour planner exposed only four assets in roughly
        # the first 90 seconds. The full library must now be distributed early.
        first_ninety = {
            image.resolve()
            for segment, image in zip(segments, planned_images)
            if segment.start < 90.0
        }
        self.assertGreaterEqual(len(first_ninety), 10)

    def test_long_visual_plan_uses_best_possible_gap_with_three_assets(self) -> None:
        builder = VideoBuilder(180, 240, target_size=(320, 180))
        images = [Path(f"scarce_{index}.jpg") for index in range(3)]
        beats = [
            SubtitleSegment(index * 20.0, (index + 1) * 20.0, f"Beat {index}")
            for index in range(3)
        ]

        _, planned_images, _ = builder._beat_aligned_visual_plan(
            image_paths=images,
            beat_segments=beats,
            story_beats=[beat.text for beat in beats],
            duration=180.0,
        )

        last_seen = {}
        for position, image_path in enumerate(planned_images):
            key = image_path.resolve()
            if key in last_seen:
                self.assertGreaterEqual(position - last_seen[key] - 1, 2)
            last_seen[key] = position

    def test_adjacent_reused_still_is_one_continuous_visual_hold(self) -> None:
        builder = VideoBuilder(1, 10, target_size=(180, 320))
        first = Path("first.jpg")
        second = Path("second.jpg")
        beats = [
            SubtitleSegment(0.0, 2.0, "Imported beads reveal trade."),
            SubtitleSegment(2.0, 4.0, "The route reached the Indian Ocean."),
            SubtitleSegment(4.0, 6.0, "A different artifact changes the evidence."),
        ]

        segments, images, beat_indices = builder._beat_aligned_visual_plan(
            image_paths=[first, first, second],
            beat_segments=beats,
            story_beats=[beat.text for beat in beats],
            duration=6.0,
        )

        self.assertEqual(images, [first, second])
        self.assertIsInstance(beat_indices[0], list)
        self.assertEqual([entry["beat_index"] for entry in beat_indices[0]], [0, 1])
        self.assertEqual(beat_indices[1], 2)
        self.assertEqual([(segment.start, segment.end) for segment in segments], [(0.0, 4.0), (4.0, 6.0)])
        self.assertIn("Imported beads reveal trade.", segments[0].text)
        self.assertIn("The route reached the Indian Ocean.", segments[0].text)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "short.mp4"
            builder._write_visual_timeline(output, segments, images, beat_indices)
            timeline = json.loads((output.parent / "visual_timeline.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["beat_index"] for entry in timeline], [0, 1, 2])
        self.assertEqual([entry.get("continuous_visual_hold", False) for entry in timeline], [True, True, False])

    def test_duplicate_assets_within_one_beat_are_replaced_when_alternatives_exist(self) -> None:
        builder = VideoBuilder(1, 10, target_size=(180, 320))
        repeated = Path("repeated.mp4")
        alternatives = [Path(f"alternative_{index}.mp4") for index in range(5)]
        images = [alternatives[0], alternatives[1], repeated, *alternatives[2:], repeated]
        beats = [
            SubtitleSegment(index * 3.0, (index + 1) * 3.0, f"Beat {index}")
            for index in range(6)
        ]

        _, planned_images, _ = builder._beat_aligned_visual_plan(
            image_paths=images,
            beat_segments=beats,
            story_beats=[beat.text for beat in beats],
            duration=18.0,
        )

        self.assertTrue(
            all(left.resolve() != right.resolve() for left, right in zip(planned_images, planned_images[1:]))
        )

    def test_fast_still_segment_changes_frames_over_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            still_path = work_dir / "patterned_still.png"
            video_path = work_dir / "animated_still.mp4"

            image = Image.new("RGB", (180, 320), "#17233d")
            draw = ImageDraw.Draw(image)
            for offset in range(0, 320, 20):
                color = "#ffcf4a" if (offset // 20) % 2 == 0 else "#31c7ef"
                draw.line((0, offset, 179, 319 - offset), fill=color, width=5)
            draw.rectangle((12, 18, 58, 94), fill="#f04f78")
            draw.ellipse((118, 226, 172, 292), fill="#70df70")
            image.save(still_path)

            builder = VideoBuilder(1, 10, target_size=(180, 320))
            builder._render_fast_segment(
                imageio_ffmpeg.get_ffmpeg_exe(),
                still_path,
                video_path,
                duration=2.0,
                is_hook=True,
            )

            clip = VideoFileClip(str(video_path), audio=False)
            try:
                early = clip.get_frame(0.20).astype(np.int16)
                later = clip.get_frame(1.20).astype(np.int16)
                # This matches the production short-motion gate, which compares
                # nearby 0.32-second samples after reducing the frame size.
                short_early = np.asarray(
                    Image.fromarray(clip.get_frame(0.80)).resize((90, 160)).convert("L"),
                    dtype=np.float32,
                )
                short_later = np.asarray(
                    Image.fromarray(clip.get_frame(1.12)).resize((90, 160)).convert("L"),
                    dtype=np.float32,
                )
            finally:
                clip.close()

            mean_absolute_difference = float(np.abs(early - later).mean())
            self.assertGreater(
                mean_absolute_difference,
                2.0,
                f"fast still renderer produced effectively frozen frames (MAD={mean_absolute_difference:.3f})",
            )
            self.assertGreaterEqual(
                float(np.abs(short_early - short_later).mean()),
                2.0,
                "short still motion did not meet the production 0.32-second gate",
            )

    def test_fast_video_hook_does_not_animate_a_static_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            source_still = work_dir / "static_opening.png"
            source_video = work_dir / "static_opening.mp4"
            output = work_dir / "hook.mp4"
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

            image = Image.new("RGB", (180, 320), "#183356")
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 20, 78, 132), fill="#f3c54c")
            draw.ellipse((100, 190, 170, 290), fill="#d95276")
            for offset in range(-120, 340, 25):
                draw.line((0, offset, 179, offset + 140), fill="#5cd6cf", width=4)
            image.save(source_still)
            source_result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-loop", "1", "-framerate", "30", "-i", str(source_still),
                    "-t", "4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(source_video),
                ],
                check=False, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(source_result.returncode, 0, source_result.stderr)

            builder = VideoBuilder(1, 10, target_size=(180, 320))
            builder._render_fast_segment(ffmpeg, source_video, output, duration=3.0, is_hook=True)

            clip = VideoFileClip(str(output), audio=False)
            try:
                early = clip.get_frame(0.20).astype(np.int16)
                later = clip.get_frame(2.20).astype(np.int16)
            finally:
                clip.close()
            difference = float(np.abs(early - later).mean())
            # Static Brain Lens hook footage is rejected during acquisition.
            # The renderer should not disguise it with synthetic camera motion.
            self.assertLess(
                difference,
                1.5,
                f"renderer imposed motion on a static video hook (MAD={difference:.3f})",
            )

    def test_fast_non_hook_video_does_not_animate_a_static_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            source_still = work_dir / "static_source.png"
            source_video = work_dir / "static_source.mp4"
            output = work_dir / "scene.mp4"
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

            image = Image.new("RGB", (180, 320), "#183356")
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 20, 78, 132), fill="#f3c54c")
            draw.ellipse((100, 190, 170, 290), fill="#d95276")
            for offset in range(-120, 340, 25):
                draw.line((0, offset, 179, offset + 140), fill="#5cd6cf", width=4)
            image.save(source_still)
            completed = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-loop", "1", "-framerate", "30", "-i", str(source_still),
                    "-t", "5", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(source_video),
                ],
                check=False, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            builder = VideoBuilder(1, 10, target_size=(180, 320))
            builder._render_fast_segment(ffmpeg, source_video, output, duration=4.0, is_hook=False)

            clip = VideoFileClip(str(output), audio=False)
            try:
                early = clip.get_frame(0.50).astype(np.int16)
                later = clip.get_frame(2.50).astype(np.int16)
            finally:
                clip.close()
            difference = float(np.abs(early - later).mean())
            self.assertLess(
                difference,
                0.5,
                f"renderer imposed motion on a static source video (MAD={difference:.3f})",
            )

    def test_long_still_segment_keeps_moving_after_old_zoom_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            still_path = work_dir / "patterned_still.png"
            video_path = work_dir / "animated_long_still.mp4"

            image = Image.new("RGB", (180, 320), "#102846")
            draw = ImageDraw.Draw(image)
            for offset in range(-280, 360, 18):
                draw.line((0, offset, 179, offset + 250), fill="#f2ca52", width=4)
            draw.rectangle((9, 17, 71, 109), fill="#e24a75")
            draw.ellipse((104, 205, 176, 305), fill="#43d8c1")
            image.save(still_path)

            builder = VideoBuilder(1, 30, target_size=(180, 320))
            builder._render_fast_segment(
                imageio_ffmpeg.get_ffmpeg_exe(),
                still_path,
                video_path,
                duration=18.0,
                is_hook=False,
            )

            clip = VideoFileClip(str(video_path), audio=False)
            try:
                comparisons = []
                for start in (5.2, 10.2, 15.2):
                    first = clip.get_frame(start).astype(np.int16)
                    second = clip.get_frame(start + 1.0).astype(np.int16)
                    comparisons.append(float(np.abs(first - second).mean()))
            finally:
                clip.close()

            self.assertTrue(
                all(value > 1.0 for value in comparisons),
                f"long still motion stalled after the former ceiling (MADs={comparisons})",
            )
            freeze_scan = HybridMediaFetcher()._background_freeze_scan(video_path, 18.0)
            self.assertEqual("pass", freeze_scan["status"])
            self.assertEqual([], freeze_scan["events"])

    def test_mixed_video_and_jpeg_segments_do_not_reset_the_concat_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            source_video = work_dir / "source.mp4"
            source_still = work_dir / "source.jpg"
            first_segment = work_dir / "segment_000.mp4"
            second_segment = work_dir / "segment_001.mp4"
            concat_path = work_dir / "segments.txt"
            logo_path = work_dir / "logo.png"
            output = work_dir / "mixed.mp4"
            diagnostics = work_dir / "render.log"

            completed = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=180x320:r=30:d=2",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-colorspace",
                    "bt709",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    str(source_video),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            still = Image.new("RGB", (180, 320), "#c82438")
            draw = ImageDraw.Draw(still)
            draw.rectangle((20, 30, 160, 285), outline="#ffe567", width=12)
            draw.line((0, 300, 180, 20), fill="#38d8ff", width=10)
            still.save(source_still, quality=92)
            Image.new("RGBA", (24, 24), (255, 255, 255, 210)).save(logo_path)

            builder = VideoBuilder(1, 10, target_size=(180, 320))
            builder._render_fast_segment(ffmpeg, source_video, first_segment, 2.0, True)
            builder._render_fast_segment(ffmpeg, source_still, second_segment, 2.0, False)
            concat_path.write_text(
                f"file '{first_segment.as_posix()}'\nfile '{second_segment.as_posix()}'\n",
                encoding="utf-8",
            )
            builder._render_fast_video_track(
                ffmpeg_path=ffmpeg,
                concat_path=concat_path,
                out_path=output,
                duration=4.0,
                ass_path=None,
                logo_path=logo_path,
                logo_width=18,
                logo_margin=4,
                diagnostic_path=diagnostics,
                timeout=120,
            )

            clip = VideoFileClip(str(output), audio=False)
            try:
                first = clip.get_frame(0.75).astype(np.int16)
                second = clip.get_frame(2.75).astype(np.int16)
                tail = clip.get_frame(3.70).astype(np.int16)
            finally:
                clip.close()

            self.assertGreater(float(np.abs(first - second).mean()), 20.0)
            self.assertLess(float(np.abs(second - tail).mean()), 20.0)
            self.assertNotIn(
                "Reconfiguring filter graph because video parameters changed",
                diagnostics.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "shortest=1:eof_action=endall:repeatlast=0",
                diagnostics.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
