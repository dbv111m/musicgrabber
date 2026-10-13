"""Regression guards for Amazon playlist scraping (GitLab issue #81).

Amazon playlists used to come back sorted alphabetically by artist, because the
collectors were sets and the result was run through sorted().  A playlist is a
running order, so these check the ordering survives, and that the explicit badge
stops gatecrashing the title while we're here.

Amazon's current JSON calls playlist rows ``VisualRowItemElement`` and names
their useful fields ``primaryText`` and ``secondaryText1``. The old generic
network collector did not know that, which is why it reported zero tracks even
while the response contained the whole list.
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


# ---------------------------------------------------------------------------
# Amazon showHome JSON
# ---------------------------------------------------------------------------

def _network_row(index, title, artist):
    return {
        "interface": "Web.TemplatesInterface.v1_0.Touch.WidgetsInterface.VisualRowItemElement",
        "rowIndex": str(index),
        "primaryText": title,
        "secondaryText1": artist,
        "primaryLink": {"deeplink": f"/albums/album?trackAsin=track-{index}"},
    }


def test_current_show_home_rows_are_extracted_in_playlist_order():
    payload = {
        "methods": [{
            "header": {
                "interface": "Web.TemplatesInterface.v1_0.Touch.WidgetsInterface.TextElement",
                "text": "A Proper Playlist",
                "observer": {"storageGroup": "PLAYLIST_NAME"},
            },
            "items": [
                _network_row(2, "Second", "Artist B"),
                _network_row(1, "First", "Artist A"),
            ],
        }]
    }
    rows = {}
    names = []

    amazonpl._extract_from_payload(payload, rows, names)

    assert amazonpl._ordered_network_tracks(rows) == [
        "Artist A - First",
        "Artist B - Second",
    ]
    assert names == ["A Proper Playlist"]


def test_network_extractor_has_no_hundred_track_ceiling():
    payload = {"items": [
        _network_row(index, f"Track {index}", f"Artist {index}")
        for index in range(125, 0, -1)
    ]}
    rows = {}

    amazonpl._extract_from_payload(payload, rows)
    tracks = amazonpl._ordered_network_tracks(rows)

    assert len(tracks) == 125
    assert tracks[0] == "Artist 1 - Track 1"
    assert tracks[-1] == "Artist 125 - Track 125"


def test_repeated_network_tracks_keep_their_first_playlist_position():
    rows = {}
    amazonpl._extract_from_payload({"items": [
        _network_row(3, "Same Song", "Same Artist"),
        _network_row(1, "Same Song", "Same Artist"),
        _network_row(2, "Other Song", "Other Artist"),
    ]}, rows)

    assert len(rows) == 3
    assert amazonpl._ordered_network_tracks(rows) == [
        "Same Artist - Same Song",
        "Other Artist - Other Song",
    ]


def test_unrelated_json_objects_are_not_mistaken_for_tracks():
    payload = {
        "title": "Not a track",
        "artistName": "Nor an artist",
        "primaryText": "Still not a row",
        "secondaryText1": "Nope",
    }
    rows = {}

    amazonpl._extract_from_payload(payload, rows)

    assert rows == {}


# ---------------------------------------------------------------------------
# Featured-artist chips (backlog note from issue #81, fixed in v4.1.0)
# ---------------------------------------------------------------------------

def test_featured_artist_suffix_is_dropped_when_the_credit_repeats_it():
    """Amazon's own title for a collaboration carries the guest's name."""
    assert amazonpl._strip_featured_suffix(
        "Stateside + Zara Larsson", "PinkPantheress feat. Zara Larsson"
    ) == "Stateside"


def test_featured_artist_suffix_survives_when_it_is_part_of_the_song_name():
    """The whole reason this is not a bare regex."""
    assert amazonpl._strip_featured_suffix("Peaches + Cream", "112") == "Peaches + Cream"
    assert amazonpl._strip_featured_suffix("Ghosts + Guilt", "Nine Inch Nails") == "Ghosts + Guilt"


def test_featured_artist_suffix_leaves_ordinary_titles_alone():
    assert amazonpl._strip_featured_suffix("Boston", "STELLA LEFTY") == "Boston"
    assert amazonpl._strip_featured_suffix("Dai Dai", "Shakira & Burna Boy") == "Dai Dai"


def test_featured_artist_suffix_ignores_a_dangling_plus():
    assert amazonpl._strip_featured_suffix("Something + ", "Someone") == "Something + "
    assert amazonpl._strip_featured_suffix(" + Someone", "Someone") == " + Someone"
