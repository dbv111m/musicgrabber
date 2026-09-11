"""
Unit tests for the track-upgrades scan (Phase 1).

Pure unit tests, no running MusicGrabber and no DB needed. The tier logic is
pure; target_tier reads settings, so we monkeypatch the setting getters; the
tag round-trip generates tiny real audio files with ffmpeg and checks our
SOURCE/SOURCE_QUALITY markers survive apply_metadata_to_file -> probe_file.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import upgrades
from upgrades import (
    tier_of,
    target_tier,
    upgrade_output_profile,
    probe_file,
    _kbps_to_tier,
    _setting_to_kbps,
    _estimate_result_tier,
    effective_tier,
    content_sha256,
    list_quarantine,
    restore_from_quarantine,
    delete_quarantine_entry,
    delete_orphaned_quarantine_file,
    TIER_LOSSLESS,
    TIER_LOSSY_320,
    TIER_LOSSY_256,
    TIER_LOSSY_192,
    TIER_LOSSY_128,
)
from metadata import apply_metadata_to_file


def test_upgrade_scan_yields_to_duplicate_safety_index(monkeypatch):
    index_states = iter((True, False))
    sleeps = []
    monkeypatch.setattr(upgrades, "get_setting_bool", lambda *a, **kw: True)
    monkeypatch.setattr(
        upgrades, "library_tag_index_in_progress", lambda *a, **kw: next(index_states)
    )
    monkeypatch.setattr(upgrades.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert upgrades._wait_for_duplicate_scan_priority("alice") is True
    assert sleeps == [0.25]


def test_upgrade_scan_honours_toggle_while_waiting(monkeypatch):
    enabled_states = iter((True, False))
    sleeps = []
    monkeypatch.setattr(
        upgrades, "get_setting_bool", lambda *a, **kw: next(enabled_states)
    )
    monkeypatch.setattr(
        upgrades, "library_tag_index_in_progress", lambda *a, **kw: True
    )
    monkeypatch.setattr(upgrades.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert upgrades._wait_for_duplicate_scan_priority("alice") is False
    assert sleeps == [0.25]


# --- tier_of -----------------------------------------------------------------

@pytest.mark.parametrize("codec", ["flac", "FLAC", "alac", "wav", "wavpack"])
def test_lossless_codecs_are_top_tier(codec):
    assert tier_of(codec, 0) == TIER_LOSSLESS


@pytest.mark.parametrize("bitrate,expected", [
    (320, TIER_LOSSY_320),
    (300, TIER_LOSSY_320),
    (299, TIER_LOSSY_256),
    (256, TIER_LOSSY_256),
    (245, TIER_LOSSY_256),
    (240, TIER_LOSSY_256),
    (239, TIER_LOSSY_192),
    (192, TIER_LOSSY_192),
    (190, TIER_LOSSY_192),
    (170, TIER_LOSSY_192),
    (169, TIER_LOSSY_128),
    (128, TIER_LOSSY_128),
    (64, TIER_LOSSY_128),
])
def test_lossy_bitrate_buckets(bitrate, expected):
    assert tier_of("mp3", bitrate) == expected
    assert _kbps_to_tier(bitrate) == expected


def test_lossless_outranks_every_lossy():
    for br in (128, 192, 256, 320):
        assert tier_of("flac", 0) > tier_of("mp3", br)


# --- _setting_to_kbps --------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("v0", 245),
    ("v2", 190),
    ("320k", 320),
    ("256", 256),
    ("192k", 192),
    ("lossless", None),
    ("0", None),
    ("", None),
])
def test_setting_to_kbps(value, expected):
    assert _setting_to_kbps(value) == expected


# --- target_tier (settings-derived, no separate knob) ------------------------

def _patch_settings(monkeypatch, values):
    monkeypatch.setattr(upgrades, "get_setting_bool",
                        lambda k, d=False, user_id=None: values.get(k, d))
    monkeypatch.setattr(upgrades, "get_setting",
                        lambda k, d="", user_id=None: values.get(k, d))


def test_target_tier_conversion_uses_selected_format(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": True,
                                  "audio_format": "opus", "opus_bitrate": "256k"})
    assert target_tier() == TIER_LOSSY_256


def test_target_tier_flac_format_is_lossless(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": False, "audio_format": "flac"})
    assert target_tier() == TIER_LOSSLESS


def test_target_tier_alac_is_lossless(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": False, "audio_format": "alac",
                                  "alac_bitrate": "lossless"})
    assert target_tier() == TIER_LOSSLESS


def test_target_tier_mp3_320(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": False, "audio_format": "mp3",
                                  "mp3_bitrate": "320"})
    assert target_tier() == TIER_LOSSY_320


def test_target_tier_mp3_v2_default(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": False, "audio_format": "mp3",
                                  "mp3_bitrate": "v2"})
    assert target_tier() == TIER_LOSSY_192


def test_target_tier_opus_320(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": False, "audio_format": "opus",
                                  "opus_bitrate": "320k"})
    assert target_tier() == TIER_LOSSY_320


def test_upgrade_output_profile_makes_lossy_conversion_explicit(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": True,
                                  "audio_format": "opus", "opus_bitrate": "256k"})
    assert upgrade_output_profile() == {
        "upgrade_conversion_enabled": True,
        "upgrade_output_format": "opus",
        "upgrade_output_label": "Opus 256 kbps",
    }


def test_upgrade_output_profile_describes_mp3_vbr(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": True,
                                  "audio_format": "mp3", "mp3_bitrate": "v2"})
    assert upgrade_output_profile()["upgrade_output_label"] == "MP3 V2 (~190 kbps)"


def test_upgrade_output_profile_distinguishes_lossy_aac_from_alac(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": True,
                                  "audio_format": "alac", "alac_bitrate": "192k"})
    assert upgrade_output_profile()["upgrade_output_label"] == "AAC 192 kbps (.m4a)"


def test_upgrade_output_profile_says_when_conversion_is_off(monkeypatch):
    _patch_settings(monkeypatch, {"default_convert_audio": False,
                                  "audio_format": "opus", "opus_bitrate": "256k"})
    assert upgrade_output_profile() == {
        "upgrade_conversion_enabled": False,
        "upgrade_output_format": None,
        "upgrade_output_label": "Keep source format",
    }


def test_below_target_flagging(monkeypatch):
    # Lossless target: a 128 MP3 is below, a FLAC is not.
    _patch_settings(monkeypatch, {"default_convert_audio": True, "audio_format": "flac"})
    tgt = target_tier()
    assert tier_of("mp3", 128) < tgt
    assert tier_of("flac", 0) >= tgt


# --- effective_tier (sees through a lossless container around lossy audio) ---

def test_effective_tier_genuine_flac_is_lossless():
    assert effective_tier("flac", 0, "FLAC 44.1kHz 16bit") == TIER_LOSSLESS
    assert effective_tier("flac", 0, None) == TIER_LOSSLESS


def test_effective_tier_flac_from_lossy_is_tiered_by_origin():
    # The crux: a YouTube grab converted to FLAC must be flagged as lossy.
    assert effective_tier("flac", 0, "FLAC (from OPUS 130kbps)") == TIER_LOSSY_128
    assert effective_tier("flac", 0, "FLAC (from MP3 320kbps)") == TIER_LOSSY_320
    assert effective_tier("alac", 0, "ALAC (from AAC 256kbps)") == TIER_LOSSY_256


def test_effective_tier_prefers_structured_lossless_provenance():
    # A stale/ambiguous display label must not demote a source explicitly recorded as FLAC.
    assert effective_tier(
        "flac", 622, "FLAC (from UNKNOWN 622kbps)", "FLAC", 622
    ) == TIER_LOSSLESS


def test_effective_tier_uses_structured_lossy_provenance():
    assert effective_tier(
        "flac", 622, "FLAC 44.1kHz 16bit", "MP3", 128
    ) == TIER_LOSSY_128


def test_effective_tier_lossy_transcode_is_capped_by_input_and_output():
    assert effective_tier(
        "opus", 256, "OPUS 256kbps (from MP3 128kbps)", "MP3", 128
    ) == TIER_LOSSY_128
    assert effective_tier(
        "opus", 128, "OPUS 128kbps (from MP3 320kbps)", "MP3", 320
    ) == TIER_LOSSY_128


def test_effective_tier_flac_from_lossy_no_bitrate_defaults_mid():
    assert effective_tier("flac", 0, "FLAC (from AAC)") == TIER_LOSSY_192


def test_effective_tier_lossy_falls_back_to_source_quality_kbps():
    # Opus often reports bitrate 0 via mutagen; lean on SOURCE_QUALITY.
    assert effective_tier("opus", 0, "OPUS 130kbps") == TIER_LOSSY_128
    assert effective_tier("mp3", 320, "MP3 320kbps") == TIER_LOSSY_320


# --- _estimate_result_tier (verified vs needs-download) ----------------------

def test_estimate_monochrome_lossless_is_verified():
    tier, verified = _estimate_result_tier({"source": "monochrome", "quality": "LOSSLESS"})
    assert tier == TIER_LOSSLESS and verified is True


def test_estimate_monochrome_hires_is_verified_lossless():
    tier, verified = _estimate_result_tier({"source": "monochrome", "quality": "HI_RES_LOSSLESS"})
    assert tier == TIER_LOSSLESS and verified is True


def test_estimate_monochrome_high_is_verified_lossy():
    tier, verified = _estimate_result_tier({"source": "monochrome", "quality": "HIGH"})
    assert tier == TIER_LOSSY_320 and verified is True


def test_estimate_soulseek_flac_is_lossless_but_unverified():
    tier, verified = _estimate_result_tier({"source": "soulseek", "quality": "FLAC"})
    assert tier == TIER_LOSSLESS and verified is False


def test_estimate_soulseek_bitrate_parsed():
    tier, verified = _estimate_result_tier({"source": "soulseek", "quality": "320 kbps MP3"})
    assert tier == TIER_LOSSY_320 and verified is False


def test_estimate_youtube_low_and_unverified():
    tier, verified = _estimate_result_tier({"source": "youtube", "quality": None})
    assert tier == TIER_LOSSY_192 and verified is False


# --- tag round-trip (SOURCE eligibility marker survives, all formats) --------

_FFMPEG = shutil.which("ffmpeg")

_FORMATS = [
    ("flac", ["-c:a", "flac"], "flac"),
    ("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"], "mp3"),
    ("m4a", ["-c:a", "aac", "-b:a", "256k"], "aac"),
    ("opus", ["-c:a", "libopus", "-b:a", "128k"], "opus"),
    ("ogg", ["-c:a", "libvorbis", "-q:a", "3"], "vorbis"),
]


def _gen(path, args):
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2"] + args + [str(path)],
        check=True,
    )


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not available")
@pytest.mark.parametrize("ext,args,expected_codec", _FORMATS)
def test_source_tag_roundtrip(tmp_path, ext, args, expected_codec):
    p = tmp_path / f"track.{ext}"
    _gen(p, args)
    apply_metadata_to_file(p, "Artist X", "Title Y",
                           source="monochrome", source_quality="FLAC 44.1kHz 16bit")
    info = probe_file(p)
    assert info is not None
    assert info["source"] == "monochrome"
    assert info["artist"] == "Artist X"
    assert info["title"] == "Title Y"
    assert info["codec"] == expected_codec
    assert info["file_id"]
    assert info["source_codec"] == "FLAC"


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not available")
def test_untagged_file_is_ineligible(tmp_path):
    # No SOURCE tag => not ours => probe reports no source => scan skips it.
    p = tmp_path / "users_own.flac"
    _gen(p, ["-c:a", "flac"])
    info = probe_file(p)
    assert info is not None
    assert info["source"] is None


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not available")
def test_source_quality_does_not_clobber_when_not_provided(tmp_path):
    # Re-tagging without source must preserve an existing SOURCE marker.
    p = tmp_path / "track.flac"
    _gen(p, ["-c:a", "flac"])
    apply_metadata_to_file(p, "A", "B", source="youtube")
    apply_metadata_to_file(p, "A", "B Renamed")  # no source passed
    assert probe_file(p)["source"] == "youtube"


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not available")
def test_transcode_origin_tags_roundtrip(tmp_path):
    p = tmp_path / "track.opus"
    _gen(p, ["-c:a", "libopus", "-b:a", "256k"])
    apply_metadata_to_file(
        p, "A", "B", source="freemp3cloud",
        source_quality="OPUS 320kbps (from MP3 128kbps)",
    )
    info = probe_file(p)
    assert info["source_codec"] == "MP3"
    assert info["source_bitrate_kbps"] == 128
    assert effective_tier(
        info["codec"],
        info["bitrate_kbps"],
        info["source_quality"],
        info["source_codec"],
        info["source_bitrate_kbps"],
    ) == TIER_LOSSY_128


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not available")
def test_content_hash_detects_same_path_replacement(tmp_path):
    p = tmp_path / "track.flac"
    _gen(p, ["-c:a", "flac"])
    before = content_sha256(p)
    _gen(p, ["-c:a", "flac", "-af", "volume=0.5"])
    assert content_sha256(p) != before


# --- Quarantine management (restore / delete) ---------------------------------

def _setup_quarantine_db(monkeypatch):
    """In-memory sqlite with just the upgrade_quarantine table, wired to upgrades.db_conn."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE upgrade_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT '',
            quarantine_path TEXT NOT NULL,
            original_path TEXT NOT NULL,
            replaced_by_path TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            file_size INTEGER,
            quarantined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(upgrades, "db_conn", fake_db_conn)
    monkeypatch.setattr(upgrades, "scan_user_library", lambda user_id=None: None)
    return conn


