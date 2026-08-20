"""Track upgrades  -  Phase 1: the library scan.

Lidarr-style "hold out for a better copy" feature. This module owns the cheap,
network-free half: walk the library, work out each file's quality tier, and flag
the ones sitting below the quality the user already downloads at. The actual
searching and file-swapping (Phase 2) lives elsewhere; the scan never touches a
file or hits the network.

Ground rules (see docs/upgrades-design.md):
- The filesystem is the source of truth. We ffprobe... well, mutagen-probe... the
  real files every scan, so a file the user upgraded themselves self-corrects.
- Only files carrying our SOURCE tag are eligible. No tag = the user's own file =
  hands off. The tag is written at download time by metadata.apply_metadata_to_file.
- Target quality is derived from the existing download/convert settings, never a
  separate knob, so there is no contradiction and no re-upgrade loop.
- Albums are deliberately out of scope; we only walk Singles + playlist folders.
"""

import sqlite3
import threading
import time
import hashlib
import re
from pathlib import Path

from audio_probe import probe_file
from constants import (
    UPGRADE_SCAN_INTERVAL_HOURS,
    UPGRADE_SEARCH_TTL_SECONDS,
    UPGRADE_MATCH_FLOOR,
    TIER_LOSSY_128, TIER_LOSSY_192, TIER_LOSSY_256, TIER_LOSSY_320, TIER_LOSSLESS,
    kbps_to_tier as _kbps_to_tier,
    SEARCH_SLOT_WAIT_AUTOMATED,
)

# These constants have long been part of this module's public import surface;
# tests and callers import them from ``upgrades`` alongside the tier helpers.
__all__ = [
    "TIER_LOSSY_128",
    "TIER_LOSSY_192",
    "TIER_LOSSY_256",
    "TIER_LOSSY_320",
    "TIER_LOSSLESS",
]
from db import db_conn
from settings import (
    get_setting_bool,
    get_setting_int,
    get_setting,
    get_singles_dir,
    get_playlists_dir,
)
from utils import spawn_daemon_thread, iter_library_audio_files

_LOSSLESS_CODECS = {"flac", "alac", "wav", "wave", "pcm", "ape", "wavpack", "tak"}

# Extensions we bother scanning. webm is skipped: yt-dlp owns its tags and we
# can't reliably stamp/read a SOURCE on it anyway.
_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".opus", ".ogg", ".oga", ".wav", ".aac"}

# Rough VBR-quality-preset -> effective average kbps. Only the common LAME
# presets matter; anything exotic falls back to a conservative middle value.
_VBR_KBPS = {"v0": 245, "v1": 225, "v2": 190, "v3": 175, "v4": 165, "v5": 150}


def tier_of(codec: str | None, bitrate_kbps: int) -> int:
    """Quality tier for an actual file given its codec and average bitrate."""
    if codec and codec.lower() in _LOSSLESS_CODECS:
        return TIER_LOSSLESS
    return _kbps_to_tier(bitrate_kbps or 0)


def _digits_to_int(s: str) -> int:
    """Largest number in a string, else 0. Largest (not first) so codec-name digits
    don't win: 'FLAC (from MP3 320kbps)' -> 320, not the 3 in 'MP3'."""
    import re
    nums = [int(n) for n in re.findall(r"\d+", s or "")]
    return max(nums) if nums else 0


def effective_tier(
    codec: str | None,
    bitrate_kbps: int,
    source_quality: str | None,
    source_codec: str | None = None,
    source_bitrate_kbps: int = 0,
) -> int:
    """True quality tier, seeing through a lossless container around a lossy source.

    A YouTube grab converted to FLAC is a FLAC *file* but 130kbps *audio*. We stamp the
    honest origin into structured SOURCE_CODEC / SOURCE_BITRATE tags and keep the older
    SOURCE_QUALITY label for display and backwards compatibility. Structured provenance
    wins when present; parsing a human-readable label is only a fallback for older files.

    Quality can never exceed either side of a transcode. MP3 128 -> Opus 256 is still a
    128-tier recording, while FLAC -> Opus 256 is a 256-tier recording. This is what lets
    the most common real upgrade (lossy-sourced -> proper lossless) be detected without
    mistaking one genuinely lossless wrapper for an upgrade over another.
    """
    sq = source_quality or ""
    is_lossless_container = bool(codec and codec.lower() in _LOSSLESS_CODECS)

    origin_codec = (source_codec or "").strip().lower()
    origin_kbps = int(source_bitrate_kbps or 0)
    has_structured_origin = bool(origin_codec)

    if not has_structured_origin and "from" in sq.lower():
        origin = re.search(r"\bfrom\s+([a-z0-9_]+)", sq, re.I)
        origin_codec = origin.group(1).lower() if origin else ""
        origin_part = re.split(r"\bfrom\b", sq, maxsplit=1, flags=re.I)[-1]
        origin_kbps = _digits_to_int(origin_part)

    if origin_codec and origin_codec not in _LOSSLESS_CODECS:
        origin_tier = _kbps_to_tier(origin_kbps) if origin_kbps else TIER_LOSSY_192
        if is_lossless_container:
            return origin_tier

        # A lossy-to-lossy conversion is capped by both the input and output.
        stored_kbps = bitrate_kbps or _digits_to_int(
            re.split(r"\bfrom\b", sq, maxsplit=1, flags=re.I)[0]
        )
        return min(origin_tier, _kbps_to_tier(stored_kbps)) if stored_kbps else origin_tier

    if is_lossless_container:
        return TIER_LOSSLESS

    # Lossy: trust the real bitrate, but mutagen reports 0 for some codecs (Opus),
    # so fall back to the kbps recorded in SOURCE_QUALITY.
    kbps = bitrate_kbps or _digits_to_int(sq)
    return _kbps_to_tier(kbps)


def _setting_to_kbps(value: str) -> int | None:
    """Map a bitrate setting value to effective kbps. None means lossless."""
    if not value:
        return None
    v = value.strip().lower().rstrip("k")
    if v in ("lossless", "0", ""):
        return None
    if v in _VBR_KBPS:
        return _VBR_KBPS[v]
    try:
        return int(v)
    except ValueError:
        return None


