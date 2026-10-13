"""Unit tests for metadata.set_playlist_comment (the macOS Music COMMENT feature).

Generates tiny real audio files with ffmpeg, so we exercise actual mutagen writes
per format rather than mocking. Skips cleanly where ffmpeg or a given encoder is
unavailable. Fast and offline; no server or network needed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from metadata import set_playlist_comment

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


def _gen(path: Path) -> None:
    """Half a second of silence in whatever format the extension implies."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "0.5", "-y", str(path)],
        check=True,
    )


def _read_comment(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".flac":
        from mutagen.flac import FLAC
        return FLAC(str(path)).get("COMMENT")
    if suffix == ".mp3":
        from mutagen.id3 import ID3
        frames = ID3(str(path)).getall("COMM")
        return [t for f in frames for t in f.text] if frames else None
    if suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4
        return MP4(str(path)).get("\xa9cmt")
    if suffix in (".ogg", ".opus"):
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        audio = OggOpus(str(path)) if suffix == ".opus" else OggVorbis(str(path))
        return audio.get("COMMENT")
    return None


@pytest.mark.parametrize("ext", [".flac", ".mp3", ".m4a", ".ogg", ".opus"])
def test_set_playlist_comment_writes_and_is_idempotent(tmp_path, ext):
    f = tmp_path / f"track{ext}"
    try:
        _gen(f)
    except Exception:
        pytest.skip(f"ffmpeg could not produce {ext} (encoder missing)")

    # Single playlist name lands in COMMENT.
    assert set_playlist_comment(f, ["My Playlist"]) is True
    assert _read_comment(f) == ["My Playlist"]

    # Multiple playlists merge with ' | ', sorted and de-duplicated.
    assert set_playlist_comment(f, ["Beta", "Alpha", "Beta"]) is True
    assert _read_comment(f) == ["Alpha | Beta"]

    # Idempotent: the same set again does not rewrite the file.
    assert set_playlist_comment(f, ["Alpha", "Beta"]) is False
    assert _read_comment(f) == ["Alpha | Beta"]

    # Nothing to write is a no-op, not a crash.
    assert set_playlist_comment(f, []) is False
    assert set_playlist_comment(f, ["  ", ""]) is False


def test_mp3_writes_id3v1_trailer(tmp_path):
    """MP3 should carry an ID3v1 tag too (belt-and-braces for older macOS Music)."""
    f = tmp_path / "track.mp3"
    try:
        _gen(f)
    except Exception:
        pytest.skip("ffmpeg could not produce mp3")
    assert set_playlist_comment(f, ["Road Trip"]) is True
    # ID3v1 sits in the final 128 bytes starting with b'TAG'.
    with open(f, "rb") as fh:
        fh.seek(-128, 2)
        assert fh.read(3) == b"TAG", "expected an ID3v1 trailer"
