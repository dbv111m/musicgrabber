import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# download_leg_healthy
# ---------------------------------------------------------------------------

def test_download_leg_health_is_the_tracks_api(monkeypatch):
    import monochrome
    monkeypatch.setattr("monochrome_tracks.health", lambda: (False, "tracks API unreachable"))
    assert monochrome.download_leg_healthy() == (False, "tracks API unreachable")
    monkeypatch.setattr("monochrome_tracks.health", lambda: (True, "answering"))
    assert monochrome.download_leg_healthy() == (True, "answering")


# ---------------------------------------------------------------------------
# Stream ladder: tracks API, then a Deezer ISRC rescue
# ---------------------------------------------------------------------------

def test_preview_uses_the_tracks_api(monkeypatch):
    import monochrome
    calls = []
    monkeypatch.setattr(
        "monochrome_tracks.resolve_stream_url",
        lambda isrc, artist, title: calls.append((isrc, artist, title)) or "https://tracks.test/track/1",
    )
    url = monochrome.get_monochrome_preview_url(
        "monochrome://123?isrc=GBAYE9200070&quality=LOSSLESS&src=deezer",
        artist_hint="Radiohead", title_hint="Creep",
    )
    assert url == "https://tracks.test/track/1"
    assert calls == [("GBAYE9200070", "Radiohead", "Creep")]


def test_bare_isrc_preview_has_nothing_to_search_by(monkeypatch):
    """The tracks API can only search by name, so an ISRC with no title cannot use it."""
    import monochrome
    monkeypatch.setattr(
        "monochrome_tracks.resolve_stream_url",
        lambda *a: (_ for _ in ()).throw(AssertionError("tracks API needs a title")),
    )
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome.get_monochrome_preview_url("GBAYE9200070")


def test_preview_rescues_isrc_with_artist_and_title(monkeypatch):
    import monochrome
    tracks_calls = []
    def fake_tracks(isrc, artist, title):
        tracks_calls.append(isrc)
        return "https://tracks.test/track/2" if isrc == "GBNEW2500001" else None
    monkeypatch.setattr("monochrome_tracks.resolve_stream_url", fake_tracks)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "GBNEW2500001")
    url = monochrome.get_monochrome_preview_url(
        "monochrome://123?isrc=GBOLD2500001&quality=LOSSLESS&src=deezer",
        artist_hint="Artist", title_hint="Track",
    )
    assert url == "https://tracks.test/track/2"
    assert tracks_calls == ["GBOLD2500001", "GBNEW2500001"]


def test_download_fails_cleanly_when_the_tracks_api_has_nothing(monkeypatch, tmp_path):
    import monochrome
    monkeypatch.setattr("monochrome_tracks.resolve_stream_url", lambda *a: None)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    output = tmp_path / "track.flac"
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome.download_monochrome_track(
            "monochrome://not-tidal?isrc=GBAYE9200070&quality=LOSSLESS", output,
            artist_hint="Radiohead", title_hint="Creep",
        )
    assert not output.exists()


def test_tracks_api_outage_is_reported_not_raised_mid_ladder(monkeypatch):
    """An outage is recorded in the trace and the ladder carries on to its end."""
    import monochrome
    monkeypatch.setattr(
        "monochrome_tracks.resolve_stream_url",
        lambda *a: (_ for _ in ()).throw(httpx.ConnectError("tracks API down")),
    )
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    trace = []
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome._resolve_monochrome_stream_url(
            "monochrome://123?isrc=GBAYE9200070&quality=LOSSLESS",
            artist_hint="Radiohead", title_hint="Creep", trace=trace,
        )
    assert trace[0]["leg"] == "Monochrome tracks API"
    assert trace[0]["ok"] is False
    assert "tracks API down" in trace[0]["detail"]


# ---------------------------------------------------------------------------
# Source default enabled
# ---------------------------------------------------------------------------

def test_monochrome_enabled_defaults_to_true_with_no_db_row(monkeypatch):
    """Fresh install has no DB row; monochrome_enabled() must return True."""
    import monochrome
    monkeypatch.setattr("settings.get_setting_bool",
                        lambda key, default=False, **kw: default)

    assert monochrome.monochrome_enabled() is True


def test_source_registry_has_monochrome_default_enabled_true():
    import search
    cfg = search.SOURCE_REGISTRY.get("monochrome", {})
    assert cfg.get("default_enabled") is True, (
        "Monochrome must default to enabled so fresh installs show results"
    )


# ---------------------------------------------------------------------------
# Tracks API search leg (tops up a light Deezer page, or stands in for Deezer)
# ---------------------------------------------------------------------------

