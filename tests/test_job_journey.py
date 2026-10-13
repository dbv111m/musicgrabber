"""Unit tests for recording cross-source fallback journeys."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from downloads import _next_source_history


def test_source_journey_starts_with_original_and_new_source():
    assert _next_source_history(None, "monochrome", "soulseek") == [
        "monochrome", "soulseek"
    ]


def test_source_journey_appends_each_fallback_once():
    existing = '["monochrome", "soulseek"]'
    assert _next_source_history(existing, "soulseek", "youtube") == [
        "monochrome", "soulseek", "youtube"
    ]
    assert _next_source_history(existing, "soulseek", "soulseek") == [
        "monochrome", "soulseek"
    ]


def test_malformed_source_journey_recovers_cleanly():
    assert _next_source_history("not-json", "youtube", "soundcloud") == [
        "youtube", "soundcloud"
    ]
