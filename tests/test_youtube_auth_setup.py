from __future__ import annotations

import json
import tempfile
import unittest
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt_auto.uploaders.youtube import UploadAuthError, YouTubeUploader

DESKTOP = {
    "installed": {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "shh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}
WEB = {"web": dict(DESKTOP["installed"])}


def _channel(secrets: Path, token: Path):
    return SimpleNamespace(
        id="ancient_history",
        youtube=SimpleNamespace(client_secrets_file=secrets, token_file=token),
    )


class ObtainCredentialsSetupTests(unittest.TestCase):
    """Setup mistakes must produce an explanation, not a traceback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.secrets = self.root / "client_secrets.json"
        self.token = self.root / "secrets" / "tokens" / "ancient_history_token.json"
        self.uploader = YouTubeUploader.__new__(YouTubeUploader)

    def _obtain(self):
        return self.uploader._obtain_credentials(_channel(self.secrets, self.token))

    def test_missing_client_secrets_names_the_file_and_the_fix(self) -> None:
        with self.assertRaises(UploadAuthError) as ctx:
            self._obtain()
        message = str(ctx.exception)
        self.assertIn("client_secrets.json", message)
        self.assertIn("Desktop app", message)

    def test_web_client_is_rejected_before_contacting_google(self) -> None:
        # google_auth_oauthlib accepts a web client, then Google rejects the
        # loopback redirect with an opaque redirect_uri_mismatch.
        self.secrets.write_text(json.dumps(WEB), encoding="utf-8")
        with self.assertRaises(UploadAuthError) as ctx:
            self._obtain()
        message = str(ctx.exception)
        self.assertIn("web application", message)
        self.assertIn("Desktop app", message)

    def test_corrupt_json_is_reported_as_such(self) -> None:
        self.secrets.write_text("not json", encoding="utf-8")
        with self.assertRaises(UploadAuthError) as ctx:
            self._obtain()
        self.assertIn("not readable JSON", str(ctx.exception))

    def test_desktop_client_reaches_the_local_server_step(self) -> None:
        self.secrets.write_text(json.dumps(DESKTOP), encoding="utf-8")
        captured = {}

        class _Creds:
            def to_json(self):
                return "{}"

        def fake_run_local_server(**kwargs):
            captured.update(kwargs)
            return _Creds()

        with patch(
            "yt_auto.uploaders.youtube.InstalledAppFlow.from_client_secrets_file"
        ) as from_file:
            from_file.return_value = SimpleNamespace(run_local_server=fake_run_local_server)
            self._obtain()

        self.assertIn("port", captured)
        self.assertTrue(self.token.exists(), "token should be written on success")

    def test_missing_browser_falls_back_to_printing_the_url(self) -> None:
        # Headless/WSL/SSH used to die on webbrowser.Error before this.
        self.secrets.write_text(json.dumps(DESKTOP), encoding="utf-8")
        captured = {}

        class _Creds:
            def to_json(self):
                return "{}"

        def fake_run_local_server(**kwargs):
            captured.update(kwargs)
            return _Creds()

        def no_browser(*args, **kwargs):
            raise webbrowser.Error("could not locate runnable browser")

        with (
            patch("yt_auto.uploaders.youtube.webbrowser.get", no_browser),
            patch(
                "yt_auto.uploaders.youtube.InstalledAppFlow.from_client_secrets_file"
            ) as from_file,
        ):
            from_file.return_value = SimpleNamespace(run_local_server=fake_run_local_server)
            self.uploader._obtain_credentials(
                _channel(self.secrets, self.token), open_browser=True
            )

        self.assertFalse(
            captured["open_browser"],
            "must not ask for a browser when none can be launched",
        )

    def test_oauth_port_can_be_pinned_for_firewalled_machines(self) -> None:
        self.secrets.write_text(json.dumps(DESKTOP), encoding="utf-8")
        captured = {}

        class _Creds:
            def to_json(self):
                return "{}"

        def fake_run_local_server(**kwargs):
            captured.update(kwargs)
            return _Creds()

        with (
            patch.dict("os.environ", {"YT_OAUTH_PORT": "8123"}, clear=False),
            patch(
                "yt_auto.uploaders.youtube.InstalledAppFlow.from_client_secrets_file"
            ) as from_file,
        ):
            from_file.return_value = SimpleNamespace(run_local_server=fake_run_local_server)
            self._obtain()

        self.assertEqual(8123, captured["port"])

    def test_bad_port_value_falls_back_to_random(self) -> None:
        self.secrets.write_text(json.dumps(DESKTOP), encoding="utf-8")
        captured = {}

        class _Creds:
            def to_json(self):
                return "{}"

        def fake_run_local_server(**kwargs):
            captured.update(kwargs)
            return _Creds()

        with (
            patch.dict("os.environ", {"YT_OAUTH_PORT": "not-a-port"}, clear=False),
            patch(
                "yt_auto.uploaders.youtube.InstalledAppFlow.from_client_secrets_file"
            ) as from_file,
        ):
            from_file.return_value = SimpleNamespace(run_local_server=fake_run_local_server)
            self._obtain()

        self.assertEqual(0, captured["port"])


if __name__ == "__main__":
    unittest.main()