def test_list_quarantine_scopes_to_the_requesting_user(monkeypatch):
    conn = _setup_quarantine_db(monkeypatch)
    conn.execute(
        "INSERT INTO upgrade_quarantine (user_id, quarantine_path, original_path, replaced_by_path, artist, title) "
        "VALUES (?,?,?,?,?,?)",
        ("alice", "/data/.upgrade_quarantine/1__a.mp3", "/music/a.mp3", "/music/a.flac", "A", "A Song"),
    )
    conn.execute(
        "INSERT INTO upgrade_quarantine (user_id, quarantine_path, original_path, replaced_by_path, artist, title) "
        "VALUES (?,?,?,?,?,?)",
        ("bob", "/data/.upgrade_quarantine/2__b.mp3", "/music/b.mp3", "/music/b.flac", "B", "B Song"),
    )
    conn.commit()

    alice_items = list_quarantine("alice")
    assert len(alice_items) == 1
    assert alice_items[0]["artist"] == "A"

    admin_items = list_quarantine("alice", is_admin=True)
    assert {i["artist"] for i in admin_items} == {"A", "B"}


def test_list_quarantine_admin_surfaces_untracked_files_as_delete_only(monkeypatch, tmp_path):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    (qdir / "20260101000000__Orphan Track.mp3").write_bytes(b"data")
    _setup_quarantine_db(monkeypatch)

    assert list_quarantine("alice") == []

    admin_items = list_quarantine("alice", is_admin=True)
    assert len(admin_items) == 1
    assert admin_items[0]["restorable"] is False
    assert admin_items[0]["title"] == "Orphan Track.mp3"


