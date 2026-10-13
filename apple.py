"""
MusicGrabber - Apple Music playlist fetching

Apple's web app only server-renders the first few hundred tracks. The complete
track list lives behind amp-api.music.apple.com, fetched with a web MusicKit
bearer token bundled with the site JS. We fetch the public page once, extract
the current JS bundle URL, pull the token from that bundle, then page through
Apple's own catalog endpoint directly.

HTML scraping is kept as a last-resort fallback for short public playlists if
the API path fails.
"""

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from constants import TIMEOUT_HTTP_SPOTIFY

_TIMEOUT = TIMEOUT_HTTP_SPOTIFY
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}
_WEB_ORIGIN = "https://music.apple.com"
_AMP_API_ORIGIN = "https://amp-api.music.apple.com"
_AMP_PAGE_LIMIT = 300
_LIBRARY_PAGE_LIMIT = 100  # Library endpoint caps at 100
_HTML_SCRAPE_CAP = 300  # Apple server-renders no more than this, whatever the real size


def fetch_apple_music_playlist(url: str, music_user_token: str | None = None) -> dict:
    """Fetch tracks from an Apple Music playlist or album URL.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count.

    Public catalog playlists: scrapes the Apple web token from their JS bundle,
    then pages through the amp-api. Falls back to HTML scraping on failure.

    Private library playlists (music.apple.com/library/...): uses the user's
    Music-User-Token from settings plus the same web bearer token.
    """
    print(f"Fetching Apple Music playlist: {url}")

    if "/library/" in urllib.parse.urlparse(url).path:
        if not music_user_token:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Apple Music private playlists require a Music-User-Token. "
                    "Add it in Settings under Apple Music."
                ),
            )
        return _fetch_library_playlist(url, music_user_token)

    html = _fetch_html(url)
    playlist_name = _extract_playlist_name(html)

    try:
        result = _fetch_via_api(url, html, playlist_name, music_user_token)
        if result["tracks"]:
            print(
                f"[Apple] API path OK: {result['count']} tracks "
                f"from '{result['playlist_name']}'"
            )
            return result
    except Exception as exc:
        print(f"[Apple] API path failed ({exc}), falling back to HTML scraping")

    return _fetch_via_html(html, playlist_name)


# ---------------------------------------------------------------------------
# Private library playlist path
# ---------------------------------------------------------------------------

