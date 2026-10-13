"""Offline tests for MusicBrainz release selection in lookup_musicbrainz().

No network: every search is served from tests/fixtures/mb_lookup_fixture.json,
captured from the real API by tests/tools/make_lookup_fixture.py.

Background, so the next person does not have to rediscover it the hard way:
MusicBrainz scores text matches purely on string similarity, so every recording
of a popular song ties on 100 and the order between them is arbitrary. Worse, it
is not stable, with Radiohead managing 0% overlap across three identical queries.
Asking for one result and hoping it happens to be the studio album scored 3.7/31
on the tuning corpus. Asking properly scores 26/31.

Tuning lives in tests/tools/eval_mb_strategies.py against the larger corpus in
tests/fixtures/mb_corpus.json; this file just stops the good behaviour rotting.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "mb_lookup_fixture.json")


def _import_metadata_or_skip():
    try:
        import metadata
    except ModuleNotFoundError as exc:
        pytest.skip(f"metadata module dependencies unavailable: {exc.name}")
    return metadata


@pytest.fixture(scope="module")
def fixture_data():
    if not os.path.exists(FIXTURE_PATH):
        pytest.skip("mb_lookup_fixture.json missing; run tests/tools/make_lookup_fixture.py")
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class _Recorder:
    """Serves cached recordings and remembers which query variants were asked for."""

    def __init__(self, entry):
        self.entry = entry
        self.calls = []

    def __call__(self, query, headers):
        if "alias:" in query:
            variant = "alias"
        elif "primarytype:album" in query:
            variant = "filtered"
        else:
            variant = "plain"
        self.calls.append(variant)
        return (self.entry.get(variant) or {}).get("recordings") or []


@pytest.fixture
def lookup(monkeypatch, fixture_data):
    """Returns a callable that runs lookup_musicbrainz against the fixture."""
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)  # no rate-limit naps

    def run(artist, title):
        entry = fixture_data[f"{artist}␟{title}"]
        recorder = _Recorder(entry)
        monkeypatch.setattr(metadata, "_mb_search_recordings", recorder)
        # The captured fixture predates the live release-group fallback. Keep
        # these scorer regressions offline; the fallback has focused tests below.
        monkeypatch.setattr(
            metadata, "_mb_resolve_recording_via_release_group",
            lambda *args, **kwargs: None,
        )
        return metadata.lookup_musicbrainz(artist, title), recorder

    return run


# Album fragment each track must land on. These were all wrong before: Daft Punk
# got Alive 2006, Nirvana a Bristol bootleg, and the two romanised titles got
# nothing whatsoever.
EXPECTED = [
    ("Daft Punk", "Around the World", "homework", "Around the World"),
    ("Nirvana", "Smells Like Teen Spirit", "nevermind", "Smells Like Teen Spirit"),
    ("Massive Attack", "Teardrop", "mezzanine", "Teardrop"),
    ("Rammstein", "Du hast", "sehnsucht", "Du hast"),
    ("Кино", "Группа крови",
     "группа крови",
     "Группа крови"),
]


@pytest.mark.parametrize("artist,title,album_fragment,expected_title", EXPECTED)
def test_lands_on_the_canonical_album(lookup, artist, title, album_fragment, expected_title):
    md, _ = lookup(artist, title)
    assert md is not None, f"no metadata at all for {artist} - {title}"
    assert album_fragment in (md.get("album") or "").lower(), (
        f"{artist} - {title} landed on {md.get('album')!r}")
    assert md.get("title") == expected_title


@pytest.mark.parametrize("artist,title,album_fragment,expected_title", EXPECTED)
def test_track_number_is_populated(lookup, artist, title, album_fragment, expected_title):
    """Guards the media-block fix; a release with no track number is no use to us."""
    md, _ = lookup(artist, title)
    number, total = md.get("track_number"), md.get("track_total")
    assert isinstance(number, int) and number >= 1, f"track number was {number!r}"
    assert total is None or total >= number


def test_normal_lookup_uses_two_queries_not_the_alias_one(lookup):
    """The alias query is a fallback. Firing it every time would cost a second of
    rate-limit sleep for nothing."""
    _, recorder = lookup("Daft Punk", "Around the World")
    assert recorder.calls == ["plain", "filtered"]


def test_romanised_title_falls_back_to_alias(lookup):
    """'Yoru ni Kakeru' matches no recording title, only an alias. Before this
    existed the track came back with no metadata at all."""
    md, recorder = lookup("YOASOBI", "Yoru ni Kakeru")
    assert recorder.calls == ["plain", "filtered", "alias"]
    assert md is not None
    assert md.get("title") == "夜に駆ける"
    assert md.get("track_number") == 1


def test_no_variant_takes_are_chosen(lookup):
    """A recording called "... (Boombox Rehearsals)" is a variant of what we asked
    for, not the thing itself, and tagging it would rename the user's file."""
    metadata = _import_metadata_or_skip()
    for artist, title, _, _ in EXPECTED:
        md, _ = lookup(artist, title)
        want = metadata._normalise_track_title(title)
        got = metadata._normalise_track_title(md.get("title"))
        assert not (want in got and got != want), (
            f"{artist} - {title} picked variant take {md.get('title')!r}")


