"""
MusicGrabber Telegram Bot

Simple functional Telegram bot for searching and downloading music.
No classes, no over-engineering - just functions.
"""

import asyncio
import html
import logging
import math
import os
import re
import sqlite3
import json
from datetime import datetime
from typing import Any, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import httpx
from rapidfuzz import fuzz

# Standalone module: talks to the MusicGrabber app purely over its HTTP API
# (MUSICGRABBER_API_URL + MUSICGRABBER_API_KEY), keeps its own sqlite for
# per-chat state. No imports from the app codebase -> merges with upstream
# never conflict.

# Telegram settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = os.getenv("TELEGRAM_ALLOWED_USERS", "")
# Admin chat id (you). Admin gets extra menu items and bypasses some limits.
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0")) or None
# API base URL - configurable for different deployments
API_BASE_URL = os.getenv("MUSICGRABBER_API_URL", "http://localhost:8080")
API_KEY = os.getenv("MUSICGRABBER_API_KEY", "")
VERSION = os.getenv("TELEGRAM_BOT_VERSION", "standalone")
BOT_DB_PATH = os.getenv("TELEGRAM_BOT_DB", "telegram_bot.db")

# =============================================================================
# Rate limiting: bot detection + logarithmic human limits
# =============================================================================
# Bot detection: if chat performs more than BOT_MAX actions within BOT_WINDOW
# seconds, it's flagged as bot-like and temporarily blocked.
RATE_LIMIT_BOT_MAX = int(os.getenv("TELEGRAM_RATE_LIMIT_BOT_MAX", "8"))
RATE_LIMIT_BOT_WINDOW = int(os.getenv("TELEGRAM_RATE_LIMIT_BOT_WINDOW", "30"))
# Bot block duration (seconds)
RATE_LIMIT_BOT_BLOCK_SEC = int(os.getenv("TELEGRAM_RATE_LIMIT_BOT_BLOCK", "600"))

# Human limits (logarithmic scale): base allowance grows with log2 of time
# since first recorded action. Prevents burst abuse while letting real users
# search/download naturally.
RATE_LIMIT_HUMAN_BASE = int(os.getenv("TELEGRAM_RATE_LIMIT_HUMAN_BASE", "3"))
RATE_LIMIT_HUMAN_MAX_CAP = int(os.getenv("TELEGRAM_RATE_LIMIT_HUMAN_MAX", "15"))

# Per-action-type limits (hard caps per hour)
RATE_LIMIT_SEARCH_HOUR = int(os.getenv("TELEGRAM_RATE_LIMIT_SEARCH_HOUR", "20"))
RATE_LIMIT_DOWNLOAD_HOUR = int(os.getenv("TELEGRAM_RATE_LIMIT_DOWNLOAD_HOUR", "8"))

# Bot-like blocks: chat_id -> unblock_timestamp
_bot_blocks: dict[int, float] = {}

# Per-user cooldown between downloads (seconds)
DOWNLOAD_COOLDOWN_SEC = int(os.getenv("TELEGRAM_DOWNLOAD_COOLDOWN", "30"))
_last_download: dict[int, float] = {}


def _api_headers() -> dict:
    return {"X-API-Key": API_KEY} if API_KEY else {}

# User context storage: persisted in SQLite, cached in memory
user_contexts: dict[int, dict[str, Any]] = {}

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
# httpx logs full request URLs (incl. bot token) at INFO — keep it quiet
logging.getLogger("httpx").setLevel(logging.WARNING)

# =============================================================================
# Database helpers
# =============================================================================

def get_db_connection():
    """Get database connection (bot's own sqlite, not the app's)."""
    conn = sqlite3.connect(BOT_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_telegram_tables():
    """Initialize Telegram-specific tables."""
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            settings_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            action TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Own job registry: chat_id -> app job_id (the app's jobs table is not
    # touched from here anymore)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_jobs (
            job_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            title TEXT,
            artist TEXT,
            video_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered INTEGER DEFAULT 0
        )
    """)
    # Backfill columns for older DBs that lack them
    for col_def in ("video_id TEXT", "delivered INTEGER DEFAULT 0", "query TEXT"):
        try:
            conn.execute(f"ALTER TABLE bot_jobs ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    # Persisted user context (survives restarts)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_contexts (
            chat_id INTEGER,
            key TEXT,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, key)
        )
    """)
    # Rate limit tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            chat_id INTEGER,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Cached search results: query -> list of tracks (global cache, no chat_id, no files stored)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            source TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT,
            artist TEXT,
            source_url TEXT,
            quality TEXT,
            duration TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_cache_query
        ON search_cache(query)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_cache_video
        ON search_cache(video_id)
    """)
    # Backfill: drop chat_id column if it exists from older schema
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(search_cache)").fetchall()]
        if "chat_id" in cols:
            conn.execute("""
                CREATE TABLE search_cache_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    source TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    source_url TEXT,
                    quality TEXT,
                    duration TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO search_cache_new (id, query, source, video_id, title, artist, source_url, quality, duration, created_at)
                SELECT id, query, source, video_id, title, artist, source_url, quality, duration, created_at FROM search_cache
            """)
            conn.execute("DROP TABLE search_cache")
            conn.execute("ALTER TABLE search_cache_new RENAME TO search_cache")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_search_cache_query
                ON search_cache(query)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_search_cache_video
                ON search_cache(video_id)
            """)
    except Exception:
        pass
    # Cached CDN / download links by video_id (no files stored, only URLs).
    # Links are kept indefinitely and invalidated only on download error.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cdn_links (
            video_id TEXT PRIMARY KEY,
            source_url TEXT,
            cdn_url TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Backfill: drop expires_at column from older schema if present
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(cdn_links)").fetchall()]
        if "expires_at" in cols:
            conn.execute("""
                CREATE TABLE cdn_links_new (
                    video_id TEXT PRIMARY KEY,
                    source_url TEXT,
                    cdn_url TEXT,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO cdn_links_new (video_id, source_url, cdn_url, source, created_at)
                SELECT video_id, source_url, cdn_url, source, created_at FROM cdn_links
            """)
            conn.execute("DROP TABLE cdn_links")
            conn.execute("ALTER TABLE cdn_links_new RENAME TO cdn_links")
    except Exception:
        pass
    conn.commit()
    conn.close()


