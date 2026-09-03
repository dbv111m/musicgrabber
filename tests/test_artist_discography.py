"""
Unit tests for metadata.fetch_artist_albums, the call behind the Albums tab's
"Albums by X" list and behind watched-artist album following.

The regression that prompted these: clicking "Haunted House" on a Knife Party
search result reported that MusicBrainz had never heard of it. MusicBrainz had
heard of it perfectly well; we were asking for `type=album` and Haunted House is
typed EP, as is nearly everything Knife Party ever released. The whole
discography came back as one record, Abandon Ship, and the album browser
politely told the user to pick something manually from a list of one.

fetch_artist_albums browses MusicBrainz release-groups (distinct albums), not
raw releases: a heavily-reissued back catalogue can carry hundreds of raw
pressings behind a couple of dozen actual albums, which used to mean several
seconds of enforced one-request-per-second pagination before the list showed
anything. Release-groups are already deduplicated, so this fetches "the
albums" directly rather than "every pressing of every album". The trade-off is
that a release-group has no release MBID of its own -- `release_mbid` always
comes back empty here now, resolved lazily elsewhere (see
metadata.resolve_album_release) only for the one album a user actually opens.
Picking the best release out of several pressings of the same release-group is
therefore no longer this function's job; that logic (_pick_release_from_group)
has its own coverage in test_metadata.py.

Payloads below are shaped like the real thing. httpx is monkeypatched, so
nothing here touches the network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KNIFE_PARTY_MBID = "82a8aee6-0772-4399-9238-b5903eab413f"


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


class _RecordingClient:
    """httpx.Client stand-in that hands back canned pages and keeps a copy of
    every query it was asked to make, so a test can check what we actually
    requested rather than only what we did with the answer."""

    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, headers=None):
        self._calls.append({"url": url, "params": dict(params or {})})
        if not self._responses:
            raise RuntimeError("Test ran out of canned responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_httpx(monkeypatch, metadata, responses):
    """Returns the list of calls made, filled in as the code under test runs."""
    shared = list(responses)
    calls: list[dict] = []
    monkeypatch.setattr(metadata.httpx, "Client", lambda *a, **kw: _RecordingClient(shared, calls))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return calls


def _release_group(title, date, rg_id, primary_type="Album", secondary=None):
    """One release-group as the MusicBrainz browse endpoint returns it."""
    return {
        "id": rg_id,
        "title": title,
        "first-release-date": date,
        "primary-type": primary_type,
        "secondary-types": secondary or [],
    }


# The shape of a real browse response for Knife Party, trimmed to the bits we read.
KNIFE_PARTY_PAGE = {
    "release-group-count": 6,
    "release-groups": [
        _release_group("100% No Modern Talking", "2011-12-12", "rg-100", "EP"),
        _release_group("Rage Valley", "2012-05-28", "rg-rage", "EP"),
        _release_group("Haunted House", "2013-05-06", "rg-haunted", "EP"),
        _release_group("Abandon Ship", "2014-11-24", "rg-abandon", "Album"),
        _release_group("Trigger Warning", "2015-11-13", "rg-trigger", "EP"),
        _release_group("Knife Party Live at Lost Lands 2022", "2022-09-25",
                        "rg-live", "Album", ["Live", "DJ-mix"]),
    ],
}


def test_we_ask_musicbrainz_for_eps_as_well_as_albums(monkeypatch):
    """The one-word bug. `type=album` alone hides most electronic discographies."""
    metadata = _import_metadata_or_skip()
    calls = _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    metadata.fetch_artist_albums(KNIFE_PARTY_MBID)

    assert calls, "no request was made at all"
    assert calls[0]["params"]["type"] == "album|ep"
    assert calls[0]["params"]["artist"] == KNIFE_PARTY_MBID


def test_we_browse_release_groups_not_raw_releases(monkeypatch):
    """The whole point of this endpoint: ask for distinct albums directly
    rather than paginating over every pressing MusicBrainz has catalogued."""
    metadata = _import_metadata_or_skip()
    calls = _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    metadata.fetch_artist_albums(KNIFE_PARTY_MBID)

    assert "release-group" in calls[0]["url"]


def test_singles_are_still_left_out(monkeypatch):
    """Deliberate: a prolific artist's list should not become a hundred one-track
    entries you have to scroll past to reach the records."""
    metadata = _import_metadata_or_skip()
    calls = _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    metadata.fetch_artist_albums(KNIFE_PARTY_MBID)

    assert "single" not in calls[0]["params"]["type"]


def test_the_ep_that_started_all_this_comes_back(monkeypatch):
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    albums = metadata.fetch_artist_albums(KNIFE_PARTY_MBID)
    titles = [a["title"] for a in albums]

    assert "Haunted House" in titles, f"got {titles}"
    haunted = next(a for a in albums if a["title"] == "Haunted House")
    assert haunted["year"] == "2013"
    assert haunted["release_group_mbid"] == "rg-haunted"
    # A release-group has no release of its own; the specific pressing to
    # download is resolved lazily elsewhere, only for an album actually opened.
    assert haunted["release_mbid"] == ""


def test_an_ep_heavy_artist_gets_a_discography_rather_than_a_single_record(monkeypatch):
    """Before the fix this list was exactly one entry long."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    titles = [a["title"] for a in metadata.fetch_artist_albums(KNIFE_PARTY_MBID)]

    assert titles == [
        "100% No Modern Talking",
        "Rage Valley",
        "Haunted House",
        "Abandon Ship",
        "Trigger Warning",
    ], "EPs and albums should interleave in release order"


