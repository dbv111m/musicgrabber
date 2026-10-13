"""
MusicGrabber - Authentication & Rate Limiting Middleware
"""

import hmac
import time
import threading
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from constants import (
    ALLOW_API_KEY_QUERY_PARAM,
    HSTS_MAX_AGE,
    HTTPS_ONLY,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW,
)
from settings import get_setting


# In-memory rate limiting store: {ip: [timestamps]}
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_rate_limit_lock = threading.Lock()
_rate_limit_last_cleanup = 0.0

# Only trust X-Forwarded-For from these direct connection IPs (i.e. reverse proxies on localhost)
_TRUSTED_PROXY_IPS = {"127.0.0.1", "::1"}

# Users-exist cache (avoid a DB hit on every single request)
_users_exist_cache: bool | None = None
_users_exist_cache_time: float = 0.0
_USERS_EXIST_CACHE_TTL = 30.0  # seconds


def _get_client_ip(request: Request) -> str:
    """Get client IP. Only trusts X-Forwarded-For when the direct connection is from a
    known local reverse proxy - prevents external clients spoofing the header to bypass
    rate limiting."""
    direct_ip = request.client.host if request.client else None
    if direct_ip in _TRUSTED_PROXY_IPS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return direct_ip or "unknown"


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    direct_ip = request.client.host if request.client else None
    if direct_ip in _TRUSTED_PROXY_IPS:
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto:
            return forwarded_proto.split(",")[0].strip().lower() == "https"
    return False


def _download_job_id_from_path(path: str) -> str | None:
    # Expected shape: /api/jobs/{job_id}/download
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "download":
        return parts[2]
    return None


def _is_audio_stream_path(path: str) -> bool:
    """The two endpoints an <audio> element points its src straight at.

    Deliberately exact rather than a tidy `endswith("/stream")`, because
    /api/search/stream is a very different animal and has no business
    accepting a playback token.
    """
    parts = path.strip("/").split("/")
    if parts == ["api", "trash", "stream"]:
        return True
    return (
        len(parts) == 4
        and parts[0] == "api"
        and parts[1] == "jobs"
        and parts[3] == "stream"
    )


def _apply_security_headers(request: Request, response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if _is_https_request(request):
        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={max(0, int(HSTS_MAX_AGE))}; includeSubDomains",
        )
    return response


def _secure_json_response(request: Request, status_code: int, content: dict, headers: dict | None = None):
    response = JSONResponse(status_code=status_code, content=content, headers=headers)
    return _apply_security_headers(request, response)


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """Check if IP is within rate limit. Returns (allowed, remaining)."""
    global _rate_limit_last_cleanup
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    with _rate_limit_lock:
        # Clean old entries
        _rate_limit_store[ip] = [t for t in _rate_limit_store[ip] if t > window_start]

        # Periodic cleanup of stale IPs to avoid unbounded growth
        if now - _rate_limit_last_cleanup > RATE_LIMIT_WINDOW:
            stale_ips = [
                addr for addr, timestamps in _rate_limit_store.items()
                if not timestamps or max(timestamps) <= window_start
            ]
            for addr in stale_ips:
                _rate_limit_store.pop(addr, None)
            _rate_limit_last_cleanup = now

        current_count = len(_rate_limit_store[ip])
        if current_count >= RATE_LIMIT_REQUESTS:
            return False, 0

        _rate_limit_store[ip].append(now)
        return True, RATE_LIMIT_REQUESTS - current_count - 1


def _check_users_exist() -> bool:
    """Check if multi-user mode is active (2+ accounts). Cached for 30s.

    A single account is treated the same as no accounts: no login required.
    The owner deleting all guest accounts returns cleanly to single-user mode.
    """
    global _users_exist_cache, _users_exist_cache_time
    now = time.time()
    if _users_exist_cache is not None and (now - _users_exist_cache_time) < _USERS_EXIST_CACHE_TTL:
        return _users_exist_cache
    try:
        from db import db_conn
        with db_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        _users_exist_cache = count >= 2
    except Exception:
        _users_exist_cache = False
    _users_exist_cache_time = now
    return _users_exist_cache


