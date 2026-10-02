#!/bin/bash
# Run the MusicGrabber integration test suite.
# Usage:
#   ./run_tests.sh           - fast tests only (no external network calls)
#   ./run_tests.sh --slow    - all tests including slow external-service tests
#   ./run_tests.sh --all     - alias for --slow

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# Load local credentials (MG_BASE_URL / MG_USERNAME / MG_PASSWORD) if present.
# This file is gitignored; the suite authenticates with it when the target
# instance is in multi-user mode. Without it, tests assume single-user mode.
CREDS="$SCRIPT_DIR/.test-credentials"
if [ -f "$CREDS" ]; then
    # shellcheck disable=SC1090
    source "$CREDS"
fi

# NOTE: always restart the local docker container before running the suite, so
# it's serving the current code rather than a stale image. e.g.
#   docker compose up -d --build music-grabber   (or: docker restart music-grabber)
echo "Reminder: restart the local container first so tests run against current code (docker restart music-grabber)."
if [ "${MG_ALLOW_HISTORY_DELETION:-}" = "1" ]; then
    echo "WARNING: Queue-history deletion is enabled. Use this only against a disposable test instance."
else
    echo "Safety: Queue-history deletion is blocked; test-created Queue rows will be retained."
fi

if [ ! -d "$VENV" ]; then
    echo "Creating venv..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q pytest requests
    # Project deps needed for unit tests that import the downloads / db / settings
    # modules directly (otherwise those tests silently skip).
    "$VENV/bin/pip" install -q httpx mutagen bcrypt fastapi pydantic apprise pyflakes
fi

if [ ! -x "$VENV/bin/pyflakes" ]; then
    "$VENV/bin/pip" install -q pyflakes
fi

if ! "$VENV/bin/python" -c "import Crypto" >/dev/null 2>&1; then
    "$VENV/bin/pip" install -q 'pycryptodome~=3.23.0'
fi

if ! "$VENV/bin/python" -c "import curl_cffi" >/dev/null 2>&1; then
    "$VENV/bin/pip" install -q 'curl_cffi~=0.13.0'
fi

PYTEST="$VENV/bin/pytest"
PYFLAKES="$VENV/bin/pyflakes"

echo "Running undefined-name/static analysis..."
"$PYFLAKES" "$SCRIPT_DIR"/../*.py

if [[ "$1" == "--slow" || "$1" == "--all" ]]; then
    shift  # consume our flag so it doesn't reach pytest, which has no idea what --slow means
    echo "Running ALL tests (including slow external-service tests)..."
    "$PYTEST" "$SCRIPT_DIR" "$@"
else
    echo "Running fast tests only. Use --slow to include external-service tests."
    "$PYTEST" "$SCRIPT_DIR" -m "not slow" "$@"
fi