def test_merge_unions_release_lists_for_the_same_recording():
    """Each query only returns the releases that matched it, so the same recording
    arrives twice carrying different releases. Keeping the first copy and binning
    the rest loses the studio album, which is how this went wrong originally."""
    metadata = _import_metadata_or_skip()
    plain = [{"id": "rec-1", "score": 100, "title": "Song",
              "releases": [{"id": "rel-a", "title": "Some Compilation"}]}]
    filtered = [{"id": "rec-1", "score": 100, "title": "Song",
                 "releases": [{"id": "rel-b", "title": "The Actual Album"}]}]
    merged = metadata._mb_merge_recordings(plain, filtered)
    assert len(merged) == 1
    titles = {r["title"] for r in merged[0]["releases"]}
    assert titles == {"Some Compilation", "The Actual Album"}


def test_merge_drops_recordings_below_the_score_floor():
    metadata = _import_metadata_or_skip()
    low = [{"id": "rec-low", "score": 40, "title": "Song", "releases": [{"id": "r"}]}]
    assert metadata._mb_merge_recordings(low) == []


def test_recording_with_no_releases_never_wins():
    metadata = _import_metadata_or_skip()
    barren = {"title": "Song", "releases": []}
    assert metadata._score_recording_canonicity(barren, "Someone") < -1000


def test_alt_take_marker_is_penalised():
    metadata = _import_metadata_or_skip()
    releases = [{"id": "r", "title": "An Album",
                 "release-group": {"primary-type": "Album", "title": "An Album"}}]
    studio = {"title": "Song", "releases": releases}
    live = {"title": "Song (Live at Wembley)", "releases": releases}
    assert (metadata._score_recording_canonicity(studio, "Someone", "Song")
            > metadata._score_recording_canonicity(live, "Someone", "Song"))


def test_transliteration_is_not_treated_as_a_variant():
    """A completely different rendering is how a romanised query legitimately
    lands on native script; only bolted-on qualifiers count as variants."""
    metadata = _import_metadata_or_skip()
    releases = [{"id": "r", "title": "An Album",
                 "release-group": {"primary-type": "Album", "title": "An Album"}}]
    native = {"title": "夜に駆ける", "releases": releases}
    qualified = {"title": "Yoru ni Kakeru (TV size)", "releases": releases}
    assert (metadata._score_recording_canonicity(native, "YOASOBI", "Yoru ni Kakeru")
            > metadata._score_recording_canonicity(qualified, "YOASOBI", "Yoru ni Kakeru"))


def test_search_asks_for_a_shortlist_not_one_result():
    """The original bug in one assertion: limit=1 handed us whichever recording
    MusicBrainz felt like listing first, which for popular songs is a raffle."""
    from constants import MB_RECORDING_SEARCH_LIMIT
    assert MB_RECORDING_SEARCH_LIMIT >= 10