def target_tier(user_id: str | None = None) -> int:
    """Derive the upgrade target tier from the user's download/convert settings.

    No separate setting: this IS "the quality I already download at". Convert-to-FLAC
    or a lossless format means lossless; otherwise the chosen codec's bitrate setting
    decides the lossy tier.
    """
    fmt = (get_setting("audio_format", "opus", user_id=user_id) or "opus").strip().lower()
    if fmt in ("flac", "wav", "alac"):
        # alac is lossless; alac_bitrate is effectively always "lossless"
        if fmt != "alac":
            return TIER_LOSSLESS
        if _setting_to_kbps(get_setting("alac_bitrate", "lossless", user_id=user_id)) is None:
            return TIER_LOSSLESS

    if fmt == "mp3":
        kbps = _setting_to_kbps(get_setting("mp3_bitrate", "v2", user_id=user_id))
    elif fmt == "opus":
        kbps = _setting_to_kbps(get_setting("opus_bitrate", "256k", user_id=user_id))
    else:
        # aac / m4a-lossy and anything else: assume a sensible high lossy target
        kbps = 256

    if kbps is None:
        return TIER_LOSSLESS
    return _kbps_to_tier(kbps)


def upgrade_output_profile(user_id: str | None = None) -> dict:
    """Describe how an accepted upgrade will actually be stored.

    Search quality describes the candidate *before* MusicGrabber handles it. The
    upgrade download then follows the normal default-conversion path, so a lossless
    candidate may quite deliberately land as Opus, MP3, or AAC. Keep this server-side
    so the Watched UI reports the same settings the worker will use.
    """
    conversion_enabled = get_setting_bool(
        "default_convert_audio", False, user_id=user_id
    )
    if not conversion_enabled:
        return {
            "upgrade_conversion_enabled": False,
            "upgrade_output_format": None,
            "upgrade_output_label": "Keep source format",
        }

    fmt = (get_setting("audio_format", "opus", user_id=user_id) or "opus").strip().lower()
    if fmt == "flac":
        label = "FLAC lossless"
    elif fmt == "alac":
        bitrate = get_setting("alac_bitrate", "lossless", user_id=user_id)
        label = "ALAC lossless" if _setting_to_kbps(bitrate) is None else f"AAC {bitrate.rstrip('kK')} kbps (.m4a)"
    elif fmt == "mp3":
        bitrate = (get_setting("mp3_bitrate", "v2", user_id=user_id) or "v2").lower()
        presets = {"v0": "MP3 V0 (~245 kbps)", "v2": "MP3 V2 (~190 kbps)"}
        label = presets.get(bitrate, f"MP3 {bitrate.rstrip('kK')} kbps")
    else:
        fmt = "opus"
        bitrate = get_setting("opus_bitrate", "256k", user_id=user_id) or "256k"
        label = f"Opus {bitrate.rstrip('kK')} kbps"

    return {
        "upgrade_conversion_enabled": True,
        "upgrade_output_format": fmt,
        "upgrade_output_label": label,
    }


def content_sha256(path: Path) -> str:
    """Strong file identity used to detect replacements at an unchanged path."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm_user(user_id: str | None) -> str:
    return user_id or ""


def _iter_audio_files(directory: Path):
    """Yield the user's real audio files, and nothing the NAS has binned.

    Delegates to the shared library walker so trash/recycle folders and symlinks
    are skipped here exactly as they are everywhere else; offering to "upgrade"
    a file the user deleted three weeks ago is not the kind of helpful we want.
    """
    if not directory:
        return
    yield from iter_library_audio_files(directory, _AUDIO_EXTS)


def scan_user_library(user_id: str | None = None) -> dict:
    """Scan one user's eligible library and refresh their upgrade_candidates rows.

    Returns a small summary dict. Network-free, never modifies audio files.
    """
    if not get_setting_bool("enable_track_upgrades", False, user_id=user_id):
        return {"scanned": 0, "eligible": 0, "below_target": 0, "skipped_unchanged": 0}

    uid = _norm_user(user_id)
    tgt = target_tier(user_id)

    dirs = [get_singles_dir(user_id)]
    pl = get_playlists_dir(user_id)
    if pl:
        dirs.append(pl)

    # The interval scan is deliberately deep: users may replace a file while
    # preserving its path and timestamps, so stat data is never trusted as proof
    # that the audio is unchanged.
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM upgrade_candidates WHERE user_id = ?",
            (uid,),
        ).fetchall()
    cache = {r["path"]: r for r in rows}
    cache_by_file_id = {r["file_id"]: r for r in rows if r["file_id"]}

    seen: set[str] = set()
    seen_ids: set[int] = set()
    scanned = eligible = below = 0

    for d in dirs:
        for path in _iter_audio_files(d):
            scanned += 1
            spath = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            mtime = st.st_mtime
            cached = cache.get(spath)
            info = probe_file(path)
            if info is None or not info.get("source"):
                # Not our file (or unreadable). Drop any stale row and move on.
                if cached is not None:
                    with db_conn() as conn:
                        conn.execute(
                            "DELETE FROM upgrade_candidates WHERE user_id=? AND path=?",
                            (uid, spath),
                        )
                        conn.commit()
                continue

            if cached is None and info.get("file_id"):
                cached = cache_by_file_id.get(info["file_id"])
                if cached is not None:
                    # Same tagged file, moved within a configured library root.
                    with db_conn() as conn:
                        conn.execute(
                            "UPDATE upgrade_candidates SET path=? WHERE user_id=? AND id=?",
                            (spath, uid, cached["id"]),
                        )
                        conn.commit()

            eligible += 1
            seen.add(spath)
            if cached is not None:
                seen_ids.add(cached["id"])
            file_tier = effective_tier(
                info["codec"],
                info["bitrate_kbps"],
                info.get("source_quality"),
                info.get("source_codec"),
                info.get("source_bitrate_kbps", 0),
            )
            is_below = 1 if file_tier < tgt else 0
            below += is_below
            try:
                file_hash = content_sha256(path)
            except OSError:
                continue

            # Any byte change lapses dismissals and cached network search results.
            dismissed = 0
            dismissed_mtime = None
            unchanged = bool(cached and cached["content_sha256"] == file_hash)
            if unchanged and cached["dismissed"]:
                dismissed = 1
                dismissed_mtime = cached["dismissed_mtime"]

            with db_conn() as conn:
                if cached is not None:
                    conn.execute(
                        """UPDATE upgrade_candidates SET path=?, mtime=?, file_size=?,
                           codec=?, bitrate_kbps=?, duration=?, file_tier=?, target_tier=?,
                           below_target=?, source=?, source_quality=?, source_codec=?,
                           source_bitrate_kbps=?, file_id=?, content_sha256=?, artist=?,
                           title=?, dismissed=?, dismissed_mtime=?,
                           found_searched=CASE WHEN ? THEN found_searched ELSE 0 END,
                           found_at=CASE WHEN ? THEN found_at ELSE NULL END,
                           found_source=CASE WHEN ? THEN found_source ELSE NULL END,
                           last_scanned=CURRENT_TIMESTAMP WHERE user_id=? AND id=?""",
                        (spath, mtime, st.st_size, info["codec"], info["bitrate_kbps"],
                         info["duration"], file_tier, tgt, is_below, info["source"],
                         info.get("source_quality"), info.get("source_codec"),
                         info.get("source_bitrate_kbps"), info.get("file_id"), file_hash,
                         info["artist"], info["title"], dismissed, dismissed_mtime,
                         unchanged, unchanged, unchanged, uid, cached["id"]),
                    )
                else:
                    cur = conn.execute(
                        """INSERT INTO upgrade_candidates
                           (user_id,path,mtime,file_size,codec,bitrate_kbps,duration,
                            file_tier,target_tier,below_target,source,source_quality,
                            source_codec,source_bitrate_kbps,file_id,content_sha256,
                            artist,title,dismissed,dismissed_mtime,last_scanned)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                        (uid, spath, mtime, st.st_size, info["codec"], info["bitrate_kbps"],
                         info["duration"], file_tier, tgt, is_below, info["source"],
                         info.get("source_quality"), info.get("source_codec"),
                         info.get("source_bitrate_kbps"), info.get("file_id"), file_hash,
                         info["artist"], info["title"], dismissed, dismissed_mtime),
                    )
                    seen_ids.add(cur.lastrowid)
                conn.commit()

    # Only prune after a scan that actually saw the roots. An offline/empty NAS
    # must never look like the user deleted their entire library.
    stale_ids = [r["id"] for r in rows if r["id"] not in seen_ids]
    if stale_ids and (scanned > 0 or not rows):
        with db_conn() as conn:
            conn.executemany(
                "DELETE FROM upgrade_candidates WHERE user_id=? AND id=?",
                [(uid, candidate_id) for candidate_id in stale_ids],
            )
            conn.commit()

    return {"scanned": scanned, "eligible": eligible, "below_target": below,
            "skipped_unchanged": 0, "scrubbed": len(stale_ids) if scanned > 0 else 0}


