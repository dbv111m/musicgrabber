"""
Unit tests for metadata.search_release_groups and match_album_to_musicbrainz.

Pure unit tests; httpx is monkeypatched so no network calls happen, and
time.sleep is stubbed so the MusicBrainz rate-limit courtesy pauses don't
actually slow the suite down.
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
    from the shared `responses` list -- a _FakeResp to return, or an
    Exception to raise. Shared list because _mb_get_with_retry opens a fresh
    Client per attempt/call."""

    def __init__(self, responses):
        self._responses = responses

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


def _patch_httpx(monkeypatch, metadata, responses):
    shared = list(responses)
    monkeypatch.setattr(metadata.httpx, "Client", lambda *a, **kw: _FakeClient(shared))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return shared


def _enable_mb(monkeypatch, metadata):
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# search_release_groups
# ---------------------------------------------------------------------------

def test_search_release_groups_without_artist_surfaces_various_artists_and_soundtracks(monkeypatch):
    """A user typing 'Trainspotting' with no artist should get sensible hits,
    including a Various Artists soundtrack -- this is a browse surface, not
    the artist-albums poller, so nothing should be filtered out here."""
    metadata = _import_metadata_or_skip()
    _enable_mb(monkeypatch, metadata)
    _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {"release-groups": [
            {
                "id": "rg-trainspotting",
                "title": "Trainspotting",
                "primary-type": "Album",
                "secondary-types": ["Soundtrack", "Compilation"],
                "first-release-date": "1996-07-08",
                "score": 100,
                "artist-credit": [{"name": "Various Artists"}],
            },
        ]}),
        _FakeResp(200, {  # per-candidate release resolution
            "id": "rg-trainspotting",
            "releases": [{"id": "rel-trainspotting", "status": "Official", "date": "1996-07-08"}],
        }),
    ])

    results = metadata.search_release_groups("Trainspotting")
    assert len(results) == 1
    hit = results[0]
    assert hit["title"] == "Trainspotting"
    assert hit["artist"] == "Various Artists"
    assert hit["year"] == "1996"
    assert hit["release_group_mbid"] == "rg-trainspotting"
    assert hit["release_mbid"] == "rel-trainspotting"
    assert hit["primary_type"] == "Album"
    assert set(hit["secondary_types"]) == {"Soundtrack", "Compilation"}
    assert hit["score"] == 100


def test_search_release_groups_narrows_by_artist_in_the_query(monkeypatch):
    metadata = _import_metadata_or_skip()
    _enable_mb(monkeypatch, metadata)
    captured_params = {}

    class _CapturingClient(_FakeClient):
        def get(self, url, params=None, headers=None):
            captured_params.update(params or {})
            return super().get(url, params=params, headers=headers)

    responses = [
        _FakeResp(200, {"release-groups": []}),
    ]
    monkeypatch.setattr(metadata.httpx, "Client", lambda *a, **kw: _CapturingClient(responses))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    metadata.search_release_groups("Discovery", artist="Daft Punk")
    assert 'releasegroup:"Discovery"' in captured_params["query"]
    assert 'artist:"Daft Punk"' in captured_params["query"]


def test_search_release_groups_returns_empty_for_blank_query(monkeypatch):
    metadata = _import_metadata_or_skip()
    _enable_mb(monkeypatch, metadata)
    assert metadata.search_release_groups("") == []
    assert metadata.search_release_groups("   ") == []


def test_search_release_groups_respects_the_musicbrainz_kill_switch(monkeypatch):
    metadata = _import_metadata_or_skip()
    monkeypatch.setattr(metadata, "get_setting_bool", lambda *a, **kw: False)
    assert metadata.search_release_groups("Anything") == []


def test_search_release_groups_raises_unavailable_when_mb_is_down(monkeypatch):
    metadata = _import_metadata_or_skip()
    _enable_mb(monkeypatch, metadata)
    _patch_httpx(monkeypatch, metadata, [_FakeResp(503), _FakeResp(502), _FakeResp(504)])
    with pytest.raises(metadata.MusicBrainzUnavailable):
        metadata.search_release_groups("Trainspotting")


