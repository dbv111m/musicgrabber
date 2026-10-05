"""Keep the documented release-credit boundaries predictable, including gaps."""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metadata


@pytest.fixture
def fetch_singles(monkeypatch):
    def fetch(releases):
        calls = []

        def get(_client, url, **kwargs):
            calls.append((url, kwargs["params"]))
            return httpx.Response(200, json={
                "release-count": len(releases), "releases": releases,
            })

        monkeypatch.setattr(httpx.Client, "get", get)
        tracks = metadata.fetch_artist_singles("followed-artist")
        assert len(calls) == 1
        url, params = calls[0]
        assert url == "https://musicbrainz.org/ws/2/release"
        assert params["artist"] == "followed-artist"
        assert params["type"] == "single"
        assert "track_artist" not in params
        return tracks

    return fetch


def credit(name, joinphrase="", *, alias=None):
    result = {"artist": {"name": name}, "joinphrase": joinphrase}
    if alias is not None:
        result["name"] = alias
    return result


def release(credits, *, secondary_types=(), tracks=None):
    return {
        "id": "release-id", "date": "2026-10-03", "title": "Single",
        "artist-credit": credits,
        "release-group": {"secondary-types": list(secondary_types)},
        "media": [{"tracks": tracks or [{"recording": {"title": "A-side"}}]}],
    }


@pytest.mark.parametrize("followed_position", range(3))
def test_shared_credit_preserves_aliases_and_join_phrases(fetch_singles, followed_position):
    names = ["Lead", "Guest", "Third"]
    names[followed_position] = "Followed artist"
    credits = [credit(names[0], " feat. "), credit(names[1], " & "),
               credit(names[2], alias="Credited alias")]
    tracks = fetch_singles([release(credits)])
    assert tracks == [{
        "title": "A-side", "artist": f"{names[0]} feat. {names[1]} & Credited alias",
        "release_date": "2026-10-03", "release_mbid": "release-id",
    }]


def test_recording_credit_wins_and_all_single_tracks_are_returned(fetch_singles):
    # Based on RAYE's real 'By Your Side' release: release and recording credits
    # use different spellings and join phrases; an extended mix is a second track.
    tracks = fetch_singles([release(
        [credit("Jonas Blue", " Featuring "), credit("RAYE", alias="Raye")],
        tracks=[
            {"recording": {"title": "By Your Side", "artist-credit": [
                credit("Jonas Blue", " feat. "), credit("RAYE"),
            ]}},
            {"recording": {"title": "By Your Side (extended mix)"}},
        ],
    )])
    assert [(t["title"], t["artist"]) for t in tracks] == [
        ("By Your Side", "Jonas Blue feat. RAYE"),
        ("By Your Side (extended mix)", "Jonas Blue Featuring Raye"),
    ]


@pytest.mark.parametrize("secondary_type", [
    "Remix", "Live", "Compilation", "Soundtrack", "Interview", "Spokenword",
    "Audiobook", "Audio drama", "DJ-mix", "Mixtape/Street",
])
def test_excluded_release_type_wins_over_shared_credit(fetch_singles, secondary_type):
    tracks = fetch_singles([release(
        [credit("Lead", " feat. "), credit("Followed artist")],
        secondary_types=["Other", secondary_type],
    )])
    assert tracks == []


def test_untyped_remix_is_still_discovered(fetch_singles):
    tracks = fetch_singles([release(
        [credit("Followed artist")],
        tracks=[{"recording": {"title": "A-side (Someone Remix)"}}],
    )])
    assert tracks[0]["title"] == "A-side (Someone Remix)"


def test_no_release_appearances_does_not_browse_track_guests(fetch_singles):
    assert fetch_singles([]) == []