def _make_tracks_item(track_id="155142501534011392", isrc="USQX91300108",
                      title="Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
                      artists=("Daft Punk", "Pharrell Williams", "Nile Rodgers"),
                      playable=True):
    """A minimal tracks API /search/tracks item, shaped like the real API."""
    return {
        "id": track_id,
        "trackId": track_id,
        "isrc": isrc,
        "title": title,
        "artistNames": list(artists),
        "artwork": "https://tracks.test/proxy/mi/cover.jpg",
        "duration": 369000,
        "playable": playable,
    }


def test_tracks_search_leg_maps_items(monkeypatch):
    import monochrome
    monkeypatch.setattr("monochrome_tracks.search_tracks", lambda q, limit: [_make_tracks_item()])

    results = monochrome._tracks_search_leg("daft punk get lucky", 5)

    assert len(results) == 1
    r = results[0]
    assert r["source"] == "monochrome"
    assert r["quality"] == "LOSSLESS"          # all the tracks API ever serves
    assert r["channel"] == "Daft Punk"         # the lead artist; features are in the title
    assert r["title"].startswith("Get Lucky")
    assert r["duration"] == "6:09"
    assert r["thumbnail"] == "https://tracks.test/proxy/mi/cover.jpg"
    assert r["source_url"].startswith("monochrome://155142501534011392?")
    assert "isrc=USQX91300108" in r["source_url"]
    assert "src=tracks" in r["source_url"]
    assert any("tracks-api" in b for b in r["score_breakdown"])


def test_tracks_search_leg_skips_junk_and_unplayable(monkeypatch):
    import monochrome
    items = [
        _make_tracks_item(track_id="1", isrc="", title="No ISRC"),
        _make_tracks_item(track_id="2", playable=False, title="Not playable"),
        _make_tracks_item(track_id="", isrc="GBABC1234560", title="No id"),
        _make_tracks_item(track_id="3", isrc="GBABC1234567", title="Good"),
    ]
    monkeypatch.setattr("monochrome_tracks.search_tracks", lambda q, limit: items)

    results = monochrome._tracks_search_leg("x", 5)

    assert [r["title"] for r in results] == ["Good"]


def test_tracks_search_leg_dedupes_by_isrc(monkeypatch):
    import monochrome
    items = [
        _make_tracks_item(track_id="10", title="Get Lucky"),
        _make_tracks_item(track_id="11", title="Get Lucky (another release)"),
    ]
    monkeypatch.setattr("monochrome_tracks.search_tracks", lambda q, limit: items)

    assert len(monochrome._tracks_search_leg("x", 5)) == 1


