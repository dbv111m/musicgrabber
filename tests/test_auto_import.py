"""Unit tests for the auto-import copy folder (downloads._copy_to_auto_import).

Pure filesystem; fast and offline.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloads


def _with_auto_import_dir(monkeypatch, value: str):
    real_get_setting = downloads.get_setting

    def fake_get_setting(key, default="", user_id=None):
        if key == "auto_import_dir":
            return value
        return real_get_setting(key, default, user_id=user_id)

    monkeypatch.setattr(downloads, "get_setting", fake_get_setting)


def test_disabled_when_setting_empty(tmp_path, monkeypatch):
    _with_auto_import_dir(monkeypatch, "")
    src = tmp_path / "track.flac"
    src.write_bytes(b"music")
    downloads._copy_to_auto_import(src)  # must simply do nothing
    assert list(tmp_path.iterdir()) == [src]


def test_copies_and_keeps_original(tmp_path, monkeypatch):
    dest = tmp_path / "auto-add"
    dest.mkdir()
    _with_auto_import_dir(monkeypatch, str(dest))
    src = tmp_path / "Artist - Song.flac"
    src.write_bytes(b"music bytes")

    downloads._copy_to_auto_import(src)

    copied = dest / src.name
    assert copied.exists() and copied.read_bytes() == b"music bytes"
    assert src.exists()  # copy, not move; the library keeps its file
    # No half-written staging file left lying about for Music to choke on
    assert not any(p.name.endswith(".importing") for p in dest.iterdir())


def test_missing_folder_is_survivable(tmp_path, monkeypatch):
    _with_auto_import_dir(monkeypatch, str(tmp_path / "not-mounted"))
    src = tmp_path / "track.flac"
    src.write_bytes(b"music")
    downloads._copy_to_auto_import(src)  # logs and moves on; no exception
    assert src.exists()


def test_missing_source_is_survivable(tmp_path, monkeypatch):
    dest = tmp_path / "auto-add"
    dest.mkdir()
    _with_auto_import_dir(monkeypatch, str(dest))
    downloads._copy_to_auto_import(Path(tmp_path / "ghost.flac"))
    downloads._copy_to_auto_import(None)
    assert list(dest.iterdir()) == []
