"""
Unit tests for MusicBrainz retry / unavailable handling on the Albums tab.
These are pure unit tests; httpx is monkeypatched so no network calls happen.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_metadata_or_skip():
    try:
        import metadata
    except ModuleNotFoundError as exc:
        pytest.skip(f"metadata module dependencies unavailable: {exc.name}")
    return metadata


class _FakeResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json


class _FakeClient:
    """Drop-in replacement for httpx.Client. Each call returns the next item
    from the shared `responses` list -- can be a _FakeResp to return, or an
    Exception to raise. Shared list because the retry helper opens a fresh
    Client per attempt; if each had its own list, retries would never advance."""

    def __init__(self, responses):
        self._responses = responses  # NB: shared reference, not copy

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, headers=None):
        if not self._responses:
            raise RuntimeError("Test ran out of canned responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_httpx(monkeypatch, metadata, responses, sleep_calls=None):
    """Patch metadata.httpx.Client to return our fake. The retry helper opens
    a fresh Client per attempt, so all instances must share one response queue
    or each retry would see the same first response.

    Optionally capture sleep calls so we can assert backoff was applied without
    actually waiting."""
    shared = list(responses)
    monkeypatch.setattr(metadata.httpx, "Client", lambda *a, **kw: _FakeClient(shared))
    # The retry helper does a local `import time as _time`; stub its sleep so
    # tests do not actually wait 1+3 seconds.
    import time
    if sleep_calls is None:
        sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


def test_retry_helper_returns_first_success(monkeypatch):
    metadata = _import_metadata_or_skip()
    sleeps = _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {"artists": []})])

    resp = metadata._mb_get_with_retry(
        "https://example/", params={}, headers={}, timeout=1,
    )
    assert resp.status_code == 200
    assert sleeps == []  # No retries needed, no backoff


def test_retry_helper_retries_on_503_then_succeeds(monkeypatch):
    metadata = _import_metadata_or_skip()
    sleeps = _patch_httpx(monkeypatch, metadata, [
        _FakeResp(503), _FakeResp(200, {"ok": True}),
    ])

    resp = metadata._mb_get_with_retry(
        "https://example/", params={}, headers={}, timeout=1,
    )
    assert resp.status_code == 200
    assert sleeps == [1]  # One backoff between two attempts


def test_retry_helper_retries_on_timeout_then_succeeds(monkeypatch):
    metadata = _import_metadata_or_skip()
    import httpx
    sleeps = _patch_httpx(monkeypatch, metadata, [
        httpx.TimeoutException("slow"),
        _FakeResp(200, {"ok": True}),
    ])

    resp = metadata._mb_get_with_retry(
        "https://example/", params={}, headers={}, timeout=1,
    )
    assert resp.status_code == 200
    assert sleeps == [1]


def test_retry_helper_raises_unavailable_after_persistent_failures(monkeypatch):
    metadata = _import_metadata_or_skip()
    sleeps = _patch_httpx(monkeypatch, metadata, [
        _FakeResp(503), _FakeResp(502), _FakeResp(504),
    ])

    with pytest.raises(metadata.MusicBrainzUnavailable):
        metadata._mb_get_with_retry(
            "https://example/", params={}, headers={}, timeout=1,
        )
    # Two backoffs (after attempts 1 and 2)
    assert sleeps == [1, 3]


def test_retry_helper_does_not_swallow_unexpected_exceptions(monkeypatch):
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [ValueError("boom")])

    with pytest.raises(ValueError):
        metadata._mb_get_with_retry(
            "https://example/", params={}, headers={}, timeout=1,
        )


def test_search_artist_propagates_unavailable(monkeypatch):
    """The Albums tab endpoint relies on this propagation to send back HTTP 503."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(503), _FakeResp(503), _FakeResp(503)])

    with pytest.raises(metadata.MusicBrainzUnavailable):
        metadata.search_artist_mbid("Bowie")