def _auth_headers(token: str, music_user_token: str | None = None) -> dict:
    """Headers for an amp-api call, with the user token attached when we have one.

    Apple's own web player sends the user token as `media-user-token`, while
    MusicKit documents it as `Music-User-Token`. They carry the same value and
    different endpoints have historically been fussy about which one they want,
    so we send both and let Apple pick a favourite.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": _WEB_ORIGIN,
    }
    if music_user_token:
        headers["Music-User-Token"] = music_user_token
        headers["media-user-token"] = music_user_token
    return headers


def _fetch_library_playlist(url: str, music_user_token: str) -> dict:
    """Fetch tracks from a private Apple Music library playlist.

    Tries each candidate web token in turn, exactly as the catalogue path does,
    so the next time Apple reshuffles its bundle this heals itself too. The
    user token rides along on every attempt here, so if amp-api refuses the
    lot, the user token is by far the likeliest culprit and we say so.
    """
    item_id = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    for index, token in enumerate(_fetch_web_tokens_for_library(), start=1):
        auth_headers = _auth_headers(token, music_user_token)
        try:
            tracks = _fetch_library_tracks(item_id, auth_headers)
        except RuntimeError as exc:
            if not _is_refusal(exc):
                raise
            continue
        if index > 1:
            print(f"[Apple] Token candidate {index} worked for the library; earlier ones were refused")
        playlist_name = _fetch_library_playlist_name(item_id, auth_headers)
        return {"tracks": tracks, "playlist_name": playlist_name, "count": len(tracks)}

    raise HTTPException(
        status_code=401,
        detail=(
            "Apple Music refused your media-user-token for this library playlist. "
            "It has probably expired: copy a fresh one into Settings under Apple Music."
        ),
    )


def _fetch_web_tokens_for_library() -> list[str]:
    """Candidate Apple Music web bearer tokens, via the public homepage."""
    html = _fetch_html(_WEB_ORIGIN)
    bundle_url = _extract_web_bundle_url(_WEB_ORIGIN, html)
    return _extract_web_tokens(bundle_url)


def _is_refusal(exc: Exception) -> bool:
    """True when amp-api said 401 or 403, which a different token might fix."""
    return "HTTP 401" in str(exc) or "HTTP 403" in str(exc)


def _fetch_library_playlist_name(item_id: str, auth_headers: dict) -> str:
    try:
        url = f"{_AMP_API_ORIGIN}/v1/me/library/playlists/{urllib.parse.quote(item_id)}"
        payload = _fetch_json(url, headers=auth_headers)
        data = payload.get("data") or []
        if data:
            name = ((data[0].get("attributes") or {}).get("name") or "").strip()
            if name:
                return name
    except Exception:
        pass
    return "Apple Music Playlist"


def _fetch_library_tracks(item_id: str, auth_headers: dict) -> list[str]:
    query = {"offset": "0", "limit": str(_LIBRARY_PAGE_LIMIT)}
    next_url = (
        f"{_AMP_API_ORIGIN}/v1/me/library/playlists/"
        f"{urllib.parse.quote(item_id)}/tracks?{urllib.parse.urlencode(query)}"
    )

    tracks: list[str] = []
    while next_url:
        payload = _fetch_json(next_url, headers=auth_headers)
        for item in (payload.get("data") or []):
            attrs = item.get("attributes") or {}
            artist = (attrs.get("artistName") or "").strip()
            title = (attrs.get("name") or "").strip()
            if artist and title:
                tracks.append(f"{artist} - {title}")
        next_path = payload.get("next")
        next_url = urllib.parse.urljoin(_AMP_API_ORIGIN, next_path) if next_path else None

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail="No tracks found in Apple Music library playlist. It may be empty or the token may have expired.",
        )
    return tracks


# ---------------------------------------------------------------------------
# API path
# ---------------------------------------------------------------------------

def _fetch_via_api(
    url: str,
    html: str,
    playlist_name: str,
    music_user_token: str | None = None,
) -> dict:
    storefront, item_type, item_id, language = _extract_catalog_context(url, html)
    bundle_url = _extract_web_bundle_url(url, html)

    # Try each candidate token until amp-api stops slamming the door. A wrong
    # token answers with a bare 401, which is indistinguishable from "computer
    # says no" until you try a different one, so that is exactly what we do.
    #
    # Catalogue calls do not need the user token, so if everything was refused
    # while carrying one, go round once more without it. An expired token in
    # Settings should not be the reason a public playlist gets cut off at 300.
    tracks = None
    last_error = None
    tokens = _extract_web_tokens(bundle_url)
    for user_token in dict.fromkeys([music_user_token, None]):
        for index, token in enumerate(tokens, start=1):
            try:
                tracks = _fetch_all_tracks(
                    storefront, item_type, item_id, token, language, user_token
                )
                if index > 1:
                    print(f"[Apple] Token candidate {index} worked; earlier ones were refused")
                if music_user_token and not user_token:
                    print("[Apple] Refused with your user token, fine without it; it may have expired")
                break
            except RuntimeError as exc:
                last_error = exc
                if not _is_refusal(exc):
                    raise
                continue
        if tracks is not None:
            break

    if tracks is None:
        raise last_error or RuntimeError("Apple amp-api refused every candidate token")

    return {
        "tracks": tracks,
        "playlist_name": playlist_name,
        "count": len(tracks),
    }


def _extract_catalog_context(url: str, html: str) -> tuple[str, str, str, str | None]:
    canonical_match = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    canonical_url = canonical_match.group(1) if canonical_match else url
    parsed = urllib.parse.urlparse(canonical_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        raise RuntimeError("Could not parse Apple Music URL path")

    storefront = parts[0]
    item_kind = parts[1].lower()
    item_type = {"playlist": "playlists", "album": "albums"}.get(item_kind)
    if not item_type:
        raise RuntimeError(f"Unsupported Apple Music content type: {item_kind}")

    item_id = parts[-1]
    lang_match = re.search(r'<html[^>]+lang="([^"]+)"', html, re.IGNORECASE)
    language = lang_match.group(1) if lang_match else None
    return storefront, item_type, item_id, language


def _extract_web_bundle_url(page_url: str, html: str) -> str:
    script_match = re.search(
        r'<script[^>]+type="module"[^>]+src="([^"]*assets/index[^"]+\.js)"',
        html,
    )
    if not script_match:
        raise RuntimeError("Could not find Apple Music web bundle URL")
    return urllib.parse.urljoin(page_url, script_match.group(1))


def _jwt_issuer(token: str) -> str:
    """Return a JWT's `iss` claim, or "" if it will not decode.

    No signature checking; we only want to know which Apple service minted it
    so we can guess which one amp-api will actually answer to.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("iss", ""))
    except Exception:
        return ""


def _extract_web_tokens(bundle_url: str) -> list[str]:
    """Return every plausible developer token in the bundle, best guess first.

    Apple ships several JWTs in that bundle and only some of them mean anything
    to amp-api. Taking the first one we found was the whole of GitLab #94: the
    one our regex happened to land on (`iss` M62YD85FTQ) answers every catalog
    request with a bare 401, while the web player's own (`iss` AMPWebPlay) and
    one other hand over the entire playlist quite happily. Nothing in the file
    says which is which, so we order by how likely each is to be the right one
    and let the caller try them in turn.
    """
    js = _fetch_text(bundle_url)

    named = []
    for pattern in (r'developerToken:"([^"]+)"', r"developerToken:`([^`]+)`"):
        named.extend(re.findall(pattern, js))

    loose = re.findall(
        r'(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})', js
    )
    # The web player's own token first among the unnamed ones. It is the one
    # the browser uses, so it is the one most likely to still work next month.
    web_player = [t for t in loose if _jwt_issuer(t) == "AMPWebPlay"]

    ordered = []
    for token in [*named, *web_player, *loose]:
        if token and token not in ordered:
            ordered.append(token)

    if not ordered:
        raise RuntimeError("Could not extract Apple Music web token")
    return ordered


