from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_auto.utils import read_json


class JsonUtilityTests(unittest.TestCase):
    def test_read_json_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text('\ufeff{"status": "healthy"}', encoding="utf-8")

            self.assertEqual(read_json(path, {}), {"status": "healthy"})


if __name__ == "__main__":
    unittest.main()
