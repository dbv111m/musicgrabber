"""
Unit tests for monochrome.resolve_album_tracks.

Fast, no-network tests: the Deezer calls and the tracks API ISRC lookup are mocked,
so nothing leaves the building.

The bug that prompted all this: RAYE released two albums four years apart, both
ending on a track called "Fin.". Resolving tracks one at a time, a free-text
search had nothing to tell them apart, cheerfully picked the wrong one, filed it
under the right album and reported success. Matching the album as a whole is the
fix, so most of what follows is about proving that a track can only ever come
from the release we actually asked for.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_monochrome_or_skip():
    try:
        import monochrome
    except ModuleNotFoundError as exc:
        pytest.skip(f"monochrome module dependencies unavailable: {exc.name}")
    return monochrome


# The two real albums involved, trimmed to what the matcher actually reads.
_BLUES_ID = 393727427
_HOPE_ID = 945900541

_BLUES_TRACKS = [
    (2097719517, "Introduction.", "QMDA62229898"),
    (2097719547, "Black Mascara.", "QM6P42216966"),
    (2097719657, "Fin.", "QMDA62229989"),
]
_HOPE_TRACKS = [
    (3917435421, "Intro: Girl Under The Grey Cloud.", "QM4TW2634480"),
    (3917435471, "Click Clack Symphony. (feat. Hans Zimmer)", "QM4TW2634504"),
    (3917435581, "Fin.", "QM4TW2634563"),
]


def _album_search_body(albums):
    return {"data": [
        {"id": aid, "title": title, "artist": {"name": artist}, "nb_tracks": nb}
        for aid, title, artist, nb in albums
    ]}


def _album_detail_body(album_id, title, tracks):
    return {
        "id": album_id,
        "title": title,
        "cover_big": "https://cover.test/big.jpg",
        "tracks": {"data": [
            {"id": tid, "title": ttl, "duration": 200} for tid, ttl, _isrc in tracks
        ]},
    }


def _fake_deezer(albums, detail_map, track_map):
    """Build a _deezer_get stand-in that serves canned album/track payloads."""
    def _get(path, params=None):
        if path == "/search/album":
            return _album_search_body(albums)
        if path.startswith("/album/"):
            return detail_map[int(path.split("/")[-1])]
        if path.startswith("/track/"):
            tid = int(path.split("/")[-1])
            return {"id": tid, "isrc": track_map[tid], "artist": {"name": "RAYE"}}
        raise AssertionError(f"unexpected Deezer path {path}")
    return _get


def _blues_and_hope():
    """Both RAYE albums wired up, so a mismatch has somewhere wrong to go."""
    albums = [
        (_BLUES_ID, "My 21st Century Blues", "RAYE", 15),
        (_HOPE_ID, "THIS MUSIC MAY CONTAIN HOPE.", "RAYE", 17),
    ]
    detail_map = {
        _BLUES_ID: _album_detail_body(_BLUES_ID, "My 21st Century Blues", _BLUES_TRACKS),
        _HOPE_ID: _album_detail_body(_HOPE_ID, "THIS MUSIC MAY CONTAIN HOPE.", _HOPE_TRACKS),
    }
    track_map = {tid: isrc for tid, _t, isrc in _BLUES_TRACKS + _HOPE_TRACKS}
    return _fake_deezer(albums, detail_map, track_map)


def _patched(monochrome, deezer_get, lookup=(None, True)):
    """Patch the network edges. Default tracks API answer is 'lookup unavailable'."""
    return (
        patch.object(monochrome, "_deezer_get", side_effect=deezer_get),
        patch.object(monochrome, "_tracks_isrc_lookup", return_value=lookup),
        patch.object(monochrome, "monochrome_enabled", return_value=True),
    )


def _run(monochrome, artist, album, titles, deezer_get, lookup=(None, True)):
    p1, p2, p3 = _patched(monochrome, deezer_get, lookup)
    with p1, p2, p3:
        return monochrome.resolve_album_tracks(artist, album, titles)


# ---------------------------------------------------------------------------
# The headline bug
# ---------------------------------------------------------------------------

def test_same_title_on_two_albums_resolves_to_the_album_asked_for():
    """The whole point: "Fin." must come off the record you asked for."""
    monochrome = _import_monochrome_or_skip()
    titles = ["Introduction.", "Black Mascara.", "Fin."]

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", titles, _blues_and_hope())

    assert len(resolved) == 3
    # The 2023 "Fin.", not the 2025 one that used to win on free-text score.
    assert "isrc=QMDA62229989" in resolved[2]["source_url"]
    assert "QM4TW2634563" not in resolved[2]["source_url"]
    assert str(_BLUES_TRACKS[2][0]) in resolved[2]["source_url"]


def test_the_other_album_gets_its_own_fin():
    """Same query, other album: the answer must flip, or nothing was pinned."""
    monochrome = _import_monochrome_or_skip()
    titles = ["Intro: Girl Under the Grey Cloud.", "Click Clack Symphony.", "Fin."]

    resolved = _run(monochrome, "RAYE", "THIS MUSIC MAY CONTAIN HOPE.", titles, _blues_and_hope())

    assert len(resolved) == 3
    assert "isrc=QM4TW2634563" in resolved[2]["source_url"]


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------

def test_feature_credits_and_case_do_not_block_a_match():
    """MusicBrainz says "Click Clack Symphony.", Deezer bolts a feat. on the end."""
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "this music may contain hope",
                    ["CLICK CLACK SYMPHONY."], _blues_and_hope())

    assert len(resolved) == 1
    assert "isrc=QM4TW2634504" in resolved[0]["source_url"]


def test_unmatched_titles_are_left_for_the_per_track_fallback():
    """A partial answer is fine; the caller has a fallback waiting."""
    monochrome = _import_monochrome_or_skip()
    titles = ["Introduction.", "A Track That Is Not On This Album", "Fin."]

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", titles, _blues_and_hope())

    assert sorted(resolved) == [0, 2]  # index 1 falls through, as it should


def test_a_deezer_track_is_never_handed_out_twice():
    """Ask for the same title twice and only one copy may be claimed."""
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues",
                    ["Fin.", "Fin."], _blues_and_hope())

    assert len(resolved) == 1


# ---------------------------------------------------------------------------
# Refusing to guess
# ---------------------------------------------------------------------------

def test_wrong_artist_is_refused_outright():
    """A wrong album would poison every track on it, so the artist must match."""
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "Adele", "My 21st Century Blues",
                    ["Introduction."], _blues_and_hope())

    assert resolved == {}


def test_unknown_album_returns_empty_rather_than_the_nearest_thing():
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "An Album Nobody Has Ever Released",
                    ["Introduction."], _blues_and_hope())

    assert resolved == {}


def test_track_count_only_breaks_ties_between_real_title_matches():
    """Deluxe editions change the count, so it must not be a veto."""
    monochrome = _import_monochrome_or_skip()
    # Same title twice; the 15-track pressing should win over the 22-track one.
    deluxe_id = 999999
    albums = [
        (deluxe_id, "My 21st Century Blues", "RAYE", 22),
        (_BLUES_ID, "My 21st Century Blues", "RAYE", 15),
    ]
    detail_map = {
        deluxe_id: _album_detail_body(deluxe_id, "My 21st Century Blues", _HOPE_TRACKS),
        _BLUES_ID: _album_detail_body(_BLUES_ID, "My 21st Century Blues", _BLUES_TRACKS),
    }
    track_map = {tid: isrc for tid, _t, isrc in _BLUES_TRACKS + _HOPE_TRACKS}

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues",
                    ["Introduction.", "Black Mascara.", "Fin."] + [""] * 12,
                    _fake_deezer(albums, detail_map, track_map))

    assert "isrc=QMDA62229989" in resolved[2]["source_url"]


# ---------------------------------------------------------------------------
# Tracks API verification
# ---------------------------------------------------------------------------

def test_a_clean_tracks_api_miss_drops_the_track():
    """The tracks API answered and does not stock it, so let the search try."""
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", ["Fin."],
                    _blues_and_hope(), lookup=(None, False))

    assert resolved == {}


def test_tracks_api_transport_failure_does_not_lose_the_track():
    """Unverifiable is not the same as absent; the API may be back by download time."""
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", ["Fin."],
                    _blues_and_hope(), lookup=(None, True))

    assert len(resolved) == 1
    assert resolved[0]["quality"] == "LOSSLESS"
    assert any("unverified" in note for note in resolved[0]["score_breakdown"])


def test_a_confirmed_track_is_lossless_and_not_marked_unverified():
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", ["Fin."],
                    _blues_and_hope(), lookup=({"trackId": "1", "isrc": "QMDA62229989"}, False))

    assert resolved[0]["quality"] == "LOSSLESS"
    assert not any("unverified" in note for note in resolved[0]["score_breakdown"])


def test_lookup_asks_by_the_albums_own_spelling():
    """The tracks API can only be searched by name, so the name had better be right."""
    monochrome = _import_monochrome_or_skip()
    p1, p2, p3 = _patched(monochrome, _blues_and_hope())
    with p1, p2 as lookup, p3:
        monochrome.resolve_album_tracks("RAYE", "My 21st Century Blues", ["fin"])

    lookup.assert_called_once_with("QMDA62229989", "RAYE", "Fin.")


def test_album_picks_outrank_anything_a_free_text_search_can_score():
    """Identity-pinned, exactly like resolve_by_isrc, so it must sit on top."""
    monochrome = _import_monochrome_or_skip()

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", ["Fin."], _blues_and_hope())

    assert resolved[0]["relevance_score"] > 1000
    assert resolved[0]["source"] == "monochrome"


# ---------------------------------------------------------------------------
# Not making a fuss
# ---------------------------------------------------------------------------

def test_deezer_falling_over_is_an_empty_answer_not_an_exception():
    """The caller has a fallback; a raised exception would skip it."""
    monochrome = _import_monochrome_or_skip()

    def _boom(path, params=None):
        raise RuntimeError("Deezer is having a lie down")

    resolved = _run(monochrome, "RAYE", "My 21st Century Blues", ["Fin."], _boom)

    assert resolved == {}


@pytest.mark.parametrize("artist,album,titles", [
    ("", "My 21st Century Blues", ["Fin."]),
    ("RAYE", "", ["Fin."]),
    ("RAYE", "My 21st Century Blues", []),
])
def test_missing_inputs_short_circuit(artist, album, titles):
    monochrome = _import_monochrome_or_skip()

    assert _run(monochrome, artist, album, titles, _blues_and_hope()) == {}
