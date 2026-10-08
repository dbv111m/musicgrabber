"""
Unit tests for album_urls.parse_album_url and resolve_album_url.

Pure unit tests: the Spotify embed fetch and Apple Music fetch are both
monkeypatched, and no real MusicBrainz lookups happen either (matching is
stubbed at the album_urls.match_album_to_musicbrainz seam). No network calls.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_album_urls_or_skip():
    try:
        import album_urls
    except ModuleNotFoundError as exc:
        pytest.skip(f"album_urls module dependencies unavailable: {exc.name}")
    return album_urls


# ---------------------------------------------------------------------------
# parse_album_url: recognition
# ---------------------------------------------------------------------------

def test_spotify_album_url_is_recognised(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import watched_playlists
    monkeypatch.setattr(
        watched_playlists, "_fetch_spotify_playlist_embed",
        lambda url, **kw: {
            "playlist_name": "Discovery",
            "tracks": ["Daft Punk - One More Time", "Daft Punk - Aerodynamic"],
            "count": 2,
        },
    )

    parsed = album_urls.parse_album_url("https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc")
    assert parsed == {
        "service": "spotify",
        "id": "2noRn2Aes5aoNVsU6iWThc",
        "artist": "Daft Punk",
        "album": "Discovery",
    }


def test_spotify_playlist_url_is_not_recognised_as_an_album():
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url("https://open.spotify.com/playlist/37i9dQZEVXbMwmF30ppw50") is None


def test_spotify_track_url_is_not_recognised_as_an_album():
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC") is None


def test_apple_music_album_url_is_recognised(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    monkeypatch.setattr(
        album_urls, "fetch_apple_music_playlist",
        lambda url, **kw: {
            "playlist_name": "OK Computer",
            "tracks": ["Radiohead - Airbag", "Radiohead - Paranoid Android"],
            "count": 2,
        },
    )

    parsed = album_urls.parse_album_url("https://music.apple.com/gb/album/ok-computer/1097864980")
    assert parsed == {
        "service": "apple",
        "id": "1097864980",
        "artist": "Radiohead",
        "album": "OK Computer",
    }


def test_apple_music_album_url_with_track_deeplink_still_recognised(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    monkeypatch.setattr(
        album_urls, "fetch_apple_music_playlist",
        lambda url, **kw: {"playlist_name": "OK Computer", "tracks": ["Radiohead - Airbag"], "count": 1},
    )
    parsed = album_urls.parse_album_url("https://music.apple.com/gb/album/ok-computer/1097864980?i=1097865010")
    assert parsed["service"] == "apple"
    assert parsed["id"] == "1097864980"


def test_apple_music_playlist_url_is_not_recognised_as_an_album():
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url("https://music.apple.com/gb/playlist/todays-hits/pl.abc123") is None


def test_apple_music_song_url_is_not_recognised_as_an_album():
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url("https://music.apple.com/gb/song/airbag/1097865010") is None


@pytest.mark.parametrize("url", [
    "not a url at all",
    "",
    None,
])
def test_out_of_scope_and_junk_urls_are_not_recognised(url):
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url(url) is None


# ---------------------------------------------------------------------------
# Monochrome albums
#
# Two catalogues behind one host: a long id is Monochrome's own (named by its
# tracks API), a short one or /t/ is Tidal's. Both lookups are stubbed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected_id,expected_route", [
    ("https://monochrome.st/album/154041926511759360", "154041926511759360", "tracks"),
    ("https://monochrome.st/album/tracks/154041926511759360", "154041926511759360", "tracks"),
    ("https://monochrome.st/album/mono/154041926511759360?play=1", "154041926511759360", "tracks"),
    ("https://monochrome.st/album/t/77646169", "77646169", "tidal"),
    ("https://monochrome.st/album/77646169", "77646169", "tidal"),
    ("https://monochrome.tf/album/77646169", "77646169", "tidal"),
])
def test_monochrome_album_urls_route_to_the_right_catalogue(url, expected_id, expected_route, monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import monochrome_tracks
    import tidal
    asked = []
    monkeypatch.setattr(
        monochrome_tracks, "fetch_release_info",
        lambda rid: asked.append(("tracks", rid)) or {"artist": "Radiohead", "album": "OK Computer"},
    )
    monkeypatch.setattr(
        tidal, "fetch_tidal_album_info",
        lambda aid: asked.append(("tidal", aid)) or {"artist": "Radiohead", "album": "OK Computer"},
    )

    result = album_urls.parse_album_url(url)

    assert result == {"service": "monochrome", "id": expected_id, "artist": "Radiohead", "album": "OK Computer"}
    assert asked == [(expected_route, expected_id)]


def test_monochrome_release_survives_a_failed_lookup(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import monochrome_tracks

    def boom(rid):
        raise RuntimeError("404")
    monkeypatch.setattr(monochrome_tracks, "fetch_release_info", boom)

    result = album_urls.parse_album_url("https://monochrome.st/album/154041926511759360")

    assert result == {"service": "monochrome", "id": "154041926511759360", "artist": None, "album": None}


def test_monochrome_playlist_url_is_not_an_album():
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url(
        "https://monochrome.st/playlist/0dfc3b10-fbdb-4419-bf54-11b90051fa6c"
    ) is None


# ---------------------------------------------------------------------------
# Amazon Music albums
#
# The scrape itself needs a real browser, so these cover the parts that do not:
# the url shape, and the title parser that turns Amazon's page title into an
# artist and an album. The browser call is stubbed out.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,asin", [
    ("https://music.amazon.co.uk/albums/B0FBGRYDM9", "B0FBGRYDM9"),
    ("https://music.amazon.com/albums/B0GS72231Q", "B0GS72231Q"),
    ("https://music.amazon.de/albums/B0H1FB344C", "B0H1FB344C"),
    # A track deep-link still names the whole album, so it stays recognised.
    ("https://music.amazon.co.uk/albums/B0GFQ5FZFY?trackAsin=B0GFQ3Y24D", "B0GFQ5FZFY"),
])
def test_amazon_album_urls_are_recognised(url, asin, monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import amazon
    monkeypatch.setattr(
        amazon, "fetch_amazon_album_info",
        lambda u: {"artist": "Olivia Dean", "album": "The Art of Loving"},
    )
    result = album_urls.parse_album_url(url)
    assert result is not None
    assert result["service"] == "amazon"
    assert result["id"] == asin
    assert result["artist"] == "Olivia Dean"
    assert result["album"] == "The Art of Loving"


@pytest.mark.parametrize("url", [
    "https://music.amazon.co.uk/playlists/B09ZHBTLFF",
    "https://music.amazon.co.uk/user-playlists/abc123def456",
    "https://music.amazon.co.uk/artists/B0ABCDEFGH",
])
def test_amazon_non_album_urls_are_not_recognised(url):
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url(url) is None


def test_amazon_album_survives_a_failed_scrape(monkeypatch):
    """A dead ASIN is still a recognised album url, just an unnamed one.

    Same contract as every other service here: recognition is a pattern match
    and must not depend on the network call working.
    """
    album_urls = _import_album_urls_or_skip()
    import amazon

    def boom(url):
        raise RuntimeError("Amazon did not name this album")

    monkeypatch.setattr(amazon, "fetch_amazon_album_info", boom)
    result = album_urls.parse_album_url("https://music.amazon.co.uk/albums/B00136RSDG")
    assert result is not None
    assert result["service"] == "amazon"
    assert result["artist"] is None and result["album"] is None


@pytest.mark.parametrize("title,expected", [
    ("Play The Art of Loving by Olivia Dean on Amazon Music Unlimited",
     ("Olivia Dean", "The Art of Loving")),
    ("Play Dai Dai by Shakira & Burna Boy on Amazon Music Unlimited",
     ("Shakira & Burna Boy", "Dai Dai")),
    ("Play Boston by STELLA LEFTY on Amazon Music Unlimited",
     ("STELLA LEFTY", "Boston")),
    # Some storefronts drop "Unlimited".
    ("Play The Romantic by Bruno Mars on Amazon Music",
     ("Bruno Mars", "The Romantic")),
    # An album title containing " by " must split on the LAST one, or the
    # record loses half its name and the artist gains a preposition.
    ("Play Poems by Heart by Olivia Dean on Amazon Music Unlimited",
     ("Olivia Dean", "Poems by Heart")),
    # The generic title is what a dead or region-locked ASIN leaves behind.
    ("Amazon Music | Listen to songs, podcasts, & more", (None, None)),
    ("", (None, None)),
])
def test_amazon_album_title_parsing(title, expected):
    amazonalbum = pytest.importorskip("amazonalbum")
    assert amazonalbum.parse_album_title(title) == expected


# ---------------------------------------------------------------------------
# YouTube Music / Beatport / Tidal / Monochrome albums
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://music.youtube.com/playlist?list=OLAK5uy_kmPseHwrPCbC1DsXhwQjLdcpQjZzDNKOU",
    "https://www.youtube.com/playlist?list=OLAK5uy_kmPseHwrPCbC1DsXhwQjLdcpQjZzDNKOU",
    "https://music.youtube.com/watch?v=abc123&list=OLAK5uy_kmPseHwrPCbC1DsXhwQjLdcpQjZzDNKOU",
])
def test_youtube_music_album_playlists_are_recognised(monkeypatch, url):
    album_urls = _import_album_urls_or_skip()
    monkeypatch.setattr(
        album_urls, "_parse_youtube_album",
        lambda pid: {"service": "youtube", "id": pid, "artist": "Radiohead", "album": "OK Computer"},
    )
    parsed = album_urls.parse_album_url(url)
    assert parsed["service"] == "youtube"
    assert parsed["id"] == "OLAK5uy_kmPseHwrPCbC1DsXhwQjLdcpQjZzDNKOU"


@pytest.mark.parametrize("url", [
    # An ordinary user playlist and a radio mix are not albums, however much
    # they look like one from a distance. OLAK5uy_ is the whole signal.
    "https://music.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI",
    "https://www.youtube.com/playlist?list=RDdQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
])
def test_non_album_youtube_playlists_are_not_recognised(url):
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url(url) is None


def test_youtube_album_reads_title_and_artist_from_ytdlp(monkeypatch):
    """The playlist title is the album; the first entry's uploader is the artist,
    with YouTube's "- Topic" plumbing stripped off."""
    album_urls = _import_album_urls_or_skip()
    import youtube

    class _Result:
        returncode = 0
        stdout = json.dumps({
            "title": "OK Computer",
            "entries": [{"title": "Airbag", "uploader": "Radiohead - Topic"}],
        })

    monkeypatch.setattr(youtube, "_ytdlp_base_args", lambda *a, **kw: [])
    monkeypatch.setattr(youtube, "run_ytdlp_with_retries", lambda *a, **kw: (_Result(), False))

    parsed = album_urls._parse_youtube_album("OLAK5uy_abc")
    assert parsed == {"service": "youtube", "id": "OLAK5uy_abc",
                      "artist": "Radiohead", "album": "OK Computer"}


