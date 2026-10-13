"""
Setting the singles "from date" on an artist you already follow.

The gap this covers: follow an artist for albums only and the row still gets a
from_date, quietly set to the day you followed them. Switch singles on later and
that date is what decides how much of their back catalogue arrives, with no way
to say otherwise. The API always accepted from_date on update; nothing ever sent
it. Now the toggle asks for a date first, and the card lets you change it after.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Radiohead: stable MBID, and nobody minds if we follow and unfollow them a lot.
_RADIOHEAD_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
_RADIOHEAD_NAME = "Radiohead"
# Far enough in the future that no refresh can decide a real single qualifies.
_SAFE_DATE = "2099-01-01"
_LATER_DATE = "2099-06-30"


def _import_models_or_skip():
    try:
        import models
    except ModuleNotFoundError as exc:
        pytest.skip(f"models dependencies unavailable: {exc.name}")
    return models


# ---------------------------------------------------------------------------
# The date has to actually be a date
# ---------------------------------------------------------------------------

def test_a_real_date_is_accepted():
    models = _import_models_or_skip()

    assert models.WatchedArtistUpdate(from_date="2026-08-09").from_date == "2026-08-09"


def test_leaving_the_date_out_entirely_is_still_fine():
    """Every other card control updates one field and sends nothing else."""
    models = _import_models_or_skip()

    assert models.WatchedArtistUpdate(watch_singles=True).from_date is None


@pytest.mark.parametrize("rubbish", ["banana", "09-08-2026", "2026-13-01", "2026-02-30", ""])
def test_anything_that_is_not_a_date_is_refused(rubbish):
    """from_date is compared against MusicBrainz dates as a plain string, so a
    non-date sorts somewhere daft and silently filters out everything or nothing.
    Refusing it at the door beats debugging that later."""
    models = _import_models_or_skip()

    with pytest.raises(Exception):
        models.WatchedArtistUpdate(from_date=rubbish)


# ---------------------------------------------------------------------------
# The round trip, against a live instance
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_enabling_singles_can_set_the_from_date_in_the_same_breath(api, base_url):
    """The whole point: switch singles on and choose the start date at once, so
    there is never a window where the artist is watching singles from a date
    nobody picked."""
    r = api.post(
        f"{base_url}/api/watched-artists",
        json={
            "mbid": _RADIOHEAD_MBID,
            "name": _RADIOHEAD_NAME,
            "from_date": _SAFE_DATE,
            "watch_singles": False,
            "auto_add_albums": True,  # albums-only follow, the case that has the gap
        },
        timeout=90,
    )
    assert r.status_code == 200, r.text
    artist_id = r.json().get("id")
    assert artist_id, f"no id in response: {r.json()}"

    try:
        r = api.put(
            f"{base_url}/api/watched-artists/{artist_id}",
            json={"watch_singles": True, "from_date": _LATER_DATE},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        updated = r.json()
        assert updated["watch_singles"] == 1 or updated["watch_singles"] is True
        assert updated["from_date"] == _LATER_DATE, \
            f"singles were switched on but kept the old date: {updated['from_date']}"
    finally:
        api.delete(f"{base_url}/api/watched-artists/{artist_id}", timeout=10)


@pytest.mark.slow
def test_the_date_can_still_be_changed_afterwards(api, base_url):
    """Second half of the ask: having set it, you can move it without unfollowing."""
    r = api.post(
        f"{base_url}/api/watched-artists",
        json={
            "mbid": _RADIOHEAD_MBID,
            "name": _RADIOHEAD_NAME,
            "from_date": _SAFE_DATE,
            "watch_singles": True,
        },
        timeout=90,
    )
    assert r.status_code == 200, r.text
    artist_id = r.json().get("id")

    try:
        r = api.put(
            f"{base_url}/api/watched-artists/{artist_id}",
            json={"from_date": _LATER_DATE},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        assert r.json()["from_date"] == _LATER_DATE

        # And it survives a read, rather than only looking right in the response.
        listed = api.get(f"{base_url}/api/watched-artists", timeout=10).json()["artists"]
        mine = next(a for a in listed if a["id"] == artist_id)
        assert mine["from_date"] == _LATER_DATE
    finally:
        api.delete(f"{base_url}/api/watched-artists/{artist_id}", timeout=10)


@pytest.mark.slow
def test_the_api_refuses_a_nonsense_date_rather_than_storing_it(api, base_url):
    r = api.post(
        f"{base_url}/api/watched-artists",
        json={
            "mbid": _RADIOHEAD_MBID,
            "name": _RADIOHEAD_NAME,
            "from_date": _SAFE_DATE,
            "watch_singles": True,
        },
        timeout=90,
    )
    assert r.status_code == 200, r.text
    artist_id = r.json().get("id")

    try:
        r = api.put(
            f"{base_url}/api/watched-artists/{artist_id}",
            json={"from_date": "last Tuesday"},
            timeout=30,
        )
        assert r.status_code == 422, f"expected a validation refusal, got {r.status_code}: {r.text}"

        listed = api.get(f"{base_url}/api/watched-artists", timeout=10).json()["artists"]
        mine = next(a for a in listed if a["id"] == artist_id)
        assert mine["from_date"] == _SAFE_DATE, "a refused update still changed the row"
    finally:
        api.delete(f"{base_url}/api/watched-artists/{artist_id}", timeout=10)