def test_restore_from_quarantine_swaps_files_and_bumps_the_replacement(monkeypatch, tmp_path):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    conn = _setup_quarantine_db(monkeypatch)

    original = tmp_path / "music" / "Artist" / "Song.mp3"
    original.parent.mkdir(parents=True)
    replaced_by = tmp_path / "music" / "Artist" / "Song.flac"
    quarantined = qdir / "20260101000000__Song.mp3"
    quarantined.write_bytes(b"old mp3 bytes")
    replaced_by.write_bytes(b"new flac bytes")

    conn.execute(
        "INSERT INTO upgrade_quarantine (user_id, quarantine_path, original_path, replaced_by_path, artist, title) "
        "VALUES (?,?,?,?,?,?)",
        ("alice", str(quarantined), str(original), str(replaced_by), "Artist", "Song"),
    )
    conn.commit()
    entry_id = conn.execute("SELECT id FROM upgrade_quarantine").fetchone()["id"]

    result = restore_from_quarantine("alice", entry_id)

    assert result["status"] == "restored"
    assert original.exists() and original.read_bytes() == b"old mp3 bytes"
    assert not replaced_by.exists()
    assert not quarantined.exists()

    rows = conn.execute("SELECT * FROM upgrade_quarantine").fetchall()
    assert len(rows) == 1, "the restored row should be gone, replaced by one for the bumped file"
    bumped = rows[0]
    assert bumped["original_path"] == str(replaced_by)
    assert Path(bumped["quarantine_path"]).read_bytes() == b"new flac bytes"