def get_user_settings(chat_id: int) -> dict:
    """Get user settings from database."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT settings_json FROM telegram_users WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()
    conn.close()

    if row:
        try:
            return json.loads(row["settings_json"])
        except json.JSONDecodeError:
            pass

    # Default settings
    return {
        "search_source": "all",
        "convert_to_flac": False,  # Default to original/MP3, not FLAC
        "download_folder": "Singles",
        "min_bitrate": 0,
    }


def save_user_settings(chat_id: int, settings: dict):
    """Save user settings to database."""
    conn = get_db_connection()
    conn.execute(
        """INSERT OR REPLACE INTO telegram_users
           (chat_id, settings_json, last_active)
           VALUES (?, ?, CURRENT_TIMESTAMP)""",
        (chat_id, json.dumps(settings))
    )
    conn.commit()
    conn.close()


def update_user_last_active(chat_id: int, username: str = None, first_name: str = None):
    """Update user last active time."""
    conn = get_db_connection()
    if username or first_name:
        conn.execute(
            """UPDATE telegram_users
               SET last_active = CURRENT_TIMESTAMP,
                   username = COALESCE(?, username),
                   first_name = COALESCE(?, first_name)
               WHERE chat_id = ?""",
            (username, first_name, chat_id)
        )
    else:
        conn.execute(
            "UPDATE telegram_users SET last_active = CURRENT_TIMESTAMP WHERE chat_id = ?",
            (chat_id,)
        )
    conn.commit()
    conn.close()


def log_bot_action(chat_id: int, action: str, details: str = None):
    """Log bot action."""
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO bot_logs (chat_id, action, details) VALUES (?, ?, ?)",
        (chat_id, action, details)
    )
    conn.commit()
    conn.close()


# =============================================================================
# Access control
# =============================================================================

def is_user_allowed(chat_id: int) -> bool:
    """Check if user is allowed to use bot."""
    if not TELEGRAM_ALLOWED_USERS:
        return True  # No restriction

    try:
        allowed_ids = [int(x.strip()) for x in TELEGRAM_ALLOWED_USERS.split(",")]
        return chat_id in allowed_ids
    except ValueError:
        return True  # Invalid config, allow all


def is_admin(chat_id: int) -> bool:
    """Check if chat_id is the admin."""
    return ADMIN_CHAT_ID is not None and chat_id == ADMIN_CHAT_ID


# =============================================================================
# Rate limiting: bot detection + logarithmic human limits
# =============================================================================

def _now() -> float:
    return datetime.now().timestamp()


def record_action(chat_id: int, action: str) -> None:
    """Record an action timestamp for rate limiting."""
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO rate_limits (chat_id, action, created_at) "
            "VALUES (?, ?, datetime(?, 'unixepoch'))",
            (chat_id, action, _now()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Failed to record rate limit action: {exc}")


def is_bot_like(chat_id: int) -> tuple[bool, str]:
    """Detect bot-like behavior: too many actions in a short window.
    Returns (is_bot, reason). Also checks if chat is currently blocked."""
    now = _now()

    # Check existing block
    if chat_id in _bot_blocks and _bot_blocks[chat_id] > now:
        remaining = int(_bot_blocks[chat_id] - now)
        return True, f"заблокирован за ботоподобное поведение (ещё {remaining} сек)"

    # Clean old entries and count recent actions
    window_start = now - RATE_LIMIT_BOT_WINDOW
    try:
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM rate_limits WHERE created_at < datetime(?, 'unixepoch')",
            (window_start,),
        )
        count = conn.execute(
            "SELECT count(*) FROM rate_limits WHERE chat_id = ? "
            "AND created_at >= datetime(?, 'unixepoch')",
            (chat_id, window_start),
        ).fetchone()[0]
        conn.close()
    except Exception:
        count = 0

    if count > RATE_LIMIT_BOT_MAX:
        _bot_blocks[chat_id] = now + RATE_LIMIT_BOT_BLOCK_SEC
        return True, f"ботоподобное поведение ({count} действий за {RATE_LIMIT_BOT_WINDOW} сек), блок на {RATE_LIMIT_BOT_BLOCK_SEC // 60} мин"

    return False, ""


def check_human_limit(chat_id: int, action: str) -> tuple[bool, str]:
    """Logarithmic scale limit for human users.
    Base allowance grows with log2 of time since first action.
    Returns (allowed, reason)."""
    if is_admin(chat_id):
        return True, ""  # Admin bypasses human limits

    now = _now()
    hour_ago = now - 3600

    # Hard caps per hour
    if action == "search":
        hour_cap = RATE_LIMIT_SEARCH_HOUR
    elif action == "download":
        hour_cap = RATE_LIMIT_DOWNLOAD_HOUR
    else:
        hour_cap = RATE_LIMIT_HUMAN_MAX_CAP

    try:
        conn = get_db_connection()
        # Count actions of this type in the last hour
        count = conn.execute(
            "SELECT count(*) FROM rate_limits WHERE chat_id = ? AND action = ? "
            "AND created_at >= datetime(?, 'unixepoch')",
            (chat_id, action, hour_ago),
        ).fetchone()[0]
        # Time since first recorded action (for log scale)
        first_ts = conn.execute(
            "SELECT min(created_at) FROM rate_limits WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]
        conn.close()
    except Exception:
        count = 0
        first_ts = None

    # Hard cap check
    if count >= hour_cap:
        return False, f"лимит {hour_cap} {action}/час исчерпан"

    # Logarithmic burst check: allowance = base + log2(hours_active + 1)
    if first_ts:
        try:
            first_dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00")) if isinstance(first_ts, str) else first_ts
            hours_active = max((now - first_dt.timestamp()) / 3600, 0)
        except Exception:
            hours_active = 0
    else:
        hours_active = 0

    allowance = RATE_LIMIT_HUMAN_BASE + int(math.log2(hours_active + 1))
    allowance = min(allowance, RATE_LIMIT_HUMAN_MAX_CAP)

    if count >= allowance:
        return False, f"лимит {allowance} {action} (шкала времени), попробуйте позже"

    return True, ""


def check_access(chat_id: int, action: str = "default") -> tuple[bool, str]:
    """Full access check: bot detection + human limits.
    Returns (allowed, reason). Admin bypasses human limits but not bot detection."""
    # Bot-like detection (applies to everyone including admin)
    bot, reason = is_bot_like(chat_id)
    if bot:
        return False, reason

    # Human limits (admin bypasses)
    human, reason = check_human_limit(chat_id, action)
    if not human:
        return False, reason

    return True, ""


def format_error(raw_error: str) -> str:
    """Map raw app error messages to human-friendly Russian text."""
    e = raw_error.lower()
    if "403" in e and ("youtube" in e or "blocked" in e):
        return "YouTube заблокировал запрос. Настройте cookies в настройках или попробуйте другой источник."
    if "peer closed connection" in e or "connection" in e and "closed" in e:
        return "Источник временно недоступен. Попробуйте позже или выберите другой трек."
    if "drm" in e or "preview-only" in e:
        return "Трек доступен только для предпрослушивания (DRM). Попробуйте другой источник."
    if "not found" in e or "404" in e:
        return "Трек не найден на этом источнике."
    if "timeout" in e:
        return "Источник не отвечает (таймаут). Попробуйте позже."
    if "unavailable" in e:
        return "Источник временно недоступен."
    if "unauthorized" in e or "401" in e or "auth" in e:
        return "Требуется авторизация на источнике."
    # Generic: return first sentence, trimmed
    first = raw_error.split(".")[0]
    return first[:120] + ("…" if len(first) > 120 else "")


# =============================================================================
# URL detection: YouTube / SoundCloud links -> direct download
# =============================================================================

_RE_YOUTUBE_WATCH = re.compile(
    r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([a-zA-Z0-9_-]{11})'
)
_RE_YOUTUBE_SHORT = re.compile(r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})')
_RE_SOUNDCLOUD = re.compile(r'soundcloud\.com/([^/\?]+)(?:/([^/\?]+))?')


def extract_url_source(query: str) -> Optional[tuple[str, str, str]]:
    """Detect if query is a YouTube or SoundCloud URL.
    Returns (source, video_id, source_url) or None."""
    q = query.strip()

    # YouTube watch URL
    m = _RE_YOUTUBE_WATCH.search(q)
    if m:
        return ("youtube", m.group(1), q)

    # YouTube shorts
    m = _RE_YOUTUBE_SHORT.search(q)
    if m:
        return ("youtube", m.group(1), q)

    # SoundCloud URL
    m = _RE_SOUNDCLOUD.search(q)
    if m:
        artist = m.group(1) or ""
        track = m.group(2) or ""
        source_url = q
        # video_id for soundcloud is the full URL path
        video_id = f"sc_{artist}_{track}".replace(" ", "_")[:64] if track else f"sc_{artist}"
        return ("soundcloud", video_id, source_url)

    return None


# =============================================================================
# Fuzzy search: typo correction and re-ranking with rapidfuzz
# =============================================================================

FUZZY_THRESHOLD = int(os.getenv("TELEGRAM_FUZZY_THRESHOLD", "60"))


def normalize_query(query: str) -> str:
    """Normalize a search query: lowercase, strip, collapse whitespace, remove
    common punctuation that breaks source indexes."""
    q = query.strip().lower()
    q = re.sub(r'[^\w\s\-]', ' ', q)  # replace punctuation with spaces
    q = re.sub(r'\s+', ' ', q).strip()
    return q


def fuzzy_variants(query: str) -> list[str]:
    """Generate query variants for fuzzy retry when the original returns empty.
    Strips stop-words, tries word subsets, removes punctuation."""
    norm = normalize_query(query)
    words = norm.split()
    variants = [norm]

    # Remove common stop-words
    stop = {"the", "a", "an", "of", "and", "feat", "ft", "vs", "remix", "edit"}
    filtered = [w for w in words if w not in stop]
    if filtered and filtered != words:
        variants.append(" ".join(filtered))

    # First two words
    if len(words) >= 2:
        variants.append(" ".join(words[:2]))

    # Last two words (title often at end)
    if len(words) >= 2:
        variants.append(" ".join(words[-2:]))

    # Dedup, preserve order
    seen: set[str] = set()
    ordered = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered[:5]


def rerank_results(results: list[dict], query: str) -> list[dict]:
    """Re-rank search results by rapidfuzz similarity to the user query.
    Tracks with higher fuzzy similarity float to the top."""
    if not results:
        return results

    norm_q = normalize_query(query)

    def score(r: dict) -> float:
        title = r.get("title") or ""
        artist = r.get("artist") or r.get("channel") or ""
        combined = f"{artist} {title}"
        # Token set ratio is robust to word order differences
        return fuzz.token_set_ratio(norm_q, combined.lower())

    return sorted(results, key=score, reverse=True)


def search_with_fallback(query: str, source: str, limit: int = 20) -> tuple[list[dict], str]:
    """Search with fuzzy fallback: if the original query returns empty,
    try normalized and reduced variants. Re-ranks results by fuzzy similarity.
    Returns (results, used_query)."""
    # Try original query first (caller handles async)
    return [], query


async def search_music_fuzzy(query: str, source: str, limit: int = 20) -> tuple[list[dict], str]:
    """Async search with fuzzy fallback.
    If the original query returns empty, tries normalized/reduced variants.
    Re-ranks results by rapidfuzz similarity to the original query.
    Returns (results, used_query)."""
    results = await search_music(query, source, limit)
    used_query = query
    if results:
        return rerank_results(results, query), used_query

    # Try fuzzy variants
    for variant in fuzzy_variants(query):
        if variant == normalize_query(query):
            continue  # already tried
        results = await search_music(variant, source, limit)
        if results:
            used_query = variant
            return rerank_results(results, query), used_query

    return [], used_query


# =============================================================================
# Search cache + CDN link cache (no files stored, only URLs and metadata)
# =============================================================================

SEARCH_CACHE_TTL_HOURS = int(os.getenv("TELEGRAM_SEARCH_CACHE_TTL", "720"))  # 30 days
CDN_LINK_TTL_HOURS = int(os.getenv("TELEGRAM_CDN_LINK_TTL", "720"))  # 30 days


def _normalize_query(query: str) -> str:
    """Normalize query for cache lookup: lowercase, strip, collapse spaces."""
    return " ".join(query.strip().lower().split())


def get_cached_search(query: str) -> list[dict] | None:
    """Return cached search results for query if within TTL.
    Returns None if no cache or cache expired. Global cache (no chat_id)."""
    norm = _normalize_query(query)
    cutoff = _now() - SEARCH_CACHE_TTL_HOURS * 3600
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT video_id, title, artist, source_url, quality, duration, source "
            "FROM search_cache WHERE query = ? "
            "AND created_at >= datetime(?, 'unixepoch') "
            "ORDER BY id ASC",
            (norm, cutoff),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning(f"Search cache read failed: {exc}")
        return None

    if not rows:
        return None

    results = []
    for row in rows:
        results.append({
            "video_id": row["video_id"],
            "title": row["title"],
            "artist": row["artist"] or row.get("channel") or "Unknown",
            "source": row["source"],
            "source_url": row["source_url"],
            "quality": row["quality"] or "",
            "duration": row["duration"] or "",
        })
    return results


def save_search_results(query: str, source: str, results: list[dict]) -> None:
    """Save search results to cache and cache CDN links for each track.
    Global cache (no chat_id)."""
    norm = _normalize_query(query)
    try:
        conn = get_db_connection()
        # Clear old cache for this query
        conn.execute(
            "DELETE FROM search_cache WHERE query = ?",
            (norm,),
        )
        for r in results:
            video_id = r.get("video_id", "")
            title = r.get("title", "")
            artist = r.get("artist", "")
            source_url = r.get("source_url", "") or ""
            quality = r.get("quality", "")
            duration = r.get("duration", "")
            src = r.get("source", source)
            conn.execute(
                "INSERT INTO search_cache "
                "(query, source, video_id, title, artist, source_url, quality, duration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (norm, src, video_id, title, artist, source_url, quality, duration),
            )
            # Cache CDN link if we have a source_url
            if source_url:
                save_cdn_link(conn, video_id, source_url, source_url, src)
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Search cache write failed: {exc}")


def save_cdn_link(conn, video_id: str, source_url: str, cdn_url: str, source: str) -> None:
    """Save/update a CDN link in the cache (within existing transaction).
    Links are kept indefinitely; invalidated only on download error."""
    conn.execute(
        "DELETE FROM cdn_links WHERE video_id = ?",
        (video_id,),
    )
    conn.execute(
        "INSERT INTO cdn_links (video_id, source_url, cdn_url, source, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (video_id, source_url, cdn_url, source),
    )


def get_cached_cdn_link(video_id: str) -> str | None:
    """Return cached CDN URL for video_id. Links are never expired by TTL;
    they are removed only when a download fails."""
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT cdn_url FROM cdn_links WHERE video_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        conn.close()
    except Exception as exc:
        logger.warning(f"CDN link cache read failed: {exc}")
        return None
    return row["cdn_url"] if row else None


def clear_expired_cache() -> None:
    """Remove expired search cache entries. CDN links are never expired by TTL."""
    search_cutoff = _now() - SEARCH_CACHE_TTL_HOURS * 3600
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM search_cache WHERE created_at < datetime(?, 'unixepoch')", (search_cutoff,))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Cache cleanup failed: {exc}")


# =============================================================================
# Context management
# =============================================================================

def set_user_context(chat_id: int, key: str, value: Any):
    """Set value in user context (memory + SQLite)."""
    if chat_id not in user_contexts:
        user_contexts[chat_id] = {}
    user_contexts[chat_id][key] = value
    # Persist to SQLite (survives restarts)
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO user_contexts (chat_id, key, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (chat_id, key, json.dumps(value) if not isinstance(value, str) else value),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Failed to persist context for chat {chat_id}: {exc}")


def get_user_context(chat_id: int, key: str, default=None):
    """Get value from user context (memory first, then SQLite)."""
    # Check memory first
    if chat_id in user_contexts and key in user_contexts[chat_id]:
        return user_contexts[chat_id][key]
    # Fallback to SQLite
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT value FROM user_contexts WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        ).fetchone()
        conn.close()
        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return row["value"]
    except Exception as exc:
        logger.warning(f"Failed to read context for chat {chat_id}: {exc}")
    return default


def clear_user_context(chat_id: int):
    """Clear user context (memory + SQLite)."""
    if chat_id in user_contexts:
        del user_contexts[chat_id]
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM user_contexts WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Failed to clear context for chat {chat_id}: {exc}")


# =============================================================================
# API helpers
# =============================================================================

async def search_music(query: str, source: str, limit: int = 10) -> list:
    """Search music via API."""
    try:
        # 'all' waits for every source; a stalling one (zvu4no from some IPs)
        # can push the total past 30s, which used to abort as an empty ReadTimeout.
        timeout = float(os.getenv("TELEGRAM_SEARCH_TIMEOUT", "90"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/search",
                json={"query": query, "source": source, "limit": limit},
                headers=_api_headers(),
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []


async def download_track(video_id: str, title: str, artist: str, source: str,
                        convert_to_flac: bool, chat_id: int, source_url: str = None,
                        update: Update = None, query: str = None) -> Optional[dict]:
    """Queue track download via the app API and return the job ID."""
    # Queue download
    try:
        async with httpx.AsyncClient(timeout=30) as client:  # Increased timeout
            payload = {
                "video_id": video_id,
                "title": title,
                "artist": artist,
                "source": source,
                "convert_audio": convert_to_flac,
            }
            # Add source_url for non-YouTube sources
            if source_url and source in ["soundcloud", "monochrome"]:
                payload["source_url"] = source_url

            response = await client.post(
                f"{API_BASE_URL}/api/download",
                json=payload,
                headers=_api_headers(),
            )
            response.raise_for_status()
            data = response.json()

            job_id = data.get("job_id")
            if job_id:
                conn = get_db_connection()
                conn.execute(
                    "INSERT OR REPLACE INTO bot_jobs (job_id, chat_id, title, artist, video_id, query) VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, chat_id, title, artist, video_id, query),
                )
                conn.commit()
                conn.close()
                # Cache CDN link if we have a source_url
                if source_url:
                    try:
                        conn2 = get_db_connection()
                        save_cdn_link(conn2, video_id, source_url, source_url, source)
                        conn2.commit()
                        conn2.close()
                    except Exception:
                        pass

            return {"status": "queued", "job_id": job_id}

    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


async def get_job_status(job_id: str) -> Optional[dict]:
    """Get job status."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{API_BASE_URL}/api/jobs/{job_id}", headers=_api_headers())
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.error(f"Job status error: {e}")
    return None


