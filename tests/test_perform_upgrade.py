"""A harness for `perform_upgrade`, the one place that overwrites your music.

Everything else in MusicGrabber adds files. This function deliberately replaces
a file the user already had, which makes it the highest-consequence code in the
application and, until now, the only part of the upgrade feature with no test
at all: reaching the swap needs a candidate row, a staged download, a content
hash and a Chromaprint fingerprint all lined up at once.

The audio here is real, and that is the point. `probe_file`, `effective_tier`,
`content_sha256`, `apply_metadata_to_file` and `fpcalc` all run for real against
files ffmpeg generated, so the quality gates are genuinely exercised rather than
agreed with by a mock. Two properties make that possible:

* the "old" and "new" files are encoded from **one master**, so Chromaprint puts
  them at 1.0000 similarity, comfortably over the 0.65 floor;
* a pure sine wave will not do it. Chromaprint needs something that varies over
  time or it returns a degenerate 11-integer fingerprint that compares at 0.0
  against everything, including itself. `_master` therefore generates a sweep
  with harmonics, and the "different recording" file is a different sweep, which
  real Chromaprint scores at 0.5854 and correctly rejects.

Only the genuinely external things are stubbed: the download, the library scan,
the M3U rewrite and the three media-server pokes.
"""

import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import library_index
import upgrades


# ---------------------------------------------------------------------------
# Real audio
# ---------------------------------------------------------------------------

# A sweep plus harmonics. Chromaprint needs spectral change over time; a steady
# tone fingerprints to noise and compares at zero against its own copy.
_SWEEP = "0.4*sin(2*PI*(200+150*t)*t)+0.3*sin(2*PI*(330+90*t)*t)+0.2*sin(2*PI*(70+20*t)*t)"
_OTHER_SWEEP = "0.4*sin(2*PI*(900-60*t)*t)+0.3*sin(2*PI*(1500+200*t)*t)"


def _ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def _master(path: Path, expr: str = _SWEEP, seconds: float = 12.0) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"aevalsrc={expr}:d={seconds}:s=44100", "-ac", "2", str(path))
    return path


def _encode(src: Path, dest: Path, *args) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-i", str(src), *args, str(dest))
    return dest


