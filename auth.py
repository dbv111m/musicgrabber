"""
MusicGrabber - Authentication helpers

Session management, password hashing, user lookups.
"""

import sqlite3
import threading
import time
import uuid
from collections import defaultdict

import bcrypt

from constants import (
    DOWNLOAD_TOKEN_TTL_SECONDS,
    LOGIN_ATTEMPT_WINDOW,
    LOGIN_LOCKOUT_SECONDS,
    LOGIN_MAX_ATTEMPTS,
    STREAM_TOKEN_TTL_SECONDS,
)
from db import db_conn


SESSION_LIFETIME_DAYS = 30
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_lockouts: dict[str, float] = {}
_login_lock = threading.Lock()
_login_last_cleanup = 0.0
_LOGIN_CLEANUP_INTERVAL = 60.0


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


_DUMMY_PASSWORD_HASH = hash_password("musicgrabber-invalid-password")


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), hashed.encode())
    except Exception:
        return False


def _login_key(username: str) -> str:
    return (username or "").strip().casefold()


def _prune_login_state(now: float) -> None:
    global _login_last_cleanup
    if now - _login_last_cleanup < _LOGIN_CLEANUP_INTERVAL:
        return

    window_start = now - LOGIN_ATTEMPT_WINDOW
    for key, attempts in list(_login_failures.items()):
        trimmed = [t for t in attempts if t > window_start]
        if trimmed:
            _login_failures[key] = trimmed
        else:
            _login_failures.pop(key, None)
    for key, locked_until in list(_login_lockouts.items()):
        if locked_until <= now:
            _login_lockouts.pop(key, None)
    _login_last_cleanup = now


def is_login_allowed(username: str) -> tuple[bool, int]:
    """Return whether login attempts are allowed for this username."""
    now = time.time()
    key = _login_key(username)
    with _login_lock:
        _prune_login_state(now)
        locked_until = _login_lockouts.get(key, 0.0)
        if locked_until > now:
            return False, max(1, int(locked_until - now))
        if locked_until:
            _login_lockouts.pop(key, None)
        return True, 0


def record_failed_login(username: str) -> int:
    """Record a failed attempt; returns lockout seconds if lockout was applied."""
    now = time.time()
    window_start = now - LOGIN_ATTEMPT_WINDOW
    key = _login_key(username)
    with _login_lock:
        _prune_login_state(now)
        attempts = [t for t in _login_failures.get(key, []) if t > window_start]
        attempts.append(now)
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            _login_lockouts[key] = now + LOGIN_LOCKOUT_SECONDS
            _login_failures.pop(key, None)
            return LOGIN_LOCKOUT_SECONDS
        _login_failures[key] = attempts
    return 0


def clear_failed_login(username: str) -> None:
    key = _login_key(username)
    with _login_lock:
        _login_failures.pop(key, None)
        _login_lockouts.pop(key, None)


def password_hash_for_timing(user: dict | None) -> str:
    """Always return a hash so login checks have similar bcrypt timing."""
    if user and user.get("password_hash"):
        return user["password_hash"]
    return _DUMMY_PASSWORD_HASH


def create_session(user_id: str) -> str:
    token = str(uuid.uuid4())
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) "
            "VALUES (?, ?, datetime('now', '+' || ? || ' days'))",
            (token, user_id, str(SESSION_LIFETIME_DAYS)),
        )
        conn.commit()
    return token


def create_download_token(user_id: str, job_id: str, ttl_seconds: int = DOWNLOAD_TOKEN_TTL_SECONDS) -> str:
    token = str(uuid.uuid4())
    ttl = max(5, int(ttl_seconds))
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO download_tokens (token, user_id, job_id, expires_at) "
            "VALUES (?, ?, ?, datetime('now', '+' || ? || ' seconds'))",
            (token, user_id, job_id, str(ttl)),
        )
        conn.commit()
    return token