async def get_user_jobs(chat_id: int, limit: int = 10) -> list:
    """Get this chat's jobs: own bot_jobs registry enriched with live status.
    Дубликаты одного трека (повторные загрузки/ретраи) схлопываются — берётся свежей."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT job_id, title, artist, delivered FROM bot_jobs WHERE chat_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (chat_id, limit * 3),
    ).fetchall()
    conn.close()

    jobs = []
    seen = set()
    for row in rows:
        key = ((row["artist"] or "").lower(), (row["title"] or "").lower())
        if key in seen:
            continue
        seen.add(key)
        jobs.append({
            "id": row["job_id"],
            "title": row["title"],
            "artist": row["artist"],
            "delivered": bool(row["delivered"]),
        })
        if len(jobs) >= limit:
            break

    # Enrich with live status concurrently (fixes N+1 sequential HTTP calls)
    async def _enrich(job):
        status = await get_job_status(job["id"])
        if status:
            job.update({k: status.get(k) for k in ("status", "progress", "error") if status.get(k) is not None})
        else:
            job["status"] = "unknown"
        return job

    enriched = await asyncio.gather(*[_enrich(j) for j in jobs])
    return enriched


async def _redeliver_missed(chat, jobs: list):
    """Self-healing delivery: re-send completed files the chat never got
    (in-memory watcher dies on container restart). Marks delivered via
    _send_track_file."""
    for job in jobs:
        if job.get("status") != "completed" or job.get("delivered"):
            continue
        data = await get_job_status(job["id"])
        if not data or data.get("status") != "completed":
            continue
        if await _send_track_file(chat, job["id"], data, job.get("title", "Unknown")):
            log_bot_action(chat.id, "redeliver", job["id"])


async def get_stats() -> Optional[dict]:
    """Get global stats."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{API_BASE_URL}/api/stats", headers=_api_headers())
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.error(f"Stats error: {e}")
    return None


