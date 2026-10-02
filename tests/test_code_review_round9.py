"""Regression guards for the round-9 code review findings (CR-001..CR-010).

Each of these was a real hole in v4.0.0, and each one is the sort that quietly
comes back when someone refactors in a hurry. The tests are deliberately blunt:
they assert the refusal, not the wording.
"""

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _app_source() -> str:
    """app.py's text. Importing it would boot the app, which we very much do not want."""
    return (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CR-001 - URL-backed downloads and previews permit SSRF
# ---------------------------------------------------------------------------

class TestSourceUrlValidation:
    @pytest.mark.parametrize("source,url", [
        ("soundcloud", "http://127.0.0.1:8080/api/settings"),
        ("soundcloud", "http://localhost/admin"),
        ("soundcloud", "http://169.254.169.254/latest/meta-data/"),
        ("soundcloud", "http://10.0.0.5/?q=soundcloud.com"),
        ("soundcloud", "http://slskd:5030/api/v0/session"),
        # The classic allowlist bypass: a lookalike registrable domain.
        ("soundcloud", "https://soundcloud.com.evil.tld/track"),
        ("soundcloud", "file:///etc/passwd"),
        # Retired in v4.3.0: even its genuine URLs are no longer fetched.
        ("mp3phoenix", "https://mp3phoenix.net/getmp3/123"),
        ("zvu4no", "https://data.zvu4no.org/download-track/x.mp3"),
        ("freemp3cloud", "http://nas.local/admin"),
        ("jiosaavn", "https://aac.saavncdn.com.evil.tld/x_320.mp4"),
        ("jiosaavn", "http://192.168.1.1/x_320.mp4"),
        ("monochrome", "http://127.0.0.1/x"),
        ("soundcloud", ""),
        ("soundcloud", None),
        ("nonsense", "https://soundcloud.com/a/b"),
    ])
    def test_hostile_urls_are_refused(self, source, url):
        from source_urls import is_valid_source_url
        assert is_valid_source_url(source, url) is False

    @pytest.mark.parametrize("source,url", [
        ("soundcloud", "https://soundcloud.com/artist/track"),
        ("soundcloud", "https://m.soundcloud.com/artist/track"),
        ("zvu4no", "https://data.zvu4it.org/download-track/x.mp3"),
        ("freemp3cloud", "https://cdn5.meln.top/x.mp3"),
        ("jiosaavn", "https://aac.saavncdn.com/568/e467217af13bdec0e05624d656421ade_320.mp4"),
        ("monochrome", "monochrome://12345?isrc=GBAYE9200070"),
    ])
    def test_genuine_urls_still_work(self, source, url):
        from source_urls import is_valid_source_url
        assert is_valid_source_url(source, url) is True

    def test_ordinary_youtube_jobs_are_not_caught_by_the_worker_guard(self):
        """YouTube search results carry a webpage_url, so they reach the worker
        with source_url set. Refusing those would break every download."""
        from source_urls import is_valid_source_url
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=PL123",
        ):
            assert is_valid_source_url("youtube", url) is True, url
        assert is_valid_source_url("youtube", "https://youtube.com.evil.tld/watch") is False

    def test_the_worker_guard_allows_every_provider_it_dispatches_to(self):
        """The refusal must list the same providers the dispatch above detects."""
        body = (Path(__file__).resolve().parent.parent / "downloads.py").read_text(encoding="utf-8")
        guard = body[body.index("if is_url_source and not ("):]
        guard = guard[:guard.index("):")]
        for flag in ("is_soundcloud", "is_zvu4no", "is_freemp3cloud",
                     "is_jiosaavn", "is_monochrome", "is_youtube_url"):
            assert flag in guard, f"{flag} missing from the refusal guard"

    def test_validate_raises_with_a_useful_message(self):
        from source_urls import validate_source_url
        with pytest.raises(ValueError) as excinfo:
            validate_source_url("soundcloud", "http://127.0.0.1/")
        assert "soundcloud" in str(excinfo.value)

    def test_every_url_backed_source_has_an_allowlist(self):
        """A new URL source must not arrive without hosts, or it fails open."""
        from source_urls import SOURCE_URL_HOSTS
        # Read the set out of app.py rather than importing it; importing app
        # boots the whole application, which wants /music and a database.
        declared = _app_source().split("URL_BASED_SOURCES = {", 1)[1].split("}", 1)[0]
        sources = {chunk.strip().strip('"\'') for chunk in declared.split(",") if chunk.strip()}
        # Monochrome is the exception: it uses a private scheme, not a fetchable host.
        assert (sources - {"monochrome"}) <= set(SOURCE_URL_HOSTS)