def test_search_release_groups_survives_a_single_candidates_release_lookup_failing(monkeypatch):
    """One candidate's release resolution hitting a wobble shouldn't sink the
    whole search; it just comes back with release_mbid=None."""
    metadata = _import_metadata_or_skip()
    _enable_mb(monkeypatch, metadata)
    _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {"release-groups": [
            {"id": "rg-1", "title": "Some Album", "primary-type": "Album",
             "score": 90, "artist-credit": [{"name": "Some Artist"}]},
        ]}),
        _FakeResp(503), _FakeResp(502), _FakeResp(504),  # release resolution exhausts retries
    ])

    results = metadata.search_release_groups("Some Album")
    assert len(results) == 1
    assert results[0]["release_mbid"] is None


def test_search_release_groups_skips_release_resolution_when_asked(monkeypatch):
    """resolve_releases=False is the interactive-search mode: one request for
    the search and not a single one for the candidates, because nobody needs
    the release MBID of the nine albums they are not going to pick."""
    metadata = _import_metadata_or_skip()
    _enable_mb(monkeypatch, metadata)
    # Exactly one canned response. _FakeClient raises if anything asks for a
    # second, so an accidental per-candidate lookup fails this test loudly.
    remaining = _patch_httpx(monkeypatch, metadata, [
        _FakeResp(200, {"release-groups": [
            {"id": "rg-1", "title": "Trainspotting", "primary-type": "Album",
             "secondary-types": ["Compilation", "Soundtrack"], "score": 100,
             "artist-credit": [{"name": "Various Artists"}]},
            {"id": "rg-2", "title": "Trainspotting #2", "primary-type": "Album",
             "secondary-types": ["Soundtrack"], "score": 88,
             "artist-credit": [{"name": "Various Artists"}]},
        ]}),
    ])

    results = metadata.search_release_groups("Trainspotting", resolve_releases=False)
    assert len(results) == 2
    assert [r["release_mbid"] for r in results] == [None, None]
    assert [r["release_group_mbid"] for r in results] == ["rg-1", "rg-2"]
    assert not remaining, "resolve_releases=False should not spend extra requests"


# ---------------------------------------------------------------------------
# match_album_to_musicbrainz
# ---------------------------------------------------------------------------

def _stub_search_release_groups(monkeypatch, metadata, candidates):
    monkeypatch.setattr(metadata, "search_release_groups", lambda *a, **kw: candidates)


def test_match_album_to_musicbrainz_confident_exact_match(monkeypatch):
    metadata = _import_metadata_or_skip()
    _stub_search_release_groups(monkeypatch, metadata, [
        {"title": "Discovery", "artist": "Daft Punk", "year": "2001",
         "release_group_mbid": "rg-1", "release_mbid": "rel-1",
         "primary_type": "Album", "secondary_types": [], "score": 100},
    ])

    result = metadata.match_album_to_musicbrainz("Daft Punk", "Discovery")
    assert result["confident"] is True
    assert result["match"]["release_group_mbid"] == "rg-1"
    assert result["confidence"] >= metadata.ALBUM_MATCH_CONFIDENCE_THRESHOLD


def test_match_album_to_musicbrainz_resolves_only_the_winning_candidate(monkeypatch):
    """The album pipeline needs a release MBID, but only for the album that
    actually wins. Ten candidates should cost one resolution, not ten."""
    metadata = _import_metadata_or_skip()
    _stub_search_release_groups(monkeypatch, metadata, [
        {"title": "Discovery", "artist": "Daft Punk", "year": "2001",
         "release_group_mbid": "rg-win", "release_mbid": None,
         "primary_type": "Album", "secondary_types": [], "score": 100},
        {"title": "Discovery Sessions", "artist": "Somebody Else", "year": "2011",
         "release_group_mbid": "rg-lose", "release_mbid": None,
         "primary_type": "Album", "secondary_types": [], "score": 60},
    ])

    resolved = []

    def _fake_resolve(rg_id, headers):
        resolved.append(rg_id)
        return f"rel-for-{rg_id}"

    monkeypatch.setattr(metadata, "_resolve_representative_release", _fake_resolve)

    result = metadata.match_album_to_musicbrainz("Daft Punk", "Discovery")
    assert resolved == ["rg-win"], "only the winner should be resolved"
    assert result["match"]["release_mbid"] == "rel-for-rg-win"