def consume_download_token(token: str, job_id: str) -> dict | None:
    """Consume a single-use download token and return the associated user context."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """UPDATE download_tokens
               SET used_at = datetime('now')
               WHERE token = ?
                 AND job_id = ?
                 AND used_at IS NULL
                 AND expires_at > datetime('now')""",
            (token, job_id),
        )
        if cursor.rowcount != 1:
            return None

        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.force_password_change, u.is_active
               FROM download_tokens dt
               JOIN users u ON u.id = dt.user_id
               WHERE dt.token = ?""",
            (token,),
        ).fetchone()
        conn.commit()

    if row is None:
        return None
    user = dict(row)
    if not user.get("is_active"):
        return None
    return user


def cleanup_expired_download_tokens() -> int:
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM download_tokens WHERE used_at IS NOT NULL OR expires_at <= datetime('now')"
        )
        deleted = cursor.rowcount
        conn.commit()
    return deleted


def create_stream_token(user_id: str, ttl_seconds: int = STREAM_TOKEN_TTL_SECONDS) -> str:
    """Mint a reusable playback token for an <audio> element.

    Reusable because players make range requests and seek about; a single-use
    token would die somewhere in the first second of the track. It carries no
    authority of its own beyond "this is who's asking", so the stream endpoints
    still do their usual ownership checks.
    """
    token = str(uuid.uuid4())
    ttl = max(5, int(ttl_seconds))
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO stream_tokens (token, user_id, expires_at) "
            "VALUES (?, ?, datetime('now', '+' || ? || ' seconds'))",
            (token, user_id, str(ttl)),
        )
        conn.commit()
    return token


def resolve_stream_token(token: str) -> dict | None:
    """The user context a live stream token belongs to, for the auth middleware."""
    if not token:
        return None
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.force_password_change, u.is_active
               FROM stream_tokens st
               JOIN users u ON u.id = st.user_id
               WHERE st.token = ?
                 AND st.expires_at > datetime('now')""",
            (token,),
        ).fetchone()
    if row is None:
        return None
    user = dict(row)
    if not user.get("is_active"):
        return None
    return user


def cleanup_expired_stream_tokens() -> int:
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM stream_tokens WHERE expires_at <= datetime('now')"
        )
        deleted = cursor.rowcount
        conn.commit()
    return deleted


def get_session_user(token: str) -> dict | None:
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.force_password_change, u.is_active,
                      (s.last_seen IS NULL OR s.last_seen < datetime('now', '-60 seconds')) AS should_touch
               FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = ?
                 AND s.expires_at > datetime('now')""",
            (token,),
        ).fetchone()
        if row is None:
            return None
        # Avoid turning every authenticated API poll into a SQLite write.
        # Queue polling can be frequent, and download workers already contend
        # for the same database lock.
        if row["should_touch"]:
            conn.execute(
                "UPDATE sessions SET last_seen = datetime('now') WHERE token = ?",
                (token,),
            )
            conn.commit()
        user = dict(row)
        user.pop("should_touch", None)
        return user


def delete_session(token: str) -> None:
    with db_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def cleanup_expired_sessions() -> int:
    with db_conn() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        deleted = cursor.rowcount
        conn.commit()
    return deleted


def get_user_by_username(username: str) -> dict | None:
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, username, role, is_active, force_password_change, created_at "
            "FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, role: str = "user") -> str:
    user_id = str(uuid.uuid4())[:8]
    pw_hash = hash_password(password)
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
                (user_id, username, pw_hash, role),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Username '{username}' already exists")
    return user_id


def update_password(user_id: str, new_password: str, keep_session_token: str | None = None) -> None:
    pw_hash = hash_password(new_password)
    with db_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, force_password_change = 0 WHERE id = ?",
            (pw_hash, user_id),
        )
        if keep_session_token:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                (user_id, keep_session_token),
            )
        else:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (user_id,),
            )
        conn.commit()


def delete_user(user_id: str) -> None:
    with db_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