def _tag(path: Path, artist: str, title: str) -> Path:
    from mutagen import File as MutagenFile

    audio = MutagenFile(path, easy=True)
    audio["artist"] = artist
    audio["title"] = title
    audio.save()
    return path


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE upgrade_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    mtime REAL, file_size INTEGER, codec TEXT, bitrate_kbps INTEGER, duration REAL,
    file_tier INTEGER, target_tier INTEGER, below_target INTEGER DEFAULT 0,
    source TEXT, artist TEXT, title TEXT,
    dismissed INTEGER DEFAULT 0, dismissed_mtime REAL,
    upgrade_state TEXT DEFAULT 'none',
    last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_id TEXT, content_sha256 TEXT, source_quality TEXT, source_codec TEXT,
    source_bitrate_kbps INTEGER, found_at TIMESTAMP, found_searched INTEGER DEFAULT 0,
    found_source TEXT, found_quality TEXT, found_tier INTEGER, found_confidence REAL,
    found_verified INTEGER DEFAULT 0, found_video_id TEXT, found_source_url TEXT,
    found_slskd_username TEXT, found_slskd_filename TEXT, found_slskd_size INTEGER
);
CREATE TABLE upgrade_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT '',
    quarantine_path TEXT NOT NULL, original_path TEXT NOT NULL, replaced_by_path TEXT NOT NULL,
    artist TEXT, title TEXT, file_size INTEGER,
    quarantined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, artist TEXT, title TEXT, status TEXT,
    final_path TEXT, override_dir TEXT
);
"""


class Upgrade:
    """One staged upgrade, ready to be applied, with everything it needs."""

    def __init__(self, tmp_path, conn, monkeypatch):
        self.tmp_path = tmp_path
        self.conn = conn
        self.monkeypatch = monkeypatch
        self.music = tmp_path / "music" / "Singles" / "Orbital"
        self.quarantine = tmp_path / "data" / ".upgrade_quarantine"
        self.quarantine.mkdir(parents=True)
        self.m3u_fixes = []
        self.scans = []
        self.candidate_id = None
        self.current = None
        self.replacement = None

    # -- setup ------------------------------------------------------------
    def library_file(self, name="Halcyon.mp3", *, expr=_SWEEP, seconds=12.0,
                     bitrate="128k", lossless=False):
        """The file already on disk, as a real encode of the master."""
        master = _master(self.tmp_path / "master.wav", expr, seconds)
        codec_args = ("-c:a", "flac") if lossless else ("-c:a", "libmp3lame", "-b:a", bitrate)
        self.current = _encode(master, self.music / name, *codec_args)
        _tag(self.current, "Orbital", "Halcyon")
        self._master_path = master
        return self.current

    def staged_replacement(self, name="Halcyon.flac", *, master=None, seconds=None):
        """What the download will produce. Lossless, from the same master by default."""
        src = master or self._master_path
        if seconds is not None:
            src = _master(self.tmp_path / "reencoded.wav", _SWEEP, seconds)
        self.replacement = _encode(src, self.tmp_path / "staged_src" / name, "-c:a", "flac")
        _tag(self.replacement, "Orbital", "Halcyon")
        return self.replacement

    def staged_replacement_lossy(self, name="Halcyon (2).mp3", bitrate="128k"):
        """A download that is no better than what is already there."""
        self.replacement = _encode(
            self._master_path, self.tmp_path / "staged_src" / name,
            "-c:a", "libmp3lame", "-b:a", bitrate,
        )
        _tag(self.replacement, "Orbital", "Halcyon")
        return self.replacement

    def candidate(self, **overrides):
        info = upgrades.probe_file(self.current)
        row = {
            "user_id": "", "path": str(self.current),
            "codec": info["codec"], "bitrate_kbps": info["bitrate_kbps"],
            "duration": info["duration"],
            "file_tier": upgrades.TIER_LOSSY_128 if hasattr(upgrades, "TIER_LOSSY_128") else 1,
            "target_tier": upgrades.TIER_LOSSLESS, "below_target": 1,
            "artist": "Orbital", "title": "Halcyon",
            "content_sha256": upgrades.content_sha256(self.current),
            "file_id": info.get("file_id"),
            "found_source": "monochrome", "found_quality": "LOSSLESS",
            "found_tier": upgrades.TIER_LOSSLESS, "found_searched": 1,
        }
        row.update(overrides)
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        cur = self.conn.execute(
            f"INSERT INTO upgrade_candidates ({cols}) VALUES ({marks})", tuple(row.values())
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs (id, artist, title, status, final_path, override_dir) "
            "VALUES ('upgjob01', 'Orbital', 'Halcyon', 'completed', NULL, '/staging')"
        )
        self.conn.commit()
        self.candidate_id = cur.lastrowid
        return self.candidate_id

    # -- inspection -------------------------------------------------------
    def apply(self, **kwargs):
        return upgrades.perform_upgrade(None, self.candidate_id, **kwargs)

    def candidate_row(self):
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT * FROM upgrade_candidates WHERE id=?", (self.candidate_id,)
        ).fetchone()

    def quarantine_rows(self):
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM upgrade_quarantine").fetchall()

    def job_row(self):
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM jobs WHERE id='upgjob01'").fetchone()

    def indexed(self):
        with library_index.index_conn() as conn:
            return sorted(
                r[0] for r in conn.execute(
                    "SELECT relative_path FROM library_files WHERE root_path = ?",
                    (str(self.tmp_path / "music"),),
                ).fetchall()
            )

    def staging_dirs(self):
        return [p.name for p in self.quarantine.parent.iterdir() if p.name.startswith("upg_")]


@pytest.fixture
def upgrade(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(upgrades, "db_conn", fake_db_conn)
    harness = Upgrade(tmp_path, conn, monkeypatch)
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: harness.quarantine)

    # Genuinely external: a real download, a library rescan, an M3U rewrite that
    # needs the settings DB, and three media servers that are not listening.
    monkeypatch.setattr(upgrades, "scan_user_library",
                        lambda user_id=None: harness.scans.append(user_id))
    monkeypatch.setattr(upgrades, "_fix_m3u_references",
                        lambda uid, old, new: harness.m3u_fixes.append((old.name, new.name)))
    import downloads
    for name in ("trigger_navidrome_scan", "trigger_jellyfin_scan", "trigger_plex_scan"):
        monkeypatch.setattr(downloads, name, lambda user_id=None: None)

    def fake_download(user_id, row, staging_dir):
        """Stand in for process_download, which would really go to the internet."""
        if harness.replacement is None:
            return None, "upgjob01"
        staging_dir.mkdir(parents=True, exist_ok=True)
        dest = staging_dir / harness.replacement.name
        dest.write_bytes(harness.replacement.read_bytes())
        return dest, "upgjob01"

    monkeypatch.setattr(upgrades, "_download_candidate_to_staging", fake_download)

    # A real index, so the swap's hooks can be observed doing their job.
    monkeypatch.setattr(library_index, "LIBRARY_INDEX_DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(library_index, "configured_roots", lambda: [str(tmp_path / "music")])
    library_index.reset_for_tests()
    yield harness
    library_index.reset_for_tests()


# ---------------------------------------------------------------------------
# The swap that works
# ---------------------------------------------------------------------------

def test_a_genuine_upgrade_swaps_the_file_and_keeps_the_old_one(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()
    old_bytes = upgrade.current.read_bytes()

    result = upgrade.apply()

    assert result["status"] == "upgraded", result
    new_path = Path(result["path"])
    assert new_path.name == "Halcyon.flac"
    assert new_path.exists()
    assert upgrades.probe_file(new_path)["codec"] == "flac"
    # The lossy original is gone from the library but not from the planet.
    assert not upgrade.current.exists()
    quarantined = Path(result["quarantined"])
    assert quarantined.exists() and quarantined.read_bytes() == old_bytes


def test_the_displaced_file_is_tracked_so_it_can_be_restored(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()

    result = upgrade.apply()

    rows = upgrade.quarantine_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["quarantine_path"] == result["quarantined"]
    assert row["original_path"] == str(upgrade.current)
    assert row["replaced_by_path"] == result["path"]
    assert (row["artist"], row["title"]) == ("Orbital", "Halcyon")
    assert row["file_size"] > 0


def test_the_upgraded_file_keeps_the_librarys_idea_of_who_it_is(upgrade):
    """The new copy's own tags are not necessarily the ones the library used."""
    upgrade.library_file()
    replacement = upgrade.staged_replacement()
    _tag(replacement, "ORBITAL (Official)", "Halcyon + On + On (HQ Full)")
    upgrade.candidate()

    result = upgrade.apply()

    assert result["status"] == "upgraded", result
    from mutagen import File as MutagenFile
    tags = MutagenFile(Path(result["path"]), easy=True)
    assert tags["artist"] == ["Orbital"]
    assert tags["title"] == ["Halcyon"]


