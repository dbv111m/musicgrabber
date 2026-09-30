"""Settings read/write tests. All writes are restore-after, leaving the service clean."""

import os
import queue
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from settings import SETTINGS_SCHEMA, get_setting_bool


EXPECTED_KEYS = [
    "music_dir",
    "enable_musicbrainz",
    "enable_lyrics",
    "default_convert_audio",
    "audio_format",
    "min_audio_bitrate",
    "singles_subdir",
    "playlists_subdir",
    "albums_subdir",
    "organise_by_artist",
    "include_track_number_in_filename",
    "skip_dupes",
    "source_offline_fallback",
    "source_health_checks_enabled",
    "source_health_check_interval_minutes",
    "source_health_cooldown_minutes",
    "search_concurrency",
    "notify_on",
]


def test_settings_get(api, base_url):
    r = api.get(f"{base_url}/api/settings", timeout=10)
    assert r.status_code == 200
    assert "settings" in r.json()


def test_settings_all_expected_keys_present(api, base_url):
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    for key in EXPECTED_KEYS:
        assert key in settings, f"settings missing key: {key}"


def test_settings_audio_format_is_valid(api, base_url):
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert settings["audio_format"] in ("flac", "alac", "opus", "mp3")


def test_fresh_install_keeps_source_format_by_default():
    assert SETTINGS_SCHEMA["default_convert_audio"]["default"] is False


def test_search_concurrency_defaults_to_one_and_is_bounded():
    from models import SettingsUpdate
    from pydantic import ValidationError

    assert SETTINGS_SCHEMA["search_concurrency"]["default"] == 1
    assert SettingsUpdate(search_concurrency=1).search_concurrency == 1
    assert SettingsUpdate(search_concurrency=5).search_concurrency == 5
    with pytest.raises(ValidationError):
        SettingsUpdate(search_concurrency=0)
    with pytest.raises(ValidationError):
        SettingsUpdate(search_concurrency=6)


def _migrate_old_db(old_db, monkeypatch):
    """Run init_db against a throwaway database on an isolated pool."""
    migration_pool = queue.LifoQueue(maxsize=db._DB_POOL_SIZE)
    monkeypatch.setattr(db, "_db_pool", migration_pool)
    monkeypatch.setattr(db, "DB_PATH", str(old_db))
    monkeypatch.delenv("DEFAULT_CONVERT_TO_FLAC", raising=False)
    monkeypatch.delenv("DEFAULT_CONVERT_AUDIO", raising=False)
    migration_pool.put(db.get_db())
    return migration_pool


def _drain(pool):
    while not pool.empty():
        pool.get_nowait().close()


def test_existing_conversion_preference_survives_db_migrations(tmp_path, monkeypatch):
    """Changing the fresh-install default must not rewrite an existing choice.

    The pre-upgrade row is written under the old key on purpose: v4.0.0 renames it
    to default_convert_audio, and a rename that loses the value is just a slower
    way of resetting everyone's settings.
    """
    old_db = tmp_path / "pre-upgrade.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [
                ("db_version", "0"),
                ("default_convert_to_flac", "true"),
            ],
        )

    # Isolate init_db's connection pool so this temporary database cannot leak
    # into the rest of the suite.
    migration_pool = _migrate_old_db(old_db, monkeypatch)

    try:
        db.init_db()
        assert get_setting_bool("default_convert_audio", False) is True
        with db.db_conn() as conn:
            leftover = conn.execute(
                "SELECT COUNT(*) FROM settings WHERE key = 'default_convert_to_flac'"
            ).fetchone()[0]
        assert leftover == 0, "the old settings key should not linger after the rename"
    finally:
        _drain(migration_pool)