def invalidate_users_cache():
    """Call this after creating or deleting users so the cache refreshes promptly."""
    global _users_exist_cache
    _users_exist_cache = None


# Public paths that never require auth
_PUBLIC_PATHS = {"/api/auth/login", "/api/config"}


class AuthMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if HTTPS_ONLY and not _is_https_request(request):
            return _secure_json_response(
                request,
                status_code=400,
                content={"detail": "HTTPS is required"},
            )

        # Static files and root are always public
        if path == "/" or path.startswith("/static"):
            return _apply_security_headers(request, await call_next(request))

        # Rate limiting on all API paths
        remaining = RATE_LIMIT_REQUESTS
        if path.startswith("/api"):
            client_ip = _get_client_ip(request)
            allowed, remaining = _check_rate_limit(client_ip)
            if not allowed:
                return _secure_json_response(
                    request,
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."},
                    headers={
                        "Retry-After": str(RATE_LIMIT_WINDOW),
                        "X-RateLimit-Limit": str(RATE_LIMIT_REQUESTS),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time() + RATE_LIMIT_WINDOW)),
                    }
                )

        # Public API paths need no auth
        if path in _PUBLIC_PATHS:
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return _apply_security_headers(request, response)

        # Determine operating mode
        users_exist = _check_users_exist()

        user = None

        if users_exist:
            # Session mode: try Bearer token first
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
                if token:
                    from auth import get_session_user
                    user = get_session_user(token)

            # Single-use download token for browser file downloads.
            if user is None:
                download_token = request.query_params.get("download_token", "")
                job_id = _download_job_id_from_path(path)
                if download_token and job_id:
                    from auth import consume_download_token
                    user = consume_download_token(download_token, job_id)

            # Library and trash playback: same headerless <audio> problem as the
            # download button below, but a player range-requests and seeks its
            # way through a track, so this token is reusable rather than single
            # use. It only identifies the caller; the endpoints themselves still
            # decide what that caller is allowed to hear.
            if user is None and _is_audio_stream_path(path):
                stream_token = request.query_params.get("stream_token", "")
                if stream_token:
                    from auth import resolve_stream_token
                    user = resolve_stream_token(stream_token)

            # API key fallback for backwards compatibility with scripts
            if user is None:
                api_key = get_setting("api_key", "")
                if api_key:
                    request_key = request.headers.get("x-api-key")
                    if not request_key and ALLOW_API_KEY_QUERY_PARAM:
                        request_key = request.query_params.get("api_key", "")
                    if request_key and hmac.compare_digest(request_key, api_key):
                        user = {"id": None, "username": "api_key", "role": "admin",
                                "force_password_change": False, "is_active": 1}

            if user is None:
                return _secure_json_response(
                    request,
                    status_code=401,
                    content={"detail": "Authentication required"},
                    headers={"WWW-Authenticate": "Bearer"}
                )
        else:
            # Single-user mode: check legacy API key if set, otherwise open access
            api_key = get_setting("api_key", "")
            if api_key and path != "/api/config":
                request_key = request.headers.get("x-api-key")
                if not request_key and ALLOW_API_KEY_QUERY_PARAM:
                    request_key = request.query_params.get("api_key", "")
                if not request_key or not hmac.compare_digest(request_key, api_key):
                    return _secure_json_response(
                        request,
                        status_code=401,
                        content={"detail": "Invalid or missing API key"},
                        headers={"WWW-Authenticate": "API-Key"}
                    )
            # Everyone is admin in single-user mode; no password shenanigans
            user = {"id": None, "username": "single_user", "role": "admin",
                    "force_password_change": False, "is_active": 1}

        # Attach user context to request state for route handlers
        request.state.user = user
        request.state.user_id = user.get("id")
        request.state.is_admin = user.get("role") == "admin"

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return _apply_security_headers(request, response)
