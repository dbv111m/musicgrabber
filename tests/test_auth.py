"""Auth endpoint tests."""

import pytest


def test_me_returns_user(api, base_url):
    r = api.get(f"{base_url}/api/auth/me", timeout=10)
    assert r.status_code == 200
    d = r.json()
    for key in ("id", "username", "role", "force_password_change"):
        assert key in d, f"/api/auth/me missing key: {key}"


def test_me_single_user_is_admin(api, base_url, is_single_user):
    if not is_single_user:
        pytest.skip("multi-user mode - single-user admin check not applicable")
    d = api.get(f"{base_url}/api/auth/me", timeout=10).json()
    assert d["role"] == "admin"


def test_login_bad_credentials(api, base_url, is_single_user):
    if is_single_user:
        pytest.skip("single-user mode - login endpoint not exercised")
    r = api.post(
        f"{base_url}/api/auth/login",
        json={"username": "nobody", "password": "wrongpassword"},
        timeout=10,
    )
    assert r.status_code == 401


def test_login_lockout_response_shape(api, base_url, is_single_user):
    """Bad login returns a consistent error shape, not a 500."""
    if is_single_user:
        pytest.skip("single-user mode")
    r = api.post(
        f"{base_url}/api/auth/login",
        json={"username": "testlockout", "password": "badpass"},
        timeout=10,
    )
    assert r.status_code in (401, 429)
    d = r.json()
    assert "detail" in d