def test_search_artist_does_not_let_capitalisation_bury_the_real_artist(monkeypatch):
    """Real MusicBrainz answer for "Raye": three obscure acts spelled exactly
    "Raye" scoring 85, and RAYE the English singer scoring 100 in capitals.

    Ordering exact capitalisation above relevance buried her in fourth place,
    behind an unnamed feature credit on a Dead Prez record. Name matches still
    come first as a group; within that group the score leads.
    """
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {"artists": [
        {"id": "m1", "name": "Raye", "disambiguation": "unknown feat. artist", "score": 85},
        {"id": "m2", "name": "Raye", "disambiguation": "Spanish DJ, producer", "score": 85},
        {"id": "m3", "name": "Raye", "disambiguation": "Amanda Cygnaeus", "score": 85},
        {"id": "m4", "name": "RAYE", "disambiguation": "English singer", "score": 100},
        {"id": "m5", "name": "Collin Raye", "disambiguation": "", "score": 88},
    ]})])

    results = metadata.search_artist_mbid("Raye")

    assert results[0]["mbid"] == "m4", "the highest-scoring name match should lead"
    # Collin Raye scores higher than the 85s but is not called "Raye", so he
    # stays behind the artists who actually own the name.
    assert [r["mbid"] for r in results][-1] == "m5"


def test_search_artist_keeps_exact_capitalisation_as_the_tiebreak(monkeypatch):
    """When scores tie, capitalisation still decides. "SiR" is not "Sir"."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {"artists": [
        {"id": "m1", "name": "Sir", "disambiguation": "someone else", "score": 100},
        {"id": "m2", "name": "SiR", "disambiguation": "American R&B singer", "score": 100},
    ]})])

    results = metadata.search_artist_mbid("SiR")

    assert results[0]["mbid"] == "m2"


def test_search_artist_returns_empty_list_on_4xx(monkeypatch):
    """4xx is a definitive 'no data', not a network problem."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(400, {})])

    assert metadata.search_artist_mbid("???") == []


def test_fetch_album_tracks_propagates_unavailable(monkeypatch):
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(503), _FakeResp(503), _FakeResp(503)])

    with pytest.raises(metadata.MusicBrainzUnavailable):
        metadata.fetch_album_tracks("fake-mbid")


# ---------------------------------------------------------------------------
# Album release year
#
# The release date rides along in the tracklist response we already fetch, so
# grabbing it costs nothing. It is the date of *this* pressing, so a remaster
# honestly says it is a remaster rather than pretending to be the original.
# ---------------------------------------------------------------------------

def _release_body(date):
    body = {
        "artist-credit": [{"name": "Pink Floyd"}],
        "media": [{"tracks": [
            {"position": 1, "title": "Speak to Me",
             "recording": {"id": "rec-1", "title": "Speak to Me", "isrcs": ["GBAYE7300001"]}},
            {"position": 2, "title": "Breathe",
             "recording": {"id": "rec-2", "title": "Breathe", "isrcs": []}},
        ]}],
    }
    if date is not None:
        body["date"] = date
    return body


@pytest.mark.parametrize("mb_date", ["1973", "1973-03", "1973-03-01"])
def test_fetch_album_release_takes_the_year_off_any_date_shape(monkeypatch, mb_date):
    """MusicBrainz is vague to varying degrees; we only ever want the front four."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, _release_body(mb_date))])

    release = metadata.fetch_album_release("rel-mbid")

    assert release["year"] == "1973"
    assert [t["title"] for t in release["tracks"]] == ["Speak to Me", "Breathe"]


@pytest.mark.parametrize("mb_date", [None, "", "unknown"])
def test_fetch_album_release_year_is_empty_when_there_is_no_usable_date(monkeypatch, mb_date):
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, _release_body(mb_date))])

    assert metadata.fetch_album_release("rel-mbid")["year"] == ""


def test_fetch_album_release_survives_a_4xx_without_pretending_it_worked(monkeypatch):
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(404, {})])

    assert metadata.fetch_album_release("rel-mbid") == {"year": "", "tracks": []}


def test_fetch_album_tracks_still_hands_back_a_plain_list(monkeypatch):
    """Back-compat guard: four callers in app.py want the list and nothing else."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, _release_body("1973-03-01"))])

    tracks = metadata.fetch_album_tracks("rel-mbid")

    assert isinstance(tracks, list)
    assert tracks[0]["isrc"] == "GBAYE7300001"
    assert tracks[1]["isrc"] is None


