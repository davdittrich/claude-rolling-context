"""Fast-follow Task A: flattened path applies the decay guard (one condense
pass on truncation or over-ceiling) and sends max_tokens=20000.

Reuses the reload/env machinery from test_flattened_reply_guard.py because
SUMMARIZER_FORMAT is a module constant computed at import time.

Run: python3 -m unittest tests.test_flattened_guard -v
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


class _SeqConn:
    """Returns canned JSON replies in sequence and captures every outgoing
    body on .bodies. Each reply is a dict; successive getresponse() calls
    consume the list (last repeats once exhausted)."""

    def __init__(self, replies):
        self._replies = [json.dumps(r).encode() for r in replies]
        self._idx = 0
        self.bodies = []

    def request(self, method, path, body=None, headers=None):
        self.bodies.append(json.loads(body))

    def getresponse(self):
        i = min(self._idx, len(self._replies) - 1)
        self._idx += 1
        return FakeResponse(self._replies[i])

    def close(self):
        pass


class _FlattenedGuardBase(unittest.TestCase):
    FORMAT = None

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["ROLLING_CONTEXT_SUMMARIZER_FORMAT"] = self.FORMAT
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

    def _seq(self, replies):
        conn = _SeqConn(replies)
        compressor._summarizer_conn = lambda timeout=120: conn
        return conn

    def _call(self):
        return self.comp._summarize_flattened(prompt="summarize this", auth_headers={})


class AnthropicFlattenedGuardTest(_FlattenedGuardBase):
    FORMAT = "anthropic"

    def _reply(self, text, stop_reason="end_turn"):
        return {"content": [{"type": "text", "text": text}], "stop_reason": stop_reason}

    def test_cap_is_20000_in_request(self):
        conn = self._seq([self._reply("fine summary")])
        self._call()
        self.assertEqual(conn.bodies[0]["max_tokens"], 20000)

    def test_normal_reply_no_condense(self):
        conn = self._seq([self._reply("fine summary")])
        out = self._call()
        self.assertEqual(out, "fine summary")
        self.assertEqual(len(conn.bodies), 1)

    def test_truncation_triggers_condense(self):
        conn = self._seq([
            self._reply("TRUNCATED", stop_reason="max_tokens"),
            self._reply("CONDENSED", stop_reason="end_turn"),
        ])
        out = self._call()
        self.assertEqual(out, "CONDENSED")
        self.assertEqual(len(conn.bodies), 2)

    def test_over_ceiling_triggers_condense(self):
        huge = "X" * (compressor.HARD_CEILING_TOKENS * 4 + 10)
        conn = self._seq([
            self._reply(huge, stop_reason="end_turn"),
            self._reply("CONDENSED", stop_reason="end_turn"),
        ])
        out = self._call()
        self.assertEqual(out, "CONDENSED")
        self.assertEqual(len(conn.bodies), 2)

    def test_condense_body_wraps_condense_prompt(self):
        conn = self._seq([
            self._reply("TRUNCATED", stop_reason="max_tokens"),
            self._reply("CONDENSED"),
        ])
        self._call()
        second = conn.bodies[1]["messages"][0]["content"]
        self.assertIn("TRUNCATED", second)
        self.assertIn("16,000", second)  # CONDENSE_PROMPT text


class OpenAIFlattenedGuardTest(_FlattenedGuardBase):
    FORMAT = "openai"

    def _reply(self, text, finish_reason="stop"):
        return {"choices": [{"message": {"content": text}, "finish_reason": finish_reason}]}

    def test_length_finish_reason_triggers_condense(self):
        conn = self._seq([
            self._reply("TRUNCATED", finish_reason="length"),
            self._reply("CONDENSED", finish_reason="stop"),
        ])
        out = self._call()
        self.assertEqual(out, "CONDENSED")
        self.assertEqual(len(conn.bodies), 2)

    def test_normal_reply_no_condense(self):
        conn = self._seq([self._reply("fine")])
        out = self._call()
        self.assertEqual(out, "fine")
        self.assertEqual(len(conn.bodies), 1)


if __name__ == "__main__":
    unittest.main()
