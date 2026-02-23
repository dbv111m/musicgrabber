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

from constants import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW
from settings import get_setting


# In-memory rate limiting store: {ip: [timestamps]}
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_rate_limit_lock = threading.Lock()
_rate_limit_last_cleanup = 0.0

# Paths that don't require authentication (static files, health checks)
AUTH_EXEMPT_PATHS = {"/", "/static", "/api/config"}


def _get_client_ip(request: Request) -> str:
    """Get client IP, respecting X-Forwarded-For for reverse proxies."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware for API key authentication and rate limiting."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for exempt paths
        if path == "/" or path.startswith("/static"):
            return await call_next(request)

        # Get configured API key
        api_key = get_setting("api_key", "")

        # If API key is configured, enforce authentication
        if api_key:
            # Config endpoint is always accessible (needed for frontend to know auth is required)
            if path != "/api/config":
                request_key = request.headers.get("x-api-key", "")
                if not hmac.compare_digest(request_key, api_key):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid or missing API key"},
                        headers={"WWW-Authenticate": "API-Key"}
                    )

        # Rate limiting (applied to all API requests)
        if path.startswith("/api"):
            client_ip = _get_client_ip(request)
            allowed, remaining = _check_rate_limit(client_ip)

            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."},
                    headers={
                        "Retry-After": str(RATE_LIMIT_WINDOW),
                        "X-RateLimit-Limit": str(RATE_LIMIT_REQUESTS),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time() + RATE_LIMIT_WINDOW))
                    }
                )

            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return response

        return await call_next(request)
