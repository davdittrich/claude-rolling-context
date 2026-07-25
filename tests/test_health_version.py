"""zd2.1: /health reports the running proxy version from plugin.json.

The version MUST come from .claude-plugin/plugin.json (the canonical source
hooks/start-proxy.sh reads), NOT a hardcoded literal — so this test re-reads
plugin.json at runtime and asserts equality, keeping it green across bumps.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import unittest
from unittest import mock

from _fakes import make_handler

import server


def _plugin_version():
    path = os.path.join(os.path.dirname(__file__), "..", ".claude-plugin", "plugin.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["version"]


def _health_json():
    """Invoke _handle_health and parse the JSON body it writes."""
    handler = make_handler(b"")
    handler._handle_health()
    raw = handler.wfile.getvalue()
    body = raw.split(b"\r\n\r\n", 1)[1]
    return json.loads(body)


class HealthVersionTest(unittest.TestCase):
    def test_module_constant_matches_plugin_json(self):
        self.assertEqual(server.PROXY_VERSION, _plugin_version())

    def test_health_reports_version_from_plugin_json(self):
        data = _health_json()
        self.assertIn("version", data)
        # re-read, not hardcoded — survives a version bump
        self.assertEqual(data["version"], _plugin_version())

    def test_load_version_happy_path_returns_plugin_version(self):
        self.assertEqual(server._load_version(), _plugin_version())

    def test_load_version_falls_back_to_unknown_on_error(self):
        # _load_version must never crash on a missing/unreadable plugin.json
        with mock.patch("server.open", side_effect=OSError("boom")):
            self.assertEqual(server._load_version(), "unknown")


if __name__ == "__main__":
    unittest.main()