def test_match_album_to_musicbrainz_survives_the_winners_resolution_failing(monkeypatch):
    """MusicBrainz going quiet at the last hurdle leaves release_mbid None so
    the caller can say 'cannot queue that yet' rather than pretending the
    match itself failed."""
    metadata = _import_metadata_or_skip()
    _stub_search_release_groups(monkeypatch, metadata, [
        {"title": "Discovery", "artist": "Daft Punk", "year": "2001",
         "release_group_mbid": "rg-win", "release_mbid": None,
         "primary_type": "Album", "secondary_types": [], "score": 100},
    ])

    def _boom(rg_id, headers):
        raise metadata.MusicBrainzUnavailable("MusicBrainz has gone for a lie down")

    monkeypatch.setattr(metadata, "_resolve_representative_release", _boom)

    result = metadata.match_album_to_musicbrainz("Daft Punk", "Discovery")
    assert result["confident"] is True
    assert result["match"]["release_mbid"] is None


def test_match_album_to_musicbrainz_weak_match_is_not_confident_but_offers_candidates(monkeypatch):
    metadata = _import_metadata_or_skip()
    _stub_search_release_groups(monkeypatch, metadata, [
        {"title": "A Completely Different Thing", "artist": "Someone Else", "year": "1985",
         "release_group_mbid": "rg-2", "release_mbid": "rel-2",
         "primary_type": "Album", "secondary_types": [], "score": 40},
    ])

    result = metadata.match_album_to_musicbrainz("Daft Punk", "Discovery")
    assert result["confident"] is False
    assert result["candidates"], "weak match should still surface candidates for the UI"
    assert result["confidence"] < metadata.ALBUM_MATCH_CONFIDENCE_THRESHOLD


def test_match_album_to_musicbrainz_no_candidates_returns_no_match(monkeypatch):
    metadata = _import_metadata_or_skip()
    _stub_search_release_groups(monkeypatch, metadata, [])

    result = metadata.match_album_to_musicbrainz("Nobody", "Nothing At All")
    assert result == {"match": None, "confidence": 0.0, "confident": False, "candidates": []}


def test_match_album_to_musicbrainz_does_not_zero_out_various_artists(monkeypatch):
    """The whole point of Task 2 for compilations: a title match against a
    'Various Artists' credit must not be gated to zero the way track matching
    would gate it."""
    metadata = _import_metadata_or_skip()
    _stub_search_release_groups(monkeypatch, metadata, [
        {"title": "Trainspotting", "artist": "Various Artists", "year": "1996",
         "release_group_mbid": "rg-3", "release_mbid": "rel-3",
         "primary_type": "Album", "secondary_types": ["Soundtrack"], "score": 100},
    ])

    result = metadata.match_album_to_musicbrainz("", "Trainspotting")
    assert result["confident"] is True
    assert result["match"]["artist"] == "Various Artists"


def test_match_album_to_musicbrainz_propagates_unavailable(monkeypatch):
    metadata = _import_metadata_or_skip()

    def _boom(*a, **kw):
        raise metadata.MusicBrainzUnavailable("mb is down")

    monkeypatch.setattr(metadata, "search_release_groups", _boom)
    with pytest.raises(metadata.MusicBrainzUnavailable):
        metadata.match_album_to_musicbrainz("Daft Punk", "Discovery")
