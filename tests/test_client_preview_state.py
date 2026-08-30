"""Static regression guards for the browser preview state machine.

The UI is deliberately dependency-free, so the project does not carry a JS test
runner. These checks keep the safety-critical cancellation and identity pieces
from disappearing; computed behaviour is also exercised in the container's
installed Chromium during review.
"""

from pathlib import Path


APP_JS = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")


def test_preview_requests_are_abortable_and_stale_completions_are_guarded():
    assert "new AbortController()" in APP_JS
    assert "previewAbortController.abort()" in APP_JS
    assert APP_JS.count("requestId !== previewRequestSerial") >= 3


def test_preview_identity_includes_source_and_source_url():
    assert "const source = (result && result.source) || 'youtube';" in APP_JS
    assert "const identity = (result && result.source_url) || videoId;" in APP_JS
    assert "previewCache.get(cacheKey)" in APP_JS
    assert "previewCache.set(cacheKey" in APP_JS


def test_duplicate_in_flight_preview_is_not_started_again():
    assert "if (previewRequestKey === cacheKey) return;" in APP_JS
    assert APP_JS.count("previewIsActiveOrLoading(videoId, result)") >= 3