# ---------------------------------------------------------------------------
# Track numbers on singles (GitLab #43)
#
# MusicBrainz returns the matched track under `track` (singular) from the search
# API and `tracks` (plural) from the lookup API. We read the wrong one for
# years, so singles arrived with no track number at all.
# ---------------------------------------------------------------------------


def test_extract_track_position_reads_search_api_singular_key():
    """Search API shape: media[].track[] (singular), number as a string."""
    metadata = _import_metadata_or_skip()
    release = {
        "media": [{
            "position": 2,
            "track": [{"id": "abc", "number": "7", "title": "Praise You"}],
            "track-count": 19,
            "track-offset": 6,
        }]
    }
    assert metadata._extract_track_position(release) == (7, 19)


def test_extract_track_position_reads_lookup_api_plural_key():
    """Lookup API shape: media[].tracks[] (plural), one matched entry."""
    metadata = _import_metadata_or_skip()
    release = {
        "media": [{
            "position": 1,
            "tracks": [{"position": 3, "number": "3", "title": "Rockafeller Skank"}],
            "track-count": 12,
            "track-offset": 2,
        }]
    }
    assert metadata._extract_track_position(release) == (3, 12)


def test_extract_track_position_falls_back_to_offset_for_vinyl():
    """Vinyl prints 'A1' rather than '1'; track-offset keeps us honest."""
    metadata = _import_metadata_or_skip()
    release = {
        "media": [{
            "track": [{"number": "B2", "title": "Side two, second one"}],
            "track-count": 10,
            "track-offset": 6,
        }]
    }
    assert metadata._extract_track_position(release) == (7, 10)


def test_extract_track_position_skips_empty_media():
    """An empty first medium must not stop us finding the populated one."""
    metadata = _import_metadata_or_skip()
    release = {
        "media": [
            {"track": [], "track-count": 0},
            {"track": [{"number": "4"}], "track-count": 11, "track-offset": 3},
        ]
    }
    assert metadata._extract_track_position(release) == (4, 11)


def test_extract_track_position_returns_none_when_hopeless():
    metadata = _import_metadata_or_skip()
    assert metadata._extract_track_position({}) == (None, None)
    assert metadata._extract_track_position({"media": []}) == (None, None)
    assert metadata._extract_track_position(
        {"media": [{"track": [{"number": "A"}], "track-count": 5}]}
    ) == (None, None)


def test_lookup_musicbrainz_tags_track_number_from_search_response(monkeypatch):
    """The real regression: a plain text search must yield a track number."""
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    monkeypatch.setattr(
        metadata, "_mb_resolve_recording_via_release_group",
        lambda *args, **kwargs: None,
    )
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "recordings": [{
            "id": "rec-1",
            "score": 100,
            "title": "Praise You",
            "length": 323000,
            "artist-credit": [{"name": "Fatboy Slim"}],
            "releases": [{
                "id": "rel-1",
                "title": "You've Come a Long Way, Baby",
                "date": "1998-10-19",
                "release-group": {"primary-type": "Album"},
                "media": [{
                    "position": 1,
                    "track": [{"id": "t-1", "number": "6", "title": "Praise You"}],
                    "track-count": 11,
                    "track-offset": 5,
                }],
            }],
        }]
    })])

    result = metadata.lookup_musicbrainz("Fatboy Slim", "Praise You")
    assert result["track_number"] == 6
    assert result["track_total"] == 11


def test_lookup_musicbrainz_by_id_requests_media_inc(monkeypatch):
    """Without inc=media the release has no media block, so no track number."""
    metadata = _import_metadata_or_skip()
    seen = {}

    class _RecordingClient(_FakeClient):
        def get(self, url, params=None, headers=None):
            seen["inc"] = (params or {}).get("inc", "")
            return super().get(url, params=params, headers=headers)

    monkeypatch.setattr(
        metadata.httpx, "Client",
        lambda *a, **kw: _RecordingClient([_FakeResp(200, {
            "length": 323000,
            "releases": [{
                "id": "rel-1",
                "title": "You've Come a Long Way, Baby",
                "date": "1998",
                "release-group": {"primary-type": "Album"},
                "media": [{
                    "tracks": [{"position": 6, "number": "6"}],
                    "track-count": 11,
                    "track-offset": 5,
                }],
            }],
        })]),
    )

    result = metadata._lookup_musicbrainz_by_id("rec-1", expected_artist="Fatboy Slim")
    assert "media" in seen["inc"].split()
    assert result["track_number"] == 6
    assert result["track_total"] == 11


