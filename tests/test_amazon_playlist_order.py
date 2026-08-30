"""Regression guards for Amazon playlist scraping (GitLab issue #81).

Amazon playlists used to come back sorted alphabetically by artist, because
both collectors were sets and the result was run through sorted().  A playlist
is a running order, so these check the ordering survives, and that the explicit
badge stops gatecrashing the title while we're here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amazonpl


def _ordered(seen):
    """Mirror the final ordering step in amazonpl.main()."""
    return [track for track, _ in sorted(seen.items(), key=lambda item: item[1])]


def test_explicit_badge_is_stripped_from_title():
    assert amazonpl._strip_explicit_badge("petal\nE") == "petal"
    assert amazonpl._strip_explicit_badge("Ain't In LA\n  E  ") == "Ain't In LA"
    assert amazonpl._strip_explicit_badge("Janice STFU\nEXPLICIT") == "Janice STFU"


def test_explicit_badge_stripping_leaves_real_titles_alone():
    # A title that merely ends in an E, or is one, keeps it.
    assert amazonpl._strip_explicit_badge("E85") == "E85"
    assert amazonpl._strip_explicit_badge("Vitamin E") == "Vitamin E"
    assert amazonpl._strip_explicit_badge("Ordinary") == "Ordinary"


def test_playlist_order_is_preserved_not_alphabetised():
    seen = {}
    amazonpl._merge_positioned(
        [(400.0, "Zara Larsson - Midnight Sun"), (480.0, "Alex Warren - Ordinary")],
        seen,
    )
    assert _ordered(seen) == ["Zara Larsson - Midnight Sun", "Alex Warren - Ordinary"]


def test_row_missed_on_the_first_pass_lands_in_its_proper_slot():
    # The virtualised list recycles rows, so the second crawl can turn up a
    # track the first one never saw.  It belongs where the page put it, not
    # tacked onto the end.
    seen = {}
    amazonpl._merge_positioned([(400.0, "first"), (560.0, "third")], seen)
    amazonpl._merge_positioned([(480.0, "second")], seen)
    assert _ordered(seen) == ["first", "second", "third"]


def test_repeated_sightings_keep_the_earliest_position():
    seen = {}
    amazonpl._merge_positioned([(400.0, "a"), (480.0, "b")], seen)
    amazonpl._merge_positioned([(2400.0, "a")], seen)
    assert seen["a"] == 400.0
    assert _ordered(seen) == ["a", "b"]


def test_a_row_at_the_very_top_is_not_lost_to_a_falsy_position():
    seen = {}
    amazonpl._merge_positioned([(80.0, "second"), (0.0, "first")], seen)
    assert _ordered(seen) == ["first", "second"]


def test_payload_extractor_keeps_discovery_order():
    out: dict[str, None] = {}
    amazonpl._extract_from_payload(
        {
            "tracks": [
                {"trackId": "1", "title": "Zebra", "artistName": "Zed"},
                {"trackId": "2", "title": "Apple", "artistName": "Ann"},
                {"trackId": "3", "title": "Zebra", "artistName": "Zed"},
            ]
        },
        out,
    )
    assert list(out) == ["Zed - Zebra", "Ann - Apple"]