def test_per_row_conversion_flags_survive_the_column_rename(tmp_path, monkeypatch):
    """The four tables carrying the flag must keep their values, not just their shape."""
    old_db = tmp_path / "pre-upgrade-columns.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # db_version 13 means every numbered migration is already behind us, which
        # is the state an actual v3 install upgrades from.
        conn.execute("INSERT INTO settings (key, value) VALUES ('db_version', '13')")
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, convert_to_flac INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO jobs (id, convert_to_flac) VALUES ('job-yes', 1)")
        conn.execute("INSERT INTO jobs (id, convert_to_flac) VALUES ('job-no', 0)")
        conn.execute("""
            CREATE TABLE watched_artists (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, mbid TEXT NOT NULL,
                from_date TEXT NOT NULL, convert_to_flac INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO watched_artists (id, name, mbid, from_date, convert_to_flac)"
            " VALUES ('a1', 'Paramore', 'mbid-1', '2026-01-01', 1)"
        )

    migration_pool = _migrate_old_db(old_db, monkeypatch)

    try:
        db.init_db()
        with db.db_conn() as conn:
            job_flags = dict(conn.execute("SELECT id, convert_audio FROM jobs").fetchall())
            artist_flag = conn.execute(
                "SELECT convert_audio FROM watched_artists WHERE id = 'a1'"
            ).fetchone()[0]
            job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}

        assert job_flags == {"job-yes": 1, "job-no": 0}
        assert artist_flag == 1
        assert "convert_to_flac" not in job_cols, "the old column should be gone, not duplicated"
    finally:
        _drain(migration_pool)


def test_migration_is_idempotent_on_an_already_renamed_db(tmp_path, monkeypatch):
    """Running init_db twice must not undo, duplicate, or blank the rename."""
    fresh_db = tmp_path / "fresh.db"
    migration_pool = _migrate_old_db(fresh_db, monkeypatch)

    try:
        db.init_db()
        with db.db_conn() as conn:
            conn.execute("INSERT INTO jobs (id, convert_audio) VALUES ('job-1', 1)")
            conn.commit()

        db.init_db()  # second boot, same database

        with db.db_conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            flag = conn.execute("SELECT convert_audio FROM jobs WHERE id = 'job-1'").fetchone()[0]

        assert "convert_audio" in cols
        assert "convert_to_flac" not in cols
        assert flag == 1
    finally:
        _drain(migration_pool)


def test_migration_removes_obsolete_qobuz_proxy_setting(tmp_path, monkeypatch):
    old_db = tmp_path / "pre-proxy-removal.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [
                ("db_version", "12"),
                ("monochrome_qobuz_proxy_url", "https://self-hosted.example.test"),
            ],
        )

    migration_pool = _migrate_old_db(old_db, monkeypatch)
    try:
        db.init_db()
        with db.db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'monochrome_qobuz_proxy_url'"
            ).fetchone()
            version = conn.execute(
                "SELECT value FROM settings WHERE key = 'db_version'"
            ).fetchone()[0]
        assert row is None
        assert version == "19"  # migrations run to the latest version in one boot
    finally:
        _drain(migration_pool)


def test_migration_removes_retired_monochrome_browser_setting(tmp_path, monkeypatch):
    old_db = tmp_path / "pre-browser-removal.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE user_settings (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, key)
            )
        """)
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [
                ("db_version", "17"),
                ("monochrome_browser_fallback_enabled", "false"),
                ("source_monochrome_enabled", "false"),
            ],
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) "
            "VALUES ('u1', 'monochrome_browser_fallback_enabled', 'true')"
        )

    migration_pool = _migrate_old_db(old_db, monkeypatch)
    try:
        db.init_db()
        with db.db_conn() as conn:
            leftovers = conn.execute(
                "SELECT COUNT(*) FROM settings WHERE key = 'monochrome_browser_fallback_enabled'"
            ).fetchone()[0] + conn.execute(
                "SELECT COUNT(*) FROM user_settings WHERE key = 'monochrome_browser_fallback_enabled'"
            ).fetchone()[0]
            neighbour = conn.execute(
                "SELECT value FROM settings WHERE key = 'source_monochrome_enabled'"
            ).fetchone()
            version = conn.execute(
                "SELECT value FROM settings WHERE key = 'db_version'"
            ).fetchone()[0]
        assert leftovers == 0
        assert neighbour == ("false",)  # somebody's deliberate choice; leave it be
        assert version == "19"
    finally:
        _drain(migration_pool)