def get_candidates(user_id: str | None = None, below_only: bool = True,
                   include_dismissed: bool = False) -> list[dict]:
    """Return the cached scan rows for a user, newest scan first."""
    uid = _norm_user(user_id)
    clauses = ["user_id = ?"]
    params: list = [uid]
    if below_only:
        clauses.append("below_target = 1")
    if not include_dismissed:
        clauses.append("dismissed = 0")
    where = " AND ".join(clauses)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT path, codec, bitrate_kbps, duration, file_tier, target_tier, "
            f"below_target, source, artist, title, dismissed, upgrade_state, last_scanned "
            f"FROM upgrade_candidates WHERE {where} ORDER BY last_scanned DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# Phase 2a: per-candidate upgrade search (on demand, user-driven, read-only)
# =============================================================================

def _estimate_result_tier(result: dict) -> tuple[int, bool]:
    """Best-effort (tier, verified) for a search result before downloading.

    verified == we know the quality up front (Monochrome). slskd and the lossy web
    sources are estimates, confirmed only by the Phase 2b download-and-compare.
    """
    source = (result.get("source") or "").lower()
    quality = result.get("quality") or ""
    q = quality.upper()
    if source == "monochrome":
        if "LOSSLESS" in q:  # HI_RES_LOSSLESS or LOSSLESS
            return TIER_LOSSLESS, True
        return TIER_LOSSY_320, True  # HIGH
    if source == "soulseek":
        if any(t in q for t in ("FLAC", "WAV", "ALAC")):
            return TIER_LOSSLESS, False
        digits = "".join(ch for ch in quality if ch.isdigit())
        if digits:
            return _kbps_to_tier(int(digits)), False
        return TIER_LOSSY_320, False
    if source in ("youtube", "soundcloud"):
        return TIER_LOSSY_192, False  # lossy, rarely an upgrade; estimate low
    if source == "freemp3cloud":
        # HQ-tagged results carry a "320kbps" quality label, the rest are 128.
        digits = "".join(ch for ch in quality if ch.isdigit())
        return (_kbps_to_tier(int(digits)) if digits else TIER_LOSSY_320), False
    return TIER_LOSSY_320, False  # zvu4no etc, estimate, unverified


def _candidate_public(row, user_id: str | None = None) -> dict:
    """Shape a candidate row for the API/UI."""
    d = dict(row)
    d["filename"] = Path(d["path"]).name
    d.update(upgrade_output_profile(user_id))
    return d


