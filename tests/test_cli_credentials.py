from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt_auto.cli import _missing_credential_actions


def _factory(client_secrets: Path, upload_enabled: bool = True) -> SimpleNamespace:
    channel = SimpleNamespace(
        id="ancient_history",
        youtube=SimpleNamespace(
            upload_enabled=upload_enabled,
            client_secrets_file=client_secrets,
        ),
    )
    return SimpleNamespace(config=SimpleNamespace(channels=[channel]))


class MissingCredentialActionsTests(unittest.TestCase):
    def test_reports_every_gap_on_a_freshly_restored_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "client_secrets.json"
            with patch.dict(os.environ, {}, clear=True):
                actions = _missing_credential_actions(_factory(missing))

        joined = " ".join(actions)
        self.assertIn("GEMINI_API_KEY", joined)
        self.assertIn("PEXELS_API_KEY", joined)
        self.assertIn("client_secrets.json", joined)

    def test_silent_once_credentials_are_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "client_secrets.json"
            secrets.write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "key", "PEXELS_API_KEY": "key"},
                clear=True,
            ):
                self.assertEqual([], _missing_credential_actions(_factory(secrets)))

    def test_either_stock_provider_satisfies_the_image_key_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "client_secrets.json"
            secrets.write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "key", "PIXABAY_API_KEY": "key"},
                clear=True,
            ):
                self.assertEqual([], _missing_credential_actions(_factory(secrets)))

    def test_blank_key_counts_as_missing(self) -> None:
        # .env.example ships every key present but empty, so "set" is not enough.
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "client_secrets.json"
            secrets.write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "   ", "PEXELS_API_KEY": "key"},
                clear=True,
            ):
                actions = _missing_credential_actions(_factory(secrets))
        self.assertEqual(1, len(actions))
        self.assertIn("GEMINI_API_KEY", actions[0])

    def test_upload_disabled_channel_does_not_demand_client_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "client_secrets.json"
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "key", "PEXELS_API_KEY": "key"},
                clear=True,
            ):
                actions = _missing_credential_actions(
                    _factory(missing, upload_enabled=False)
                )
        self.assertEqual([], actions)


if __name__ == "__main__":
    unittest.main()
