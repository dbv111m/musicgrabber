"""Unit tests for opt-in ReplayGain tagging.

ReplayGain writes numbers and touches nothing else, so most of these are tag
round-trips across the four container families that have somewhere to put them.
The measurement and album-gain tests generate real audio with ffmpeg; still
offline, still a couple of seconds, so they stay in the fast suite.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloads
import metadata
from constants import REPLAYGAIN_REFERENCE_LUFS


def _tone(path: Path, freq: int = 440, gain_db: float = -12.0, seconds: float = 4.0) -> None:
    """A sine wave at a chosen level, standing in for a track."""
    codec = {
        ".flac": ["-c:a", "flac"],
        ".mp3": ["-c:a", "libmp3lame"],
        ".m4a": ["-c:a", "alac"],
        ".opus": ["-c:a", "libopus"],
        ".ogg": ["-c:a", "libvorbis"],
    }[path.suffix.lower()]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-af", f"volume={gain_db}dB", *codec, "-y", str(path)],
        check=True,
    )


@pytest.fixture(autouse=True)
def _no_db_chmod(monkeypatch):
    """set_file_permissions reads a DB setting; irrelevant here, and /data is
    not mounted outside the container. Stub it so tests stay hermetic."""
    monkeypatch.setattr(metadata, "set_file_permissions", lambda p: None)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_gain_is_written_with_an_explicit_sign():
    assert metadata._format_gain(-3.456) == "-3.46 dB"
    assert metadata._format_gain(9.75) == "+9.75 dB"
    assert metadata._format_gain(0) == "+0.00 dB"


def test_peak_is_written_as_a_linear_value():
    assert metadata._format_peak(0.9876543) == "0.987654"
    assert metadata._format_peak(-0.5) == "0.000000"  # Negatives are nonsense


# ---------------------------------------------------------------------------
# Tag round-trips, one per container family
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", [".flac", ".mp3", ".m4a", ".opus", ".ogg"])
def test_replaygain_round_trip(tmp_path, ext):
    f = tmp_path / f"track{ext}"
    _tone(f)

    assert metadata.apply_replaygain_tags(
        f, track_gain_db=-4.25, track_peak=0.812345,
        album_gain_db=-3.5, album_peak=0.95,
        reference_lufs=REPLAYGAIN_REFERENCE_LUFS,
    )

    tags = metadata.read_replaygain_tags(f)
    assert tags["replaygain_track_gain"] == "-4.25 dB"
    assert tags["replaygain_track_peak"] == "0.812345"
    assert tags["replaygain_album_gain"] == "-3.50 dB"
    assert tags["replaygain_album_peak"] == "0.950000"
    assert tags["replaygain_reference_loudness"] == "-18.00 LUFS"


def test_existing_tags_are_preserved_by_default(tmp_path):
    f = tmp_path / "track.flac"
    _tone(f)
    metadata.apply_replaygain_tags(f, track_gain_db=-4.0, track_peak=0.5)

    # Somebody else's carefully calculated numbers: leave them be.
    assert metadata.apply_replaygain_tags(f, track_gain_db=-99.0, track_peak=0.1) is False
    assert metadata.read_replaygain_tags(f)["replaygain_track_gain"] == "-4.00 dB"


def test_existing_tags_are_overwritten_when_asked(tmp_path):
    f = tmp_path / "track.flac"
    _tone(f)
    metadata.apply_replaygain_tags(f, track_gain_db=-4.0, track_peak=0.5)

    assert metadata.apply_replaygain_tags(
        f, track_gain_db=-9.0, track_peak=0.25, replace_existing=True
    ) is True
    tags = metadata.read_replaygain_tags(f)
    assert tags["replaygain_track_gain"] == "-9.00 dB"
    assert tags["replaygain_track_peak"] == "0.250000"


def test_partial_write_leaves_other_tags_alone(tmp_path):
    """Album values land later than track values; they must not wipe them."""
    f = tmp_path / "track.flac"
    _tone(f)
    metadata.apply_replaygain_tags(f, track_gain_db=-4.0, track_peak=0.5)
    metadata.apply_replaygain_tags(f, album_gain_db=-3.0, album_peak=0.9, replace_existing=True)

    tags = metadata.read_replaygain_tags(f)
    assert tags["replaygain_track_gain"] == "-4.00 dB"
    assert tags["replaygain_album_gain"] == "-3.00 dB"


def test_vorbis_comment_keys_are_not_duplicated(tmp_path):
    """Vorbis comments allow repeated keys; two answers is one too many."""
    from mutagen.flac import FLAC
    f = tmp_path / "track.flac"
    _tone(f)
    audio = FLAC(str(f))
    audio["replaygain_track_gain"] = "-1.00 dB"  # lowercase, as some taggers write
    audio.save()

    metadata.apply_replaygain_tags(f, track_gain_db=-7.0, replace_existing=True)

    audio = FLAC(str(f))
    matches = [k for k in audio.keys() if k.lower() == "replaygain_track_gain"]
    assert len(matches) == 1
    assert audio[matches[0]] == ["-7.00 dB"]


def test_unsupported_container_is_skipped_not_fatal(tmp_path):
    f = tmp_path / "track.webm"
    f.write_bytes(b"not really a webm")
    assert metadata.apply_replaygain_tags(f, track_gain_db=-4.0) is False
    assert metadata.read_replaygain_tags(f) == {}


def test_nothing_to_write_is_a_no_op(tmp_path):
    f = tmp_path / "track.flac"
    _tone(f)
    assert metadata.apply_replaygain_tags(f) is False


def test_read_of_an_untagged_file_is_empty(tmp_path):
    f = tmp_path / "track.flac"
    _tone(f)
    assert metadata.read_replaygain_tags(f) == {}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def test_measure_returns_gain_relative_to_the_reference(tmp_path):
    f = tmp_path / "quiet.flac"
    _tone(f, gain_db=-20.0)

    measured = downloads._measure_replaygain(f)
    assert measured is not None
    gain, peak, lufs = measured
    # Deliberately quiet, so it needs turning up.
    assert lufs < REPLAYGAIN_REFERENCE_LUFS
    assert gain == pytest.approx(REPLAYGAIN_REFERENCE_LUFS - lufs, abs=0.01)
    assert 0 < peak < 1


def test_measure_gives_up_on_silence(tmp_path, monkeypatch):
    f = tmp_path / "silence.flac"
    monkeypatch.setattr(
        downloads, "_measure_loudness",
        lambda p: {"input_i": "-91.0", "input_tp": "-99.0"},
    )
    assert downloads._measure_replaygain(f) is None


def test_measure_survives_ffmpeg_saying_nothing(tmp_path, monkeypatch):
    f = tmp_path / "track.flac"
    monkeypatch.setattr(downloads, "_measure_loudness", lambda p: None)
    assert downloads._measure_replaygain(f) is None


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_noop_when_setting_off(tmp_path, monkeypatch):
    f = tmp_path / "track.flac"
    f.write_bytes(b"must not be touched")
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: False)
    monkeypatch.setattr(
        downloads, "_measure_replaygain",
        lambda *a: pytest.fail("measured despite the setting being off"),
    )
    downloads._apply_replaygain(f)
    assert f.read_bytes() == b"must not be touched"


def test_noop_for_containers_with_nowhere_to_put_tags(tmp_path, monkeypatch):
    f = tmp_path / "track.webm"
    f.write_bytes(b"webm has no tag home here")
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr(
        downloads, "_measure_replaygain",
        lambda *a: pytest.fail("measured an untaggable container"),
    )
    downloads._apply_replaygain(f)


def test_existing_track_gain_skips_measurement_entirely(tmp_path, monkeypatch):
    """Not just "do not write": do not spend an ffmpeg pass finding out."""
    f = tmp_path / "track.flac"
    _tone(f)
    metadata.apply_replaygain_tags(f, track_gain_db=-4.0, track_peak=0.5)

    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: key == "enable_replaygain",
    )
    monkeypatch.setattr(
        downloads, "_measure_replaygain",
        lambda *a: pytest.fail("re-measured a file that already had tags"),
    )
    downloads._apply_replaygain(f)
    assert metadata.read_replaygain_tags(f)["replaygain_track_gain"] == "-4.00 dB"


def test_measures_and_tags_when_enabled(tmp_path, monkeypatch):
    f = tmp_path / "track.flac"
    _tone(f, gain_db=-20.0)
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: key == "enable_replaygain",
    )
    downloads._apply_replaygain(f)

    tags = metadata.read_replaygain_tags(f)
    assert "replaygain_track_gain" in tags
    assert tags["replaygain_reference_loudness"] == "-18.00 LUFS"


# ---------------------------------------------------------------------------
# Album gain
# ---------------------------------------------------------------------------

def _album_of(tmp_path, levels: list[float]) -> Path:
    album = tmp_path / "Album"
    album.mkdir()
    for index, level in enumerate(levels):
        _tone(album / f"{index + 1:02d} track.flac", freq=330 + index * 55, gain_db=level)
    return album


def test_album_gain_sits_between_the_loud_and_quiet_tracks(tmp_path, monkeypatch):
    """The point of album gain: relative dynamics survive, tracks are not levelled."""
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: key == "enable_replaygain",
    )
    album = _album_of(tmp_path, [-6.0, -12.0, -24.0])
    for track in sorted(album.iterdir()):
        downloads._apply_replaygain(track)

    downloads._apply_album_replaygain(str(album))

    gains, albums = [], set()
    for track in sorted(album.iterdir()):
        tags = metadata.read_replaygain_tags(track)
        gains.append(float(tags["replaygain_track_gain"].split()[0]))
        albums.add(tags["replaygain_album_gain"])

    assert len(albums) == 1, "every track on the album shares one album gain"
    album_gain = float(albums.pop().split()[0])
    assert min(gains) < album_gain < max(gains)


def test_album_pass_waits_for_every_track(tmp_path, monkeypatch):
    """A half-finished album gets no album gain; the numbers would be wrong."""
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: key == "enable_replaygain",
    )
    album = _album_of(tmp_path, [-6.0, -12.0, -24.0])
    tracks = sorted(album.iterdir())
    downloads._apply_replaygain(tracks[0])  # Only one has landed so far

    downloads._apply_album_replaygain(str(album))

    assert "replaygain_album_gain" not in metadata.read_replaygain_tags(tracks[0])


def test_album_pass_ignores_a_lone_track(tmp_path, monkeypatch):
    """One file in a folder is a single, not an album."""
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: key == "enable_replaygain",
    )
    album = _album_of(tmp_path, [-12.0])
    downloads._apply_replaygain(sorted(album.iterdir())[0])

    downloads._apply_album_replaygain(str(album))

    tags = metadata.read_replaygain_tags(sorted(album.iterdir())[0])
    assert "replaygain_track_gain" in tags
    assert "replaygain_album_gain" not in tags


def test_album_pass_is_off_when_the_setting_is(tmp_path, monkeypatch):
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: False)
    album = _album_of(tmp_path, [-6.0, -12.0])
    downloads._apply_album_replaygain(str(album))
    for track in album.iterdir():
        assert metadata.read_replaygain_tags(track) == {}


def test_album_pass_tolerates_a_missing_folder(monkeypatch):
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    downloads._apply_album_replaygain("/nowhere/in/particular")
    downloads._apply_album_replaygain(None)
    downloads._apply_album_replaygain("")
