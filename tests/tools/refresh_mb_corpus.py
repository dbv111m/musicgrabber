#!/usr/bin/env python3
"""Fetch MusicBrainz responses for the release-selection corpus and cache them.

This is the only part of the exercise that touches the network. It is slow on
purpose: MusicBrainz asks for one request per second and it would be rude to
argue. Run it when you want to refresh the fixtures, then tune scoring offline
against the cache as many times as you like without bothering anyone's server.

    tests/.venv/bin/python tests/tools/refresh_mb_corpus.py

We cache several query variants per track so that different strategies can be
compared offline. Responses are trimmed to the fields release selection actually
reads, which keeps the fixture readable and stops us committing several megs of
MusicBrainz's more enthusiastic JSON.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CORPUS = FIXTURES / "mb_corpus.json"
CACHE = FIXTURES / "mb_corpus_cache.json"

MB_URL = "https://musicbrainz.org/ws/2/recording/"
HEADERS = {"User-Agent": "MusicGrabber/corpus-tuning (https://gitlab.com/g33kphr33k/musicgrabber)"}
LIMIT = 25
RATE_LIMIT_SECS = 1.1

# The studio-album filter, expressed in MusicBrainz's Lucene dialect.
STUDIO_FILTER = (
    " AND primarytype:album AND status:official"
    " AND NOT secondarytype:live AND NOT secondarytype:compilation"
)


def build_queries(artist: str, title: str) -> dict[str, str]:
    """The query variants we want to compare. `plain` is what ships today."""
    a = artist.replace('"', '')
    t = title.replace('"', '')
    return {
        "plain": f'artist:"{a}" AND recording:"{t}"',
        "filtered": f'artist:"{a}" AND recording:"{t}"{STUDIO_FILTER}',
        "alias": f'artist:"{a}" AND (recording:"{t}" OR alias:"{t}")',
        "alias_filtered": f'artist:"{a}" AND (recording:"{t}" OR alias:"{t}"){STUDIO_FILTER}',
    }


def _credit(entries) -> list[dict]:
    """Trim an artist-credit block to the two fields anyone reads."""
    out = []
    for ac in entries or []:
        if isinstance(ac, dict):
            out.append({
                "name": ac.get("name") or (ac.get("artist") or {}).get("name") or "",
                "joinphrase": ac.get("joinphrase") or "",
            })
    return out


def _trim_media(media) -> list[dict]:
    """Keep just enough of the media block to recover a track number.

    MusicBrainz nests the matched track under `track` in search results and
    `tracks` in lookups, so we keep whichever turned up rather than picking a
    side in that particular argument.
    """
    out = []
    for medium in media or []:
        entries = medium.get("track") or medium.get("tracks") or []
        out.append({
            "track-count": medium.get("track-count"),
            "track-offset": medium.get("track-offset"),
            "track": [{"number": t.get("number"), "position": t.get("position")}
                      for t in entries if isinstance(t, dict)],
        })
    return out


def _trim_release(rel: dict) -> dict:
    rg = rel.get("release-group") or {}
    return {
        "id": rel.get("id"),
        "title": rel.get("title"),
        "date": rel.get("date"),
        "status": rel.get("status"),
        "media": _trim_media(rel.get("media")),
        "artist-credit": _credit(rel.get("artist-credit")),
        "release-group": {
            "id": rg.get("id"),
            "title": rg.get("title"),
            "primary-type": rg.get("primary-type") or rg.get("type"),
            "secondary-types": rg.get("secondary-types") or [],
            "first-release-date": rg.get("first-release-date"),
            "artist-credit": _credit(rg.get("artist-credit")),
        },
    }


def _trim_recording(rec: dict) -> dict:
    return {
        "id": rec.get("id"),
        "title": rec.get("title"),
        "score": int(rec.get("score", 0)),
        "length": rec.get("length"),
        "artist-credit": _credit(rec.get("artist-credit")),
        "releases": [_trim_release(r) for r in (rec.get("releases") or [])],
    }


def fetch(query: str) -> dict:
    params = {
        "query": query,
        "fmt": "json",
        "limit": LIMIT,
        "inc": "releases release-groups artist-credits",
    }
    url = MB_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - a fixture refresh should report, not explode
        return {"error": str(exc), "recordings": []}
    return {"recordings": [_trim_recording(r) for r in (data.get("recordings") or [])]}


def main() -> int:
    # MusicBrainz search is not deterministic: identical queries return different
    # slices of equally-scoring recordings. Snapshots let us sample it more than
    # once so a tuning result isn't just one lucky roll of the dice.
    out_path = CACHE
    argv = sys.argv[1:]
    if "--out" in argv:
        out_path = Path(argv[argv.index("--out") + 1])

    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    tracks = corpus["tracks"]
    cache: dict = {}
    total = sum(len(build_queries(t["artist"], t["title"])) for t in tracks)
    done = 0

    for track in tracks:
        artist, title = track["artist"], track["title"]
        key = f"{artist}␟{title}"
        cache[key] = {}
        for variant, query in build_queries(artist, title).items():
            result = fetch(query)
            cache[key][variant] = result
            done += 1
            n = len(result.get("recordings") or [])
            flag = "  <-- ZERO" if n == 0 and not result.get("error") else ""
            err = f"  ERROR {result['error']}" if result.get("error") else ""
            print(f"[{done:3d}/{total}] {artist} - {title}  ({variant}): {n} recs{flag}{err}",
                  flush=True)
            time.sleep(RATE_LIMIT_SECS)

    out_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"\nWrote {out_path} ({size_kb:.0f} KB, {len(cache)} tracks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
