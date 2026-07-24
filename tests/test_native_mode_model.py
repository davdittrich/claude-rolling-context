"""Tests for Strategy A: ROLLING_CONTEXT_MODEL forces flattened mode.

Invariants proven here:
- Setting ROLLING_CONTEXT_MODEL switches NATIVE_MODE off (a pinned summarizer
  model means the user wants a foreign model, so the cloned-session-request
  trick can't produce a prompt-cache hit anyway -> use flattened instead).
- Leaving ROLLING_CONTEXT_MODEL unset keeps native mode on.
- _summarize_native always sends the session's own payload["model"], never
  self.summarizer_model, even if summarizer_model is set on the instance
  (defends against a future edit reintroducing the foreign-model cache miss).

Run: python3 -m unittest discover -s tests
"""
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402

_ENV_KEYS = (
    "ROLLING_CONTEXT_SUMMARIZER_URL",
    "ROLLING_CONTEXT_SUMMARIZER_KEY",
    "ROLLING_CONTEXT_SUMMARIZER_FORMAT",
    "ROLLING_CONTEXT_MODEL",
)


class NativeModePredicateTest(unittest.TestCase):
    """NATIVE_MODE is a module-level constant computed at import time from
    the environment, so these tests reload the module under a controlled
    environment rather than mutating live globals."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(compressor)

    def _reload(self, **env):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        importlib.reload(compressor)
        return compressor

    def test_model_set_disables_native(self):
        mod = self._reload(ROLLING_CONTEXT_MODEL="claude-opus-4-1-20250805")
        self.assertFalse(mod.NATIVE_MODE)

    def test_model_unset_keeps_native(self):
        mod = self._reload()
        self.assertTrue(mod.NATIVE_MODE)

    def test_model_set_alongside_other_native_defaults(self):
        # Sanity: it's specifically MODEL_SET tripping the switch, not a stale
        # SUMMARIZER_* default.
        mod = self._reload(ROLLING_CONTEXT_MODEL="qwen3:8b")
        self.assertTrue(mod.MODEL_SET)
        self.assertFalse(mod.SUMMARIZER_URL_SET)
        self.assertEqual(mod.SUMMARIZER_API_KEY, "")
        self.assertEqual(mod.SUMMARIZER_FORMAT, "anthropic")
        self.assertFalse(mod.NATIVE_MODE)


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    """Captures the outgoing request body instead of hitting the network."""

    def __init__(self):
        self.last_body = None

    def request(self, method, path, body=None, headers=None):
        self.last_body = json.loads(body)

    def getresponse(self):
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "summary"}}
        sse = f"data: {json.dumps(event)}\n\n".encode()
        return _FakeResponse(sse)

    def close(self):
        pass


class SummarizeNativeModelTest(unittest.TestCase):
    """Exercises _summarize_native directly (network call stubbed out) to
    prove the request body's model always tracks payload["model"]."""

    def setUp(self):
        self._fake_conn = _FakeConn()
        self._real_conn_fn = compressor._summarizer_conn
        compressor._summarizer_conn = lambda timeout=600: self._fake_conn

    def tearDown(self):
        compressor._summarizer_conn = self._real_conn_fn

    def test_uses_payload_model_ignoring_summarizer_model(self):
        # summarizer_model is set on the instance to a DIFFERENT, foreign
        # model -- if the fix regresses to `self.summarizer_model or ...`
        # this would leak into the request and this assertion fails.
        comp = compressor.RollingCompressor(summarizer_model="some-foreign-model")
        payload = {"model": "claude-sonnet-4-5-20250929"}
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        comp._summarize_native(payload, messages, cut=2, auth_headers={})
        self.assertEqual(self._fake_conn.last_body["model"], "claude-sonnet-4-5-20250929")
        self.assertNotEqual(self._fake_conn.last_body["model"], "some-foreign-model")

    def test_falls_back_to_legacy_default_when_payload_lacks_model(self):
        comp = compressor.RollingCompressor(summarizer_model="")
        payload = {}
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        comp._summarize_native(payload, messages, cut=2, auth_headers={})
        self.assertEqual(self._fake_conn.last_body["model"], compressor.LEGACY_DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