def test_weak_recording_result_uses_release_group_fallback(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    weak = {
        "id": "live-rec", "score": 100, "title": "Karma Police",
        "artist-credit": [{"name": "Radiohead"}],
        "releases": [{
            "id": "bootleg", "title": "A Bootleg",
            "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
        }],
    }
    canonical = {
        "id": "studio-rec", "title": "Karma Police", "length": 264000,
        "artist-credit": [{"name": "Radiohead"}],
        "releases": [{
            "id": "ok-computer", "title": "OK Computer", "date": "1997-06-16",
            "artist-credit": [{"name": "Radiohead"}],
            "release-group": {"primary-type": "Album", "title": "OK Computer"},
            "media": [{"track": [{"number": "6"}], "track-count": 12}],
        }],
    }
    monkeypatch.setattr(metadata, "_mb_search_recordings", lambda *a, **k: [weak])
    fallback_calls = []

    def fallback(*args):
        fallback_calls.append(args[:2])
        return canonical

    monkeypatch.setattr(metadata, "_mb_resolve_recording_via_release_group", fallback)

    result = metadata.lookup_musicbrainz("Radiohead", "Karma Police")
    assert fallback_calls == [("Radiohead", "Karma Police")]
    assert result["album"] == "OK Computer"
    assert result["release_mbid"] == "ok-computer"
    assert result["track_number"] == 6


def test_well_supported_recording_skips_release_group_fallback(monkeypatch):
    metadata = _import_metadata_or_skip()
    from constants import MB_RELEASE_GROUP_FALLBACK_MAX_RELEASES

    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    releases = [
        {
            "id": f"release-{index}", "title": "The Album",
            "release-group": {"primary-type": "Album", "title": "The Album"},
        }
        for index in range(MB_RELEASE_GROUP_FALLBACK_MAX_RELEASES + 1)
    ]
    recording = {
        "id": "studio-rec", "score": 100, "title": "Song",
        "artist-credit": [{"name": "Artist"}], "releases": releases,
    }
    monkeypatch.setattr(metadata, "_mb_search_recordings", lambda *a, **k: [recording])

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("strong recording should not spend fallback requests")

    monkeypatch.setattr(
        metadata, "_mb_resolve_recording_via_release_group", unexpected_fallback,
    )
    assert metadata.lookup_musicbrainz("Artist", "Song")["album"] == "The Album"


class _NoNetworkReleaseResponse:
    """A 404-alike so _build_musicbrainz_guess_for_release's optional release-detail
    fetch backs off cleanly, instead of a real request leaving the network to decide
    how offline this "offline" test file actually is."""
    status_code = 404

    def json(self):
        return {}


class _NoNetworkHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return _NoNetworkReleaseResponse()


def test_guess_candidates_prefer_canonicity_over_a_rehearsal_bootleg(monkeypatch):
    """Same failure mode as test_weak_recording_result_uses_release_group_fallback,
    but through guess_musicbrainz_tag_candidates() ("Guess Again" in the tag
    editor), which used to sort by raw MusicBrainz relevance and had no alt-take
    penalty at all. A real example: "Let It Be" text-searched a rehearsal take
    catalogued on the "Artifacts II" bootleg box set ahead of the actual single,
    because MusicBrainz scored the rehearsal's title a confident text match."""
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(metadata.httpx, "Client", _NoNetworkHttpxClient)

    rehearsal = {
        "id": "rehearsal-rec", "score": 100, "title": "Let It Be (Let It Be rehearsals)",
        "artist-credit": [{"name": "The Beatles"}],
        "releases": [{
            "id": "artifacts-ii", "title": "Artifacts II", "date": "1994",
            "artist-credit": [{"name": "The Beatles"}],
            "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"],
                               "title": "Artifacts II"},
        }],
    }
    studio = {
        "id": "studio-rec", "title": "Let It Be", "length": 243000,
        "artist-credit": [{"name": "The Beatles"}],
        "releases": [{
            "id": "let-it-be-album", "title": "Let It Be", "date": "1970-05-08",
            "artist-credit": [{"name": "The Beatles"}],
            "release-group": {"primary-type": "Album", "title": "Let It Be"},
            "media": [{"track": [{"number": "11"}], "track-count": 12}],
        }],
    }
    monkeypatch.setattr(metadata, "_mb_search_recordings", lambda *a, **k: [rehearsal])
    monkeypatch.setattr(
        metadata, "_mb_resolve_recording_via_release_group",
        lambda *a, **k: studio,
    )

    candidates = metadata.guess_musicbrainz_tag_candidates("The Beatles", "Let It Be")
    assert candidates, "expected at least one candidate"
    assert candidates[0]["album"] == "Let It Be"
    assert candidates[0]["metadata_source"] == "musicbrainz_text"
    assert not any(c["album"] == "Artifacts II" for c in candidates), (
        "the rehearsal bootleg should have been superseded, not merely outranked")