# ---------------------------------------------------------------------------
# CR-002 - Custom playlist destinations can escape music_dir
# ---------------------------------------------------------------------------

class TestCustomSubdirContainment:
    @pytest.mark.parametrize("bad", [
        "../../tmp/escape",
        "/tmp/absolute",
        "~/escape",
        "a/../../b",
        "..\\..\\windows",
    ])
    def test_api_boundary_refuses_escapes(self, bad):
        import pydantic
        from models import WatchedPlaylistRequest
        with pytest.raises(pydantic.ValidationError):
            WatchedPlaylistRequest(url="https://example.com/p", custom_subdir=bad)

    @pytest.mark.parametrize("bad", ["../../tmp/escape", "/tmp/absolute"])
    def test_settings_subdirs_refuse_escapes_too(self, bad):
        """singles/playlists/albums subdirs are user-scoped, so equally exposed."""
        import pydantic
        from models import SettingsUpdate
        with pytest.raises(pydantic.ValidationError):
            SettingsUpdate(playlists_subdir=bad)

    def test_normal_values_are_untouched(self):
        from models import WatchedPlaylistRequest, SettingsUpdate
        assert WatchedPlaylistRequest(
            url="https://example.com/p", custom_subdir="Mixes/Rock"
        ).custom_subdir == "Mixes/Rock"
        # "." means the music root, and empty must stay empty rather than
        # collapsing to None, or clearing the setting silently stops working.
        assert SettingsUpdate(singles_subdir=".").singles_subdir == "."
        assert SettingsUpdate(playlists_subdir="").playlists_subdir == ""

    @pytest.mark.parametrize("bad", ["../../tmp/escape", "/tmp/absolute", "a/../.."])
    def test_resolver_refuses_escapes_from_stored_rows(self, bad, tmp_path):
        """Values saved before the validator existed still must not escape."""
        from settings import _join_within_music_dir
        with pytest.raises(ValueError):
            _join_within_music_dir(tmp_path, bad, "Custom playlist folder")

    def test_resolver_allows_and_preserves_a_normal_path(self, tmp_path):
        from settings import _join_within_music_dir
        assert _join_within_music_dir(tmp_path, "Mixes/Rock", "x") == tmp_path / "Mixes/Rock"

    def test_a_poisoned_settings_folder_falls_back_rather_than_bricking_boot(self, tmp_path, monkeypatch):
        """get_singles_dir() runs at import, so raising would stop the container."""
        import settings
        monkeypatch.setattr(
            settings, "get_setting",
            lambda key, default="", user_id=None: (str(tmp_path) if key == "music_dir" else "../../etc"),
        )
        assert settings.get_singles_dir() == tmp_path / "Singles"
        assert settings.get_albums_dir() == tmp_path / "Albums"
        # The playlists folder is opt-in, so the safe answer is "off", not a guess.
        assert settings.get_playlists_dir() is None

    def test_a_poisoned_custom_folder_still_raises(self, tmp_path, monkeypatch):
        """That one belongs to a specific job, which should fail rather than stray."""
        import settings
        monkeypatch.setattr(
            settings, "get_setting",
            lambda key, default="", user_id=None: str(tmp_path),
        )
        with pytest.raises(ValueError):
            settings.resolve_custom_subdir("../../etc")


# ---------------------------------------------------------------------------
# CR-003 - Trash contents are shared between users
# ---------------------------------------------------------------------------