def search_candidate(user_id: str | None, candidate_id: int, force: bool = False) -> dict | None:
    """Find the best better-than-current copy for one candidate; cache it (TTL).

    Returns the refreshed candidate dict, or None if the id doesn't exist. Hits the
    network via search_all, so callers should rate-limit (the UI does, one row at a time).
    """
    uid = _norm_user(user_id)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM upgrade_candidates WHERE user_id=? AND id=?", (uid, candidate_id)
        ).fetchone()
    if row is None:
        return None

    # Serve cached result while it's fresh.
    if not force and row["found_searched"] and row["found_at"]:
        try:
            if time.time() - float(row["found_at"]) < UPGRADE_SEARCH_TTL_SECONDS:
                return _candidate_public(row, user_id)
        except (TypeError, ValueError):
            pass

    artist = row["artist"] or ""
    title = row["title"] or ""
    if not (artist or title):
        try:
            from utils import extract_artist_title
            a, t = extract_artist_title(Path(row["path"]).stem, "")
            artist, title = a or "", t or ""
        except Exception:
            pass
    query = f"{artist} - {title}".strip(" -") or Path(row["path"]).stem
    file_tier = row["file_tier"] or 0
    expected_dur = row["duration"] or None

    best = None
    try:
        from search import search_all
        from matching import compute_match_confidence
        include_sk = get_setting_bool("source_soulseek_enabled", False, user_id=user_id)
        results, _ = search_all(query, 8, include_soulseek=include_sk,
                                slot_wait=SEARCH_SLOT_WAIT_AUTOMATED)
    except Exception as e:
        print(f"Upgrade search failed for candidate {candidate_id}: {e}")
        results = []

    for r in results:
        tier, verified = _estimate_result_tier(r)
        if tier <= file_tier:
            continue  # not actually better than what's on disk
        cand_title = r.get("title") or ""
        cand_artist = r.get("channel") or r.get("artist") or ""
        try:
            cand_dur = float(r.get("duration") or 0) or None
        except (TypeError, ValueError):
            cand_dur = None
        conf, _ = compute_match_confidence(
            artist or None, title or None, cand_title, cand_artist,
            candidate_duration_s=cand_dur, expected_duration_s=expected_dur, query=query,
        )
        if conf < UPGRADE_MATCH_FLOOR:
            continue
        score = r.get("relevance_score", 0) or 0
        if best is None or (tier, score) > (best["tier"], best["result"].get("relevance_score", 0) or 0):
            best = {"tier": tier, "verified": verified, "confidence": conf, "result": r}

    with db_conn() as conn:
        if best:
            r = best["result"]
            conn.execute(
                """UPDATE upgrade_candidates SET found_searched=1, found_at=?,
                   found_source=?, found_quality=?, found_tier=?, found_confidence=?, found_verified=?,
                   found_video_id=?, found_source_url=?, found_slskd_username=?,
                   found_slskd_filename=?, found_slskd_size=?
                   WHERE user_id=? AND id=?""",
                (time.time(), r.get("source"), r.get("quality"), best["tier"],
                 round(best["confidence"], 3), 1 if best["verified"] else 0,
                 r.get("video_id"), r.get("source_url"), r.get("slskd_username"),
                 r.get("slskd_filename"), r.get("slskd_size"), uid, candidate_id),
            )
        else:
            conn.execute(
                """UPDATE upgrade_candidates SET found_searched=1, found_at=?,
                   found_source=NULL, found_quality=NULL, found_tier=NULL, found_confidence=NULL,
                   found_verified=0, found_video_id=NULL, found_source_url=NULL,
                   found_slskd_username=NULL, found_slskd_filename=NULL, found_slskd_size=NULL
                   WHERE user_id=? AND id=?""",
                (time.time(), uid, candidate_id),
            )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM upgrade_candidates WHERE user_id=? AND id=?", (uid, candidate_id)
        ).fetchone()
    return _candidate_public(row, user_id)


def get_candidates_page(user_id: str | None, page: int = 1, per_page: int = 10,
                        include_dismissed: bool = False) -> dict:
    """A stable, paginated slice of below-target candidates for the Watched Upgrades view."""
    uid = _norm_user(user_id)
    page = max(1, int(page or 1))
    per_page = max(1, min(50, int(per_page or 10)))
    clauses = ["user_id=?", "below_target=1"]
    params: list = [uid]
    if not include_dismissed:
        clauses.append("dismissed=0")
    # Once we've actually looked and found nothing, there's no path to an upgrade;
    # keep the row out rather than showing a dead end forever. A not-yet-searched
    # row (found_searched=0) still belongs here, we just don't know yet.
    clauses.append("NOT (found_searched=1 AND found_source IS NULL)")
    where = " AND ".join(clauses)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM upgrade_candidates WHERE {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM upgrade_candidates WHERE {where} "
            f"ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, path LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    pages = (total + per_page - 1) // per_page if total else 0
    return {"items": [_candidate_public(r, user_id) for r in rows], "total": total,
            "page": page, "per_page": per_page, "pages": pages}


def dismiss_candidate(user_id: str | None, candidate_id: int) -> bool:
    """Dismiss a candidate. Keyed to current mtime, so a later file change re-surfaces it."""
    uid = _norm_user(user_id)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT mtime FROM upgrade_candidates WHERE user_id=? AND id=?", (uid, candidate_id)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE upgrade_candidates SET dismissed=1, dismissed_mtime=? WHERE user_id=? AND id=?",
            (row["mtime"], uid, candidate_id),
        )
        conn.commit()
    return True


# =============================================================================
# Phase 2b: download + verify + swap (the file-touching half)
# =============================================================================

# Same-recording gates. Deliberately strict: we are replacing the user's file.
UPGRADE_DURATION_TOLERANCE_S = 5.0      # plus a 5% allowance for long tracks
UPGRADE_SWAP_MATCH_FLOOR = 0.72         # higher than the search floor
UPGRADE_FP_SIMILARITY_FLOOR = 0.65      # Chromaprint bit-similarity; ~0.5 is noise