# ---------------------------------------------------------------------------
# MusicBrainz release URLs -> album download fields
# ---------------------------------------------------------------------------


def test_parse_musicbrainz_release_url_accepts_the_real_shapes():
    metadata = _import_metadata_or_skip()
    mbid = "435fe38e-404c-3887-8300-cc94420a121c"
    cases = {
        f"https://musicbrainz.org/release/{mbid}": ("release", mbid),
        f"https://musicbrainz.org/release-group/{mbid}": ("release-group", mbid),
        f"http://musicbrainz.org/release/{mbid}": ("release", mbid),
        f"https://beta.musicbrainz.org/release-group/{mbid}/disc/1": ("release-group", mbid),
        f"https://MUSICBRAINZ.org/RELEASE/{mbid.upper()}": ("release", mbid),
    }
    for url, expected in cases.items():
        assert metadata.parse_musicbrainz_release_url(url) == expected, url


def test_parse_musicbrainz_release_url_rejects_everything_else():
    metadata = _import_metadata_or_skip()
    mbid = "435fe38e-404c-3887-8300-cc94420a121c"
    for url in (
        f"https://musicbrainz.org/artist/{mbid}",
        f"https://musicbrainz.org/recording/{mbid}",
        "https://musicbrainz.org/release/not-a-uuid",
        "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy",
        "musicbrainz.org/release/" + mbid,  # No scheme, no deal
        "not a url at all",
        "",
        None,
    ):
        assert metadata.parse_musicbrainz_release_url(url) is None, url


def test_pick_release_from_group_prefers_official_and_earliest():
    metadata = _import_metadata_or_skip()
    chosen = metadata._pick_release_from_group([
        {"id": "bootleg", "status": "Bootleg", "date": "1997-01-01"},
        {"id": "reissue", "status": "Official", "date": "2008-05-05"},
        {"id": "original", "status": "Official", "date": "1998-10-12"},
    ])
    assert chosen["id"] == "original"


def test_pick_release_from_group_prefers_complete_multi_disc_official_release():
    metadata = _import_metadata_or_skip()
    chosen = metadata._pick_release_from_group([
        {
            "id": "fragment", "status": "Official", "date": "1996-01-01",
            "media": [{"track-count": 10}],
        },
        {
            "id": "complete", "status": "Official", "date": "1997-01-01",
            "media": [{"track-count": 10}, {"track-count": 11}],
        },
        {
            "id": "bootleg", "status": "Bootleg", "date": "1995-01-01",
            "media": [{"track-count": 20}, {"track-count": 20}],
        },
    ])
    assert chosen["id"] == "complete"


def test_pick_release_from_group_does_not_let_vague_dates_win():
    """A bare '1998' string-sorts before '1998-10-12'; it must not win on that."""
    metadata = _import_metadata_or_skip()
    chosen = metadata._pick_release_from_group([
        {"id": "vague", "status": "Official", "date": "1998"},
        {"id": "precise", "status": "Official", "date": "1998-10-12"},
    ])
    assert chosen["id"] == "precise"


def test_pick_release_from_group_handles_missing_dates_and_empties():
    metadata = _import_metadata_or_skip()
    assert metadata._pick_release_from_group([]) is None
    assert metadata._pick_release_from_group([{"no": "id"}]) is None
    chosen = metadata._pick_release_from_group([
        {"id": "dateless", "status": "Official"},
        {"id": "dated", "status": "Official", "date": "2001-01-01"},
    ])
    assert chosen["id"] == "dated"


