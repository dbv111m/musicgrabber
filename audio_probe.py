"""Read-only audio inspection shared by upgrades and provenance auditing.

This module only opens files through Mutagen.  It never writes tags or calls
``save()``, which is an important (if rather unglamorous) part of the audit's
contract.
"""

import re
from pathlib import Path

import mutagen


AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".ape",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wave",
    ".wma",
    ".wv",
}


def _digits_to_int(value: str) -> int:
    numbers = [int(number) for number in re.findall(r"\d+", value or "")]
    return max(numbers) if numbers else 0


def _decode_tag_value(value):
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "ignore")
    return str(value)


def _read_tag(tags, vorbis_key: str, mp4_key: str, id3: str | None = None):
    """Read one tag across Vorbis, ID3/TXXX, and MP4 freeform layouts."""
    for key in (vorbis_key, vorbis_key.lower(), mp4_key):
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value:
            return _decode_tag_value(value)

    try:
        if id3:
            frame = tags.get(id3)
            if frame is not None:
                return _decode_tag_value(getattr(frame, "text", None))
        frames = tags.getall(f"TXXX:{vorbis_key}")
        if frames:
            return _decode_tag_value(getattr(frames[0], "text", None))
    except Exception:
        pass
    return None


def _container_for_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return {
        "m4a": "mp4",
        "oga": "ogg",
        "wave": "wav",
        "aif": "aiff",
        "wv": "wavpack",
    }.get(suffix, suffix or "unknown")


def probe_file(path: Path) -> dict | None:
    """Inspect an audio file without modifying it.

    ``None`` means Mutagen could not recognise/read the file.  A successful
    result deliberately reports observed properties and recorded tags only; it
    makes no claim that a lossless-looking container contains lossless-origin
    audio.
    """
    try:
        audio = mutagen.File(str(path))
    except Exception:
        return None
    if audio is None:
        return None

    info = getattr(audio, "info", None)
    tags = audio.tags

    cls = type(audio).__name__.lower()
    codec = None
    if "flac" in cls:
        codec = "flac"
    elif "mp3" in cls or "easymp3" in cls:
        codec = "mp3"
    elif "opus" in cls:
        codec = "opus"
    elif "vorbis" in cls or "oggvorbis" in cls:
        codec = "vorbis"
    elif "wave" in cls or cls == "wav":
        codec = "wav"
    elif "aiff" in cls:
        codec = "aiff"
    elif "monkey" in cls:
        codec = "ape"
    elif "wavpack" in cls:
        codec = "wavpack"
    elif "mp4" in cls or "m4a" in cls:
        mp4_codec = (getattr(info, "codec", "") or "").lower()
        codec = "alac" if "alac" in mp4_codec else "aac"
    else:
        codec = cls or None

    source = source_quality = title = artist = album = album_artist = file_id = source_codec = source_bitrate = None
    if tags is not None:
        source = _read_tag(tags, "SOURCE", "----:com.musicgrabber:SOURCE")
        source_quality = _read_tag(
            tags, "SOURCE_QUALITY", "----:com.musicgrabber:SOURCE_QUALITY"
        )
        title = _read_tag(tags, "TITLE", "\xa9nam", id3="TIT2")
        artist = _read_tag(tags, "ARTIST", "\xa9ART", id3="TPE1")
        album = _read_tag(tags, "ALBUM", "\xa9alb", id3="TALB")
        album_artist = _read_tag(tags, "ALBUMARTIST", "aART", id3="TPE2")
        file_id = _read_tag(
            tags, "MUSICGRABBER_FILE_ID", "----:com.musicgrabber:FILE_ID"
        )
        source_codec = _read_tag(
            tags, "SOURCE_CODEC", "----:com.musicgrabber:SOURCE_CODEC"
        )
        source_bitrate = _read_tag(
            tags, "SOURCE_BITRATE", "----:com.musicgrabber:SOURCE_BITRATE"
        )

    return {
        "container": _container_for_path(path),
        "codec": codec,
        "bitrate_kbps": int((getattr(info, "bitrate", 0) or 0) / 1000),
        "duration": float(getattr(info, "length", 0) or 0),
        "sample_rate_hz": int(getattr(info, "sample_rate", 0) or 0),
        "bits_per_sample": int(
            (
                getattr(info, "bits_per_sample", 0)
                or getattr(info, "bit_depth", 0)
                or 0
            )
        ),
        "channels": int(getattr(info, "channels", 0) or 0),
        "source": source,
        "source_quality": source_quality,
        "artist": artist,
        "title": title,
        "album": album,
        "album_artist": album_artist,
        "file_id": file_id,
        "source_codec": source_codec,
        "source_bitrate_kbps": _digits_to_int(source_bitrate or ""),
    }