def _fetch_all_tracks(
    storefront: str,
    item_type: str,
    item_id: str,
    token: str,
    language: str | None,
    music_user_token: str | None = None,
) -> list[str]:
    query = {
        "offset": "0",
        "limit": str(_AMP_PAGE_LIMIT),
    }
    if language:
        query["l"] = language

    next_url = (
        f"{_AMP_API_ORIGIN}/v1/catalog/{storefront}/{item_type}/"
        f"{urllib.parse.quote(item_id)}/tracks?{urllib.parse.urlencode(query)}"
    )

    tracks: list[str] = []
    while next_url:
        payload = _fetch_json(
            next_url,
            headers=_auth_headers(token, music_user_token),
        )
        data = payload.get("data") or []
        for item in data:
            attrs = item.get("attributes") or {}
            artist = (attrs.get("artistName") or "").strip()
            title = (attrs.get("name") or "").strip()
            if artist and title:
                tracks.append(f"{artist} - {title}")

        next_path = payload.get("next")
        next_url = urllib.parse.urljoin(_AMP_API_ORIGIN, next_path) if next_path else None

    if not tracks:
        raise RuntimeError("Apple amp-api returned no tracks")

    return tracks


# ---------------------------------------------------------------------------
# HTML scraping fallback
# ---------------------------------------------------------------------------

def _fetch_via_html(html: str, playlist_name: str) -> dict:
    """Scrape the server-rendered JSON blob. Works reliably but caps at ~300 tracks."""
    tracks = _extract_tracks_from_html(html)

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail=(
                "No tracks found in Apple Music playlist. "
                "It may be empty, private, or region-locked."
            ),
        )

    print(f"[Apple] HTML scrape: {len(tracks)} tracks from '{playlist_name}'")
    result = {"tracks": tracks, "playlist_name": playlist_name, "count": len(tracks)}

    # Apple server-renders at most ~300 tracks and its own trackCount in that
    # blob is capped to match, so a long playlist looks complete and is not.
    # If we came back holding suspiciously close to the cap, say so rather than
    # let somebody discover the missing 200 songs themselves months later.
    # No token advice here: public playlists need none, and suggesting one sent
    # people off hunting for something that could not help.
    if len(tracks) >= _HTML_SCRAPE_CAP - 5:
        result["warning"] = (
            f"Apple only server-rendered {len(tracks)} tracks and the API path failed, "
            f"so this list is probably truncated. Try again later; if it keeps "
            f"happening, Apple has moved the furniture and it is worth reporting."
        )
    return result


def _fetch_html(url: str) -> str:
    return _fetch_text(url)


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Apple Music returned HTTP {exc.code}. "
                "The playlist may be private or region-locked."
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch Apple Music page: {exc}"
        )


def _fetch_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req_headers = {**_HEADERS, **(headers or {})}
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"Apple amp-api returned HTTP {exc.code}: {body[:200]}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Apple amp-api response: {exc}") from exc


def _extract_playlist_name(html: str) -> str:
    plain_title = re.search(r'<meta name="apple:title" content="([^"]+)"', html)
    if plain_title:
        return plain_title.group(1).strip()

    og_title = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    playlist_name = og_title.group(1).strip() if og_title else "Apple Music Playlist"
    return re.sub(r"\s+on Apple Music$", "", playlist_name, flags=re.IGNORECASE).strip()


def _extract_tracks_from_html(html: str) -> list[str]:
    blob_match = re.search(
        r'id="serialized-server-data"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not blob_match:
        return []
    try:
        data = json.loads(blob_match.group(1))
    except json.JSONDecodeError:
        return []
    return _walk_for_tracks(data)


def _walk_for_tracks(obj, _seen=None, _depth=0) -> list[str]:
    """Recursively walk the server data and collect unique Artist - Title strings."""
    if _seen is None:
        _seen = []
    if _depth > 20:
        return _seen

    if isinstance(obj, dict):
        artist = obj.get("artistName")
        title = obj.get("title")
        if isinstance(artist, str) and isinstance(title, str):
            artist = artist.strip()
            title = title.strip()
            if artist and title:
                entry = f"{artist} - {title}"
                if entry not in _seen:
                    _seen.append(entry)
        for v in obj.values():
            _walk_for_tracks(v, _seen, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_tracks(item, _seen, _depth + 1)

    return _seen