def test_fetch_release_summary_returns_album_download_fields(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "id": "rel-1",
        "title": "You've Come a Long Way, Baby",
        "date": "1998-10-12",
        "artist-credit": [{"name": "Fatboy Slim"}],
        "media": [{"track-count": 11}],
    })])

    summary = metadata.fetch_release_summary("release", "rel-1")
    assert summary == {
        "artist": "Fatboy Slim",
        "album_title": "You've Come a Long Way, Baby",
        "release_mbid": "rel-1",
        "year": "1998",
        "track_count": 11,
    }


def test_fetch_release_summary_sums_multi_disc_track_counts(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "id": "rel-2",
        "title": "A Double Album",
        "date": "2001",
        "artist-credit": [{"name": "Someone"}],
        "media": [{"track-count": 12}, {"track-count": 9}],
    })])

    assert metadata.fetch_release_summary("release", "rel-2")["track_count"] == 21


def test_fetch_release_summary_resolves_a_release_group(monkeypatch):
    """Group URLs are what MusicBrainz search actually hands you."""
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {  # release-group lookup
            "id": "rg-1",
            "title": "You've Come a Long Way, Baby",
            "releases": [
                {"id": "rel-late", "status": "Official", "date": "2008-01-01"},
                {"id": "rel-first", "status": "Official", "date": "1998-10-12"},
            ],
        }),
        _FakeResp(200, {"releases": [
            {"id": "rel-late", "status": "Official", "date": "2008-01-01"},
            {"id": "rel-first", "status": "Official", "date": "1998-10-12"},
        ]}),
        _FakeResp(200, {  # then the chosen release
            "id": "rel-first",
            "title": "You've Come a Long Way, Baby",
            "date": "1998-10-12",
            "artist-credit": [{"name": "Fatboy Slim"}],
            "media": [{"track-count": 11}],
        }),
    ])

    summary = metadata.fetch_release_summary("release-group", "rg-1")
    assert summary["release_mbid"] == "rel-first"
    assert summary["artist"] == "Fatboy Slim"


def test_release_group_keeps_various_artists_and_complete_tracklist(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {
            "id": "rg-va",
            "title": "A Proper Compilation",
            "artist-credit": [{"name": "Various Artists"}],
            "releases": [{"id": "disc-one", "status": "Official", "date": "1996"}],
        }),
        _FakeResp(200, {"releases": [
            {
                "id": "disc-one", "status": "Official", "date": "1996",
                "media": [{"track-count": 10}],
            },
            {
                "id": "complete", "status": "Official", "date": "1997",
                "media": [{"track-count": 10}, {"track-count": 11}],
            },
        ]}),
        _FakeResp(200, {
            "id": "complete",
            "title": "A Proper Compilation (2 CD)",
            "date": "1997",
            "artist-credit": [{"name": "Featured Performer"}],
            "media": [{"track-count": 10}, {"track-count": 11}],
        }),
    ])

    summary = metadata.fetch_release_summary("release-group", "rg-va")

    assert summary == {
        "artist": "Various Artists",
        "album_title": "A Proper Compilation",
        "release_mbid": "complete",
        "year": "1997",
        "track_count": 21,
    }


def test_legitimate_disc_word_in_group_title_is_not_stripped(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {
            "id": "rel-discotheque",
            "title": "Discothèque",
            "date": "2020",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"id": "rg-discotheque"},
            "media": [{"track-count": 9}],
        }),
        _FakeResp(200, {
            "id": "rg-discotheque",
            "title": "Discothèque",
            "artist-credit": [{"name": "Artist"}],
        }),
        _FakeResp(200, {"releases": [{
            "id": "rel-discotheque", "status": "Official", "date": "2020",
            "media": [{"track-count": 9}],
        }]}),
    ])

    summary = metadata.fetch_release_summary("release", "rel-discotheque")

    assert summary["album_title"] == "Discothèque"


def test_fetch_release_summary_joins_multiple_credited_artists(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "id": "rel-3",
        "title": "A Collaboration",
        "date": "2015-03-01",
        "artist-credit": [
            {"name": "Underworld", "joinphrase": " & "},
            {"name": "Iggy Pop"},
        ],
        "media": [{"track-count": 8}],
    })])

    assert metadata.fetch_release_summary("release", "rel-3")["artist"] == "Underworld & Iggy Pop"


