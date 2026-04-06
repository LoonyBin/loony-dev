"""Tests for encode_marker / decode_last_seen round-trip (issue #82)."""
from __future__ import annotations

import unittest

from loony_dev.tasks.base import (
    FAILURE_MARKER,
    FAILURE_MARKER_PREFIX,
    SUCCESS_MARKER,
    SUCCESS_MARKER_PREFIX,
    decode_last_seen,
    encode_marker,
)


class TestEncodeMarker(unittest.TestCase):

    def test_success_marker_format(self) -> None:
        result = encode_marker(SUCCESS_MARKER_PREFIX, "2025-01-15T10:32:00Z")
        self.assertEqual(result, "<!-- loony-success last-seen=2025-01-15T10:32:00Z -->")

    def test_failure_marker_format(self) -> None:
        result = encode_marker(FAILURE_MARKER_PREFIX, "2025-06-01T00:00:00Z")
        self.assertEqual(result, "<!-- loony-failure last-seen=2025-06-01T00:00:00Z -->")

    def test_encoded_marker_starts_with_prefix(self) -> None:
        marker = encode_marker(SUCCESS_MARKER_PREFIX, "2025-01-15T10:32:00Z")
        self.assertTrue(marker.startswith(SUCCESS_MARKER_PREFIX))


class TestDecodeLastSeen(unittest.TestCase):

    def test_round_trip_success(self) -> None:
        ts = "2025-01-15T10:32:00Z"
        marker = encode_marker(SUCCESS_MARKER_PREFIX, ts)
        self.assertEqual(decode_last_seen(marker), ts)

    def test_round_trip_in_full_comment_body(self) -> None:
        ts = "2025-03-20T08:00:00Z"
        marker = encode_marker(SUCCESS_MARKER_PREFIX, ts)
        body = f"{marker}\n\nReview comments addressed.\n\nSome summary."
        self.assertEqual(decode_last_seen(body), ts)

    def test_old_marker_returns_none(self) -> None:
        """Legacy markers without last-seen should return None."""
        self.assertIsNone(decode_last_seen(SUCCESS_MARKER))
        self.assertIsNone(decode_last_seen(FAILURE_MARKER))

    def test_old_marker_with_body_returns_none(self) -> None:
        body = f"{SUCCESS_MARKER}\n\nReview comments addressed."
        self.assertIsNone(decode_last_seen(body))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(decode_last_seen(""))

    def test_malformed_body_returns_none(self) -> None:
        self.assertIsNone(decode_last_seen("<!-- loony-success incomplete"))

    def test_malformed_body_does_not_crash(self) -> None:
        # Should not raise regardless of input
        for body in ["", "   ", "<!-- -->", "last-seen= -->", "last-seen=-->"]:
            decode_last_seen(body)  # must not raise