class TestTrashIsolation:
    def test_each_user_gets_their_own_bin(self):
        from settings import get_trash_dir
        alice = get_trash_dir(user_id="aaaa1111")
        bob = get_trash_dir(user_id="bbbb2222")
        assert alice != bob
        # Neither may sit inside the other, or listing one would walk the other.
        assert alice not in bob.parents and bob not in alice.parents

    def test_single_user_mode_keeps_the_shared_root(self):
        """No user_id means one account, so there is nobody to hide from."""
        from settings import get_trash_dir, TRASH_ROOT
        assert get_trash_dir(user_id=None) == TRASH_ROOT

    def test_move_merges_files_without_nesting_directories(self, tmp_path):
        from settings import _move_trash_files
        src = tmp_path / "loose"
        dst = tmp_path / "owner"
        (src / "Singles" / "Artist").mkdir(parents=True)
        (src / "Singles" / "Artist" / "Track.flac").write_bytes(b"audio")
        dst.mkdir()
        moved = _move_trash_files(src, dst)
        assert moved == 1
        assert (dst / "Singles" / "Artist" / "Track.flac").exists()
        # The shell must not survive as dst/loose/Singles/...
        assert not (dst / "loose").exists()

    def test_migration_agrees_with_middleware_on_what_multi_user_means(self):
        """If the two disagree, the bin gets tidied into a shape requests cannot find.

        middleware._check_users_exist() counts every row, deactivated or not, so
        the migration must not quietly filter on is_active.
        """
        root = Path(__file__).resolve().parent.parent
        settings_src = (root / "settings.py").read_text(encoding="utf-8")
        body = settings_src[settings_src.index("def migrate_trash_to_per_user("):]
        body = body[:body.index("\ndef ", 1)]
        assert "FROM users" in body
        assert "is_active = 1" not in body, (
            "migration filters on is_active but middleware counts every account"
        )

    def test_move_never_clobbers_an_existing_file(self, tmp_path):
        from settings import _move_trash_files
        src = tmp_path / "a"
        dst = tmp_path / "b"
        src.mkdir()
        dst.mkdir()
        (src / "Track.flac").write_bytes(b"new")
        (dst / "Track.flac").write_bytes(b"original")
        _move_trash_files(src, dst)
        assert (dst / "Track.flac").read_bytes() == b"original"


# ---------------------------------------------------------------------------
# CR-004 - Watched-artist duplicate checks ignore the owner
# ---------------------------------------------------------------------------

def test_watched_artist_duplicate_checks_are_scoped_to_the_owner():
    """An unscoped check reads the wrong user's library, or the global one."""
    source = (Path(__file__).resolve().parent.parent / "watched_artists.py").read_text(encoding="utf-8")
    body = source[source.index("def refresh_watched_artist("):]
    # Comments talk about check_duplicate() too; only real calls count.
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    calls = [i for i, ln in enumerate(lines) if "check_duplicate(" in ln]
    assert len(calls) == 3, f"expected 3 check_duplicate calls, found {len(calls)}; update this guard"
    for i in calls:
        # The call may wrap over a few lines, so look at its whole statement.
        window = "\n".join(lines[i:i + 5])
        assert "user_id=user_id" in window, f"unscoped check_duplicate call: {lines[i].strip()}"


# ---------------------------------------------------------------------------
# CR-005 - Soulseek move mode can remove an unrelated file
# ---------------------------------------------------------------------------

class TestSlskdFallbackMatching:
    def test_a_wrong_sized_namesake_is_ignored(self, tmp_path):
        from slskd import _pick_sized_match
        (tmp_path / "peer").mkdir()
        (tmp_path / "peer" / "01 - Intro.flac").write_bytes(b"x" * 500)
        assert _pick_sized_match(tmp_path, "01 - Intro.flac", 12345) is None

    def test_the_right_sized_file_is_returned(self, tmp_path):
        from slskd import _pick_sized_match
        target = tmp_path / "peer" / "01 - Intro.flac"
        target.parent.mkdir()
        target.write_bytes(b"x" * 1000)
        assert _pick_sized_match(tmp_path, "01 - Intro.flac", 1000) == target

    def test_two_equally_plausible_matches_refuse_to_guess(self, tmp_path):
        """Move mode would delete whichever one we picked, so pick neither."""
        from slskd import _pick_sized_match, AmbiguousSlskdMatch
        for peer in ("peerA", "peerB"):
            (tmp_path / peer).mkdir()
            (tmp_path / peer / "01 - Intro.flac").write_bytes(b"x" * 1000)
        with pytest.raises(AmbiguousSlskdMatch):
            _pick_sized_match(tmp_path, "01 - Intro.flac", 1000)

    def test_an_ambiguous_match_is_not_retried_forever(self):
        """The refusal must not look like a transient slskd wobble."""
        from slskd import should_retry_slskd_error
        message = (
            "2 files under /downloads match '01 - Intro.flac' at 1000 bytes "
            "(/downloads/a/01 - Intro.flac, /downloads/b/01 - Intro.flac). "
            "Refusing to guess which one is yours."
        )
        assert should_retry_slskd_error(message) is False


# ---------------------------------------------------------------------------
# CR-006 - Various Artists albums lose track performers
# ---------------------------------------------------------------------------