def _quarantine_dir() -> Path:
    from constants import DB_PATH
    d = DB_PATH.parent / ".upgrade_quarantine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fpcalc_raw(path: Path) -> tuple[float, list[int]] | None:
    """(duration, raw Chromaprint fingerprint ints) for a file, or None."""
    import json
    import subprocess
    try:
        out = subprocess.run(
            ["fpcalc", "-raw", "-json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        return float(data.get("duration") or 0), list(data.get("fingerprint") or [])
    except Exception:
        return None


def _fingerprint_similarity(fa: list[int], fb: list[int]) -> float:
    """Best bit-similarity (0..1) of two raw fingerprints over a small offset search.

    Same recording at different qualities scores ~0.85+; unrelated tracks sit near the
    0.5 noise floor, so the floor cleanly separates them. Capped in length for speed.
    """
    if not fa or not fb:
        return 0.0
    cap = 600
    fa, fb = fa[:cap], fb[:cap]
    best = 0.0
    for off in range(-25, 26):
        x = fa[off:] if off >= 0 else fa
        y = fb if off >= 0 else fb[-off:]
        n = min(len(x), len(y))
        if n < 40:
            continue
        errors = 0
        for i in range(n):
            errors += bin(x[i] ^ y[i]).count("1")
        sim = 1.0 - errors / (32.0 * n)
        if sim > best:
            best = sim
    return best


def _download_candidate_to_staging(user_id: str | None, row, staging_dir: Path) -> tuple[Path | None, str]:
    """Download the proposed upgrade into a staging dir using the normal pipeline.

    Reuses process_download / process_slskd_download with override_dir=staging so we get
    all the source-specific handling and quality probing for free, then we take the file
    out of staging ourselves. Returns (file, job_id); the caller decides the job row's
    fate once it knows whether the swap actually went ahead, so a genuine upgrade leaves
    a normal Queue history entry instead of vanishing without a trace.
    """
    import uuid as _uuid
    from downloads import process_download, process_slskd_download

    source = row["found_source"]
    video_id = row["found_video_id"] or ""
    source_url = row["found_source_url"]
    artist = row["artist"] or ""
    title = row["title"] or Path(row["path"]).stem
    convert = get_setting_bool("default_convert_audio", False, user_id=user_id)
    job_id = str(_uuid.uuid4())[:8]
    staging_dir.mkdir(parents=True, exist_ok=True)

    with db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, video_id, title, artist, status, download_type, source,
                slskd_username, slskd_filename, slskd_size, convert_audio, source_url,
                user_id, override_dir)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, video_id, title, artist, "queued", "upgrade", source,
             row["found_slskd_username"], row["found_slskd_filename"], row["found_slskd_size"],
             int(convert), source_url, user_id, str(staging_dir)),
        )
        conn.commit()

    try:
        if source == "soulseek":
            process_slskd_download(
                job_id, row["found_slskd_username"], row["found_slskd_filename"],
                artist, title, convert, user_id=user_id,
                override_dir=str(staging_dir), slskd_size=row["found_slskd_size"],
                skip_dupe_check=True,
            )
        else:
            process_download(
                job_id, video_id, convert, source_url=source_url,
                user_id=user_id, override_dir=str(staging_dir), skip_dupe_check=True,
            )
    except Exception as e:
        print(f"Upgrade download error (candidate {row['id']}): {e}")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        status = j["status"] if j else None

    files = [p for p in staging_dir.rglob("*")
             if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
    if status == "completed" and files:
        return max(files, key=lambda p: p.stat().st_mtime), job_id
    return None, job_id


def _fix_m3u_references(user_id: str | None, old_path: Path, new_path: Path) -> None:
    """Best-effort: rewrite playlist .m3u entries when an upgrade changed the file's name."""
    if old_path.name == new_path.name:
        return
    try:
        roots = []
        pl = get_playlists_dir(user_id)
        if pl:
            roots.append(pl)
        roots.append(get_singles_dir(user_id).parent)  # music root
        seen = set()
        for root in roots:
            if not root or not root.exists():
                continue
            for m3u in root.rglob("*.m3u"):
                if m3u in seen:
                    continue
                seen.add(m3u)
                try:
                    text = m3u.read_text(encoding="utf-8", errors="ignore")
                    if old_path.name in text:
                        m3u.write_text(text.replace(old_path.name, new_path.name), encoding="utf-8")
                except OSError:
                    continue
    except Exception:
        pass


def perform_upgrade(user_id: str | None, candidate_id: int, force: bool = False) -> dict:
    """Download the proposed copy, verify it, and (only if safe) swap it in.

    Returns {"status": "upgraded"|"rejected"|"error", "reason": ...}. On anything short of
    a clean pass the original file is left exactly as it was; the download is binned.

    force=True skips the same-recording gates (better/duration/match/fingerprint) for when
    the user deliberately wants the proposed copy regardless, e.g. swapping a long YouTube
    edit for the studio cut. The download still has to be valid audio, and the old file
    still goes to quarantine, so a forced mistake is always recoverable.
    """
    uid = _norm_user(user_id)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM upgrade_candidates WHERE user_id=? AND id=?", (uid, candidate_id)
        ).fetchone()
    if row is None:
        return {"status": "error", "reason": "Candidate not found"}
    if not row["found_source"]:
        return {"status": "error", "reason": "No proposed upgrade to apply"}

    current = Path(row["path"])
    if not current.exists():
        return {"status": "error", "reason": "Original file no longer exists"}
    current_info = probe_file(current)
    if current_info is None:
        return {"status": "error", "reason": "Original file is no longer readable audio"}
    if row["file_id"] and current_info.get("file_id") != row["file_id"]:
        scan_user_library(user_id)
        return {"status": "error",
                "reason": "Original file was externally replaced; library state refreshed"}
    try:
        original_hash = content_sha256(current)
    except OSError:
        return {"status": "error", "reason": "Original file could not be revalidated"}
    if row["content_sha256"] and original_hash != row["content_sha256"]:
        scan_user_library(user_id)
        return {"status": "error",
                "reason": "Original file changed since the upgrade search; search again"}

    # Never trust cached quality data at the point of action.
    current_tier = effective_tier(
        current_info["codec"], current_info["bitrate_kbps"],
        current_info.get("source_quality"),
        current_info.get("source_codec"),
        current_info.get("source_bitrate_kbps", 0),
    )
    if not force and current_tier >= (row["target_tier"] or TIER_LOSSLESS):
        scan_user_library(user_id)
        return {"status": "rejected", "reason": "Original file is already at target quality"}

    import tempfile
    staging = Path(tempfile.mkdtemp(prefix=f"upg_{candidate_id}_", dir=str(_quarantine_dir().parent)))
    job_id = None
    upgraded_ok = False
    try:
        staged, job_id = _download_candidate_to_staging(user_id, row, staging)
        if staged is None:
            _set_upgrade_state(uid, candidate_id, "rejected")
            return {"status": "rejected", "reason": "Download failed or produced no file"}

        info = probe_file(staged)
        if info is None:
            return {"status": "rejected", "reason": "Downloaded file was not readable audio"}

        # 1) Genuinely better than what's on disk (compares the file as it landed).
        staged_tier = effective_tier(
            info["codec"],
            info["bitrate_kbps"],
            info.get("source_quality"),
            info.get("source_codec"),
            info.get("source_bitrate_kbps", 0),
        )
        if not force and staged_tier <= current_tier:
            return {"status": "rejected",
                    "reason": f"Not actually better ({info.get('source_quality') or info['codec']})"}

        # 2) Same recording: duration, then title/artist confidence, then fingerprint.
        # All of these are skipped on a forced upgrade (the user is overriding identity).
        if not force:
            cur_dur = float(current_info["duration"] or 0)
            new_dur = float(info["duration"] or 0)
            tol = max(UPGRADE_DURATION_TOLERANCE_S, cur_dur * 0.05)
            if cur_dur and new_dur and abs(cur_dur - new_dur) > tol:
                return {"status": "rejected",
                        "reason": f"Duration mismatch ({cur_dur:.0f}s vs {new_dur:.0f}s)"}

            try:
                from matching import compute_match_confidence
                conf, _ = compute_match_confidence(
                    row["artist"] or None, row["title"] or None,
                    info.get("title") or "", info.get("artist") or "",
                    candidate_duration_s=new_dur or None, expected_duration_s=cur_dur or None,
                )
            except Exception:
                conf = 1.0  # matching unavailable; lean on duration + fingerprint
            if conf < UPGRADE_SWAP_MATCH_FLOOR:
                return {"status": "rejected", "reason": f"Title/artist match too weak ({conf:.0%})"}

            fp_cur = _fpcalc_raw(current)
            fp_new = _fpcalc_raw(staged)
            if fp_cur and fp_new:
                sim = _fingerprint_similarity(fp_cur[1], fp_new[1])
                if sim < UPGRADE_FP_SIMILARITY_FLOOR:
                    return {"status": "rejected",
                            "reason": f"Fingerprint says different recording ({sim:.0%})"}
            # If fpcalc is unavailable we don't hard-fail; duration + confidence already passed.

        # The download may have taken minutes. Recheck the live file immediately
        # before touching it to close the time-of-check/time-of-use window.
        try:
            if not current.exists() or content_sha256(current) != original_hash:
                scan_user_library(user_id)
                return {"status": "error",
                        "reason": "Original file changed while the upgrade downloaded; swap aborted"}
        except OSError:
            return {"status": "error",
                    "reason": "Original file could not be revalidated before swap"}

        # 3) All gates passed. Quarantine the old file, swap the new one into its place.
        new_path = current.with_suffix(staged.suffix)
        import shutil
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        quarantined = _quarantine_dir() / f"{stamp}__{current.name}"
        shutil.move(str(current), str(quarantined))
        try:
            shutil.move(str(staged), str(new_path))
        except Exception as e:
            # Swap failed mid-flight: restore the original so we never lose the file.
            shutil.move(str(quarantined), str(current))
            return {"status": "error", "reason": f"Swap failed, original restored: {e}"}
        upgraded_ok = True

        try:
            with db_conn() as conn:
                conn.execute(
                    """INSERT INTO upgrade_quarantine
                       (user_id, quarantine_path, original_path, replaced_by_path,
                        artist, title, file_size)
                       VALUES (?,?,?,?,?,?,?)""",
                    (uid, str(quarantined), str(current), str(new_path),
                     row["artist"], row["title"], quarantined.stat().st_size),
                )
                conn.commit()
        except Exception as e:
            print(f"Quarantine tracking row failed (candidate {candidate_id}): {e}")

        # Keep the library entry's identity (artist/title/album); the download
        # already stamped SOURCE/SOURCE_QUALITY. current_info was read from the
        # original file before it went to quarantine, so this survives even if
        # the new copy's own tags (or lack thereof) disagree.
        try:
            from metadata import apply_metadata_to_file
            apply_metadata_to_file(
                new_path, row["artist"] or "", row["title"] or new_path.stem,
                album=current_info.get("album") or "",
                album_artist=current_info.get("album_artist") or None,
            )
        except Exception:
            pass

        _fix_m3u_references(user_id, current, new_path)
        # All three, same as every other completed-download site. This used to
        # poke Navidrome only, so Jellyfin and Plex users were left staring at
        # the old file's tags until their next scheduled scan wandered past.
        try:
            from downloads import trigger_navidrome_scan, trigger_jellyfin_scan, trigger_plex_scan
            trigger_navidrome_scan(user_id=user_id)
            trigger_jellyfin_scan(user_id=user_id)
            trigger_plex_scan(user_id=user_id)
        except Exception:
            pass

        # Refresh the candidate row to reflect the upgraded file.
        new_info = probe_file(new_path) or info
        new_tier = effective_tier(
            new_info["codec"],
            new_info["bitrate_kbps"],
            new_info.get("source_quality"),
            new_info.get("source_codec"),
            new_info.get("source_bitrate_kbps", 0),
        )
        # A genuine upgrade can still land short of the target: the fetched copy
        # beats what was there, then convert-audio re-encodes it to something
        # below your tier anyway. The row legitimately stays on the list in that
        # case, so tell the caller rather than letting the UI assume it's gone.
        still_below = new_tier < (row["target_tier"] or TIER_LOSSLESS)
        try:
            st = new_path.stat()
            with db_conn() as conn:
                conn.execute(
                    """UPDATE upgrade_candidates SET path=?, mtime=?, file_size=?, codec=?,
                       bitrate_kbps=?, duration=?, file_tier=?, below_target=?, source=?,
                       source_quality=?, source_codec=?, source_bitrate_kbps=?,
                       file_id=?, content_sha256=?,
                       upgrade_state='upgraded', found_searched=0, found_source=NULL,
                       found_quality=NULL, found_tier=NULL, found_confidence=NULL,
                       found_verified=0, found_video_id=NULL, found_source_url=NULL,
                       found_slskd_username=NULL, found_slskd_filename=NULL, found_slskd_size=NULL
                       WHERE user_id=? AND id=?""",
                    (str(new_path), st.st_mtime, st.st_size, new_info["codec"],
                     new_info["bitrate_kbps"], new_info["duration"], new_tier,
                     1 if still_below else 0,
                     new_info.get("source") or row["source"],
                     new_info.get("source_quality"), new_info.get("source_codec"),
                     new_info.get("source_bitrate_kbps"), new_info.get("file_id"),
                     content_sha256(new_path), uid, candidate_id),
                )
                conn.commit()
        except Exception as e:
            print(f"Upgrade row refresh failed (candidate {candidate_id}): {e}")

        return {"status": "upgraded", "from": row["found_quality"] or "",
                "path": str(new_path), "quarantined": str(quarantined),
                "still_below_target": still_below,
                "new_quality": new_info.get("source_quality") or new_info["codec"]}
    finally:
        if job_id:
            # A genuine upgrade gets to stay in the Queue as an ordinary completed
            # job, same as any other download, pointed at where the file actually
            # ended up. Anything short of that (rejected, errored, gate failure)
            # was never a real acquisition and is cleaned up as before.
            try:
                with db_conn() as conn:
                    if upgraded_ok:
                        conn.execute(
                            "UPDATE jobs SET final_path=?, override_dir=NULL WHERE id=?",
                            (str(new_path), job_id),
                        )
                    else:
                        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                    conn.commit()
            except Exception as e:
                print(f"Upgrade job history update failed (candidate {candidate_id}): {e}")
        try:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            pass