def test_tracks_search_leg_shrugs_off_an_outage(monkeypatch):
    import monochrome
    monkeypatch.setattr(
        "monochrome_tracks.search_tracks",
        lambda *a: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    assert monochrome._tracks_search_leg("x", 5) == []


def test_search_monochrome_uses_the_tracks_api_when_deezer_is_empty(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_leg", lambda q, limit: [])
    monkeypatch.setattr("monochrome_tracks.search_tracks", lambda q, limit: [_make_tracks_item()])

    results = monochrome.search_monochrome("daft punk get lucky", 5)

    assert results
    assert results[0]["title"].startswith("Get Lucky")
    assert any("tracks-api" in b for b in results[0]["score_breakdown"])


# ---------------------------------------------------------------------------
# Deezer ISRC leg (v2.9.4): search and rescue
# ---------------------------------------------------------------------------

def _make_deezer_item(track_id=62847142, isrc="GB28K1100036", title="Titanium (feat. Sia)",
                      artist="David Guetta", album="Nothing but the Beat",
                      duration=245, rank=956365, title_version=""):
    """A minimal Deezer /search track item, shaped like the real API."""
    return {
        "id": track_id,
        "isrc": isrc,
        "title": title,
        "title_version": title_version,
        "artist": {"name": artist},
        "album": {"title": album, "cover_big": "https://img.test/cover.jpg"},
        "duration": duration,
        "rank": rank,
    }


def test_isrc_valid():
    import monochrome
    assert monochrome._isrc_valid("GB28K1100036")
    assert monochrome._isrc_valid("usum71703861")     # case-insensitive
    assert not monochrome._isrc_valid("QT&JC2622262") # Tidal's finest ampersand
    assert not monochrome._isrc_valid("QTJC2622262")  # eleven characters
    assert not monochrome._isrc_valid("")
    assert not monochrome._isrc_valid("GB28K110003X") # designation must be digits


def test_deezer_search_leg_verifies_against_the_tracks_api(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(),
                            _make_deezer_item(track_id=2, isrc="GB28K1100170", title="Titanium (Radio Edit)"),
                            _make_deezer_item(track_id=3, isrc="GB28K1100999", title="Titanium (not stocked)"),
                        ])
    asked = []

    def fake_lookup(isrc, artist, title):
        asked.append((isrc, artist, title))
        if isrc in ("GB28K1100036", "GB28K1100170"):
            return {"trackId": "1", "isrc": isrc}, False
        return None, False  # clean miss: the tracks API does not stock it
    monkeypatch.setattr(monochrome, "_tracks_isrc_lookup", fake_lookup)

    results = monochrome._deezer_search_leg("david guetta titanium", 5)

    assert len(results) == 2  # the clean miss was dropped
    assert {r["quality"] for r in results} == {"LOSSLESS"}
    assert all("src=deezer" in r["source_url"] for r in results)
    assert all(r["source"] == "monochrome" for r in results)
    assert all(any("via=deezer-isrc" in b for b in r["score_breakdown"]) for r in results)
    assert not any("unverified" in b for r in results for b in r["score_breakdown"])
    # Asked by Deezer's own artist and title, since the tracks API only searches by name.
    assert ("GB28K1100036", "David Guetta", "Titanium (feat. Sia)") in asked


def test_deezer_search_leg_keeps_unverified_when_the_tracks_api_is_down(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [_make_deezer_item()])
    monkeypatch.setattr(monochrome, "_tracks_isrc_lookup", lambda isrc, artist, title: (None, True))

    results = monochrome._deezer_search_leg("david guetta titanium", 5)

    assert len(results) == 1
    assert results[0]["quality"] == "LOSSLESS"
    assert any("unverified" in b for b in results[0]["score_breakdown"])


def test_deezer_search_leg_skips_junk_isrcs(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(isrc="QT&JC2622262", title="Junk ISRC"),
                            _make_deezer_item(isrc=""),
                        ])
    monkeypatch.setattr(monochrome, "_tracks_isrc_lookup",
                        lambda isrc, artist, title: ({"trackId": "1", "isrc": isrc}, False))

    assert monochrome._deezer_search_leg("whatever", 5) == []


def test_deezer_isrc_rescue_prefers_studio_version(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(isrc="GBCEE0300050", title="Titanium (Live At Wembley)",
                                              title_version="(Live At Wembley)"),
                            _make_deezer_item(isrc="GB28K1100036", title="Titanium (feat. Sia)"),
                        ])

    rescued = monochrome._deezer_isrc_rescue("David Guetta", "Titanium", "QT&JC2622262")

    assert rescued == "GB28K1100036"


def test_deezer_isrc_rescue_returns_empty_on_weak_match(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(isrc="FRXXX9900001", title="Entirely Different Song",
                                              artist="Someone Else"),
                        ])

    assert monochrome._deezer_isrc_rescue("David Guetta", "Titanium", "") == ""


def test_deezer_isrc_rescue_no_hints_no_network(monkeypatch):
    import monochrome

    def boom(*a, **k):
        raise AssertionError("should not search Deezer without hints")
    monkeypatch.setattr(monochrome, "_deezer_search_tracks", boom)

    assert monochrome._deezer_isrc_rescue("", "", "QT&JC2622262") == ""


def test_search_monochrome_skips_the_tracks_leg_when_deezer_delivers(monkeypatch):
    import monochrome
    fake_results = [
        {"relevance_score": 100 - i, "source_url": f"monochrome://x{i}?isrc=GBABC123456{i}&src=deezer"}
        for i in range(5)
    ]
    monkeypatch.setattr(monochrome, "_deezer_search_leg", lambda q, limit: fake_results)

    def boom(*a, **k):
        raise AssertionError("tracks leg should not run when Deezer fills the limit")
    monkeypatch.setattr(monochrome, "_tracks_search_leg", boom)

    results = monochrome.search_monochrome("query", 5)
    assert len(results) == 5


def test_search_monochrome_tops_up_from_the_tracks_api_and_dedupes(monkeypatch):
    import monochrome
    deezer = [{"relevance_score": 90, "source_url": "monochrome://1?isrc=GB28K1100036&src=deezer"}]
    tracks = [
        {"relevance_score": 80, "source_url": "monochrome://2?isrc=GB28K1100036&src=tracks"},  # dupe
        {"relevance_score": 70, "source_url": "monochrome://3?isrc=USUM71703861&src=tracks"},
    ]
    monkeypatch.setattr(monochrome, "_deezer_search_leg", lambda q, limit: deezer)
    monkeypatch.setattr(monochrome, "_tracks_search_leg", lambda q, limit: tracks)

    results = monochrome.search_monochrome("query", 5)

    assert len(results) == 2
    assert results[0]["source_url"].startswith("monochrome://1")
    assert results[1]["source_url"].startswith("monochrome://3")