def test_each_entry_says_whether_it_is_an_album_or_an_ep(monkeypatch):
    """The list now holds two kinds of thing, so it has to be able to say which."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    by_title = {a["title"]: a["primary_type"] for a in metadata.fetch_artist_albums(KNIFE_PARTY_MBID)}

    assert by_title["Haunted House"] == "EP"
    assert by_title["Abandon Ship"] == "Album"


def test_live_and_dj_mix_records_are_still_shown_the_door(monkeypatch):
    """Widening to EPs must not quietly widen to everything else as well."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, KNIFE_PARTY_PAGE)])

    titles = [a["title"] for a in metadata.fetch_artist_albums(KNIFE_PARTY_MBID)]

    assert "Knife Party Live at Lost Lands 2022" not in titles


def test_a_remix_ep_is_excluded_the_same_as_a_remix_album(monkeypatch):
    """The secondary-type filter has to apply to EPs too, not just albums."""
    metadata = _import_metadata_or_skip()
    _patch_httpx(monkeypatch, metadata, [_FakeResp(200, {
        "release-group-count": 2,
        "release-groups": [
            _release_group("Rage Valley", "2012-05-28", "rg-rage", "EP"),
            _release_group("Rage Valley (Remixes)", "2012-08-01", "rg-rmx", "EP", ["Remix"]),
        ],
    })])

    titles = [a["title"] for a in metadata.fetch_artist_albums(KNIFE_PARTY_MBID)]

    assert titles == ["Rage Valley"]


def test_pagination_advances_by_offset_and_sleeps_between_pages(monkeypatch):
    """A discography long enough to need a second page should ask for it at
    the next offset, and respect MusicBrainz's one-request-per-second limit."""
    metadata = _import_metadata_or_skip()
    page_one = {
        "release-group-count": 2,
        "release-groups": [_release_group("First", "2001-01-01", "rg-1")],
    }
    page_two = {
        "release-group-count": 2,
        "release-groups": [_release_group("Second", "2002-01-01", "rg-2")],
    }
    calls = _patch_httpx(monkeypatch, metadata, [_FakeResp(200, page_one), _FakeResp(200, page_two)])

    import time
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    titles = [a["title"] for a in metadata.fetch_artist_albums(KNIFE_PARTY_MBID)]

    assert titles == ["First", "Second"]
    assert len(calls) == 2
    assert calls[1]["params"]["offset"] == 1
    assert slept == [1]