# =============================================================================
# Keyboard builders
# =============================================================================

def build_main_keyboard(admin: bool = False):
    """Build main menu keyboard. Admins get extra items."""
    keyboard = [
        [InlineKeyboardButton("🎵 Поиск музыки", callback_data="menu_search")],
        [InlineKeyboardButton("📋 Мои загрузки", callback_data="menu_queue")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
    ]
    if admin:
        keyboard.insert(2, [InlineKeyboardButton("🔧 Админ: отладка", callback_data="menu_debug")])
        keyboard.insert(3, [InlineKeyboardButton("📢 Админ: рассылка", callback_data="menu_broadcast")])
    return InlineKeyboardMarkup(keyboard)


def build_settings_keyboard(settings: dict, admin: bool = False):
    """Build settings menu keyboard."""
    source_labels = {
        "youtube": "YouTube",
        "soundcloud": "SoundCloud",
        "monochrome": "Monochrome",
        "all": "Все источники"
    }
    source_label = source_labels.get(settings["search_source"], "Все источники")

    quality_label = "FLAC" if settings["convert_to_flac"] else "Оригинал"

    keyboard = [
        [InlineKeyboardButton(f"🔊 Источник: {source_label}", callback_data="set_source")],
        [InlineKeyboardButton(f"🎼 Качество: {quality_label}", callback_data="set_quality")],
        [InlineKeyboardButton(f"📁 Папка: {settings['download_folder']}", callback_data="set_folder")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_results_keyboard(results: list, page: int = 0, per_page: int = 5):
    """Build search results keyboard. Each result: [⬇️ download]."""
    keyboard = []
    start = page * per_page
    end = start + per_page

    for i, result in enumerate(results[start:end]):
        idx = start + i + 1
        source_emoji = {
            "youtube": "🎞",
            "soundcloud": "🎧",
            "monochrome": "💿",
            "soulseek": "🎶",
            "freemp3cloud": "📻",
        }.get(result.get("source", "youtube"), "🎵")

        quality_raw = result.get("quality", "N/A")
        quality_map = {
            "LOSSLESS": "",
            "HI_RES": "Hi-Res",
            "HIGH": "",
            "NORMAL": "",
            "LOW": "",
        }
        quality = quality_map.get(quality_raw, quality_raw) if quality_raw else ""
        quality_str = f" [{quality}]" if quality else ""
        title = (result.get("title") or "Unknown")[:30]
        artist = (result.get("artist") or result.get("channel") or "Unknown")[:20]

        label = f"⬇️ {idx}. {source_emoji} {artist} - {title}{quality_str}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"download_{start + i}")])

    # Navigation
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"page_{page-1}"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"page_{page+1}"))
    nav_row.append(InlineKeyboardButton("❌", callback_data="search_cancel"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="menu_main")])

    return InlineKeyboardMarkup(keyboard)


def build_queue_keyboard(jobs: list):
    """Build queue management keyboard."""
    keyboard = []
    for job in jobs[:5]:  # Max 5 jobs
        status = job["status"]
        title = job.get("title", "Unknown")[:25]

        status_emoji = {
            "queued": "⏳",
            "downloading": "⬇️",
            "completed": "✅",
            "failed": "❌",
        }.get(status, "❓")

        label = f"{status_emoji} {title}"
        if status == "failed":
            callback = f"retry_{job['id']}"  # retry failed job
        elif status in ("queued", "downloading"):
            callback = f"cancel_{job['id']}"  # cancel active job
        else:
            callback = "noop"

        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="queue_refresh"),
                     InlineKeyboardButton("🗑 Очистить", callback_data="queue_clear")])
    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="menu_main")])

    return InlineKeyboardMarkup(keyboard)


# =============================================================================
# Command handlers
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not is_user_allowed(chat_id):
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return

    # Access check
    allowed, reason = check_access(chat_id, "start")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "start")

    update_user_last_active(chat_id, user.username, user.first_name)
    clear_user_context(chat_id)

    admin = is_admin(chat_id)
    text = f"""🎵 <b>MusicGrabber Bot</b>

Версия: {VERSION}
{'<b>Режим: администратор</b>' if admin else ''}

Поиск и загрузка музыки в лучшем качестве!

Выберите действие в меню ниже:"""

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_main_keyboard(admin=admin))
    log_bot_action(chat_id, "start")


