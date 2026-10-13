"""Unit tests for the opt-in loudness normaliser (downloads._normalise_loudness).

Gating tests are pure and fast. The round-trip test generates a deliberately
quiet sine wave with ffmpeg, normalises it for real, and measures the result;
offline, a couple of seconds, so it stays in the fast suite.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloads
from constants import LOUDNORM_TARGET_I


def _quiet_flac(path: Path, seconds: float = 5.0) -> None:
    """A 440 Hz sine turned well down, i.e. a classic quiet YouTube rip.

    -18 dB puts the mono sine around -40 LUFS integrated: clearly too quiet,
    but comfortably above the normaliser's digital-silence guard.
    """
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-af", "volume=-18dB", "-y", str(path)],
        check=True,
    )


def test_noop_when_setting_off(tmp_path, monkeypatch):
    f = tmp_path / "track.flac"
    f.write_bytes(b"not audio, must not be touched")
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: False)
    monkeypatch.setattr(downloads, "_measure_loudness", lambda *a: pytest.fail("measured despite setting off"))
    downloads._normalise_loudness(f, "youtube")
    assert f.read_bytes() == b"not audio, must not be touched"


def test_noop_for_lossless_sources(tmp_path, monkeypatch):
    f = tmp_path / "track.flac"
    f.write_bytes(b"a proper master, hands off")
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr(downloads, "_measure_loudness", lambda *a: pytest.fail("measured a lossless source"))
    downloads._normalise_loudness(f, "monochrome")
    downloads._normalise_loudness(f, "soulseek")
    assert f.read_bytes() == b"a proper master, hands off"


def test_noop_for_unsupported_container(tmp_path, monkeypatch):
    f = tmp_path / "track.webm"
    f.write_bytes(b"webm stays webm")
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr(downloads, "_measure_loudness", lambda *a: pytest.fail("measured an unsupported container"))
    downloads._normalise_loudness(f, "youtube")
    assert f.read_bytes() == b"webm stays webm"


def test_skips_reencode_when_already_at_target(tmp_path, monkeypatch):
    f = tmp_path / "track.flac"
    f.write_bytes(b"already sitting pretty at -14")
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr(
        downloads, "_measure_loudness",
        lambda *a: {"input_i": str(LOUDNORM_TARGET_I), "input_tp": "-2.0",
                    "input_lra": "5.0", "input_thresh": "-25.0", "target_offset": "0.0"},
    )
    downloads._normalise_loudness(f, "youtube")
    assert f.read_bytes() == b"already sitting pretty at -14"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_round_trip_normalises_quiet_flac(tmp_path, monkeypatch):
    f = tmp_path / "quiet.flac"
    _quiet_flac(f)

    before = downloads._measure_loudness(f)
    assert before is not None
    assert float(before["input_i"]) < LOUDNORM_TARGET_I - 5  # confirm it starts quiet

    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    downloads._normalise_loudness(f, "youtube")

    after = downloads._measure_loudness(f)
    assert after is not None
    # Short synthetic clips wobble a little; within 2 LU of target is a pass
    assert abs(float(after["input_i"]) - LOUDNORM_TARGET_I) <= 2.0
    # And the sample rate stayed put (no sneaky 192 kHz upsample)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(f)],
        capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "44100"
