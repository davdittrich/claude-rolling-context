"""Which settings file supplies the effective value, in the Fact 3 order (spec section 2, section 6).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from proxy import chain

KEY = "ANTHROPIC_BASE_URL"


class EffectiveValueTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="effective-")
        self.project = tempfile.mkdtemp(prefix="effective-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        self.env_patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        self.env_patch.start()
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        self.addCleanup(self.env_patch.stop)

    def _write(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {KEY: value}}, f)

    def test_unset_everywhere_reports_nothing(self):
        self.assertEqual(chain.effective(KEY, self.project), (None, None))

    def test_user_settings_supply_the_value(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        self._write(path, "http://127.0.0.1:1111")
        value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:1111")
        self.assertEqual(source, path)

    def test_project_local_beats_user_settings(self):
        self._write(os.path.join(self.home, ".claude", "settings.json"), "http://127.0.0.1:1111")
        local = os.path.join(self.project, ".claude", "settings.local.json")
        self._write(local, "http://127.0.0.1:2222")
        value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:2222")
        self.assertEqual(source, local)

    def test_source_is_reported_not_only_the_value(self):
        local = os.path.join(self.project, ".claude", "settings.local.json")
        self._write(local, "http://127.0.0.1:2222")
        _, source = chain.effective(KEY, self.project)
        self.assertTrue(source.endswith("settings.local.json"))

    def test_unparseable_settings_raise_rather_than_defaulting(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(chain.UnparseableSettings) as ctx:
            chain.effective(KEY, self.project)
        self.assertEqual(ctx.exception.path, path)

    def test_missing_env_block_is_not_an_error(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"permissions": {}}, f)
        self.assertEqual(chain.effective(KEY, self.project), (None, None))

    def test_environment_supplies_the_value_when_no_file_does(self):
        with mock.patch.dict(os.environ, {KEY: "http://127.0.0.1:3333"}):
            value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:3333")
        self.assertEqual(source, "<environment>")

    def test_file_value_beats_environment_value(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        self._write(path, "http://127.0.0.1:1111")
        with mock.patch.dict(os.environ, {KEY: "http://127.0.0.1:9999"}):
            value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:1111")
        self.assertEqual(source, path)


if __name__ == "__main__":
    unittest.main()