HELP_TEXT = """❓ <b>Помощь</b>

<b>Команды:</b>
/start - Главное меню
/search - Поиск музыки
/queue - Мои загрузки
/history - История загрузок
/stats - Статистика
/settings - Настройки
/whoami - Ваш chat_id
/ping - Проверка работоспособности
/cancel - Отмена текущего действия

<b>Как пользоваться:</b>
1. Нажмите "🎵 Поиск музыки"
2. Введите название трека
3. Выберите результат из списка
4. Файл автоматически загрузится и придёт в чат

<b>Источники:</b>
🎞 YouTube
🎧 SoundCloud
💿 Monochrome (Lossless FLAC)
🎶 Soulseek (если настроен)

<b>Настройки:</b>
• Выбор источника поиска
• Качество (FLAC/оригинал)
• Папка для сохранения

<b>Ограничения:</b>
Поиск и скачка ограничены для защиты от спама.
Администратор имеет расширенное меню и обходит лимиты.

Вопросы и предложения: https://github.com/dbv111m/musicgrabber"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")
    log_bot_action(update.effective_chat.id, "help")


async def callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle help menu callback."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(HELP_TEXT, parse_mode="HTML", reply_markup=build_main_keyboard(admin=is_admin(query.message.chat_id)))


async def callback_menu_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin debug menu callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    if not is_admin(chat_id):
        await query.answer("❌ Только для администратора.")
        return

    await query.answer()

    # Get recent bot logs
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT action, details, created_at FROM bot_logs ORDER BY created_at DESC LIMIT 15",
        ).fetchall()
        # Rate limit stats
        blocked = len([c for c, t in _bot_blocks.items() if t > datetime.now().timestamp()])
        conn.close()
    except Exception:
        rows = []
        blocked = 0

    log_lines = [f"{r['created_at']} {r['action']}" + (f": {r['details']}" if r['details'] else "") for r in rows]
    text = f"""🔧 <b>Админ: отладка</b>

заблокировано ботов: {blocked}
user_contexts в памяти: {len(user_contexts)}

Последние действия:
{chr(10).join(log_lines) if log_lines else '—'}
"""
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_keyboard(admin=True))


async def callback_menu_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin broadcast menu callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    if not is_admin(chat_id):
        await query.answer("❌ Только для администратора.")
        return

    await query.answer()
    set_user_context(chat_id, "awaiting_broadcast", True)

    text = """📢 <b>Админ: рассылка</b>

Введите текст сообщения для рассылки всем пользователям.
Для отмены отправьте /cancel."""

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_keyboard(admin=True))


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /search command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    # Access check
    allowed, reason = check_access(chat_id, "search")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "search")

    clear_user_context(chat_id)
    set_user_context(chat_id, "awaiting_search", True)

    text = """🔍 <b>Поиск музыки</b>

Введите название трека или "Исполнитель - Название"

Примеры:
• Arctic Monkeys
• Arctic Monkeys - Do I Wanna Know?
• qotsa go with the flow

Для отмены отправьте /cancel"""

    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "search_init")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /queue command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    # Access check
    allowed, reason = check_access(chat_id, "queue")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "queue")

    jobs = await get_user_jobs(chat_id)

    if not jobs:
        text = "📋 <b>Мои загрузки</b>\n\nПока нет загрузок. Используйте /search для загрузки музыки."
        await update.message.reply_text(text, parse_mode="HTML")
        return

    text = f"📋 <b>Мои загрузки</b>\n\nВсего: {len(jobs)}"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_queue_keyboard(jobs))
    asyncio.create_task(_redeliver_missed(update.effective_chat, jobs))
    log_bot_action(chat_id, "queue_view")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    # Access check
    allowed, reason = check_access(chat_id, "stats")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "stats")

    stats = await get_stats()
    if not stats:
        await update.message.reply_text("❌ Не удалось получить статистику.")
        return

    total = stats.get("total_jobs", 0)
    completed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    storage_gb = stats.get("storage_bytes", 0) / (1024**3)
    file_count = stats.get("file_count", 0)

    success_rate = (completed / total * 100) if total > 0 else 0

    text = f"""📊 <b>Статистика</b>

🎵 Всего загрузок: {total}
✅ Завершено: {completed} ({success_rate:.1f}%)
❌ Ошибок: {failed}

📦 Место на диске: {storage_gb:.2f} GB
🎧 Треков: {file_count}

Остальная статистика доступна в веб-интерфейсе."""

    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "stats_view")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    settings = get_user_settings(chat_id)

    text = """⚙️ <b>Настройки</b>

Выберите параметр для изменения:"""

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings_view")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command."""
    chat_id = update.effective_chat.id
    clear_user_context(chat_id)
    await update.message.reply_text("❌ Отменено.", reply_markup=build_main_keyboard())


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command — recent downloads for this chat."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    # Access check
    allowed, reason = check_access(chat_id, "queue")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "queue")

    jobs = await get_user_jobs(chat_id, limit=20)
    if not jobs:
        await update.message.reply_text("📜 <b>История</b>\n\nПока нет загрузок.", parse_mode="HTML")
        return

    lines = ["📜 <b>Последние загрузки</b>\n"]
    for job in jobs[:10]:
        status = job.get("status", "unknown")
        status_emoji = {
            "queued": "⏳",
            "downloading": "⬇️",
            "completed": "✅",
            "failed": "❌",
        }.get(status, "❓")
        title = job.get("title", "Unknown")
        artist = job.get("artist", "Unknown")
        lines.append(f"{status_emoji} {artist} — {title}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    log_bot_action(chat_id, "history")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /whoami — show user's chat_id."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Access check
    allowed, reason = check_access(chat_id, "default")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "default")

    admin_marker = " <b>(администратор)</b>" if is_admin(chat_id) else ""
    text = f"""👤 <b>Ваш профиль</b>

chat_id: <code>{chat_id}</code>{admin_marker}
username: {user.username or '—'}
first_name: {user.first_name or '—'}
"""
    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "whoami")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ping — bot health check."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    # Access check
    allowed, reason = check_access(chat_id, "default")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "default")

    # Check API connectivity
    api_ok = False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{API_BASE_URL}/api/config", headers=_api_headers())
            api_ok = resp.status_code == 200
    except Exception:
        pass

    admin_marker = " <b>(администратор)</b>" if is_admin(chat_id) else ""
    blocked = len([c for c, t in _bot_blocks.items() if t > datetime.now().timestamp()])
    text = f"""🏓 <b>Pong!</b>{admin_marker}

Бот работает.
API MusicGrabber: {'✅ доступен' if api_ok else '❌ недоступен'}
Заблокировано ботов: {blocked}
Контекстов в памяти: {len(user_contexts)}
"""
    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "ping")


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /debug — bot internals (admin only)."""
    chat_id = update.effective_chat.id

    if not is_admin(chat_id):
        await update.message.reply_text("❌ Только для администратора.")
        return

    # Access check
    allowed, reason = check_access(chat_id, "debug")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "debug")

    # Get recent bot logs
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT action, details, created_at FROM bot_logs WHERE chat_id = ? "
            "ORDER BY created_at DESC LIMIT 10",
            (chat_id,),
        ).fetchall()
        blocked = len([c for c, t in _bot_blocks.items() if t > datetime.now().timestamp()])
        conn.close()
    except Exception:
        rows = []
        blocked = 0

    log_lines = [f"{r['created_at']} {r['action']}" + (f": {r['details']}" if r['details'] else "") for r in rows]
    text = f"""🔧 <b>Отладка (админ)</b>

chat_id: {chat_id}
заблокировано ботов: {blocked}
user_contexts в памяти: {len(user_contexts)}
rate_limit_bot_max: {RATE_LIMIT_BOT_MAX} / {RATE_LIMIT_BOT_WINDOW}с
rate_limit_human_base: {RATE_LIMIT_HUMAN_BASE} (макс {RATE_LIMIT_HUMAN_MAX_CAP})

Последние действия:
{chr(10).join(log_lines) if log_lines else '—'}
"""
    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "debug")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast <text> — send message to all known users (admin only)."""
    chat_id = update.effective_chat.id

    if not is_admin(chat_id):
        await update.message.reply_text("❌ Только для администратора.")
        return

    # Access check
    allowed, reason = check_access(chat_id, "broadcast")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "broadcast")

    if not context.args:
        await update.message.reply_text("❌ Использование: /broadcast <текст>", parse_mode="HTML")
        return

    message_text = " ".join(context.args)

    # Get all known chat_ids
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT DISTINCT chat_id FROM telegram_users").fetchall()
        conn.close()
    except Exception:
        rows = []

    sent = 0
    for row in rows:
        target = row["chat_id"]
        try:
            await context.bot.send_message(chat_id=target, text=f"📢 <b>Рассылка:</b>\n\n{message_text}", parse_mode="HTML")
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"📢 Отправлено {sent} пользователям.", parse_mode="HTML")
    log_bot_action(chat_id, "broadcast", f"sent:{sent}")


