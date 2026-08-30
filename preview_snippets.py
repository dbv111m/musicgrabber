"""Short-lived, decrypted preview snippets.

Monochrome's unified playback only ever hands back CENC-encrypted FLAC-in-MP4,
whatever quality you ask it for, and no browser will play a note of that.
Downloads already solve this with ffmpeg and an in-memory key; previews get the
same treatment on a much smaller scale. The opening seconds are decrypted and
transcoded to MP3, parked in a temp directory under an unguessable token, and
served back through MusicGrabber rather than the CDN.

Kept deliberately light on imports: the auth middleware resolves these tokens
on every snippet request and has no business dragging the whole Monochrome
stack along for the ride.
"""

import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from constants import (
    PREVIEW_SNIPPET_MAX_CACHED,
    PREVIEW_SNIPPET_SECONDS,
    PREVIEW_SNIPPET_TTL,
    TIMEOUT_PREVIEW_SNIPPET_BUILD,
)

_SNIPPET_DIR = Path(tempfile.gettempdir()) / "musicgrabber-previews"
_snippets: dict[str, dict] = {}
_lock = threading.Lock()


def _discard_locked(token: str) -> None:
    snippet = _snippets.pop(token, None)
    if snippet:
        snippet["path"].unlink(missing_ok=True)


def _prune_locked(now: float) -> None:
    """Bin anything past its TTL, then the oldest until we're back under the cap."""
    expired = [
        token for token, snippet in _snippets.items()
        if now - snippet["created_at"] >= PREVIEW_SNIPPET_TTL
    ]
    for token in expired:
        _discard_locked(token)
    while len(_snippets) > PREVIEW_SNIPPET_MAX_CACHED:
        oldest = min(_snippets, key=lambda token: _snippets[token]["created_at"])
        _discard_locked(oldest)


def _live_snippet_locked(token: str) -> dict | None:
    snippet = _snippets.get(token)
    if not snippet:
        return None
    if time.time() - snippet["created_at"] >= PREVIEW_SNIPPET_TTL:
        _discard_locked(token)
        return None
    return snippet


def build_snippet(source_url: str, decryption_key: str, user: dict | None) -> str:
    """Decrypt the opening seconds to MP3 and return the token that fetches it.

    The token is the only way back to the file, so it is a full uuid4 rather
    than anything anyone could reasonably guess, and it stops working once the
    snippet ages out.
    """
    _SNIPPET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = _SNIPPET_DIR / f"{token}.mp3"
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-decryption_key", decryption_key,
            "-i", source_url,
            "-t", str(PREVIEW_SNIPPET_SECONDS),
            "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_PREVIEW_SNIPPET_BUILD,
    )
    if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        detail = (result.stderr or "ffmpeg produced no audio").strip()[-300:]
        raise RuntimeError(f"Preview snippet could not be prepared: {detail}")

    now = time.time()
    with _lock:
        _snippets[token] = {"path": path, "user": user, "created_at": now}
        _prune_locked(now)
    return token


def store_snippet(data: bytes, user: dict | None) -> str:
    """Park bytes a browser can already play, and hand back the token for them.

    build_snippet() above is for audio no browser will touch. This one is for
    audio it would play perfectly happily if only it were allowed to go and
    fetch it, which behind Cloudflare it is not.
    """
    _SNIPPET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = _SNIPPET_DIR / f"{token}.mp3"
    path.write_bytes(data)

    now = time.time()
    with _lock:
        _snippets[token] = {"path": path, "user": user, "created_at": now}
        _prune_locked(now)
    return token


def snippet_user(token: str) -> dict | None:
    """The user context a live token belongs to, for the auth middleware.

    An <audio> element cannot send an Authorization header, so the token in the
    URL stands in for one; it still resolves to whoever asked for the preview
    rather than throwing the endpoint open to all comers.
    """
    with _lock:
        snippet = _live_snippet_locked(token)
        return snippet["user"] if snippet else None


def snippet_path(token: str) -> Path | None:
    with _lock:
        snippet = _live_snippet_locked(token)
        return snippet["path"] if snippet else None