def test_text_guess_joins_multiple_credited_artists_without_double_space(monkeypatch):
    """Recording credits get the same joinphrase treatment as release credits.

    MusicBrainz puts the separator inside each credit's joinphrase, so joining
    on a space too used to give "Underworld &  Iggy Pop". No release here, so
    the function returns before it would go anywhere near the network.
    """
    metadata = _import_metadata_or_skip()
    recording = {
        "id": "rec-1",
        "title": "Born Slippy",
        "artist-credit": [
            {"name": "Underworld", "joinphrase": " & "},
            {"name": "Iggy Pop"},
        ],
    }

    guess = metadata._build_musicbrainz_guess_for_release(
        recording, None, "Underworld", "Born Slippy", {},
    )
    assert guess["artist"] == "Underworld & Iggy Pop"
    assert guess["album_artist"] == "Underworld & Iggy Pop"


def test_text_guess_joins_release_credits_without_double_space(monkeypatch):
    """Same again for the release's own credit list. The release lookup is
    given a 404 so we stop right after the join we care about."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(404, {})])
    recording = {"id": "rec-2", "title": "Go", "artist-credit": [{"name": "The Chemical Brothers"}]}
    release = {
        "id": "rel-9",
        "title": "Born in the Echoes",
        "date": "2015-07-17",
        "artist-credit": [
            {"name": "The Chemical Brothers", "joinphrase": " feat. "},
            {"name": "Q-Tip"},
        ],
    }

    guess = metadata._build_musicbrainz_guess_for_release(
        recording, release, "The Chemical Brothers", "Go", {},
    )
    assert guess["album_artist"] == "The Chemical Brothers feat. Q-Tip"
    assert guess["album"] == "Born in the Echoes"
    assert guess["year"] == "2015"


def test_fetch_release_summary_returns_none_for_unknown_release(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(404, {})])
    assert metadata.fetch_release_summary("release", "nope") is None


def test_fetch_release_summary_returns_none_without_artist_or_title(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {"id": "rel-4", "title": ""})])
    assert metadata.fetch_release_summary("release", "rel-4") is None


def test_fetch_release_summary_respects_the_musicbrainz_kill_switch(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: False)
    assert metadata.fetch_release_summary("release", "rel-1") is None


def test_fetch_release_summary_propagates_unavailable(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(503), _FakeResp(503), _FakeResp(503)])
    with pytest.raises(metadata.MusicBrainzUnavailable):
        metadata.fetch_release_summary("release", "rel-1")


# ---------------------------------------------------------------------------
# Compilations must not decide what album a track belongs to.
#
# Real case, 2026-08-09: a Paramore "Ignorance" download was fingerprinted to a
# recording whose only MusicBrainz release was "Promo Only: Mainstream Radio,
# January 2010". The scorer rated it -20 and picked it anyway, because max() over
# one candidate returns that candidate however much it is disliked.
# ---------------------------------------------------------------------------

def test_compilation_release_group_is_refused_as_album_context():
    metadata = _import_metadata_or_skip()
    rg = {"primary-type": "Album", "secondary-types": ["Compilation"],
          "title": "Promo Only: Mainstream Radio, January 2010"}
    assert metadata._release_group_is_poor_album_context(rg) is True


def test_various_artists_release_group_is_refused_as_album_context():
    metadata = _import_metadata_or_skip()
    rg = {"primary-type": "Album", "artist-credit": [{"name": "Various Artists"}]}
    assert metadata._release_group_is_poor_album_context(rg) is True


@pytest.mark.parametrize("secondary", ["Live", "Soundtrack", "Remix", "DJ-mix", "Demo"])
def test_other_non_canonical_secondary_types_are_refused(secondary):
    metadata = _import_metadata_or_skip()
    assert metadata._release_group_is_poor_album_context(
        {"primary-type": "Album", "secondary-types": [secondary]}) is True


def test_a_plain_studio_album_is_perfectly_good_album_context():
    metadata = _import_metadata_or_skip()
    rg = {"primary-type": "Album", "secondary-types": [], "title": "Brand New Eyes",
          "artist-credit": [{"name": "Paramore"}]}
    assert metadata._release_group_is_poor_album_context(rg) is False


def test_by_id_lookup_drops_album_context_when_only_a_compilation_is_known(monkeypatch):
    """Artist and title survive; album, year, track position and cover art do not."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "length": 205000,
        "releases": [{
            "id": "rel-promo",
            "title": "Promo Only: Mainstream Radio, January 2010",
            "date": "2009-12-15",
            "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"],
                              "title": "Promo Only: Mainstream Radio, January 2010"},
            "artist-credit": [{"name": "Various Artists"}],
            "media": [{"position": 1, "tracks": [{"number": "8", "title": "Ignorance"}],
                       "track-count": 20}],
        }],
    })])

    result = metadata._lookup_musicbrainz_by_id("rec-1", "Paramore") or {}

    assert "album" not in result, "a radio promo compilation is not this track's album"
    assert "track_number" not in result, "and it certainly does not set the track number"
    assert "track_total" not in result
    assert "release_mbid" not in result, "cover art would come from the compilation too"
    # The genuinely useful part of the lookup still comes back.
    assert result.get("expected_duration_secs") == 205.0


