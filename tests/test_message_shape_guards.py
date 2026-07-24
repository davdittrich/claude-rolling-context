"""Tests for two request-shape defects that Anthropic rejects with a 400:

(a) Native summarization (_summarize_native) appended a fresh `user` compact
    prompt after a `convo[-1]` that was itself `user` -> two consecutive
    `user` turns. Fix: merge the compact instruction into that trailing user
    message instead of appending a new one.
(b) `_validate_tool_pairs` scanned the WHOLE message array for orphaned
    tool_result blocks and cut everything up to the LAST one found, which
    could slice through the injected [summary, ack] prefix (SUMMARY_MARKER
    at messages[0], ack at messages[1]) or discard valid, unrelated pairs
    further into the conversation. Fix: trim only a leading orphaned
    tool_result turn (plus its now-dangling reply), never the prefix.

A third test proves the combined injection contract still holds: after both
fixes, _has_summary/_extract_summary still see the prefix at [0]/[1], and a
previously-stored compression still matches via CompressionStore.find_match.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
import server  # noqa: E402
from compressor import NATIVE_COMPACT_PROMPT, SUMMARY_MARKER, SUMMARY_END_MARKER  # noqa: E402


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


class NativeCompactPromptMergeTest(unittest.TestCase):
    """(a) A trailing user turn must not get a second, consecutive user
    message appended -- the compact instruction has to merge into it."""

    def setUp(self):
        self._fake_conn = _FakeConn()
        self._real_conn_fn = compressor._summarizer_conn
        compressor._summarizer_conn = lambda timeout=600: self._fake_conn

    def tearDown(self):
        compressor._summarizer_conn = self._real_conn_fn

    def test_user_ending_convo_yields_no_consecutive_user_roles(self):
        comp = compressor.RollingCompressor()
        payload = {"model": "claude-sonnet-4-5-20250929"}
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "turn 2"},
        ]
        comp._summarize_native(payload, messages, cut=3, auth_headers={})

        sent = self._fake_conn.last_body["messages"]
        roles = [m["role"] for m in sent]
        for i in range(1, len(roles)):
            self.assertNotEqual(
                roles[i], roles[i - 1],
                f"consecutive same-role turns at {i - 1}/{i}: {roles}",
            )
        # Merged into the existing turn -- no new message appended.
        self.assertEqual(len(sent), 3)
        self.assertEqual(roles[-1], "user")
        last_content = sent[-1]["content"]
        self.assertIsInstance(last_content, list)
        texts = [b.get("text", "") for b in last_content if isinstance(b, dict)]
        self.assertIn("turn 2", texts)
        self.assertTrue(any(NATIVE_COMPACT_PROMPT in t for t in texts))

    def test_assistant_ending_convo_still_appends_new_user_turn(self):
        # Regression guard: the common case (convo ends on assistant) must
        # keep working exactly as before -- a new user message is appended.
        comp = compressor.RollingCompressor()
        payload = {"model": "claude-sonnet-4-5-20250929"}
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "reply 1"},
        ]
        comp._summarize_native(payload, messages, cut=2, auth_headers={})

        sent = self._fake_conn.last_body["messages"]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[-1]["role"], "user")
        self.assertEqual(sent[-1]["content"], NATIVE_COMPACT_PROMPT)


class ValidateToolPairsTest(unittest.TestCase):
    """(b) Only a leading orphaned tool_result turn (plus its dangling
    reply) may be trimmed. The injected [summary, ack] prefix and any valid
    non-orphan pairs further in must survive untouched."""

    def _summary_prefix(self):
        summary_msg = {
            "role": "user",
            "content": (
                f"{SUMMARY_MARKER}\nprior conversation history\n{SUMMARY_END_MARKER}\n\n"
                "Continue from where we left off."
            ),
        }
        ack_msg = {
            "role": "assistant",
            "content": "I have the full context from our previous conversation.",
        }
        return summary_msg, ack_msg

    def test_no_op_when_all_pairs_valid(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "f", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
            ]},
        ]
        result = server._validate_tool_pairs(messages)
        self.assertEqual(result, messages)

    def test_prefix_and_deeper_valid_pair_survive_a_leading_orphan(self):
        summary_msg, ack_msg = self._summary_prefix()
        orphan = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "stale"}],
        }
        dangling_reply = {"role": "assistant", "content": "noted"}
        next_user = {"role": "user", "content": "let's continue"}
        tool_call = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "f", "input": {}},
        ]}
        tool_result = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "42"},
        ]}
        messages = [summary_msg, ack_msg, orphan, dangling_reply, next_user, tool_call, tool_result]

        result = server._validate_tool_pairs(messages)

        # Prefix untouched, at the required positions.
        self.assertIs(result[0], summary_msg)
        self.assertIs(result[1], ack_msg)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")
        # Orphan + its dangling reply dropped; the rest survives verbatim.
        self.assertEqual(result[2:], [next_user, tool_call, tool_result])
        # First surviving message is user (contract for the API).
        self.assertEqual(result[0]["role"], "user")

    def test_orphan_without_prefix_is_dropped_and_stays_user_first(self):
        orphan = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "stale"}],
        }
        dangling_reply = {"role": "assistant", "content": "noted"}
        next_user = {"role": "user", "content": "hello"}
        messages = [orphan, dangling_reply, next_user]

        result = server._validate_tool_pairs(messages)

        self.assertEqual(result, [next_user])
        self.assertEqual(result[0]["role"], "user")


class InjectionContractTest(unittest.TestCase):
    """(c) After both fixes: _has_summary is True on the merged array, the
    prefix is exactly [summary(user), ack(assistant)] at [0]/[1], and a
    previously-stored compression still matches via find_match."""

    def test_contract_holds_through_match_and_validate(self):
        comp = compressor.RollingCompressor()

        summary_msg = {
            "role": "user",
            "content": (
                f"{SUMMARY_MARKER}\nold history\n{SUMMARY_END_MARKER}\n\n"
                "Continue from where we left off."
            ),
        }
        ack_msg = {
            "role": "assistant",
            "content": "I have the full context from our previous conversation.",
        }
        prefix = [summary_msg, ack_msg]
        self.assertTrue(comp._has_summary(prefix))

        # What this stored compression replaces.
        old_messages = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ]
        old_hashes = server._hash_messages(old_messages)

        store = server.CompressionStore()
        entry = store.add()
        entry["prefix"] = prefix
        entry["original_hashes"] = old_hashes

        # A new request: the same old_messages still verbatim, followed by a
        # tail whose first message is an orphaned tool_result (its tool_use
        # lived in old_messages -- gone once the prefix replaces them).
        tail = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "orphan_call", "content": "stale"},
            ]},
            {"role": "assistant", "content": "dangling reply"},
            {"role": "user", "content": "next real turn"},
        ]
        incoming = old_messages + tail
        incoming_hashes = server._hash_messages(incoming)

        match, match_end = store.find_match(incoming_hashes, incoming)
        self.assertIs(match, entry)
        self.assertEqual(match_end, len(old_hashes))

        new_messages = incoming[match_end:]
        merged = match["prefix"] + new_messages
        merged = server._validate_tool_pairs(merged)

        self.assertTrue(comp._has_summary(merged))
        self.assertEqual(merged[0], summary_msg)
        self.assertEqual(merged[1], ack_msg)
        self.assertEqual(merged[0]["role"], "user")
        self.assertEqual(merged[1]["role"], "assistant")
        self.assertEqual(merged[2]["content"], "next real turn")
        self.assertEqual(comp._extract_summary(merged), "old history")


if __name__ == "__main__":
    unittest.main()