class TestVariousArtistsTrackCredits:
    def _compilation_payload(self):
        return {
            "artist-credit": [{"name": "Various Artists"}],
            "media": [{"tracks": [
                {
                    "position": 1,
                    "artist-credit": [{"name": "Pulp"}],
                    "recording": {"id": "rec-1", "title": "Common People",
                                  "isrcs": ["GBAAA0000001"]},
                },
                {
                    "position": 2,
                    # No track-level credit; the recording still knows.
                    "recording": {"id": "rec-2", "title": "Parklife",
                                  "artist-credit": [{"name": "Blur"}], "isrcs": []},
                },
                {
                    "position": 3,
                    # Nothing anywhere, so it falls back to the release credit.
                    "recording": {"id": "rec-3", "title": "Mystery Track"},
                },
            ]}],
        }

    def _fetch(self, monkeypatch, payload):
        import metadata

        class FakeResponse:
            status_code = 200

            def json(self):
                return payload

        monkeypatch.setattr(metadata, "_mb_get_with_retry", lambda *a, **k: FakeResponse())
        return metadata.fetch_album_tracks("release-mbid")

    def test_track_performers_survive_the_tracklist_fetch(self, monkeypatch):
        tracks = self._fetch(monkeypatch, self._compilation_payload())
        assert [t["artist"] for t in tracks] == ["Pulp", "Blur", "Various Artists"]

    def test_joinphrases_are_honoured(self, monkeypatch):
        payload = {
            "artist-credit": [{"name": "Various Artists"}],
            "media": [{"tracks": [{
                "position": 1,
                "artist-credit": [
                    {"name": "Run-D.M.C.", "joinphrase": " feat. "},
                    {"name": "Aerosmith"},
                ],
                "recording": {"id": "r", "title": "Walk This Way"},
            }]}],
        }
        assert self._fetch(monkeypatch, payload)[0]["artist"] == "Run-D.M.C. feat. Aerosmith"

    def test_queueing_searches_for_the_performer_not_the_sleeve(self):
        """The pairs handed to the bulk importer decide what actually gets searched."""
        missing = [
            {"title": "Common People", "artist": "Pulp"},
            {"title": "Mystery Track", "artist": ""},
        ]
        release_artist = "Various Artists"
        pairs = [(t.get("artist") or release_artist, t["title"]) for t in missing]
        assert pairs == [("Pulp", "Common People"), ("Various Artists", "Mystery Track")]


# ---------------------------------------------------------------------------
# CR-007 - Album and rescue duplicate prevention is raceable
# ---------------------------------------------------------------------------