class _FakeStreamResponse:
    """Pretend httpx.stream context manager serving a small FLAC-ish blob."""
    def __init__(self, payload=b"FLACDATA" * 16):
        self._payload = payload
        self.headers = {"content-length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size=65536):
        yield self._payload


def test_download_rescues_malformed_isrc_via_deezer(monkeypatch, tmp_path):
    import monochrome
    rescue_calls = []
    monkeypatch.setattr(
        monochrome, "_deezer_isrc_rescue",
        lambda artist, title, bad_isrc: rescue_calls.append((artist, title, bad_isrc)) or "GB28K1100036",
    )
    stream_calls = []
    monkeypatch.setattr(
        "monochrome_tracks.resolve_stream_url",
        lambda isrc, *a: stream_calls.append(isrc) or "https://tracks.test/track/3",
    )
    monkeypatch.setattr(monochrome.httpx, "stream", lambda *a, **k: _FakeStreamResponse())
    output = tmp_path / "track.flac"
    monochrome.download_monochrome_track(
        "monochrome://12345?isrc=QT%26JC2622262&quality=LOSSLESS&src=tidal",
        output, artist_hint="David Guetta", title_hint="Titanium",
    )
    assert rescue_calls == [("David Guetta", "Titanium", "QT&JC2622262")]
    assert stream_calls == ["GB28K1100036"]
    assert output.exists() and output.stat().st_size > 0

def test_jobs_from_older_releases_still_resolve_by_isrc(monkeypatch, tmp_path):
    """A queued job from a Tidal- or qbdlx-era search still downloads: only the ISRC matters."""
    import monochrome
    asked = []
    monkeypatch.setattr(
        "monochrome_tracks.resolve_stream_url",
        lambda isrc, artist, title: asked.append(isrc) or "https://tracks.test/track/9",
    )
    monkeypatch.setattr(monochrome.httpx, "stream", lambda *a, **k: _FakeStreamResponse())
    for n, src in enumerate(("tidal", "qbdlx")):
        output = tmp_path / f"track-{n}.flac"
        monochrome.download_monochrome_track(
            f"monochrome://18420572?isrc=GBZZZ9900001&quality=HI_RES_LOSSLESS&src={src}", output,
            artist_hint="Artist", title_hint="Track",
        )
        assert output.exists()
    assert asked == ["GBZZZ9900001", "GBZZZ9900001"]


def test_monochrome_playlist_urls_detect_as_monochrome():
    from watched_playlists import detect_playlist_platform

    platform, playlist_id = detect_playlist_platform(
        "https://monochrome.tf/playlist/0dfc3b10-fbdb-4419-bf54-11b90051fa6c"
    )

    assert platform == "monochrome"
    assert playlist_id == "0dfc3b10-fbdb-4419-bf54-11b90051fa6c"


def test_monochrome_playlist_fetch_uses_tidal_embed_not_hifi_api(monkeypatch):
    # A Monochrome playlist share is a Tidal playlist UUID wearing Monochrome's
    # hostname; it should resolve via the same embed-page scrape as a plain
    # tidal.com link, not hifi-api, which has been dead since Tidal revoked its
    # OAuth client.
    import watched_playlists

    def fake_fetch_tidal_playlist(uuid):
        assert uuid == "0dfc3b10-fbdb-4419-bf54-11b90051fa6c"
        return {"tracks": ["Kylie Cantrall - Carrie Bradshaw"], "playlist_name": "Chill Pop", "count": 1}

    monkeypatch.setattr(watched_playlists, "fetch_tidal_playlist", fake_fetch_tidal_playlist)

    tracks, name, warning, original_ids = watched_playlists.fetch_playlist_tracks(
        "https://monochrome.tf/playlist/0dfc3b10-fbdb-4419-bf54-11b90051fa6c", "monochrome",
    )

    assert tracks == [("Kylie Cantrall", "Carrie Bradshaw")]
    assert name == "Chill Pop"


@pytest.mark.parametrize("version", ["Acapella", "A Cappella", "Vocals Only"])
def test_vocals_only_version_is_penalised_unless_asked_for(version):
    import monochrome

    penalty, reason = monochrome._version_penalty(version, "Adele Hello")
    assert (penalty, reason) == (-45, "version_instrumental=-45")
    assert monochrome._version_penalty(version, f"Adele Hello {version}") == (0, None)