def test_restore_from_quarantine_rejects_other_users_entry(monkeypatch, tmp_path):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    conn = _setup_quarantine_db(monkeypatch)
    conn.execute(
        "INSERT INTO upgrade_quarantine (user_id, quarantine_path, original_path, replaced_by_path) "
        "VALUES (?,?,?,?)",
        ("alice", str(qdir / "x.mp3"), str(tmp_path / "x.mp3"), str(tmp_path / "x.flac")),
    )
    conn.commit()
    entry_id = conn.execute("SELECT id FROM upgrade_quarantine").fetchone()["id"]

    result = restore_from_quarantine("bob", entry_id)
    assert result["status"] == "error"
    assert result["reason"] == "Quarantine entry not found"


def test_restore_from_quarantine_cleans_up_row_when_file_already_gone(monkeypatch, tmp_path):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    conn = _setup_quarantine_db(monkeypatch)
    conn.execute(
        "INSERT INTO upgrade_quarantine (user_id, quarantine_path, original_path, replaced_by_path) "
        "VALUES (?,?,?,?)",
        ("alice", str(qdir / "gone.mp3"), str(tmp_path / "x.mp3"), str(tmp_path / "x.flac")),
    )
    conn.commit()
    entry_id = conn.execute("SELECT id FROM upgrade_quarantine").fetchone()["id"]

    result = restore_from_quarantine("alice", entry_id)

    assert result["status"] == "error"
    assert conn.execute("SELECT * FROM upgrade_quarantine").fetchall() == []


