"""Smoke tests - these must all pass before anything else is worth running."""


def test_service_reachable(api, base_url):
    r = api.get(f"{base_url}/api/config", timeout=10)
    assert r.status_code == 200


def test_config_shape(api, base_url):
    r = api.get(f"{base_url}/api/config", timeout=10)
    d = r.json()
    for key in ("version", "default_convert_audio", "audio_format", "music_dir", "auth_required", "volume_mounted", "listen_addr", "listen_port"):
        assert key in d, f"config missing key: {key}"
    assert isinstance(d["version"], str) and d["version"]
    assert d["default_convert_audio"] == d["default_convert_to_flac"]
    assert d["volume_mounted"] is True, "volume not mounted - is Docker running correctly?"
    assert isinstance(d["listen_addr"], str) and d["listen_addr"]
    assert isinstance(d["listen_port"], int) and 1 <= d["listen_port"] <= 65535


def test_sources_shape(api, base_url):
    r = api.get(f"{base_url}/api/sources", timeout=10)
    assert r.status_code == 200
    sources = r.json()["sources"]
    assert isinstance(sources, list)
    assert len(sources) >= 1, "no sources registered"
    for src in sources:
        for key in ("id", "label", "badge", "colour", "enabled", "has_preview"):
            assert key in src, f"source missing key: {key}"


def test_expected_sources_present(api, base_url):
    sources = api.get(f"{base_url}/api/sources", timeout=10).json()["sources"]
    ids = {s["id"] for s in sources}
    for expected in ("youtube", "soundcloud", "zvu4no", "freemp3cloud", "jiosaavn"):
        assert expected in ids, f"source '{expected}' is missing"


def test_ui_reachable(api, base_url):
    r = api.get(f"{base_url}/", timeout=10)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("Content-Type", "")
