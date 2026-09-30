"""
What the live tests download and search for.

Every slow run used to fetch the same 19-second video a dozen times and fire
the same three searches, which is a fine impression of a bot from YouTube's
side of the fence. It also let one test's download of "Me at the zoo" land on
top of another's. So each download now takes the next clip from a pool that is
shuffled once per run, and each search picks a song at random.

Short clips stay under 30 seconds, which is below the AcoustID fingerprinting
threshold, so MusicBrainz cannot match them and album routing never fires.
Tests that check routing is off rely on that. Every clip was checked on
2026-09-30 as public, not age-gated, and uploaded years ago by an account with
the views to suggest it is going nowhere. If one does vanish, the failure
names it; swap it out rather than shrinking the pool.
"""

import itertools
import random
import time


# (video_id, artist, title)
SHORT_CLIPS = [
    ("y8Kyi0WNg40", "magnets99", "Dramatic Look"),                        # 6s, 2007
    ("su6_bKJJk74", "SoundEffectsFactory", "Cat Meow Sound Effect"),      # 7s, 2012
    ("R8f18JFWDv4", "Sound laboratory", "Old door creaking"),             # 8s, 2016
    ("0mfJn604GT4", "arival0", "Applause Sound Effect"),                  # 9s, 2017
]
# Not Keyboard Cat: MusicBrainz knows him by name, has a Christmas album of
# his, and routed him into it without needing a fingerprint.

_clips = SHORT_CLIPS[:]
random.shuffle(_clips)
_clip_cycle = itertools.cycle(_clips)


def next_short_clip():
    """A different clip each call until the pool wraps."""
    return next(_clip_cycle)


# A track MusicBrainz reliably identifies via AcoustID fingerprint: the 1976
# album track on the Ramones' own channel, so the audio is the release itself.
# It must NOT be in the library, or the dupe check completes the job without
# downloading anything and the routing tests have nothing to look at. That is
# how Thriller (and Blur's "Song 2", tried next) quietly skipped for ages.
MATCHABLE_SONG = ("NQDPx_k66w4", "Ramones", "Blitzkrieg Bop")


# Well-known enough that every source should find something.
SEARCH_SONGS = [
    "Kate Bush Running Up That Hill",
    "Blondie Heart of Glass",
    "Pulp Common People",
    "David Bowie Heroes",
    "Fleetwood Mac Dreams",
    "The Cure Just Like Heaven",
    "Talking Heads Once in a Lifetime",
    "New Order Blue Monday",
    "Massive Attack Teardrop",
    "The Smiths There Is a Light That Never Goes Out",
]


def search_song():
    return random.choice(SEARCH_SONGS)


def tidy_up(api, base_url, job_id, timeout=180):
    """Wait for a queued download to finish, then delete its file.

    Deleting straight after queueing removes nothing, because the file does not
    exist yet; it then lands in the library a few seconds later and stays
    there. The next run's test on the same clip is then waved through by the
    dupe check without downloading anything.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api.get(f"{base_url}/api/jobs/{job_id}", timeout=10).json().get("status")
        if status in ("completed", "failed", "completed_with_errors"):
            break
        time.sleep(3)
    api.delete(f"{base_url}/api/jobs/{job_id}/file", timeout=10)