def test_delete_quarantine_entry_removes_file_and_row(monkeypatch, tmp_path):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    conn = _setup_quarantine_db(monkeypatch)
    f = qdir / "delete_me.mp3"
    f.write_bytes(b"junk")
    conn.execute(
        "INSERT INTO upgrade_quarantine (user_id, quarantine_path, original_path, replaced_by_path) "
        "VALUES (?,?,?,?)",
        ("alice", str(f), str(tmp_path / "x.mp3"), str(tmp_path / "x.flac")),
    )
    conn.commit()
    entry_id = conn.execute("SELECT id FROM upgrade_quarantine").fetchone()["id"]

    result = delete_quarantine_entry("alice", entry_id)

    assert result["status"] == "deleted"
    assert not f.exists()
    assert conn.execute("SELECT * FROM upgrade_quarantine").fetchall() == []


def test_delete_orphaned_quarantine_file_refuses_path_traversal(tmp_path, monkeypatch):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not delete me")

    result = delete_orphaned_quarantine_file("../secret.txt")

    assert result["status"] == "error"
    assert secret.exists()


def test_delete_orphaned_quarantine_file_deletes_a_real_orphan(tmp_path, monkeypatch):
    qdir = tmp_path / ".upgrade_quarantine"
    qdir.mkdir()
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: qdir)
    f = qdir / "Breakin' Outta Hell.opus"
    f.write_bytes(b"junk")

    result = delete_orphaned_quarantine_file(f.name)

    assert result["status"] == "deleted"
    assert not f.exists()


