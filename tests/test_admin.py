"""Admin endpoint tests - stats, mismatches, blacklist."""


def test_stats_shape(api, base_url):
    r = api.get(f"{base_url}/api/stats", timeout=30)
    assert r.status_code == 200
    d = r.json()
    for key in ("total_jobs", "completed", "failed", "sources", "daily"):
        assert key in d, f"stats missing key: {key}"
    assert isinstance(d["total_jobs"], int)
    assert isinstance(d["sources"], dict)
    assert isinstance(d["daily"], list)


def test_mismatches_list(api, base_url):
    r = api.get(f"{base_url}/api/mismatches", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "mismatches" in d
    assert isinstance(d["mismatches"], list)


def test_blacklist_list(api, base_url):
    r = api.get(f"{base_url}/api/blacklist", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "entries" in d or "blacklist" in d, f"unexpected blacklist response shape: {list(d.keys())}"


def test_music_dirs(api, base_url):
    r = api.get(f"{base_url}/api/music-dirs", timeout=10)
    assert r.status_code == 200


def test_playlists_list(api, base_url):
    """M3U playlists available for routing selector."""
    r = api.get(f"{base_url}/api/playlists", timeout=10)
    assert r.status_code == 200


def test_bulk_imports_list(api, base_url):
    r = api.get(f"{base_url}/api/bulk-imports", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "imports" in d or "bulk_imports" in d, f"unexpected shape: {list(d.keys())}"


def test_sources_health_shape(api, base_url):
    r = api.get(f"{base_url}/api/sources/health", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "sources" in d
    assert isinstance(d["sources"], list)
    if d["sources"]:
        item = d["sources"][0]
        for key in ("id", "label", "healthy", "reason", "checked_at", "retry_at", "available", "enabled"):
            assert key in item, f"source health missing key: {key}"


def test_sources_health_recheck(api, base_url):
    r = api.post(f"{base_url}/api/sources/health/recheck", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "checking"


def test_audio_audit_status_shape(api, base_url):
    r = api.get(f"{base_url}/api/audio-audit", timeout=10)
    assert r.status_code == 200
    data = r.json()
    for key in ("criteria_version", "read_only_notice", "running", "current", "summary", "options"):
        assert key in data, f"audio audit status missing key: {key}"
    assert "no audio files were renamed" in data["read_only_notice"].lower()


def test_audio_audit_rejects_ambiguous_lossless_filter(api, base_url):
    r = api.get(
        f"{base_url}/api/audio-audit/files?lossless_only=yes-really",
        timeout=10,
    )
    assert r.status_code == 400


def test_audio_audit_dry_run_json_export(api, base_url):
    r = api.get(f"{base_url}/api/audio-audit/export?format=json", timeout=30)
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")
    report = r.json()
    assert "metadata" in report
    assert "files" in report
    assert "no audio files were renamed" in report["metadata"]["read_only_notice"].lower()