# =============================================================================
# Message handlers
# =============================================================================

async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search query message."""
    chat_id = update.effective_chat.id

    # Access check
    allowed, reason = check_access(chat_id, "search")
    if not allowed:
        await update.message.reply_text(f"⏳ {reason}")
        return
    record_action(chat_id, "search")

    # If admin is in broadcast mode, treat message as broadcast content
    if is_admin(chat_id) and get_user_context(chat_id, "awaiting_broadcast"):
        clear_user_context(chat_id)
        message_text = update.message.text.strip()
        try:
            conn = get_db_connection()
            rows = conn.execute("SELECT DISTINCT chat_id FROM telegram_users").fetchall()
            conn.close()
        except Exception:
            rows = []

        sent = 0
        for row in rows:
            target = row["chat_id"]
            try:
                await context.bot.send_message(chat_id=target, text=f"📢 <b>Рассылка:</b>\n\n{message_text}", parse_mode="HTML")
                sent += 1
            except Exception:
                pass

        await update.message.reply_text(f"📢 Отправлено {sent} пользователям.", parse_mode="HTML")
        log_bot_action(chat_id, "broadcast", f"sent:{sent}")
        return

    query = update.message.text.strip()

    # --- URL detection: YouTube / SoundCloud -> direct download ---
    url_info = extract_url_source(query)
    if url_info:
        source, video_id, source_url = url_info
        settings = get_user_settings(chat_id)
        # Dedup check
        try:
            conn = get_db_connection()
            existing = conn.execute(
                "SELECT job_id, status FROM bot_jobs WHERE chat_id = ? AND video_id = ? "
                "AND status != 'failed' ORDER BY created_at DESC LIMIT 1",
                (chat_id, video_id),
            ).fetchone()
            conn.close()
        except Exception:
            existing = None
        if existing:
            await update.message.reply_text(
                f"⚠️ <b>Уже в очереди</b>\n\nЗадание #{existing['job_id'][:8]} уже имеет статус '{existing['status']}'.",
                parse_mode="HTML")
            return
        # Send download action
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        download_result = await download_track(
            video_id=video_id,
            title=query,
            artist="",
            source=source,
            convert_to_flac=settings["convert_to_flac"],
            chat_id=chat_id,
            source_url=source_url,
            update=update,
            query=query,
        )
        if not download_result:
            await update.message.reply_text("❌ Ошибка при добавлении в очередь.")
            return
        job_id = download_result.get("job_id")
        if not job_id:
            await update.message.reply_text("❌ Ошибка при добавлении в очередь.")
            return
        await update.message.reply_text(
            f"⏳ <b>Добавлено в очередь по ссылке</b>\n\n🔗 {html.escape(query)}\n\nПридёт уведомление о результате (или /queue для прогресса).",
            parse_mode="HTML")
        asyncio.create_task(_watch_job(job_id, chat_id, update.message, query))
        log_bot_action(chat_id, "download_url", f"{query}, job: {job_id}")
        return

    # --- Fuzzy search ---
    clear_user_context(chat_id)
    set_user_context(chat_id, "search_query", query)

    # Send typing action while searching
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Send searching message
    status_msg = await update.message.reply_text(f"🔍 Ищу: <b>{query}</b>...", parse_mode="HTML")

    # Get user settings
    settings = get_user_settings(chat_id)
    source = settings["search_source"]

    # Search: check cache first, then fuzzy API
    cached = get_cached_search(query)
    if cached:
        results = cached
        used_query = query
        logger.info(f"Search cache hit for query {query!r}: {len(results)} results")
    else:
        results, used_query = await search_music_fuzzy(query, source, limit=20)
        if results:
            save_search_results(used_query, source, results)

    if not results:
        await status_msg.edit_text("❌ Ничего не найдено. Попробуйте другой запрос.")
        return

    # Store results
    set_user_context(chat_id, "search_results", results)
    set_user_context(chat_id, "search_page", 0)

    # Update message with results
    total = len(results)
    shown = min(5, total)
    display_query = used_query if used_query != query else query
    text = f"""🎵 <b>Найдено: {total}</b>

Показано: {shown}
Запрос: {html.escape(display_query)}

Выберите трек для загрузки:"""

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=build_results_keyboard(results))
    log_bot_action(chat_id, "search", f"query: {query}, used: {used_query}, results: {total}")


# =============================================================================
# Callback handlers
# =============================================================================

async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    admin = is_admin(chat_id)

    text = f"""🎵 <b>MusicGrabber Bot</b>

Выберите действие:{' <b>(режим администратора)</b>' if admin else ''}"""

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_keyboard(admin=admin))


async def callback_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search menu callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "search")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "search")

    clear_user_context(chat_id)
    set_user_context(chat_id, "awaiting_search", True)

    text = """🔍 <b>Поиск музыки</b>

Введите название трека или "Исполнитель - Название"

Для отмены отправьте /cancel"""

    await query.edit_message_text(text, parse_mode="HTML")


async def callback_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle queue menu callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "queue")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "queue")

    await query.answer()

    chat_id = query.message.chat_id
    jobs = await get_user_jobs(chat_id)

    if not jobs:
        text = "📋 <b>Мои загрузки</b>\n\nПока нет загрузок."
        await query.edit_message_text(text, parse_mode="HTML")
        return

    text = f"📋 <b>Мои загрузки</b>\n\nВсего: {len(jobs)}"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_queue_keyboard(jobs))
    asyncio.create_task(_redeliver_missed(query.message.chat, jobs))


async def callback_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stats menu callback."""
    query = update.callback_query
    await query.answer()

    stats = await get_stats()
    if not stats:
        await query.edit_message_text("❌ Не удалось получить статистику.")
        return

    total = stats.get("total_jobs", 0)
    completed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    storage_gb = stats.get("storage_bytes", 0) / (1024**3)
    file_count = stats.get("file_count", 0)
    success_rate = (completed / total * 100) if total > 0 else 0

    text = f"""📊 <b>Статистика</b>

🎵 Всего: {total}
✅ Завершено: {completed} ({success_rate:.1f}%)
❌ Ошибок: {failed}
📦 Место: {storage_gb:.2f} GB
🎧 Треков: {file_count}"""

    await query.edit_message_text(text, parse_mode="HTML")


async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings menu callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "settings")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "settings")

    settings = get_user_settings(chat_id)
    admin = is_admin(chat_id)

    text = """⚙️ <b>Настройки</b>

Выберите параметр:"""
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings, admin=admin))


async def callback_set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle source setting callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "settings")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "settings")

    await query.answer()

    chat_id = query.message.chat_id
    settings = get_user_settings(chat_id)

    # Cycle through sources
    sources = ["youtube", "soundcloud", "monochrome", "all"]
    current_idx = sources.index(settings["search_source"]) if settings["search_source"] in sources else 0
    next_idx = (current_idx + 1) % len(sources)
    settings["search_source"] = sources[next_idx]

    save_user_settings(chat_id, settings)

    source_labels = {
        "youtube": "YouTube 🎞",
        "soundcloud": "SoundCloud 🎧",
        "monochrome": "Monochrome 💿",
        "all": "Все источники 🎯"
    }

    text = f"🔊 <b>Источник: {source_labels[sources[next_idx]]}</b>"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings", f"source: {sources[next_idx]}")


async def callback_set_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quality setting callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "settings")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "settings")

    await query.answer()

    settings = get_user_settings(chat_id)

    # Toggle FLAC
    settings["convert_to_flac"] = not settings["convert_to_flac"]
    save_user_settings(chat_id, settings)

    quality = "FLAC 🎼" if settings["convert_to_flac"] else "Оригинал 📻"

    text = f"🎼 <b>Качество: {quality}</b>"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings", f"flac: {settings['convert_to_flac']}")