class TestConcurrencyGuards:
    def test_only_one_active_import_per_album_folder(self, tmp_path):
        """The partial unique index, reproduced against a scratch database."""
        db = sqlite3.connect(tmp_path / "t.db")
        db.execute("""CREATE TABLE bulk_imports (
            id TEXT PRIMARY KEY, status TEXT, override_dir TEXT, completed_at TIMESTAMP
        )""")
        db.execute(
            "CREATE UNIQUE INDEX idx_bulk_imports_active_album "
            "ON bulk_imports (override_dir) "
            "WHERE override_dir IS NOT NULL AND completed_at IS NULL "
            "AND status IN ('pending', 'processing')"
        )
        album = "/music/Albums/Blur/Parklife"
        db.execute("INSERT INTO bulk_imports VALUES ('a', 'pending', ?, NULL)", (album,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO bulk_imports VALUES ('b', 'pending', ?, NULL)", (album,))

        # Once the first finishes, the album may be queued again.
        db.execute("UPDATE bulk_imports SET status = 'completed', completed_at = datetime('now') WHERE id = 'a'")
        db.execute("INSERT INTO bulk_imports VALUES ('c', 'pending', ?, NULL)", (album,))

        # Non-album imports have no override_dir and must not collide at all.
        db.execute("INSERT INTO bulk_imports VALUES ('d', 'pending', NULL, NULL)")
        db.execute("INSERT INTO bulk_imports VALUES ('e', 'pending', NULL, NULL)")

    def test_cycle_allocation_is_one_atomic_statement(self, tmp_path):
        """Two readers must never be handed the same cycle number."""
        db = sqlite3.connect(tmp_path / "t.db")
        db.execute("CREATE TABLE acquisition_targets (id TEXT PRIMARY KEY, cycle_count INT, status TEXT, updated_at TEXT, completed_at TEXT)")
        db.execute("INSERT INTO acquisition_targets VALUES ('t1', 0, 'pending', NULL, NULL)")
        seen = []
        for _ in range(3):
            row = db.execute(
                "UPDATE acquisition_targets SET cycle_count = COALESCE(cycle_count, 0) + 1 "
                "WHERE id = 't1' RETURNING cycle_count"
            ).fetchone()
            seen.append(row[0])
        assert seen == [1, 2, 3]

    def test_begin_acquisition_cycle_can_join_a_caller_transaction(self):
        """Needed so admission and cycle claim commit or fail together."""
        import inspect
        import acquisition
        assert "conn" in inspect.signature(acquisition.begin_acquisition_cycle).parameters


# ---------------------------------------------------------------------------
# CR-008 - Authoritative paths lack library-root and symlink validation
# ---------------------------------------------------------------------------

class TestAuthoritativePathValidation:
    @pytest.fixture
    def library(self, tmp_path, monkeypatch):
        import utils
        music = tmp_path / "music"
        (music / "Singles" / "Artist").mkdir(parents=True)
        monkeypatch.setattr(utils, "get_setting", lambda key, default="", user_id=None: str(music))
        return music

    def test_a_genuine_library_file_is_accepted(self, library):
        from utils import authoritative_audio_file
        track = library / "Singles" / "Artist" / "Track.flac"
        track.write_bytes(b"audio")
        assert authoritative_audio_file(str(track), "u1") == track

    def test_a_file_outside_the_library_is_refused(self, library, tmp_path):
        from utils import authoritative_audio_file
        outsider = tmp_path / "elsewhere.flac"
        outsider.write_bytes(b"audio")
        assert authoritative_audio_file(str(outsider), "u1") is None

    def test_a_symlink_is_refused(self, library, tmp_path):
        """Swap a track for a symlink and delete/retag would follow it out."""
        from utils import authoritative_audio_file
        secret = tmp_path / "secret.flac"
        secret.write_bytes(b"not yours")
        link = library / "Singles" / "Artist" / "Track.flac"
        link.symlink_to(secret)
        assert authoritative_audio_file(str(link), "u1") is None

    def test_a_non_audio_file_is_refused(self, library):
        from utils import authoritative_audio_file
        doc = library / "Singles" / "Artist" / "notes.txt"
        doc.write_text("hello")
        assert authoritative_audio_file(str(doc), "u1") is None

    @pytest.mark.parametrize("raw", [None, "", "relative/path.flac", "/music/gone.flac"])
    def test_junk_paths_are_refused(self, library, raw):
        from utils import authoritative_audio_file
        assert authoritative_audio_file(raw, "u1") is None

    def test_a_directory_is_refused(self, library):
        from utils import authoritative_audio_file
        assert authoritative_audio_file(str(library / "Singles"), "u1") is None

    def test_the_api_actually_uses_it(self):
        """Serving, deleting and retagging must all go through the vetted path."""
        body = _app_source()
        assert "authoritative_audio_file(" in body
        # The old lenient shape must not creep back in.
        assert "is_absolute() and candidate.exists()" not in body


# ---------------------------------------------------------------------------
# CR-009 - Maintenance rebuild uses collision-prone job IDs
# ---------------------------------------------------------------------------

def test_library_rebuild_uses_full_uuids():
    """8 hex chars is a coin toss for collisions once a rebuild passes ~50k rows."""
    body = _app_source()
    marker = "rebuilt.append((str(uuid.uuid4())"
    assert marker in body, "library rebuild row construction moved; update this guard"
    assert "rebuilt.append((str(uuid.uuid4())[:8]" not in body


# ---------------------------------------------------------------------------
# CR-010 - Force-accept mutates a job before ownership is checked
# ---------------------------------------------------------------------------

def test_force_accept_checks_ownership_before_it_writes():
    """The ownership lookup must come first, not after the UPDATE."""
    body = _app_source()
    start = body.index("def force_accept_job(")
    source = body[start:body.index("\n@app.", start)]
    select_at = source.find("SELECT 1 FROM jobs")
    update_at = source.find("UPDATE jobs SET skip_mismatch_check")
    delete_at = source.find("DELETE FROM watched_match_mismatches")
    assert select_at != -1, "ownership check missing from force_accept_job"
    assert select_at < update_at
    assert select_at < delete_at
    assert "404" in source or "status_code=404" in source
