from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_dashboard_server():
    """Load scripts/live_dashboard_server.py, which is not an importable package."""
    path = ROOT / "scripts" / "live_dashboard_server.py"
    spec = importlib.util.spec_from_file_location("live_dashboard_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dash = _load_dashboard_server()


class _Handler:
    """Minimal stand-in for BaseHTTPRequestHandler."""

    def __init__(self, client_ip="127.0.0.1", headers=None, path="/api/settings"):
        self.client_address = (client_ip, 51000)
        self.headers = headers or {}
        self.path = path
        self.responses = []


def _respond(handler, payload, status=200):
    handler.responses.append((status, payload))


class DashboardTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(dash, "_json_response", _respond)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- no token configured: local allowed, remote refused ------------------

    def test_loopback_allowed_when_no_token_configured(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": ""}, clear=False):
            self.assertTrue(dash._dashboard_token_ok(_Handler("127.0.0.1")))

    def test_ipv6_loopback_allowed_when_no_token_configured(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": ""}, clear=False):
            self.assertTrue(dash._dashboard_token_ok(_Handler("::1")))

    def test_remote_refused_when_no_token_configured(self) -> None:
        # The server binds 0.0.0.0 and the installer opens 8787 to the LAN, so
        # an unconfigured deployment must not accept control actions from off-box.
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": ""}, clear=False):
            handler = _Handler("192.168.1.50")
            self.assertFalse(dash._dashboard_token_ok(handler))
            self.assertFalse(dash._require_token(handler))
            status, payload = handler.responses[-1]
            self.assertEqual(401, status)
            self.assertEqual("remote_control_disabled", payload["error"])

    # -- token configured: required everywhere -------------------------------

    def test_remote_allowed_with_correct_header_token(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": "s3cret"}, clear=False):
            handler = _Handler("192.168.1.50", {"X-Dashboard-Token": "s3cret"})
            self.assertTrue(dash._dashboard_token_ok(handler))
            self.assertTrue(dash._require_token(handler))

    def test_bearer_and_query_token_are_accepted(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": "s3cret"}, clear=False):
            bearer = _Handler("192.168.1.50", {"Authorization": "Bearer s3cret"})
            self.assertTrue(dash._dashboard_token_ok(bearer))
            query = _Handler("192.168.1.50", path="/api/settings?token=s3cret")
            self.assertTrue(dash._dashboard_token_ok(query))

    def test_wrong_token_refused(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": "s3cret"}, clear=False):
            handler = _Handler("192.168.1.50", {"X-Dashboard-Token": "nope"})
            self.assertFalse(dash._dashboard_token_ok(handler))
            self.assertFalse(dash._require_token(handler))
            status, payload = handler.responses[-1]
            self.assertEqual(401, status)
            self.assertEqual("invalid_or_missing_token", payload["error"])

    def test_loopback_still_needs_the_token_once_one_is_configured(self) -> None:
        # Otherwise anything running on the box sidesteps the token entirely.
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": "s3cret"}, clear=False):
            self.assertFalse(dash._dashboard_token_ok(_Handler("127.0.0.1")))

    def test_configured_token_is_whitespace_trimmed(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": "  s3cret  "}, clear=False):
            handler = _Handler("192.168.1.50", {"X-Dashboard-Token": "s3cret"})
            self.assertTrue(dash._dashboard_token_ok(handler))

    def test_whitespace_only_token_counts_as_unconfigured(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": "   "}, clear=False):
            self.assertTrue(dash._dashboard_token_ok(_Handler("127.0.0.1")))
            self.assertFalse(dash._dashboard_token_ok(_Handler("192.168.1.50")))

    def test_malformed_client_address_is_not_treated_as_loopback(self) -> None:
        with patch.dict(os.environ, {"YT_DASHBOARD_TOKEN": ""}, clear=False):
            self.assertFalse(dash._dashboard_token_ok(_Handler("not-an-ip")))


if __name__ == "__main__":
    unittest.main()