def test_by_id_lookup_still_uses_a_real_album_when_there_is_one(monkeypatch):
    """The guard must not throw away good album context along with the bad."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "length": 205000,
        "releases": [{
            "id": "rel-bne",
            "title": "brand new eyes",
            "date": "2009-09-29",
            "release-group": {"primary-type": "Album", "secondary-types": [],
                              "title": "brand new eyes"},
            "artist-credit": [{"name": "Paramore"}],
            "media": [{"position": 1, "tracks": [{"number": "2", "title": "Ignorance"}],
                       "track-count": 11}],
        }],
    })])

    result = metadata._lookup_musicbrainz_by_id("rec-1", "Paramore") or {}

    assert result.get("album") == "brand new eyes"
    assert result.get("track_number") == 2
    assert result.get("track_total") == 11
    assert result.get("release_mbid") == "rel-bne"


def test_isrc_lookup_prefers_the_studio_recording_over_a_bootleg(monkeypatch):
    """MusicBrainz sometimes attaches one ISRC to both a studio recording and one
    of its many bootleg live takes. Taking whichever the API lists first used to
    mean a Tidal-sourced "Creep" could come back tagged as a 1998 Tibetan Freedom
    Concert bootleg; the ISRC endpoint must score candidates like the title-search
    path already does, not trust list order."""
    metadata = _import_metadata_or_skip()

    bootleg_recording = {
        "id": "bootleg-id",
        "title": "Creep",
        "artist-credit": [{"name": "Radiohead"}],
        "releases": [{
            "id": "rel-bootleg",
            "title": "1998-06-14: Tibetan Freedom Concert, RFK, Washington DC, USA",
            "artist-credit": [{"name": "Radiohead"}],
            "release-group": {
                "title": "1998-06-14: Tibetan Freedom Concert, RFK, Washington DC, USA",
                "primary-type": "Album", "secondary-types": ["Live"],
                "artist-credit": [{"name": "Radiohead"}],
            },
        }],
    }
    studio_recording = {
        "id": "studio-id",
        "title": "Creep",
        "length": 238640,
        "artist-credit": [{"name": "Radiohead"}],
        "releases": [
            {
                "id": "rel-pablo-honey",
                "title": "Pablo Honey",
                "artist-credit": [{"name": "Radiohead"}],
                "release-group": {"title": "Pablo Honey", "primary-type": "Album",
                                  "secondary-types": [], "artist-credit": [{"name": "Radiohead"}]},
                "media": [{"position": 1, "tracks": [{"number": "2", "title": "Creep"}], "track-count": 12}],
            },
            {
                "id": "rel-single",
                "title": "Creep",
                "artist-credit": [{"name": "Radiohead"}],
                "release-group": {"title": "Creep", "primary-type": "Single",
                                  "secondary-types": [], "artist-credit": [{"name": "Radiohead"}]},
            },
        ],
    }

    _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {"recordings": [bootleg_recording, studio_recording]}),
        _FakeResp(200, {"length": 238640, "releases": studio_recording["releases"]}),
    ])

    result = metadata.lookup_musicbrainz_by_isrc(
        "GBAAA9300111", expected_artist="Radiohead", expected_title="Creep"
    )

    assert result is not None
    assert result["recording_id"] == "studio-id"
    assert result.get("album") == "Pablo Honey"


def test_acoustid_recording_album_skips_compilation_release_groups():
    metadata = _import_metadata_or_skip()
    recording = {
        "id": "rec-1", "title": "Ignorance",
        "artists": [{"name": "Paramore"}],
        "releasegroups": [
            {"title": "Promo Only: Mainstream Radio, January 2010",
             "type": "Album", "secondary-types": ["Compilation"]},
            {"title": "brand new eyes", "type": "Album", "secondary-types": [],
             "artist-credit": [{"name": "Paramore"}]},
        ],
    }

    assert metadata._extract_recording_metadata(recording, "Paramore")["album"] == "brand new eyes"


def test_acoustid_recording_album_is_blank_when_every_option_is_a_compilation():
    metadata = _import_metadata_or_skip()
    recording = {
        "id": "rec-1", "title": "Ignorance",
        "artists": [{"name": "Paramore"}],
        "releasegroups": [
            {"title": "Promo Only: Mainstream Radio, January 2010",
             "type": "Album", "secondary-types": ["Compilation"]},
        ],
    }

    assert metadata._extract_recording_metadata(recording, "Paramore")["album"] is None


# ---------------------------------------------------------------------------
# "2/14" in a Vorbis comment. Legal, common, and until 2026-08-09 it made
# MusicGrabber conclude the file had no track number at all, which is precisely
# when a MusicBrainz guess steps in and gets it wrong.
# ---------------------------------------------------------------------------

def _flac_with(tmp_path, name="t.flac", **tags):
    import subprocess
    from mutagen.flac import FLAC
    path = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", str(path)],
        check=True,
    )
    audio = FLAC(str(path))
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


def test_slash_form_track_number_is_read_from_a_flac(tmp_path):
    metadata = _import_metadata_or_skip()
    path = _flac_with(tmp_path, TRACKNUMBER="02/11")
    assert metadata.read_existing_track_number(path) == (2, 11)


def test_plain_track_number_and_separate_total_still_work(tmp_path):
    metadata = _import_metadata_or_skip()
    path = _flac_with(tmp_path, TRACKNUMBER="2", TRACKTOTAL="14")
    assert metadata.read_existing_track_number(path) == (2, 14)


def test_an_explicit_total_beats_the_one_baked_into_the_slash(tmp_path):
    metadata = _import_metadata_or_skip()
    path = _flac_with(tmp_path, TRACKNUMBER="2/11", TRACKTOTAL="14")
    assert metadata.read_existing_track_number(path) == (2, 14)


def test_an_unparseable_track_number_does_not_discard_a_good_total(tmp_path):
    """Vinyl writes "A1"; that is no reason to forget the album has 14 tracks."""
    metadata = _import_metadata_or_skip()
    path = _flac_with(tmp_path, TRACKNUMBER="A1", TRACKTOTAL="14")
    assert metadata.read_existing_track_number(path) == (None, 14)


def test_a_file_with_no_track_tags_reports_nothing(tmp_path):
    metadata = _import_metadata_or_skip()
    path = _flac_with(tmp_path, ARTIST="Paramore")
    assert metadata.read_existing_track_number(path) == (None, None)


def test_zero_and_negative_track_numbers_are_ignored(tmp_path):
    metadata = _import_metadata_or_skip()
    assert metadata.read_existing_track_number(_flac_with(tmp_path, "z.flac", TRACKNUMBER="0")) == (None, None)
    assert metadata.read_existing_track_number(_flac_with(tmp_path, "n.flac", TRACKNUMBER="-3")) == (None, None)


def test_split_track_field_handles_the_shapes_that_turn_up():
    metadata = _import_metadata_or_skip()
    assert metadata._split_track_field("02/11") == ("02", "11")
    assert metadata._split_track_field("7") == ("7", None)
    assert metadata._split_track_field(" 3 / 12 ") == ("3", "12")
    assert metadata._split_track_field(None) == (None, None)
    assert metadata._split_track_field("") == (None, None)
