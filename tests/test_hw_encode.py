import unittest
from unittest.mock import patch

from yt_auto.hw_encode import (
    H264EncodeProfile,
    reset_h264_encode_profile_cache,
    resolve_h264_encode_profile,
)


class HardwareEncodeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_h264_encode_profile_cache()

    def test_qsv_argv_uses_global_quality(self) -> None:
        profile = H264EncodeProfile(name="qsv", codec="h264_qsv")
        args = profile.argv(crf=18)
        self.assertEqual(args[:2], ["-c:v", "h264_qsv"])
        self.assertIn("-global_quality", args)
        self.assertIn("18", args)

    def test_libx264_force_skips_probe(self) -> None:
        with patch("yt_auto.hw_encode._probe_encoder", side_effect=AssertionError("should not probe")):
            profile = resolve_h264_encode_profile(force="libx264")
        self.assertEqual(profile.name, "libx264")
        self.assertEqual(profile.codec, "libx264")


if __name__ == "__main__":
    unittest.main()
