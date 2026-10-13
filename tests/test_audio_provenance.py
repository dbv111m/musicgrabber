"""Tests for the deliberately conservative, read-only provenance audit."""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import audio_provenance as audit
from audio_probe import probe_file


def _info(codec, **extra):
    return {
        "codec": codec,
        "container": extra.pop("container", codec),
        "bitrate_kbps": extra.pop("bitrate_kbps", 0),
        "duration": 180,
        "sample_rate_hz": 44100,
        "bits_per_sample": 16 if codec == "flac" else 0,
        "channels": 2,
        "source": extra.pop("source", "musicgrabber-test"),
        "source_quality": extra.pop("source_quality", None),
        "source_codec": extra.pop("source_codec", None),
        "source_bitrate_kbps": extra.pop("source_bitrate_kbps", 0),
        "file_id": None,
        "artist": "Artist",
        "title": "Title",
        **extra,
    }


@pytest.mark.parametrize(
    "info,classification,effective",
    [
        (
            _info("flac", source_codec="mp3", source_bitrate_kbps=128),
            "known_lossy_transcode",
            "lossy_128",
        ),
        (
            _info("mp3", bitrate_kbps=320, source_codec="mp3", source_bitrate_kbps=320),
            "native_lossy",
            "lossy_320",
        ),
        (
            _info("opus", bitrate_kbps=256, source_codec="flac"),
            "lossy_derivative",
            "lossy_256",
        ),
        (
            _info("flac", source_codec="flac"),
            "recorded_lossless",
            "recorded_lossless",
        ),
        (
            _info("flac", source=None),
            "historical_unknown",
            "unknown",
        ),
    ],
)
def test_provenance_matrix(info, classification, effective):
    result = audit.classify_provenance(info)
    assert result["classification"] == classification
    assert result["effective_quality"] == effective
    assert result["evidence"]
    assert result["caveats"]


def test_untagged_flac_is_never_promoted_to_lossless():
    result = audit.classify_provenance(
        _info("flac", bitrate_kbps=922, source=None, source_quality=None)
    )
    assert result["classification"] == "historical_unknown"
    assert result["effective_quality"] == "unknown"
    assert "genuinely lossless" in result["caveats"][0]


def test_legacy_from_tag_records_lossy_origin():
    result = audit.classify_provenance(
        _info(
            "flac",
            source="youtube",
            source_quality="FLAC (from OPUS 130kbps)",
        )
    )
    assert result["classification"] == "known_lossy_transcode"
    assert result["origin_codec"] == "opus"
    assert result["origin_bitrate_kbps"] == 130
    assert any(item["kind"] == "legacy_metadata" for item in result["evidence"])


def test_lossy_file_without_provenance_has_only_an_upper_bound():
    result = audit.classify_provenance(
        _info("mp3", bitrate_kbps=320, source=None, source_quality=None)
    )
    assert result["classification"] == "historical_unknown"
    assert result["effective_quality"] == "lossy_320"


_FFMPEG = shutil.which("ffmpeg")


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not available")
def test_probe_is_byte_for_byte_read_only(tmp_path):
    path = tmp_path / "do-not-touch.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_mtime = path.stat().st_mtime_ns
    assert probe_file(path) is not None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert path.stat().st_mtime_ns == before_mtime


def test_interactive_library_read_pauses_audit_probe(monkeypatch, tmp_path):
    priority_started = threading.Event()
    release_priority = threading.Event()
    probe_finished = threading.Event()

    @audit.prioritise_over_audio_audit
    def interactive_read():
        priority_started.set()
        release_priority.wait(timeout=2)

    interactive_thread = threading.Thread(target=interactive_read)
    interactive_thread.start()
    assert priority_started.wait(timeout=1)

    monkeypatch.setattr(audit, "probe_file", lambda path: {"codec": "flac"})
    monkeypatch.setattr(audit, "PROBE_YIELD_SECONDS", 0)

    def probe():
        audit._polite_probe(tmp_path / "track.flac")
        probe_finished.set()

    probe_thread = threading.Thread(target=probe)
    probe_thread.start()
    time.sleep(0.05)
    assert not probe_finished.is_set()

    release_priority.set()
    interactive_thread.join(timeout=1)
    probe_thread.join(timeout=1)
    assert probe_finished.is_set()


