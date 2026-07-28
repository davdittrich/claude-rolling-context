"""Tests for _summarize_flattened's reply-parsing guard.

Invariants proven here:
- A summarizer reply with empty/missing/null content raises RuntimeError
  (so the caller's cooldown path in server.py's _do_background_compression
  fires) instead of crashing with IndexError/KeyError or silently returning
  None / "" / the literal string "None" as a summary.
- Guard applies to BOTH wire formats: openai (choices[0].message.content)
  and anthropic (content[0].text).
- A well-formed reply still returns the extracted text unchanged.

SUMMARIZER_FORMAT is a module-level constant computed at import time from
ROLLING_CONTEXT_SUMMARIZER_FORMAT, so tests reload the module under a
controlled environment (mirrors tests/test_native_mode_model.py).

Run: python3 -m unittest discover -s tests
"""
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeResponse  # noqa: E402

_ENV_KEYS = (
    "ROLLING_CONTEXT_SUMMARIZER_URL",
    "ROLLING_CONTEXT_SUMMARIZER_KEY",
    "ROLLING_CONTEXT_SUMMARIZER_FORMAT",
    "ROLLING_CONTEXT_MODEL",
)


class _FakeConn:
    """Returns a canned JSON reply instead of hitting the network."""

    def __init__(self, reply_obj):
        self._body = json.dumps(reply_obj).encode()

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        return FakeResponse(self._body)

    def close(self):
        pass


class _FlattenedReplyGuardTestBase(unittest.TestCase):
    """Common reload/restore machinery. Subclasses set FORMAT and build a
    RollingCompressor with the args that format requires."""

    FORMAT = None  # set by subclass: "openai" or "anthropic"

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["ROLLING_CONTEXT_SUMMARIZER_FORMAT"] = self.FORMAT
        # Any format other than the (implicit) native default must set a
        # summarizer URL/key/model to actually exercise flattened mode's
        # module constants consistently; _summarize_flattened itself does
        # not gate on NATIVE_MODE, so this is just realism, not a dependency.
        os.environ["ROLLING_CONTEXT_SUMMARIZER_URL"] = "http://localhost:9"
        importlib.reload(compressor)
        self._real_conn_fn = compressor._summarizer_conn
        model = "gpt-4o-mini" if self.FORMAT == "openai" else ""
        self.comp = compressor.RollingCompressor(summarizer_model=model)

    def tearDown(self):
        compressor._summarizer_conn = self._real_conn_fn
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(compressor)

    def _fake_reply(self, reply_obj):
        compressor._summarizer_conn = lambda ep, timeout=120: _FakeConn(reply_obj)

    def _call(self):
        return self.comp._summarize_flattened(prompt="summarize this", auth_headers={})


class OpenAIFlattenedReplyGuardTest(_FlattenedReplyGuardTestBase):
    FORMAT = "openai"

    def test_happy_path_returns_text_unchanged(self):
        self._fake_reply({"choices": [{"message": {"content": "the summary"}}]})
        self.assertEqual(self._call(), "the summary")

    def test_missing_choices_key_raises(self):
        self._fake_reply({})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_empty_choices_list_raises(self):
        self._fake_reply({"choices": []})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_null_content_raises(self):
        self._fake_reply({"choices": [{"message": {"content": None}}]})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_empty_string_content_raises(self):
        self._fake_reply({"choices": [{"message": {"content": ""}}]})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_missing_content_key_raises(self):
        self._fake_reply({"choices": [{"message": {}}]})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_never_returns_none_or_literal_none_string(self):
        for reply_obj in (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": None}}]},
        ):
            self._fake_reply(reply_obj)
            try:
                result = self._call()
            except RuntimeError:
                continue
            self.fail(f"expected RuntimeError, got a return value: {result!r}")


class AnthropicFlattenedReplyGuardTest(_FlattenedReplyGuardTestBase):
    FORMAT = "anthropic"

    def test_happy_path_returns_text_unchanged(self):
        self._fake_reply({"content": [{"type": "text", "text": "the summary"}]})
        self.assertEqual(self._call(), "the summary")

    def test_missing_content_key_raises(self):
        self._fake_reply({})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_empty_content_list_raises(self):
        self._fake_reply({"content": []})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_null_text_raises(self):
        self._fake_reply({"content": [{"type": "text", "text": None}]})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_empty_string_text_raises(self):
        self._fake_reply({"content": [{"type": "text", "text": ""}]})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_missing_text_key_raises(self):
        self._fake_reply({"content": [{"type": "text"}]})
        with self.assertRaises(RuntimeError):
            self._call()

    def test_never_returns_none_or_literal_none_string(self):
        for reply_obj in (
            {},
            {"content": []},
            {"content": [{"type": "text", "text": None}]},
        ):
            self._fake_reply(reply_obj)
            try:
                result = self._call()
            except RuntimeError:
                continue
            self.fail(f"expected RuntimeError, got a return value: {result!r}")


if __name__ == "__main__":
    unittest.main()