def test_youtube_album_lookup_failure_still_recognises_the_url(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import youtube

    monkeypatch.setattr(youtube, "_ytdlp_base_args", lambda *a, **kw: [])
    monkeypatch.setattr(youtube, "run_ytdlp_with_retries", lambda *a, **kw: (None, True))

    parsed = album_urls._parse_youtube_album("OLAK5uy_abc")
    assert parsed == {"service": "youtube", "id": "OLAK5uy_abc", "artist": None, "album": None}


def test_beatport_release_url_is_recognised(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import beatport
    monkeypatch.setattr(
        beatport, "fetch_beatport_release_info",
        lambda url: {"artist": "Ultra Nate", "album": "Movin' To The Sun"},
    )
    parsed = album_urls.parse_album_url("https://www.beatport.com/release/movin-to-the-sun/6959796")
    assert parsed == {"service": "beatport", "id": "6959796",
                      "artist": "Ultra Nate", "album": "Movin' To The Sun"}


@pytest.mark.parametrize("url", [
    "https://www.beatport.com/chart/some-chart/12345",
    "https://www.beatport.com/top-100",
    "https://www.beatport.com/genre/techno/6/top-100",
])
def test_beatport_charts_are_not_albums(url):
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url(url) is None


@pytest.mark.parametrize("url,service", [
    ("https://tidal.com/album/77640617", "tidal"),
    ("https://tidal.com/browse/album/77640617", "tidal"),
    ("https://monochrome.tf/album/77640617", "monochrome"),
    ("https://monochrome.samidy.com/album/77640617", "monochrome"),
])
def test_tidal_and_monochrome_albums_are_recognised(monkeypatch, url, service):
    """A Monochrome album share is a Tidal album id on a different host, so both
    resolve through the same embed."""
    album_urls = _import_album_urls_or_skip()
    import tidal
    monkeypatch.setattr(
        tidal, "fetch_tidal_album_info",
        lambda album_id: {"artist": "U2", "album": "Achtung Baby"},
    )
    parsed = album_urls.parse_album_url(url)
    assert parsed == {"service": service, "id": "77640617",
                      "artist": "U2", "album": "Achtung Baby"}


@pytest.mark.parametrize("url", [
    "https://tidal.com/playlist/1b418bb8-90b7-4f6c-8a05-fa9ffe72fbf5",
    "https://tidal.com/browse/playlist/1b418bb8-90b7-4f6c-8a05-fa9ffe72fbf5",
    "https://monochrome.tf/playlist/1b418bb8-90b7-4f6c-8a05-fa9ffe72fbf5",
])
def test_tidal_and_monochrome_playlists_are_not_albums(url):
    album_urls = _import_album_urls_or_skip()
    assert album_urls.parse_album_url(url) is None


def test_tidal_album_scrape_failure_still_recognises_the_url(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    import tidal

    def _boom(album_id):
        raise RuntimeError("Tidal had a moment")

    monkeypatch.setattr(tidal, "fetch_tidal_album_info", _boom)
    parsed = album_urls.parse_album_url("https://tidal.com/album/77640617")
    assert parsed == {"service": "tidal", "id": "77640617", "artist": None, "album": None}


def test_spotify_album_scrape_failure_still_recognises_the_url(monkeypatch):
    """A dead link or service hiccup shouldn't make a genuine album url look
    unrecognised; it just comes back with no artist/album scraped."""
    album_urls = _import_album_urls_or_skip()
    import watched_playlists

    def _boom(url, **kw):
        raise RuntimeError("Spotify had a moment")

    monkeypatch.setattr(watched_playlists, "_fetch_spotify_playlist_embed", _boom)

    parsed = album_urls.parse_album_url("https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc")
    assert parsed == {"service": "spotify", "id": "2noRn2Aes5aoNVsU6iWThc", "artist": None, "album": None}


# ---------------------------------------------------------------------------
# resolve_album_url
# ---------------------------------------------------------------------------

def test_resolve_album_url_not_recognised():
    album_urls = _import_album_urls_or_skip()
    result = album_urls.resolve_album_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert result["recognised"] is False
    assert result["service"] is None
    assert result["confident"] is False
    assert result["match"] is None


def test_resolve_album_url_confident_match_passes_straight_through(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    monkeypatch.setattr(
        album_urls, "parse_album_url",
        lambda url: {"service": "spotify", "id": "abc123", "artist": "Daft Punk", "album": "Discovery"},
    )
    monkeypatch.setattr(
        album_urls, "match_album_to_musicbrainz",
        lambda artist, album: {
            "match": {"release_group_mbid": "rg-1", "release_mbid": "rel-1", "title": album, "artist": artist},
            "confidence": 0.95, "confident": True, "candidates": [],
        },
    )

    result = album_urls.resolve_album_url("https://open.spotify.com/album/abc123")
    assert result["recognised"] is True
    assert result["service"] == "spotify"
    assert result["confident"] is True
    assert result["match"]["release_group_mbid"] == "rg-1"


def test_resolve_album_url_weak_match_asks_for_confirmation(monkeypatch):
    album_urls = _import_album_urls_or_skip()
    monkeypatch.setattr(
        album_urls, "parse_album_url",
        lambda url: {"service": "apple", "id": "999", "artist": "Some Artist", "album": "Some Album"},
    )
    monkeypatch.setattr(
        album_urls, "match_album_to_musicbrainz",
        lambda artist, album: {
            "match": {"release_group_mbid": "rg-2", "release_mbid": None, "title": "Close But Not Quite", "artist": artist},
            "confidence": 0.4, "confident": False,
            "candidates": [{"release_group_mbid": "rg-2", "title": "Close But Not Quite"}],
        },
    )

    result = album_urls.resolve_album_url("https://music.apple.com/gb/album/some-album/999")
    assert result["recognised"] is True
    assert result["confident"] is False
    assert result["candidates"]


def test_resolve_album_url_scrape_failure_skips_matching(monkeypatch):
    """No album title scraped -> nothing to match against, and we must not
    even try (an empty-string album search is a waste of a MusicBrainz call)."""
    album_urls = _import_album_urls_or_skip()
    monkeypatch.setattr(
        album_urls, "parse_album_url",
        lambda url: {"service": "spotify", "id": "abc123", "artist": None, "album": None},
    )
    called = []
    monkeypatch.setattr(
        album_urls, "match_album_to_musicbrainz",
        lambda artist, album: called.append((artist, album)),
    )

    result = album_urls.resolve_album_url("https://open.spotify.com/album/abc123")
    assert result["recognised"] is True
    assert result["match"] is None
    assert result["confident"] is False
    assert called == []
