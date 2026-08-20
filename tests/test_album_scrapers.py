"""
Unit tests for the album-only scrapers behind album_urls: Tidal's album embed
reader and Beatport's release reader.

Both are fed captured HTML rather than the live services, so these are pure and
offline. The fixtures below are trimmed from real responses (U2's "Achtung
Baby" on Tidal, a Beatport release page), keeping the bits each parser reads.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_or_skip(name):
    try:
        return __import__(name)
    except ModuleNotFoundError as exc:
        pytest.skip(f"{name} dependencies unavailable: {exc.name}")


# ---------------------------------------------------------------------------
# Tidal album embed
# ---------------------------------------------------------------------------

_TIDAL_ALBUM_HTML = """
<html><body>
  <div class="media-information">
    <header>
      <h1 class="media-album">
        <a href="https://tidal.com/album/77640617" target="_blank">Achtung Baby</a>
      </h1>
    </header>
    <span class="media-artist" title="Artist: U2">
      <a href="https://tidal.com/artist/17123" target="_blank">U2</a>
    </span>
  </div>
  <list-item product-type="track"><span slot="title">Zoo Station</span><span slot="artist"></span></list-item>
</body></html>
"""


def test_tidal_album_embed_yields_artist_and_album(monkeypatch):
    tidal = _import_or_skip("tidal")
    monkeypatch.setattr(tidal, "_fetch_html", lambda url: _TIDAL_ALBUM_HTML)

    info = tidal.fetch_tidal_album_info("77640617")
    assert info == {"artist": "U2", "album": "Achtung Baby"}


def test_tidal_album_embed_uses_the_album_endpoint(monkeypatch):
    """Albums live at /albums/<id>, not the /playlists/<uuid> the playlist
    fetcher uses; getting this wrong is a silent 404."""
    tidal = _import_or_skip("tidal")
    seen = {}

    def _fake_fetch(url):
        seen["url"] = url
        return _TIDAL_ALBUM_HTML

    monkeypatch.setattr(tidal, "_fetch_html", _fake_fetch)
    tidal.fetch_tidal_album_info("77640617")
    assert seen["url"] == "https://embed.tidal.com/albums/77640617"


def test_tidal_album_embed_survives_markup_it_cannot_read(monkeypatch):
    tidal = _import_or_skip("tidal")
    monkeypatch.setattr(tidal, "_fetch_html", lambda url: "<html><body>nothing here</body></html>")

    assert tidal.fetch_tidal_album_info("77640617") == {"artist": None, "album": None}


# ---------------------------------------------------------------------------
# Beatport release
# ---------------------------------------------------------------------------

def _beatport_html(queries):
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"dehydratedState": {"queries": queries}}}})
        + "</script></body></html>"
    )


_BEATPORT_QUERIES = [
    {
        "queryKey": ["release-6959796"],
        "state": {"data": {
            "id": 6959796,
            "name": "Movin' To The Sun (Extended Mix)",
            "artists": [{"id": 1988, "name": "Ultra Nate"}, {"id": 2, "name": "HUGEL"}],
            "track_count": 1,
        }},
    },
    {
        "queryKey": ["release-6959796-recommendations"],
        "state": {"data": {"results": [
            {"name": "Something Else Entirely", "artists": [{"name": "Another Act"}]},
        ]}},
    },
]


def test_beatport_release_yields_artist_and_album(monkeypatch):
    beatport = _import_or_skip("beatport")
    monkeypatch.setattr(beatport, "_fetch_html", lambda url: _beatport_html(_BEATPORT_QUERIES))

    info = beatport.fetch_beatport_release_info("https://www.beatport.com/release/x/6959796")
    assert info == {"artist": "Ultra Nate", "album": "Movin' To The Sun (Extended Mix)"}


def test_beatport_release_ignores_the_recommendations_block(monkeypatch):
    """The regression this reader exists for: the generic tree-walk used by the
    chart scraper hoovers up the "you might also like" rail, which turned a
    one-track single into a 37-track album by half the label's roster."""
    beatport = _import_or_skip("beatport")
    # Recommendations first, so a lazy "take the first release-ish thing" would trip.
    monkeypatch.setattr(
        beatport, "_fetch_html",
        lambda url: _beatport_html(list(reversed(_BEATPORT_QUERIES))),
    )

    info = beatport.fetch_beatport_release_info("https://www.beatport.com/release/x/6959796")
    assert info["album"] == "Movin' To The Sun (Extended Mix)"
    assert info["artist"] == "Ultra Nate"


@pytest.mark.parametrize("html", [
    "<html><body>no next data at all</body></html>",
    '<html><script id="__NEXT_DATA__">{not json}</script></html>',
    '<html><script id="__NEXT_DATA__">{"props":{}}</script></html>',
])
def test_beatport_release_survives_pages_it_cannot_read(monkeypatch, html):
    beatport = _import_or_skip("beatport")
    monkeypatch.setattr(beatport, "_fetch_html", lambda url: html)

    assert beatport.fetch_beatport_release_info(
        "https://www.beatport.com/release/x/1"
    ) == {"artist": None, "album": None}