_AUDIT_SCHEMA = """
CREATE TABLE audio_audit_runs (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT '', music_root TEXT NOT NULL,
    criteria_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
    is_current INTEGER NOT NULL DEFAULT 0, total_files INTEGER NOT NULL DEFAULT 0,
    scanned_files INTEGER NOT NULL DEFAULT 0, classified_files INTEGER NOT NULL DEFAULT 0,
    unreadable_files INTEGER NOT NULL DEFAULT 0, excluded_paths INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP, error TEXT
);
CREATE TABLE audio_audit_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL, filename TEXT NOT NULL,
    file_size INTEGER, mtime REAL, container TEXT, codec TEXT, bitrate_kbps INTEGER,
    duration REAL, sample_rate_hz INTEGER, bits_per_sample INTEGER, channels INTEGER,
    source TEXT, source_quality TEXT, source_codec TEXT, source_bitrate_kbps INTEGER,
    file_id TEXT, artist TEXT, title TEXT, stored_quality TEXT NOT NULL,
    effective_quality TEXT NOT NULL, effective_tier INTEGER, classification TEXT NOT NULL,
    classification_label TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]',
    caveats_json TEXT NOT NULL DEFAULT '[]', read_error TEXT,
    UNIQUE(run_id, path)
);
"""


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_AUDIT_SCHEMA)
    conn.commit()
    conn.close()

    @contextlib.contextmanager
    def connection():
        test_conn = sqlite3.connect(db_path)
        try:
            yield test_conn
            if test_conn.in_transaction:
                test_conn.rollback()
        finally:
            test_conn.close()

    monkeypatch.setattr(audit, "db_conn", connection)
    monkeypatch.setattr(audit, "PROBE_YIELD_SECONDS", 0)
    return db_path


def test_scan_publishes_atomically_and_filters_results(
    tmp_path, monkeypatch, audit_db
):
    music = tmp_path / "music"
    music.mkdir()
    (music / "recorded.flac").write_bytes(b"one")
    (music / "transcode.flac").write_bytes(b"two")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"three")
    try:
        (music / "outside-link.mp3").symlink_to(outside)
    except OSError:
        pass

    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            """
            INSERT INTO audio_audit_runs
                (id,user_id,music_root,criteria_version,status,is_current)
            VALUES ('old','','/old','1','completed',1)
            """
        )
        conn.commit()

    observed_current = []

    def fake_probe(path):
        with sqlite3.connect(audit_db) as conn:
            row = conn.execute(
                "SELECT id FROM audio_audit_runs WHERE is_current=1"
            ).fetchone()
            observed_current.append(row[0])
        if path.name == "transcode.flac":
            return _info("flac", source_codec="mp3", source_bitrate_kbps=128)
        return _info("flac", source_codec="flac")

    monkeypatch.setattr(
        audit,
        "get_setting",
        lambda key, default="", user_id=None: (
            str(music) if key == "music_dir" else default
        ),
    )
    monkeypatch.setattr(audit, "get_trash_dir", lambda user_id=None: tmp_path / "trash")
    monkeypatch.setattr(audit, "probe_file", fake_probe)

    status = audit.run_audit_scan(None, "new")
    assert observed_current == ["old", "old"]
    assert status["current"]["id"] == "new"
    assert status["summary"]["counts"]["recorded_lossless"] == 1
    assert status["summary"]["counts"]["known_lossy_transcode"] == 1
    assert status["current"]["excluded_paths"] in (0, 1)

    filtered = audit.get_audit_files(
        None, {"classification": "known_lossy_transcode"}
    )
    assert filtered["total"] == 1
    assert filtered["items"][0]["path"] == "transcode.flac"
    assert filtered["items"][0]["evidence"]

    recorded = audit.get_audit_files(None, {"lossless_only": "recorded"})
    assert recorded["total"] == 1
    assert recorded["items"][0]["path"] == "recorded.flac"


def test_failed_scan_does_not_replace_current(tmp_path, monkeypatch, audit_db):
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            """
            INSERT INTO audio_audit_runs
                (id,user_id,music_root,criteria_version,status,is_current)
            VALUES ('trusted','','/old','1','completed',1)
            """
        )
        conn.commit()

    missing = tmp_path / "missing"
    monkeypatch.setattr(
        audit,
        "get_setting",
        lambda key, default="", user_id=None: (
            str(missing) if key == "music_dir" else default
        ),
    )

    status = audit.run_audit_scan(None, "failed")
    assert status["current"]["id"] == "trusted"
    assert status["last_failed"]["id"] == "failed"
    assert "not available" in status["last_failed"]["error"]


def test_interrupted_running_scan_is_recovered(tmp_path, monkeypatch, audit_db):
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            """
            INSERT INTO audio_audit_runs
                (id,user_id,music_root,criteria_version,status)
            VALUES ('interrupted','','/music','1','running')
            """
        )
        conn.commit()

    audit._active_users.discard("")
    status = audit.get_audit_status(None)
    assert status["running"] is None
    assert status["last_failed"]["id"] == "interrupted"
    assert "interrupted" in status["last_failed"]["error"].lower()