def test_the_candidate_row_now_describes_the_new_file(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()

    result = upgrade.apply()

    row = upgrade.candidate_row()
    assert row["path"] == result["path"]
    assert row["codec"] == "flac"
    assert row["file_tier"] == upgrades.TIER_LOSSLESS
    assert row["upgrade_state"] == "upgraded"
    assert row["below_target"] == 0, "lossless against a lossless target is not still below"
    assert result["still_below_target"] is False
    # The search cache is cleared, or the page would offer the same upgrade again.
    assert row["found_source"] is None
    assert row["found_searched"] == 0
    # And the stored hash matches the file actually on disk now.
    assert row["content_sha256"] == upgrades.content_sha256(Path(result["path"]))


def test_a_successful_upgrade_leaves_an_ordinary_completed_job(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()

    result = upgrade.apply()

    job = upgrade.job_row()
    assert job is not None, "a real acquisition should stay in the Queue"
    assert job["final_path"] == result["path"]
    assert job["override_dir"] is None, "the staging dir is not where the file lives"


def test_a_format_changing_upgrade_gets_the_playlists_rewritten(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()

    upgrade.apply()

    assert upgrade.m3u_fixes == [("Halcyon.mp3", "Halcyon.flac")]


def test_the_index_follows_the_swap(upgrade):
    """Phase 5's hook, and the reason this harness exists.

    The file at the new path is a different file with a different codec, so the
    old row has to go and a fresh one be read. Carrying the old row across would
    leave duplicate detection describing a bitrate that is no longer there.
    """
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()
    library_index.reconcile(upgrade.tmp_path / "music")
    assert upgrade.indexed() == ["Singles/Orbital/Halcyon.mp3"]

    result = upgrade.apply()

    assert upgrade.indexed() == ["Singles/Orbital/Halcyon.flac"]
    with library_index.index_conn() as conn:
        codec, bitrate = conn.execute(
            "SELECT codec, bitrate_kbps FROM library_files WHERE relative_path = ?",
            ("Singles/Orbital/Halcyon.flac",),
        ).fetchone()
    assert codec == "flac"
    assert bitrate == upgrades.probe_file(Path(result["path"]))["bitrate_kbps"]


# ---------------------------------------------------------------------------
# The gates that refuse
# ---------------------------------------------------------------------------

def _untouched(upgrade, before_bytes):
    """The original is exactly where and what it was, and nothing was quarantined."""
    assert upgrade.current.exists()
    assert upgrade.current.read_bytes() == before_bytes
    assert upgrade.quarantine_rows() == []
    assert list(upgrade.quarantine.iterdir()) == []


def test_a_replacement_that_is_not_actually_better_is_refused(upgrade):
    """The file is genuinely below target, and the replacement is no improvement.

    Both conditions matter. An original that already meets the target is refused
    one gate earlier, by a different check with a different message, so a test
    that only asserted "rejected" would pass with this gate switched off
    entirely. Mutation testing caught exactly that, hence the assertion on the
    reason rather than just the status.
    """
    upgrade.library_file()                      # mp3 128k, well below lossless
    upgrade.staged_replacement_lossy()          # and the download is the same again
    upgrade.candidate()                         # target is lossless
    before = upgrade.current.read_bytes()

    result = upgrade.apply()

    assert result["status"] == "rejected"
    assert "Not actually better" in result["reason"], result
    _untouched(upgrade, before)


def test_a_different_length_recording_is_refused(upgrade):
    upgrade.library_file(seconds=12.0)
    # Same master, re-rendered much longer: a full-album edit, not this track.
    upgrade.staged_replacement(seconds=40.0)
    upgrade.candidate()
    before = upgrade.current.read_bytes()

    result = upgrade.apply()

    assert result["status"] == "rejected"
    assert "Duration mismatch" in result["reason"]
    _untouched(upgrade, before)


def test_a_different_recording_is_refused_by_the_fingerprint(upgrade):
    """Real Chromaprint, doing the job it is there for.

    Same length and same tags, so duration and title/artist both pass; only the
    audio itself gives it away. Measured at 0.5854 similarity against a 0.65
    floor, which is the gap this gate exists to catch.
    """
    upgrade.library_file()
    other = _master(upgrade.tmp_path / "other.wav", _OTHER_SWEEP, 12.0)
    upgrade.staged_replacement(master=other)
    upgrade.candidate()
    before = upgrade.current.read_bytes()

    result = upgrade.apply()

    assert result["status"] == "rejected"
    assert "different recording" in result["reason"]
    _untouched(upgrade, before)


def test_an_original_that_changed_since_the_search_aborts_the_swap(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate(content_sha256="a stale hash from a file that has moved on")

    result = upgrade.apply()

    assert result["status"] == "error"
    assert "changed since the upgrade search" in result["reason"]
    assert upgrade.current.exists()
    assert upgrade.scans == [None], "the library should be rescanned to catch up"


def test_a_download_that_produced_nothing_is_a_rejection_not_a_swap(upgrade):
    upgrade.library_file()
    upgrade.replacement = None  # the download fails
    upgrade.candidate()
    before = upgrade.current.read_bytes()

    result = upgrade.apply()

    assert result["status"] == "rejected"
    assert "Download failed" in result["reason"], result
    assert upgrade.candidate_row()["upgrade_state"] == "rejected"
    _untouched(upgrade, before)


def test_a_rejected_upgrade_does_not_leave_a_job_in_the_queue(upgrade):
    upgrade.library_file(seconds=12.0)
    upgrade.staged_replacement(seconds=40.0)
    upgrade.candidate()

    assert upgrade.apply()["status"] == "rejected"

    assert upgrade.job_row() is None, "a failed gate was never a real acquisition"


def test_an_original_already_at_target_is_not_downloaded_at_all(upgrade):
    upgrade.library_file(name="Halcyon.flac", lossless=True)
    upgrade.staged_replacement(name="Halcyon2.flac")
    upgrade.candidate()

    result = upgrade.apply()

    assert result["status"] == "rejected"
    assert "already at target quality" in result["reason"]


def test_a_missing_original_is_an_error_not_a_crash(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()
    upgrade.current.unlink()

    result = upgrade.apply()

    assert result["status"] == "error"
    assert "no longer exists" in result["reason"]


# ---------------------------------------------------------------------------
# force=True, and tidying up
# ---------------------------------------------------------------------------

def test_force_overrides_the_identity_gates_but_still_quarantines(upgrade):
    """The deliberate override: a long YouTube edit for the studio cut.

    Every same-recording gate is skipped, so the swap goes through on audio that
    would otherwise be refused outright. The old file still goes to quarantine,
    which is what makes a forced mistake recoverable.
    """
    upgrade.library_file()
    other = _master(upgrade.tmp_path / "other.wav", _OTHER_SWEEP, 40.0)
    upgrade.staged_replacement(master=other)
    upgrade.candidate()
    old_bytes = upgrade.current.read_bytes()

    assert upgrade.apply()["status"] == "rejected", "unforced, this must be refused"

    upgrade.candidate()  # a fresh candidate row, same files
    result = upgrade.apply(force=True)

    assert result["status"] == "upgraded", result
    assert Path(result["path"]).exists()
    assert Path(result["quarantined"]).read_bytes() == old_bytes


def test_staging_is_cleaned_up_whichever_way_it_went(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate()
    assert upgrade.apply()["status"] == "upgraded"
    assert upgrade.staging_dirs() == []

    # And on the way out through a gate.
    upgrade.library_file()
    upgrade.staged_replacement(seconds=40.0)
    upgrade.candidate()
    assert upgrade.apply()["status"] == "rejected"
    assert upgrade.staging_dirs() == []


def test_a_candidate_with_nothing_found_is_not_a_swap(upgrade):
    upgrade.library_file()
    upgrade.staged_replacement()
    upgrade.candidate(found_source=None)

    result = upgrade.apply()

    assert result["status"] == "error"
    assert "No proposed upgrade" in result["reason"]


def test_an_unknown_candidate_is_an_error(upgrade):
    assert upgrades.perform_upgrade(None, 9999)["status"] == "error"