async def callback_set_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle folder setting callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "settings")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "settings")

    await query.answer()

    settings = get_user_settings(chat_id)
    current_folder = settings.get("download_folder", "Singles")

    # Cycle through common folders
    folders = ["Singles", "Albums", "Compilations", "Incoming"]
    if current_folder in folders:
        next_idx = (folders.index(current_folder) + 1) % len(folders)
        settings["download_folder"] = folders[next_idx]
    else:
        settings["download_folder"] = folders[0]

    save_user_settings(chat_id, settings)

    text = f"📁 <b>Папка: {settings['download_folder']}</b>"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings", f"folder: {settings['download_folder']}")


async def _send_track_file(chat, job_id: str, job: Optional[dict], title: str) -> bool:
    """Fetch the finished file from the chat and send it into the chat.

    Tries cached CDN link first (direct URL, no app round-trip). If the CDN
    link returns an error, performs a fresh search to get a new source_url,
    updates the CDN cache, and retries. Falls back to /api/jobs/{id}/download.
    Downloads to a temp file first (streaming) to avoid loading large files
    entirely into memory. Falls back to False on any failure."""
    import tempfile
    try:
        # Look up video_id and query from bot_jobs
        video_id = None
        search_query = None
        try:
            conn = get_db_connection()
            row = conn.execute(
                "SELECT video_id, query FROM bot_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            conn.close()
            if row:
                video_id = row["video_id"]
                search_query = row["query"]
        except Exception:
            pass

        async def _download_from_url(url: str) -> str | None:
            """Stream a URL to a temp file. Returns temp path or None."""
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return None
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > 49 * 1024 * 1024:
                        logger.warning(f"job {job_id}: file over 50MB cap")
                        return None  # special: too large
                    suffix = os.path.splitext((job or {}).get("final_path") or "")[1] or ".mp3"
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        async for chunk in resp.aiter_bytes():
                            tmp.write(chunk)
                        return tmp.name
            return None

        tmp_path = None
        cdn_url = None

        # Try cached CDN link first
        if video_id:
            cdn_url = get_cached_cdn_link(video_id)

        if cdn_url:
            tmp_path = await _download_from_url(cdn_url)
            if tmp_path is None:
                # CDN link is dead: remove from cache and do a fresh search
                logger.warning(f"job {job_id}: CDN link dead, refreshing via search")
                try:
                    conn = get_db_connection()
                    conn.execute("DELETE FROM cdn_links WHERE video_id = ?", (video_id,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                cdn_url = None

        # If CDN failed or missing, do a fresh search to get a new source_url
        if tmp_path is None and search_query:
            try:
                fresh = await search_music(search_query, "all", limit=5)
                for r in fresh:
                    if r.get("video_id") == video_id and r.get("source_url"):
                        new_url = r["source_url"]
                        # Update CDN cache
                        try:
                            conn2 = get_db_connection()
                            save_cdn_link(conn2, video_id, new_url, new_url, r.get("source", "all"))
                            conn2.commit()
                            conn2.close()
                        except Exception:
                            pass
                        cdn_url = new_url
                        break
            except Exception as exc:
                logger.warning(f"job {job_id}: fresh search failed: {exc}")

        # Try the freshly fetched CDN URL
        if cdn_url and tmp_path is None:
            tmp_path = await _download_from_url(cdn_url)

        # Fall back to app download endpoint
        if tmp_path is None:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "GET",
                    f"{API_BASE_URL}/api/jobs/{job_id}/download",
                    headers=_api_headers(),
                ) as resp:
                    if resp.status_code != 200:
                        logger.warning(f"job {job_id}: download endpoint returned {resp.status_code}")
                        return False
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > 49 * 1024 * 1024:
                        logger.warning(f"job {job_id}: file over 50MB Telegram cap")
                        return False
                    suffix = os.path.splitext((job or {}).get("final_path") or "")[1] or ".mp3"
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        async for chunk in resp.aiter_bytes():
                            tmp.write(chunk)
                        tmp_path = tmp.name

        filename = os.path.basename((job or {}).get("final_path") or "") or "track.mp3"
        with open(tmp_path, "rb") as f:
            await chat.send_document(document=f, filename=filename, caption=f"🎵 {title}")
        os.unlink(tmp_path)
        conn = get_db_connection()
        conn.execute("UPDATE bot_jobs SET delivered = 1 WHERE job_id = ?", (job_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.warning(f"job {job_id}: sending file failed: {exc}")
        try:
            os.unlink(tmp_path)  # type: ignore[name-defined]
        except Exception:
            pass
        return False


async def _watch_job(job_id: str, chat_id: int, message, title: str,
                     interval: float = 5.0, max_polls: int = 180):
    """Poll a queued job until it completes/fails, then notify the chat.
    Fire-and-forget: without it the user never learns the outcome (looks
    like a hang). ~15 min cap at 5s. Shows progress updates in the message."""
    status = "unknown"
    progress = None
    last_progress_text = None
    for _ in range(max_polls):
        await asyncio.sleep(interval)
        data = await get_job_status(job_id)
        if not data:
            continue
        status = data.get("status", "unknown")
        progress = data.get("progress")
        # Update message with progress while still running
        if status in ("queued", "downloading"):
            prog_text = f" ({progress}%)" if progress is not None else ""
            new_text = f"⏳ <b>Загружается</b>\n\n🎵 {html.escape(title)}{prog_text}"
            if new_text != last_progress_text:
                try:
                    await message.edit_text(new_text, parse_mode="HTML")
                    last_progress_text = new_text
                except Exception:
                    pass  # message not modified or other error
        if status in ("completed", "failed"):
            break
    safe_title = html.escape(title)
    if status == "completed":
        delivered = await _send_track_file(message.chat, job_id, data, title)
        if delivered:
            return
        await message.chat.send_message(f"✅ <b>Загружено</b>\n\n🎵 {safe_title}", parse_mode="HTML")
    elif status == "failed":
        err = format_error((data or {}).get("error") or "неизвестная ошибка")
        await message.chat.send_message(f"❌ <b>Ошибка загрузки</b>\n\n🎵 {safe_title}\nПричина: {err}", parse_mode="HTML")
    else:
        logger.info(f"job {job_id}: watcher gave up after {max_polls} polls (last: {status})")
        try:
            await message.edit_text(f"⏰ <b>Таймаут загрузки</b>\n\n🎵 {safe_title}\nПревышено время ожидания ({max_polls * interval:.0f} сек).", parse_mode="HTML")
        except Exception:
            pass


async def callback_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle download callback."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "download")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "download")

    # Per-user cooldown between downloads (admin bypasses)
    if not is_admin(chat_id):
        now = datetime.now().timestamp()
        last = _last_download.get(chat_id, 0)
        if now - last < DOWNLOAD_COOLDOWN_SEC:
            remaining = int(DOWNLOAD_COOLDOWN_SEC - (now - last))
            await query.answer(f"⏳ Подождите {remaining} сек между скачками.")
            return
        _last_download[chat_id] = now

    await query.answer("⏳ Добавлено в очередь...")

    result_idx = int(query.data.split("_")[1])

    # Get result
    results = get_user_context(chat_id, "search_results", [])
    if result_idx >= len(results):
        await query.edit_message_text("❌ Ошибка: результат не найден.")
        return

    track = results[result_idx]

    # Get settings
    settings = get_user_settings(chat_id)

    # Send upload_document action while downloading
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    # Dedup: check if user already has a non-failed job for this track
    track_key = ((track.get("artist") or track.get("channel") or "").lower(),
                 (track.get("title") or "").lower())
    try:
        conn = get_db_connection()
        existing = conn.execute(
            "SELECT job_id, status FROM bot_jobs WHERE chat_id = ? AND lower(title) = ? AND lower(artist) = ? "
            "AND status != 'failed' ORDER BY created_at DESC LIMIT 1",
            (chat_id, track_key[1], track_key[0]),
        ).fetchone()
        conn.close()
    except Exception:
        existing = None

    if existing:
        await query.edit_message_text(
            f"⚠️ <b>Уже в очереди</b>\n\n"
            f"🎵 {html.escape(track.get('title', 'Unknown'))}\n"
            f"🎤 {html.escape(track.get('artist') or track.get('channel', 'Unknown'))}\n\n"
            f"Задание #{existing['job_id'][:8]} уже имеет статус '{existing['status']}'.",
            parse_mode="HTML",
            reply_markup=build_results_keyboard(results),
        )
        return

    # Download
    search_query = get_user_context(chat_id, "search_query", "")
    download_result = await download_track(
        video_id=track["video_id"],
        title=track.get("title", "Unknown"),
        artist=track.get("artist") or track.get("channel", "Unknown"),
        source=track.get("source", "youtube"),
        convert_to_flac=settings["convert_to_flac"],
        chat_id=chat_id,
        source_url=track.get("source_url"),
        update=update,
        query=search_query,
    )

    if not download_result:
        await query.edit_message_text("❌ Ошибка при добавлении в очередь.")
        return

    job_id = download_result.get("job_id")
    if not job_id:
        await query.edit_message_text("❌ Ошибка при добавлении в очередь.")
        return

    text = f"""⏳ <b>Добавлено в очередь</b>

🎵 {track.get('title', 'Unknown')}
🎤 {track.get('artist') or track.get('channel', 'Unknown')}

Придёт уведомление о результате (или /queue для прогресса)."""

    asyncio.create_task(_watch_job(job_id, chat_id, query.message, track.get('title', 'Unknown')))

    await query.edit_message_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "download", f"{track.get('title')}, job: {job_id}")




async def callback_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle page navigation callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    page = int(query.data.split("_")[1])

    results = get_user_context(chat_id, "search_results", [])
    search_query = get_user_context(chat_id, "search_query", "")

    set_user_context(chat_id, "search_page", page)

    total = len(results)
    shown = min((page + 1) * 5, total)

    text = f"""🎵 <b>Найдено: {total}</b>

Показано: {shown}
Запрос: {search_query}

Выберите трек:"""

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_results_keyboard(results, page))


async def callback_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search cancel callback."""
    query = update.callback_query
    await query.answer()

    clear_user_context(query.message.chat_id)
    await query.edit_message_text("❌ Поиск отменен.", reply_markup=build_main_keyboard())


async def callback_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle no-op callback."""
    await update.callback_query.answer()


async def callback_queue_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-render the queue keyboard with fresh statuses."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "queue")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return

    jobs = await get_user_jobs(chat_id)
    text = f"📋 <b>Мои загрузки</b>\n\nВсего: {len(jobs)}"
    record_action(chat_id, "queue")
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_queue_keyboard(jobs))
    except Exception:
        pass  # nothing changed -> Telegram rejects identical edits
    asyncio.create_task(_redeliver_missed(query.message.chat, jobs))


async def callback_queue_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drop this chat's job registry (app-side library/downloads untouched)."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "queue")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return

    conn = get_db_connection()
    record_action(chat_id, "queue")
    conn.execute("DELETE FROM bot_jobs WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    await query.answer("Очередь очищена")
    await query.edit_message_text(
        "🗑 <b>Очередь очищена</b>\n\nИстория загрузок убрана, файлы в библиотеке не тронуты.",
        parse_mode="HTML")
    log_bot_action(chat_id, "queue_clear")


async def callback_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry a failed job via the app API and watch it to deliver the file."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "download")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "download")

    job_id = query.data.split("_", 1)[1]
    await query.answer()

    conn = get_db_connection()
    row = conn.execute("SELECT title FROM bot_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    title = (row["title"] if row else None) or "Трек"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/jobs/{job_id}/retry",
                headers=_api_headers(),
            )
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Retry error: {e}")
        await query.edit_message_text(f"❌ Не удалось повторить: {format_error(str(e))}", parse_mode="HTML")
        return

    asyncio.create_task(_watch_job(job_id, chat_id, query.message, title))
    await query.edit_message_text(
        f"🔄 <b>Повтор запущен</b>\n\n🎵 {html.escape(title)}\n\nПридёт уведомление о результате.",
        parse_mode="HTML")
    log_bot_action(chat_id, "retry", job_id)


async def callback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a queued/downloading job via the app API."""
    query = update.callback_query
    chat_id = query.message.chat_id

    # Access check
    allowed, reason = check_access(chat_id, "download")
    if not allowed:
        await query.answer(f"⏳ {reason}")
        return
    record_action(chat_id, "download")

    job_id = query.data.split("_", 1)[1]
    await query.answer()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/jobs/{job_id}/cancel",
                headers=_api_headers(),
            )
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Cancel error: {e}")
        await query.edit_message_text(f"❌ Не удалось отменить: {e}", parse_mode="HTML")
        return

    await query.edit_message_text(
        f"🛑 <b>Задание отменено</b>\n\n🎵 Job: {job_id}",
        parse_mode="HTML")
    log_bot_action(chat_id, "cancel", job_id)


# =============================================================================
# Bot setup
# =============================================================================

def setup_bot():
    """Setup bot application."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return None

    # Init database tables
    init_telegram_tables()
    # Clean expired cache entries
    clear_expired_cache()

    # Create application. concurrent_updates: a slow handler (httpx to the app,
    # up to 30s timeout) must not block answering other button presses.
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True)
    # Local Bot API stand (e.g. tgfake) for e2e tests without real Telegram
    api_url = os.getenv("TELEGRAM_API_URL")
    if api_url:
        # PTB expects the full prefix incl. /bot (default https://api.telegram.org/bot)
        builder = builder.base_url(api_url.rstrip("/") + "/bot")
    app = builder.build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_query))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(callback_main_menu, pattern="^menu_main$"))
    app.add_handler(CallbackQueryHandler(callback_search, pattern="^menu_search$"))
    app.add_handler(CallbackQueryHandler(callback_queue, pattern="^menu_queue$"))
    app.add_handler(CallbackQueryHandler(callback_stats, pattern="^menu_stats$"))
    app.add_handler(CallbackQueryHandler(callback_settings, pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(callback_help, pattern="^menu_help$"))
    app.add_handler(CallbackQueryHandler(callback_menu_debug, pattern="^menu_debug$"))
    app.add_handler(CallbackQueryHandler(callback_menu_broadcast, pattern="^menu_broadcast$"))
    app.add_handler(CallbackQueryHandler(callback_set_source, pattern="^set_source$"))
    app.add_handler(CallbackQueryHandler(callback_set_quality, pattern="^set_quality$"))
    app.add_handler(CallbackQueryHandler(callback_set_folder, pattern="^set_folder$"))
    app.add_handler(CallbackQueryHandler(callback_download, pattern="^download_\\d+$"))
    app.add_handler(CallbackQueryHandler(callback_page, pattern="^page_\\d+$"))
    app.add_handler(CallbackQueryHandler(callback_search_cancel, pattern="^search_cancel$"))
    app.add_handler(CallbackQueryHandler(callback_queue_refresh, pattern="^queue_refresh$"))
    app.add_handler(CallbackQueryHandler(callback_queue_clear, pattern="^queue_clear$"))
    app.add_handler(CallbackQueryHandler(callback_retry, pattern="^retry_"))
    app.add_handler(CallbackQueryHandler(callback_cancel, pattern="^cancel_"))
    app.add_handler(CallbackQueryHandler(callback_noop, pattern="^noop$"))

    return app


def run_bot():
    """Run bot (blocking, polling mode)."""
    app = setup_bot()
    if not app:
        logger.error("Failed to setup bot")
        return

    logger.info("Starting MusicGrabber Telegram bot (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


async def run_bot_async():
    """Run bot asynchronously (for integration with FastAPI)."""
    app = setup_bot()
    if not app:
        logger.error("Failed to setup bot")
        return

    logger.info("Starting MusicGrabber Telegram bot (async)...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)


def run_bot_process():
    """Run bot in separate process (for background execution)."""
    import multiprocessing
    process = multiprocessing.Process(target=run_bot, daemon=True)
    process.start()
    logger.info("Telegram bot started in background process")
    return process


# =============================================================================
# For running standalone
# =============================================================================

if __name__ == "__main__":
    run_bot()
