"""
MusicGrabber - Source URL validation

The browser hands us a `source` and a `source_url` and asks us to go and fetch
it. Left unchecked that is a request to make the server fetch anything at all,
from inside the container: `http://127.0.0.1:8080/api/...`, the NAS admin panel
next door on the Docker network, `http://169.254.169.254/` on a cloud box. The
declared source proves nothing, since the caller writes that too.

So every URL-backed source gets a hostname allowlist, and the URL must actually
belong to the provider it claims to come from. Substring tests like
`"soundcloud.com" in url` are not a boundary; `https://soundcloud.com.evil.tld/`
and `http://10.0.0.5/?q=soundcloud.com` both sail straight through one. Hostname
suffix matching is the whole point of this module.

If a provider moves to a new CDN, downloads from it will start failing here with
a clear message rather than mysteriously; add the new host below.
"""

from urllib.parse import urlsplit

# Sources whose downloads are driven by a URL supplied with the request, mapped
# to the hostnames those URLs are allowed to have. Matching is exact-or-subdomain,
# so "soundcloud.com" covers "m.soundcloud.com" but never "soundcloud.com.evil.tld".
SOURCE_URL_HOSTS: dict[str, tuple[str, ...]] = {
    # YouTube is not URL-backed at the API (it is identified by video ID), but
    # search results carry a webpage_url and it reaches the download worker, so
    # it needs an entry here for the worker's own check to recognise it.
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    # Search returns the track permalink; yt-dlp does the rest.
    "soundcloud": ("soundcloud.com", "snd.sc"),
    # The site rebranded from zvu4no.org and serves audio off data.<host>.
    "zvu4no": ("zvu4it.org",),
    # Search runs on freemp3cloud.com, the actual MP3s come off meln.top.
    "freemp3cloud": ("freemp3cloud.com", "meln.top"),
}

# Monochrome does not use a fetchable URL at all: it uses a private
# `monochrome://<id>?isrc=...` handle which the resolution ladder turns into a
# real stream server-side. Nothing here is ever handed to a fetcher as-is.
MONOCHROME_URL_SCHEME = "monochrome://"


def _host_matches(hostname: str, allowed: tuple[str, ...]) -> bool:
    """Exact host or a subdomain of it, and nothing that merely looks similar."""
    hostname = (hostname or "").lower().rstrip(".")
    return any(
        hostname == host or hostname.endswith(f".{host}")
        for host in allowed
    )


def is_valid_source_url(source: str, url: str | None) -> bool:
    """Does this URL genuinely belong to the source that claims it?"""
    source = (source or "").strip().lower()
    url = (url or "").strip()
    if not url:
        return False

    if source == "monochrome":
        return url.startswith(MONOCHROME_URL_SCHEME)

    allowed = SOURCE_URL_HOSTS.get(source)
    if not allowed:
        return False

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    try:
        hostname = parts.hostname
    except ValueError:
        # Malformed authority (a bracketed IPv6 literal that isn't one, say).
        return False
    return _host_matches(hostname or "", allowed)


def validate_source_url(source: str, url: str | None) -> str:
    """Return the URL if it belongs to the source, otherwise complain loudly.

    Raises ValueError, which callers at the HTTP boundary turn into a 400.
    """
    if not is_valid_source_url(source, url):
        allowed = SOURCE_URL_HOSTS.get((source or "").strip().lower())
        expected = ", ".join(allowed) if allowed else "the source's own hosts"
        raise ValueError(
            f"source_url does not belong to {source or 'that source'} "
            f"(expected {expected})"
        )
    return (url or "").strip()
