"""Unit tests for the search-result quality tier used by the minimum-quality filter.

Pure functions, no network. The interesting cases are the sources that decline
to say anything about quality before download, because a filter that quietly
treats "did not say" as "bad" would hide every YouTube result in the list.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import (
    TIER_UNKNOWN, TIER_LOSSY_128, TIER_LOSSY_192, TIER_LOSSY_256,
    TIER_LOSSY_320, TIER_LOSSLESS, kbps_to_tier,
)


def _import_search_or_skip():
    try:
        import search
    except ModuleNotFoundError as exc:
        pytest.skip(f"search module dependencies unavailable: {exc.name}")
    return search


# The exact strings each source puts in the "quality" field today. If a source
# changes its wording, this table is the thing that should fail first.
@pytest.mark.parametrize("label,expected", [
    # Soulseek (slskd.parse_slskd_quality)
    ("FLAC", TIER_LOSSLESS),
    ("FLAC 24bit/96kHz", TIER_LOSSLESS),
    ("WAV", TIER_LOSSLESS),
    ("MP3 320", TIER_LOSSY_320),
    ("MP3 256", TIER_LOSSY_256),
    ("MP3 192", TIER_LOSSY_192),
    ("MP3 128", TIER_LOSSY_128),
    ("AAC 256", TIER_LOSSY_256),
    ("AAC 128", TIER_LOSSY_128),
    ("OGG/Opus", TIER_UNKNOWN),   # No bitrate given, so we do not invent one
    ("Unknown", TIER_UNKNOWN),
    # FreeMp3Cloud
    ("320kbps", TIER_LOSSY_320),
    ("128kbps", TIER_LOSSY_128),
    # Monochrome / Tidal tiers
    ("HI_RES_LOSSLESS", TIER_LOSSLESS),
    ("LOSSLESS", TIER_LOSSLESS),
    ("HIGH", TIER_LOSSY_320),
    # zvu4no states a format and nothing else
    ("MP3", TIER_UNKNOWN),
])
def test_real_source_labels_map_to_the_right_tier(label, expected):
    search = _import_search_or_skip()
    assert search.quality_tier_of_result({"quality": label}) == expected


def test_sources_that_say_nothing_are_unknown_not_bad():
    """YouTube and SoundCloud declare no quality at all."""
    search = _import_search_or_skip()
    for result in (
        {"quality": None, "source": "youtube"},
        {"quality": None, "source": "soundcloud"},
        {"quality": "", "source": "youtube"},
        {"quality": "   ", "source": "youtube"},
        {"source": "youtube"},
        {},
    ):
        assert search.quality_tier_of_result(result) == TIER_UNKNOWN


def test_lossless_wins_regardless_of_a_bitrate_in_the_label():
    """A 24bit/96kHz FLAC must not be bucketed by the '96' in its label."""
    search = _import_search_or_skip()
    assert search.quality_tier_of_result({"quality": "FLAC 24bit/96kHz"}) == TIER_LOSSLESS
    assert search.quality_tier_of_result({"quality": "ALAC 16bit/44kHz"}) == TIER_LOSSLESS


def test_tier_matching_is_case_insensitive():
    search = _import_search_or_skip()
    assert search.quality_tier_of_result({"quality": "flac"}) == TIER_LOSSLESS
    assert search.quality_tier_of_result({"quality": "Lossless"}) == TIER_LOSSLESS
    assert search.quality_tier_of_result({"quality": "mp3 320"}) == TIER_LOSSY_320


def test_stamp_quality_tiers_annotates_in_place():
    search = _import_search_or_skip()
    batch = [
        {"quality": "FLAC"},
        {"quality": None},
        {"quality": "MP3 192"},
    ]
    search._stamp_quality_tiers(batch)
    assert [r["quality_tier"] for r in batch] == [TIER_LOSSLESS, TIER_UNKNOWN, TIER_LOSSY_192]


def test_kbps_to_tier_boundaries():
    """The shared bucketing the upgrades scanner uses on real files."""
    assert kbps_to_tier(320) == TIER_LOSSY_320
    assert kbps_to_tier(300) == TIER_LOSSY_320
    assert kbps_to_tier(299) == TIER_LOSSY_256
    assert kbps_to_tier(240) == TIER_LOSSY_256
    assert kbps_to_tier(239) == TIER_LOSSY_192
    assert kbps_to_tier(170) == TIER_LOSSY_192
    assert kbps_to_tier(169) == TIER_LOSSY_128
    assert kbps_to_tier(0) == TIER_LOSSY_128


def test_upgrades_still_uses_the_shared_tier_scale():
    """The scale moved to constants.py; upgrades must not have its own copy."""
    try:
        import upgrades
    except ModuleNotFoundError as exc:
        pytest.skip(f"upgrades dependencies unavailable: {exc.name}")
    assert upgrades.TIER_LOSSLESS == TIER_LOSSLESS
    assert upgrades.TIER_LOSSY_320 == TIER_LOSSY_320
    assert upgrades._kbps_to_tier is kbps_to_tier
