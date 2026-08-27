from __future__ import annotations

import unittest
from unittest.mock import patch

from yt_auto.pipeline import _load_optional_dotenv


class OptionalDotenvTests(unittest.TestCase):
    def test_permission_denied_uses_process_environment(self) -> None:
        with patch("yt_auto.pipeline.load_dotenv", side_effect=PermissionError):
            self.assertIn("process environment", _load_optional_dotenv() or "")

    def test_other_dotenv_failures_are_not_silenced(self) -> None:
        with patch("yt_auto.pipeline.load_dotenv", side_effect=RuntimeError("bad dotenv")):
            with self.assertRaisesRegex(RuntimeError, "bad dotenv"):
                _load_optional_dotenv()


if __name__ == "__main__":
    unittest.main()