# --- Scan change detection (size + mtime gating) -------------------------------

_CANDIDATES_DDL = """
    CREATE TABLE upgrade_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL,
        mtime REAL,
        file_size INTEGER,
        codec TEXT,
        bitrate_kbps INTEGER,
        duration REAL,
        file_tier INTEGER,
        target_tier INTEGER,
        below_target INTEGER DEFAULT 0,
        source TEXT,
        artist TEXT,
        title TEXT,
        dismissed INTEGER DEFAULT 0,
        dismissed_mtime REAL,
        upgrade_state TEXT DEFAULT 'none',
        last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        file_id TEXT,
        content_sha256 TEXT,
        source_quality TEXT,
        source_codec TEXT,
        source_bitrate_kbps INTEGER,
        found_at TIMESTAMP,
        found_searched INTEGER DEFAULT 0,
        found_source TEXT,
        UNIQUE(user_id, path)
    );
"""


def _setup_scan(monkeypatch, tmp_path, target=TIER_LOSSLESS):
    """Wire scan_user_library to one real file, an in-memory DB, and no disk reads.

    probe_file and content_sha256 are replaced with counters that explode if the
    scan opens a file it had no business opening. That is the entire point of the
    change under test: an unchanged library should cost stat calls and nothing else.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_CANDIDATES_DDL)

    @contextmanager
    def fake_db_conn():
        yield conn

    track = tmp_path / "Fatboy Slim - Praise You.mp3"
    track.write_bytes(b"not really an mp3, but it has a size and an mtime")

    calls = {"probe": 0, "hash": 0}

    def fake_probe(path):
        calls["probe"] += 1
        return {
            "codec": "mp3", "bitrate_kbps": 128, "duration": 200.0,
            "source": "youtube", "source_quality": None, "source_codec": None,
            "source_bitrate_kbps": 0, "file_id": "fid-1",
            "artist": "Fatboy Slim", "title": "Praise You",
        }

    def fake_hash(path):
        calls["hash"] += 1
        return "deadbeef"

    monkeypatch.setattr(upgrades, "db_conn", fake_db_conn)
    monkeypatch.setattr(upgrades, "probe_file", fake_probe)
    monkeypatch.setattr(upgrades, "content_sha256", fake_hash)
    monkeypatch.setattr(upgrades, "get_setting_bool", lambda *a, **kw: True)
    monkeypatch.setattr(upgrades, "target_tier", lambda user_id=None: target)
    monkeypatch.setattr(upgrades, "get_singles_dir", lambda user_id=None: tmp_path)
    monkeypatch.setattr(upgrades, "get_playlists_dir", lambda user_id=None: None)
    monkeypatch.setattr(upgrades, "_iter_audio_files", lambda d: iter([track]))
    monkeypatch.setattr(
        upgrades, "_wait_for_duplicate_scan_priority", lambda user_id=None: True
    )
    return conn, track, calls


def _seed_cached_row(conn, track, **overrides):
    st = track.stat()
    row = {
        "user_id": "alice", "path": str(track), "mtime": st.st_mtime,
        "file_size": st.st_size, "codec": "mp3", "bitrate_kbps": 128,
        "duration": 200.0, "file_tier": TIER_LOSSY_128, "target_tier": TIER_LOSSLESS,
        "below_target": 1, "source": "youtube", "artist": "Fatboy Slim",
        "title": "Praise You", "dismissed": 0, "dismissed_mtime": None,
        "file_id": "fid-1", "content_sha256": "deadbeef", "source_quality": None,
        "source_codec": None, "source_bitrate_kbps": 0, "found_searched": 1,
        "found_source": "slskd",
    }
    row.update(overrides)
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO upgrade_candidates ({cols}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    conn.commit()


def test_scan_leaves_unchanged_files_on_disk(monkeypatch, tmp_path):
    conn, track, calls = _setup_scan(monkeypatch, tmp_path)
    _seed_cached_row(conn, track)

    summary = upgrades.scan_user_library("alice")

    assert calls == {"probe": 0, "hash": 0}
    assert summary["skipped_unchanged"] == 1
    assert summary["eligible"] == 1
    assert summary["below_target"] == 1
    assert summary["scrubbed"] == 0


def test_scan_reprobes_when_mtime_moves(monkeypatch, tmp_path):
    conn, track, calls = _setup_scan(monkeypatch, tmp_path)
    _seed_cached_row(conn, track, mtime=track.stat().st_mtime - 500)

    summary = upgrades.scan_user_library("alice")

    assert calls == {"probe": 1, "hash": 1}
    assert summary["skipped_unchanged"] == 0


def test_scan_reprobes_when_size_changes(monkeypatch, tmp_path):
    conn, track, calls = _setup_scan(monkeypatch, tmp_path)
    _seed_cached_row(conn, track, file_size=track.stat().st_size + 1)

    summary = upgrades.scan_user_library("alice")

    assert calls == {"probe": 1, "hash": 1}
    assert summary["skipped_unchanged"] == 0


def test_scan_probes_files_it_has_never_seen(monkeypatch, tmp_path):
    conn, track, calls = _setup_scan(monkeypatch, tmp_path)

    summary = upgrades.scan_user_library("alice")

    assert calls == {"probe": 1, "hash": 1}
    assert summary["skipped_unchanged"] == 0
    assert summary["eligible"] == 1


def test_scan_reranks_unchanged_file_when_target_tier_moves(monkeypatch, tmp_path):
    # The file has not budged, but the user has stopped wanting lossless. The row
    # must follow the new target without anyone opening the file to ask.
    conn, track, calls = _setup_scan(monkeypatch, tmp_path, target=TIER_LOSSY_128)
    _seed_cached_row(conn, track)

    summary = upgrades.scan_user_library("alice")

    assert calls == {"probe": 0, "hash": 0}
    assert summary["below_target"] == 0
    row = conn.execute("SELECT * FROM upgrade_candidates").fetchone()
    assert row["below_target"] == 0
    assert row["target_tier"] == TIER_LOSSY_128
    assert row["file_tier"] == TIER_LOSSY_128


def test_scan_keeps_dismissal_and_search_cache_for_unchanged_file(monkeypatch, tmp_path):
    conn, track, calls = _setup_scan(monkeypatch, tmp_path)
    _seed_cached_row(conn, track, dismissed=1, dismissed_mtime=track.stat().st_mtime)

    upgrades.scan_user_library("alice")

    row = conn.execute("SELECT * FROM upgrade_candidates").fetchone()
    assert calls["probe"] == 0
    assert row["dismissed"] == 1
    assert row["found_searched"] == 1
    assert row["found_source"] == "slskd"


def test_scan_does_not_scrub_the_row_it_skipped(monkeypatch, tmp_path):
    # An unchanged file still counts as "seen", or the prune at the end of the
    # scan would cheerfully delete the entire library's worth of candidates.
    conn, track, _ = _setup_scan(monkeypatch, tmp_path)
    _seed_cached_row(conn, track)

    upgrades.scan_user_library("alice")

    assert conn.execute("SELECT COUNT(*) FROM upgrade_candidates").fetchone()[0] == 1


def test_scan_reprobes_a_row_too_thin_to_rerank(monkeypatch, tmp_path):
    # Size and mtime match, but a row with no codec cannot be re-ranked from
    # cache, so it has to earn its keep the expensive way.
    conn, track, calls = _setup_scan(monkeypatch, tmp_path)
    _seed_cached_row(conn, track, codec=None)

    upgrades.scan_user_library("alice")

    assert calls["probe"] == 1