def _set_upgrade_state(uid: str, candidate_id: int, state: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE upgrade_candidates SET upgrade_state=? WHERE user_id=? AND id=?",
            (state, uid, candidate_id),
        )
        conn.commit()


# Live progress for the background "Upgrade All" run, keyed by user. Upgrade All
# is fire-and-forget in a daemon thread, so without this the UI's only option was
# to tell the user to refresh and hope they did; a run that removed nothing then
# looked identical to one that never started.
_upgrade_all_progress: dict[str, dict] = {}
_upgrade_all_lock = threading.Lock()


def get_upgrade_all_progress(user_id: str | None) -> dict:
    """Progress of this user's Upgrade All run, or an idle state if none has run."""
    with _upgrade_all_lock:
        state = _upgrade_all_progress.get(_norm_user(user_id))
        return dict(state) if state else {
            "running": False, "total": 0, "done": 0,
            "upgraded": 0, "rejected": 0, "errored": 0,
        }


def mark_upgrade_all_starting(user_id: str | None) -> None:
    """Flag a run as live before its thread has drawn breath.

    The route calls this synchronously so a poll arriving in the gap between
    "thread spawned" and "thread got scheduled" sees running=True, rather than
    an idle state it would read as "already finished, nothing happened".
    """
    with _upgrade_all_lock:
        _upgrade_all_progress[_norm_user(user_id)] = {
            "running": True, "total": 0, "done": 0,
            "upgraded": 0, "rejected": 0, "errored": 0,
        }