def test_guess_candidates_use_fingerprint_tier_when_file_is_available(monkeypatch, tmp_path):
    """When the actual file can be fingerprinted, that audio-verified recording's
    releases should come first, ahead of anything the text search turns up."""
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(metadata.httpx, "Client", _NoNetworkHttpxClient)

    audio_file = tmp_path / "track.flac"
    audio_file.write_bytes(b"not really audio, just needs to exist")

    monkeypatch.setattr(metadata, "_run_fpcalc", lambda path: (243, "fake-fingerprint"))
    monkeypatch.setattr(
        metadata, "_lookup_acoustid",
        lambda duration, fingerprint, artist, title: {
            "recording_id": "studio-rec", "artist": artist, "title": title,
        },
    )
    monkeypatch.setattr(
        metadata, "_mb_fetch_recording_with_releases",
        lambda recording_id, headers: {
            "id": "studio-rec", "title": "Let It Be",
            "artist-credit": [{"name": "The Beatles"}],
            "releases": [{
                "id": "let-it-be-album", "title": "Let It Be", "date": "1970-05-08",
                "artist-credit": [{"name": "The Beatles"}],
                "release-group": {"primary-type": "Album", "title": "Let It Be"},
                "media": [{"track": [{"number": "11"}], "track-count": 12}],
            }],
        },
    )
    # Text-search tier should not even be needed here, but stub it so a bug that
    # accidentally calls it does not turn into a real network request.
    monkeypatch.setattr(metadata, "_mb_search_recordings", lambda *a, **k: [])

    candidates = metadata.guess_musicbrainz_tag_candidates(
        "The Beatles", "Let It Be", file_path=audio_file,
    )
    assert candidates, "expected at least one candidate"
    assert candidates[0]["metadata_source"] == "acoustid_fingerprint"
    assert candidates[0]["album"] == "Let It Be"


def test_guess_candidates_fall_back_to_text_search_without_a_file(monkeypatch):
    """No file (or fingerprinting finds nothing) should still work exactly as
    guess_musicbrainz_tag_candidates always did: text search only."""
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(metadata.httpx, "Client", _NoNetworkHttpxClient)

    recording = {
        "id": "studio-rec", "score": 100, "title": "Song",
        "artist-credit": [{"name": "Artist"}],
        "releases": [{
            "id": "release-1", "title": "The Album",
            "release-group": {"primary-type": "Album", "title": "The Album"},
        }],
    }
    monkeypatch.setattr(metadata, "_mb_search_recordings", lambda *a, **k: [recording])
    monkeypatch.setattr(
        metadata, "_mb_resolve_recording_via_release_group", lambda *a, **k: None,
    )

    candidates = metadata.guess_musicbrainz_tag_candidates("Artist", "Song")
    assert candidates
    assert candidates[0]["metadata_source"] == "musicbrainz_text"
    assert candidates[0]["album"] == "The Album"


def test_release_group_fallback_uses_the_recording_repeated_across_editions(monkeypatch):
    metadata = _import_metadata_or_skip()
    calls = []
    responses = iter([
        {
            "release-groups": [{
                "id": "single-group", "score": 100,
                "title": "Karma Police", "primary-type": "Single",
            }],
        },
        {
            "releases": [
                {
                    "id": "single-a", "media": [{"tracks": [{
                        "recording": {"id": "studio", "title": "Karma Police", "length": 264000},
                    }]}],
                },
                {
                    "id": "single-b", "media": [{"tracks": [{
                        "recording": {"id": "studio", "title": "Karma Police", "length": 264000},
                    }]}],
                },
                {
                    "id": "single-c", "media": [{"tracks": [{
                        "recording": {"id": "alternate", "title": "Karma Police", "length": 250000},
                    }]}],
                },
            ],
        },
        {
            "releases": [{
                "id": "album-release", "title": "OK Computer", "date": "1997",
                "release-group": {"title": "OK Computer", "primary-type": "Album"},
                "media": [{"track-count": 12, "tracks": [{
                    "number": "6", "recording": {"id": "studio"},
                }]}],
            }],
        },
    ])

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_get(url, *, params, headers, timeout):
        calls.append((url, params))
        return Response(next(responses))

    monkeypatch.setattr(metadata, "_mb_get_with_retry", fake_get)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    recording = metadata._mb_resolve_recording_via_release_group(
        "Radiohead", "Karma Police", {},
    )
    assert recording["id"] == "studio"
    assert recording["releases"][0]["title"] == "OK Computer"
    assert metadata._extract_track_position(recording["releases"][0]) == (6, 12)
    assert calls[0][0].endswith("/release-group/")
    assert calls[1][1]["release-group"] == "single-group"
    assert calls[2][1]["recording"] == "studio"
    assert "recordings" in calls[2][1]["inc"].split()
