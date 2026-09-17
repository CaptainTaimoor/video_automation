from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_auto.cli import _config_path


class CliDotenvTests(unittest.TestCase):
    def test_config_path_survives_denied_dotenv_access(self) -> None:
        with (
            patch("yt_auto.cli._load_optional_dotenv", return_value="permission denied"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(_config_path(), Path("config/settings.yaml"))

    def test_config_path_honors_process_environment(self) -> None:
        with (
            patch("yt_auto.cli._load_optional_dotenv", return_value=None),
            patch.dict(os.environ, {"YT_AUTOMATION_CONFIG": "config/custom.yaml"}, clear=True),
        ):
            self.assertEqual(_config_path(), Path("config/custom.yaml"))


if __name__ == "__main__":
    unittest.main()
