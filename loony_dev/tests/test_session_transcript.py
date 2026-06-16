"""Tests for the pure JSONL → observe-event parser (issue #202)."""
from __future__ import annotations

import unittest

from loony_dev import session_transcript as st


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


class ParseEntryTestCase(unittest.TestCase):
    def test_user_string_prompt(self) -> None:
        events = st.parse_entry(
            {"type": "user", "uuid": "u1", "timestamp": "t",
             "message": {"content": "please implement X"}}
        )
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["kind"], "user")
        self.assertEqual(ev["text"], "please implement X")
        self.assertEqual(ev["id"], "u1#0")
        self.assertEqual(ev["ts"], "t")

    def test_empty_user_string_is_skipped(self) -> None:
        self.assertEqual(
            st.parse_entry({"type": "user", "uuid": "u", "message": {"content": "   "}}), []
        )

    def test_assistant_text_and_thinking(self) -> None:
        events = st.parse_entry(
            {"type": "assistant", "uuid": "a1", "timestamp": "t", "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "here is the answer"},
                ],
            }}
        )
        self.assertEqual(_kinds(events), ["thinking", "assistant"])
        self.assertEqual(events[0]["text"], "let me think")
        self.assertEqual(events[1]["text"], "here is the answer")
        # Distinct ids per block so replay stays idempotent.
        self.assertNotEqual(events[0]["id"], events[1]["id"])
        # tool_use stop_reason is mid-turn → no stop event.
        self.assertNotIn("stop", _kinds(events))

    def test_assistant_tool_use(self) -> None:
        events = st.parse_entry(
            {"type": "assistant", "uuid": "a2", "timestamp": "t", "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "toolu_9", "name": "Bash",
                     "input": {"command": "ls"}},
                ],
            }}
        )
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["kind"], "tool_use")
        self.assertEqual(ev["tool"], "Bash")
        self.assertEqual(ev["args"], {"command": "ls"})
        self.assertEqual(ev["tool_use_id"], "toolu_9")

    def test_user_tool_result_is_not_a_paste(self) -> None:
        # A type:user entry whose blocks are all tool_result is tool output, not
        # a user prompt — the key disambiguation from the issue.
        events = st.parse_entry(
            {"type": "user", "uuid": "u2", "timestamp": "t", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_9",
                 "content": "file1\nfile2"},
            ]}}
        )
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["kind"], "tool_result")
        self.assertEqual(ev["tool_use_id"], "toolu_9")
        self.assertEqual(ev["text"], "file1\nfile2")
        self.assertFalse(ev["is_error"])

    def test_tool_result_error_and_list_content(self) -> None:
        events = st.parse_entry(
            {"type": "user", "uuid": "u3", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t", "is_error": True,
                 "content": [{"type": "text", "text": "boom"}]},
            ]}}
        )
        self.assertEqual(events[0]["kind"], "tool_result")
        self.assertTrue(events[0]["is_error"])
        self.assertEqual(events[0]["text"], "boom")

    def test_terminal_assistant_emits_stop(self) -> None:
        for reason in ("end_turn", "stop_sequence"):
            with self.subTest(reason=reason):
                events = st.parse_entry(
                    {"type": "assistant", "uuid": "a", "message": {
                        "stop_reason": reason,
                        "content": [{"type": "text", "text": "done"}],
                    }}
                )
                self.assertEqual(_kinds(events), ["assistant", "stop"])
                self.assertEqual(events[-1]["stop_reason"], reason)
                self.assertEqual(events[-1]["id"], "a#stop")

    def test_interrupt(self) -> None:
        events = st.parse_entry(
            {"type": "user", "uuid": "u4", "timestamp": "t", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user]"},
            ]}}
        )
        self.assertEqual(_kinds(events), ["interrupt"])
        self.assertEqual(events[0]["id"], "u4#interrupt")

    def test_system_and_unknown_types_yield_nothing(self) -> None:
        for entry in (
            {"type": "system", "uuid": "s", "message": {"content": "init"}},
            {"type": "queue-operation", "operation": "x"},
            {"type": "ai-title", "aiTitle": "t"},
            {"not": "a known shape"},
        ):
            with self.subTest(entry=entry):
                self.assertEqual(st.parse_entry(entry), [])

    def test_multi_block_assistant_keeps_unique_ids(self) -> None:
        events = st.parse_entry(
            {"type": "assistant", "uuid": "a", "message": {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "text", "text": "two"},
                ],
            }}
        )
        ids = [e["id"] for e in events]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