def perform_upgrade_all(user_id: str | None) -> dict:
    """Best-effort upgrade of every below-target candidate that has a proposal."""
    uid = _norm_user(user_id)
    mark_upgrade_all_starting(uid)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM upgrade_candidates WHERE user_id=? AND below_target=1 "
            "AND dismissed=0 AND found_source IS NOT NULL", (uid,)
        ).fetchall()]

    with _upgrade_all_lock:
        _upgrade_all_progress[uid]["total"] = len(ids)

    upgraded = rejected = errored = 0
    try:
        for cid in ids:
            try:
                res = perform_upgrade(user_id, cid)
                st = res.get("status")
            except Exception:
                st = "error"
            upgraded += st == "upgraded"
            rejected += st == "rejected"
            errored += st == "error"
            with _upgrade_all_lock:
                _upgrade_all_progress[uid].update(
                    done=upgraded + rejected + errored,
                    upgraded=upgraded, rejected=rejected, errored=errored,
                )
    finally:
        # Always stops "running", even if something above threw: a progress bar
        # that spins forever is worse than no progress bar at all.
        with _upgrade_all_lock:
            _upgrade_all_progress[uid]["running"] = False

    return {"total": len(ids), "upgraded": upgraded, "rejected": rejected, "errored": errored}


# =============================================================================
# Phase 2c: quarantine management (restore a wrongly-upgraded file, or bin it
# for good). Nothing here ever hard-deletes the file a restore would need; a
# restore instead quarantines whatever it displaces, so undoing an undo is
# still possible. Only an explicit delete is final.
# =============================================================================

def list_quarantine(user_id: str | None, is_admin: bool = False) -> list[dict]:
    """Quarantined files this user can act on.

    Admins additionally see every user's entries plus any file sitting in the
    quarantine folder without a tracking row at all (predates this table, or
    was dropped in by hand); those come back with "restorable": False since
    their original library location was never recorded, only deletion is safe.
    """
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        if is_admin:
            rows = conn.execute(
                "SELECT * FROM upgrade_quarantine ORDER BY quarantined_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM upgrade_quarantine WHERE user_id=? ORDER BY quarantined_at DESC",
                (_norm_user(user_id),),
            ).fetchall()

    items = [dict(r, restorable=True) for r in rows]

    if is_admin:
        from datetime import datetime, timezone
        known = {r["quarantine_path"] for r in rows}
        try:
            for p in _quarantine_dir().iterdir():
                if not p.is_file() or str(p) in known:
                    continue
                st = p.stat()
                # UTC, same naive "YYYY-MM-DD HH:MM:SS" shape SQLite's CURRENT_TIMESTAMP
                # produces for the tracked rows above, so the two sort and format together.
                mtime_utc = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                items.append({
                    "id": None,
                    "user_id": None,
                    "quarantine_path": str(p),
                    "original_path": None,
                    "replaced_by_path": None,
                    "artist": None,
                    "title": p.name.split("__", 1)[-1] if "__" in p.name else p.name,
                    "file_size": st.st_size,
                    "quarantined_at": mtime_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    "restorable": False,
                })
        except OSError:
            pass
        items.sort(key=lambda x: x["quarantined_at"], reverse=True)

    return items