def test_migration_removes_retired_qbdlx_setting(tmp_path, monkeypatch):
    old_db = tmp_path / "pre-qbdlx-removal.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [("db_version", "18"), ("monochrome_qbdlx_fallback_enabled", "true")],
        )

    migration_pool = _migrate_old_db(old_db, monkeypatch)
    try:
        db.init_db()
        with db.db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'monochrome_qbdlx_fallback_enabled'"
            ).fetchone()
            version = conn.execute(
                "SELECT value FROM settings WHERE key = 'db_version'"
            ).fetchone()[0]
        assert row is None
        assert version == "19"
    finally:
        _drain(migration_pool)


def test_migration_clears_dead_default_hifi_api_url_but_not_a_self_hosted_one(tmp_path, monkeypatch):
    """A stored value that's still one of the dead public defaults gets cleared;
    a genuinely self-hosted URL is left alone, since it may still work fine."""
    old_db = tmp_path / "pre-hifi-cleanup.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO settings (key, value) VALUES ('db_version', '13')")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('monochrome_hifi_api_url', 'https://monochrome-api.samidy.com')"
        )

    migration_pool = _migrate_old_db(old_db, monkeypatch)
    try:
        db.init_db()
        with db.db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'monochrome_hifi_api_url'"
            ).fetchone()
            version = conn.execute(
                "SELECT value FROM settings WHERE key = 'db_version'"
            ).fetchone()[0]
        assert row is None
        assert version == "19"
    finally:
        _drain(migration_pool)

    self_hosted_db = tmp_path / "pre-hifi-cleanup-self-hosted.db"
    with sqlite3.connect(self_hosted_db) as conn:
        conn.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO settings (key, value) VALUES ('db_version', '13')")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('monochrome_hifi_api_url', 'https://hifi.example-self-hosted.test')"
        )

    migration_pool = _migrate_old_db(self_hosted_db, monkeypatch)
    try:
        db.init_db()
        with db.db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'monochrome_hifi_api_url'"
            ).fetchone()
        assert row is not None
        assert row[0] == "https://hifi.example-self-hosted.test"
    finally:
        _drain(migration_pool)


def test_legacy_env_var_still_drives_the_renamed_setting(tmp_path, monkeypatch):
    """A compose file written for v3 must not silently stop converting."""
    from settings import _is_env_override

    monkeypatch.delenv("DEFAULT_CONVERT_AUDIO", raising=False)
    monkeypatch.setenv("DEFAULT_CONVERT_TO_FLAC", "true")
    assert get_setting_bool("default_convert_audio", False) is True
    assert _is_env_override("default_convert_audio") is True, "the UI field must still lock"

    # And when both are set, the current name is the one in charge.
    monkeypatch.setenv("DEFAULT_CONVERT_AUDIO", "false")
    assert get_setting_bool("default_convert_audio", True) is False


def test_settings_monochrome_enabled_by_default(api, base_url):
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert settings["source_monochrome_enabled"] is True


def test_settings_monochrome_hifi_url_blank_by_default_and_proxy_removed(api, base_url):
    """No honest public default remains; a fresh install starts blank, not pointed at a dead host."""
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert settings.get("monochrome_hifi_api_url", "") == ""
    assert "monochrome_qobuz_proxy_url" not in settings


def test_settings_write_and_restore(api, base_url):
    """Toggle organise_by_artist, verify it persists, then restore."""
    original = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    original_val = original["organise_by_artist"]
    flipped = not original_val

    r = api.put(f"{base_url}/api/settings", json={"organise_by_artist": flipped}, timeout=10)
    assert r.status_code == 200

    updated = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert updated["organise_by_artist"] == flipped, "setting didn't persist after PUT"

    # Restore
    api.put(f"{base_url}/api/settings", json={"organise_by_artist": original_val}, timeout=10)
    restored = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert restored["organise_by_artist"] == original_val