def restore_from_quarantine(user_id: str | None, entry_id: int, is_admin: bool = False) -> dict:
    """Undo one upgrade: move the quarantined file back to where it lived, and
    quarantine whatever the upgrade put there instead of deleting it outright.
    """
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM upgrade_quarantine WHERE id=?", (entry_id,)).fetchone()
    if row is None or (not is_admin and row["user_id"] != _norm_user(user_id)):
        return {"status": "error", "reason": "Quarantine entry not found"}

    quarantined = Path(row["quarantine_path"])
    original = Path(row["original_path"])
    replaced_by = Path(row["replaced_by_path"])

    if not quarantined.exists():
        with db_conn() as conn:
            conn.execute("DELETE FROM upgrade_quarantine WHERE id=?", (entry_id,))
            conn.commit()
        return {"status": "error", "reason": "Quarantined file no longer exists on disk"}

    import shutil
    from datetime import datetime

    # Whatever the upgrade put in its place gets bumped into quarantine itself
    # (a format-changing upgrade, e.g. mp3 -> flac, means this is a different
    # file from "original" below, so it needs clearing out of the way either way).
    bumped_path = None
    if replaced_by.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        bumped_path = _quarantine_dir() / f"{stamp}__{replaced_by.name}"
        shutil.move(str(replaced_by), str(bumped_path))

    if original != replaced_by and original.exists():
        # Something unrelated is squatting on the restore target. Put the
        # bumped file back and bail rather than guess what to do with either.
        if bumped_path is not None:
            shutil.move(str(bumped_path), str(replaced_by))
        return {"status": "error",
                "reason": f"Restore target already occupied, sort this out manually: {original}"}

    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(quarantined), str(original))
    except Exception as e:
        if bumped_path is not None:
            shutil.move(str(bumped_path), str(replaced_by))
        return {"status": "error", "reason": f"Restore failed: {e}"}

    with db_conn() as conn:
        conn.execute("DELETE FROM upgrade_quarantine WHERE id=?", (entry_id,))
        if bumped_path is not None:
            conn.execute(
                """INSERT INTO upgrade_quarantine
                   (user_id, quarantine_path, original_path, replaced_by_path,
                    artist, title, file_size)
                   VALUES (?,?,?,?,?,?,?)""",
                (row["user_id"], str(bumped_path), str(replaced_by), str(bumped_path),
                 row["artist"], row["title"], bumped_path.stat().st_size),
            )
        conn.commit()

    scan_user_library(row["user_id"])
    return {"status": "restored", "path": str(original)}


def delete_quarantine_entry(user_id: str | None, entry_id: int, is_admin: bool = False) -> dict:
    """Permanently discard a quarantined file. No undo past this point."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM upgrade_quarantine WHERE id=?", (entry_id,)).fetchone()
    if row is None or (not is_admin and row["user_id"] != _norm_user(user_id)):
        return {"status": "error", "reason": "Quarantine entry not found"}

    try:
        path = Path(row["quarantine_path"])
        if path.exists():
            path.unlink()
    except OSError as e:
        return {"status": "error", "reason": f"Could not delete file: {e}"}

    with db_conn() as conn:
        conn.execute("DELETE FROM upgrade_quarantine WHERE id=?", (entry_id,))
        conn.commit()
    return {"status": "deleted"}


def delete_orphaned_quarantine_file(filename: str) -> dict:
    """Admin-only cleanup for a quarantine-folder file with no tracking row
    (predates this table, or was dropped in by hand): no known original
    location, so deletion is the only safe action.
    """
    path = _quarantine_dir() / filename
    try:
        if path.parent.resolve() != _quarantine_dir().resolve() or not path.is_file():
            return {"status": "error", "reason": "File not found"}
        path.unlink()
    except OSError as e:
        return {"status": "error", "reason": f"Could not delete file: {e}"}
    return {"status": "deleted"}


def _eligible_user_ids() -> list[str | None]:
    """Active non-peon users to scan, plus the global/single-user pass (None)."""
    try:
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id FROM users WHERE is_active = 1 AND role != 'peon'"
            ).fetchall()
        ids = [r["id"] for r in rows]
    except Exception:
        ids = []
    # None covers single-user mode (no per-user dirs) and the global settings fallback.
    return ids if ids else [None]


_scheduler_running = False
_scheduler_lock = threading.Lock()


def run_scan_all() -> dict:
    """Scan every eligible user once. Safe to call from a route or the scheduler."""
    totals = {"users": 0, "scanned": 0, "eligible": 0, "below_target": 0}
    for uid in _eligible_user_ids():
        try:
            summary = scan_user_library(uid)
        except Exception as e:
            print(f"Upgrade scan error for user {uid}: {e}")
            continue
        totals["users"] += 1
        totals["scanned"] += summary["scanned"]
        totals["eligible"] += summary["eligible"]
        totals["below_target"] += summary["below_target"]
    return totals


def upgrade_scan_scheduler():
    """Background loop: periodically rescan eligible libraries. Cheap, no network."""
    print(f"Upgrade scan scheduler started (every {UPGRADE_SCAN_INTERVAL_HOURS}h)")
    time.sleep(45)  # let the heavier watched schedulers settle first
    while _scheduler_running:
        try:
            interval = get_setting_int("upgrade_scan_interval_hours", UPGRADE_SCAN_INTERVAL_HOURS)
            if interval <= 0:
                interval = UPGRADE_SCAN_INTERVAL_HOURS
            totals = run_scan_all()
            if totals["users"]:
                print(f"Upgrade scan: {totals['below_target']} below-target across "
                      f"{totals['eligible']} eligible files ({totals['users']} user(s))")
        except Exception as e:
            print(f"Upgrade scan scheduler error: {e}")
            interval = UPGRADE_SCAN_INTERVAL_HOURS

        sleep_seconds = max(3600, interval * 3600)
        elapsed = 0
        while elapsed < sleep_seconds and _scheduler_running:
            time.sleep(60)
            elapsed += 60


def start_upgrade_scheduler():
    """Start the background scan scheduler if not already running."""
    global _scheduler_running
    with _scheduler_lock:
        if _scheduler_running:
            return
        _scheduler_running = True
    spawn_daemon_thread(upgrade_scan_scheduler)