def test_settings_put_unknown_key_ignored(api, base_url):
    """Unknown fields in PUT body should not cause a 500."""
    r = api.put(f"{base_url}/api/settings", json={"not_a_real_setting_xyz": True}, timeout=10)
    assert r.status_code == 200


def test_email_test_endpoint_reports_invalid_form_values(api, base_url):
    """The SMTP tester must explain bad input without attempting delivery."""
    r = api.post(
        f"{base_url}/api/settings/test/email",
        json={
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_user": "",
            "smtp_pass": "",
            "smtp_from": "",
            "smtp_to": "tester@example.com",
            "smtp_tls": True,
        },
        timeout=10,
    )

    assert r.status_code == 200
    assert r.json() == {"success": False, "message": "SMTP host is required"}


def test_settings_mp3_bitrate_write_and_restore(api, base_url):
    """mp3_bitrate should persist (this regressed in v2.8.2)."""
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    original = settings.get("mp3_bitrate", "v2")

    target = "320k" if original != "320k" else "v0"
    api.put(f"{base_url}/api/settings", json={"mp3_bitrate": target}, timeout=10)
    updated = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert updated["mp3_bitrate"] == target, "mp3_bitrate didn't persist"

    api.put(f"{base_url}/api/settings", json={"mp3_bitrate": original}, timeout=10)


def test_settings_opus_bitrate_write_and_restore(api, base_url):
    """opus_bitrate should persist."""
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    original = settings.get("opus_bitrate", "320k")

    target = "256k" if original != "256k" else "192k"
    api.put(f"{base_url}/api/settings", json={"opus_bitrate": target}, timeout=10)
    updated = api.get(f"{base_url}/api/settings", timeout=10).json()["settings"]
    assert updated["opus_bitrate"] == target, "opus_bitrate didn't persist"

    api.put(f"{base_url}/api/settings", json={"opus_bitrate": original}, timeout=10)


# ---------------------------------------------------------------------------
# Schema / API-model parity
#
# Settings are written through the SettingsUpdate Pydantic model, which drops
# any field it does not declare. Adding a key to SETTINGS_SCHEMA and wiring up
# the UI is therefore not enough: without a matching model field the PUT
# silently succeeds and changes nothing, which is a genuinely annoying way to
# spend an afternoon. Ask me how I know.
# ---------------------------------------------------------------------------

# Schema keys deliberately not writable through /api/settings. Anything else
# missing from SettingsUpdate is a wiring bug, not a design decision.
_NON_WRITABLE_SETTINGS = {
    "spotify_cookies_expired",   # Internal state, set by the cookie check
    "acoustid_api_key",          # Written via its own dedicated flow
    "max_concurrent_downloads",  # Not exposed in the Settings UI
    "webhook_url",
    "youtube_bot_backoff_min",
    "youtube_bot_backoff_max",
}


def test_every_settings_schema_key_is_writable_or_knowingly_excluded():
    from models import SettingsUpdate
    model_fields = set(SettingsUpdate.model_fields.keys())
    missing = set(SETTINGS_SCHEMA) - model_fields - _NON_WRITABLE_SETTINGS
    assert not missing, (
        "These settings exist in SETTINGS_SCHEMA but have no SettingsUpdate "
        f"field, so writing them silently does nothing: {sorted(missing)}"
    )


def test_settings_update_model_has_no_phantom_fields():
    """A model field with no schema key can never be persisted."""
    from models import SettingsUpdate
    phantom = set(SettingsUpdate.model_fields.keys()) - set(SETTINGS_SCHEMA)
    assert not phantom, f"SettingsUpdate fields with no schema entry: {sorted(phantom)}"


def test_replaygain_settings_are_present_and_default_off():
    from models import SettingsUpdate
    for key in ("enable_replaygain", "replaygain_replace_existing"):
        assert key in SETTINGS_SCHEMA, f"{key} missing from SETTINGS_SCHEMA"
        assert SETTINGS_SCHEMA[key]["default"] is False, f"{key} must be opt-in"
        assert key in SettingsUpdate.model_fields, f"{key} not writable via the API"
