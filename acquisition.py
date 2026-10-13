"""Persistent automatic-acquisition policy and attempt ledger.

Queue jobs are individual pieces of work. Watched tracks outlive those jobs, so
their acquisition target and source-attempt history live here instead of being
folded into whichever Queue row happens to be newest.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from sqlite3 import Connection

from constants import FALLBACK_MATCH_CONFIDENCE_FLOOR
from db import db_conn
from matching import compute_match_confidence
from search import quality_tier_of_result


_RESCUE_CANDIDATE_FIELDS = (
    "video_id", "title", "artist", "channel", "duration", "thumbnail",
    "source", "source_url", "quality", "quality_tier", "relevance_score",
    "score_breakdown", "slskd_username", "slskd_filename", "slskd_size",
    "size", "album", "year", "bitrate", "size_bytes", "match_confidence",
)


def acquisition_candidate_summary(candidate: dict) -> dict:
    """Keep enough candidate data to explain, preview and later rescue a job."""
    summary = {
        field: candidate.get(field)
        for field in _RESCUE_CANDIDATE_FIELDS
        if candidate.get(field) is not None
    }
    summary.setdefault("video_id", "")
    summary.setdefault("title", "")
    summary.setdefault("channel", "")
    summary.setdefault("source", "youtube")
    # Preserve the historical rationale field names used by the Queue panel.
    summary["score"] = candidate.get("relevance_score", candidate.get("score"))
    summary["breakdown"] = candidate.get(
        "score_breakdown", candidate.get("breakdown", [])
    )
    return summary


def acquisition_candidate_key(candidate: dict) -> str:
    """Stable opaque identity for selecting a server-stored rescue candidate."""
    source = str(candidate.get("source") or "youtube").strip().lower()
    identity = "\x1f".join(
        str(candidate.get(field) or "").strip()
        for field in ("video_id", "source_url", "slskd_username", "slskd_filename")
    )
    return hashlib.sha256(f"{source}\x1f{identity}".encode("utf-8")).hexdigest()[:24]


def stored_rescue_candidates(conn: Connection, target_id: str) -> list[dict]:
    """Return de-duplicated candidates saved across every job for one target."""
    rows = conn.execute(
        """SELECT sd.decision_json, j.video_id, j.source, j.source_url,
                  j.slskd_username, j.slskd_filename, j.slskd_size
           FROM search_decisions sd
           JOIN jobs j ON j.id = sd.job_id
           WHERE j.acquisition_target_id = ?
           ORDER BY sd.created_at DESC, sd.id DESC""",
        (target_id,),
    ).fetchall()
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        try:
            decision = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        for raw in [decision.get("selected"), *(decision.get("runners_up") or [])]:
            if not isinstance(raw, dict):
                continue
            candidate = dict(raw)
            if (
                str(candidate.get("video_id") or "") == str(row[1] or "")
                and str(candidate.get("source") or "youtube").lower()
                == str(row[2] or "youtube").lower()
            ):
                candidate.setdefault("source_url", row[3])
                candidate.setdefault("slskd_username", row[4])
                candidate.setdefault("slskd_filename", row[5])
                candidate.setdefault("slskd_size", row[6])
            if "relevance_score" not in candidate and candidate.get("score") is not None:
                candidate["relevance_score"] = candidate["score"]
            if "score_breakdown" not in candidate and candidate.get("breakdown") is not None:
                candidate["score_breakdown"] = candidate["breakdown"]
            key = acquisition_candidate_key(candidate)
            if key in seen:
                continue
            candidate["candidate_key"] = key
            candidates.append(candidate)
            seen.add(key)
    return candidates


def normalise_allowed_sources(value: str | list[str] | set[str] | None) -> str:
    """Return the stable comma-separated representation stored in the ledger."""
    if value is None:
        return "all"
    if isinstance(value, str):
        raw = value.split(",")
    else:
        raw = value
    sources = sorted({str(source).strip().lower() for source in raw if str(source).strip()})
    if not sources or "all" in sources:
        return "all"
    return ",".join(sources)


def parse_allowed_sources(value: str | None) -> set[str] | None:
    normalised = normalise_allowed_sources(value)
    if normalised == "all":
        return None
    return set(normalised.split(","))


def ensure_acquisition_target(
    *,
    owner_type: str,
    owner_key: str,
    mode: str,
    artist: str,
    title: str,
    user_id: str | None,
    allowed_sources: str | list[str] | set[str] | None = None,
    priority_source: str | None = None,
    convert_audio: bool = False,
    isrc: str | None = None,
    destination: dict | None = None,
    conn: Connection | None = None,
) -> str:
    """Create or refresh the durable identity for one requested recording."""
    target_id = str(uuid.uuid4())
    allowed = normalise_allowed_sources(allowed_sources)
    priority = (priority_source or "").strip().lower() or None
    destination_json = json.dumps(destination or {}, sort_keys=True)

    def _write(db: Connection) -> str:
        db.execute(
            """INSERT INTO acquisition_targets
               (id, user_id, owner_type, owner_key, mode, artist, title, isrc,
                allowed_sources, priority_source, convert_audio, destination_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_type, owner_key) DO UPDATE SET
                   user_id = excluded.user_id,
                   mode = excluded.mode,
                   artist = excluded.artist,
                   title = excluded.title,
                   isrc = COALESCE(excluded.isrc, acquisition_targets.isrc),
                   allowed_sources = excluded.allowed_sources,
                   priority_source = excluded.priority_source,
                   convert_audio = excluded.convert_audio,
                   destination_json = excluded.destination_json,
                   updated_at = datetime('now')""",
            (
                target_id, user_id, owner_type, owner_key, mode, artist or "",
                title or "", (isrc or "").strip() or None, allowed, priority,
                int(bool(convert_audio)), destination_json,
            ),
        )
        row = db.execute(
            "SELECT id FROM acquisition_targets WHERE owner_type = ? AND owner_key = ?",
            (owner_type, owner_key),
        ).fetchone()
        return row[0]

    if conn is not None:
        return _write(conn)
    with db_conn() as db:
        result = _write(db)
        db.commit()
        return result


def begin_acquisition_cycle(target_id: str, conn=None) -> int:
    """Open the next bounded source pass for a target.

    One statement, not read-then-write: two threads retrying the same target at
    the same moment used to read the same cycle_count and both claim to be
    cycle 3, which quietly doubles the source budget the cycle limit exists to
    enforce. UPDATE ... RETURNING increments and reports back atomically, so the
    two callers get 3 and 4 like grown-ups.

    Pass `conn` to enlist in a caller's open transaction, so admitting a rescue
    and claiming its cycle number happen as one indivisible act.
    """
    def _write(db):
        row = db.execute(
            """UPDATE acquisition_targets
               SET cycle_count = COALESCE(cycle_count, 0) + 1,
                   status = 'processing',
                   updated_at = datetime('now'),
                   completed_at = NULL
               WHERE id = ?
               RETURNING cycle_count""",
            (target_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown acquisition target: {target_id}")
        return int(row[0])

    if conn is not None:
        return _write(conn)
    with db_conn() as db:
        cycle = _write(db)
        db.commit()
        return cycle


def attach_job_to_acquisition(job_id: str, target_id: str, cycle: int) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET acquisition_target_id = ?, acquisition_cycle = ? WHERE id = ?",
            (target_id, cycle, job_id),
        )
        conn.commit()


def start_acquisition_attempt(
    target_id: str,
    cycle: int,
    job_id: str | None,
    candidate: dict,
) -> int:
    """Append one candidate attempt and return its ledger row ID."""
    source = (candidate.get("source") or "youtube").strip().lower()
    candidate_id = str(candidate.get("video_id") or "").strip() or None
    candidate_artist = (
        candidate.get("artist") or candidate.get("channel") or candidate.get("uploader") or ""
    )
    with db_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(MAX(sequence), 0) + 1
               FROM acquisition_attempts
               WHERE target_id = ? AND cycle_number = ?""",
            (target_id, cycle),
        ).fetchone()
        sequence = int(row[0] or 1)
        cursor = conn.execute(
            """INSERT INTO acquisition_attempts
               (target_id, cycle_number, job_id, sequence, source, candidate_id,
                candidate_title, candidate_artist, candidate_quality, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'attempting')""",
            (
                target_id, cycle, job_id, sequence, source, candidate_id,
                candidate.get("title") or "", candidate_artist,
                candidate.get("quality") or None,
            ),
        )
        conn.execute(
            """UPDATE acquisition_targets
               SET attempt_count = attempt_count + 1,
                   last_attempt_at = datetime('now'), status = 'processing',
                   updated_at = datetime('now')
               WHERE id = ?""",
            (target_id,),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_acquisition_attempt(attempt_id: int, status: str, error: str | None = None) -> None:
    """Close one attempt and update the target's current summary."""
    clean_error = (error or "").strip()[:1000] or None
    with db_conn() as conn:
        row = conn.execute(
            "SELECT target_id FROM acquisition_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        if not row:
            return
        target_id = row[0]
        conn.execute(
            """UPDATE acquisition_attempts
               SET status = ?, error = ?, completed_at = datetime('now')
               WHERE id = ?""",
            (status, clean_error, attempt_id),
        )
        target_status = "completed" if status == "completed" else "failed"
        conn.execute(
            """UPDATE acquisition_targets
               SET status = ?, last_error = ?, updated_at = datetime('now'),
                   completed_at = CASE WHEN ? = 'completed' THEN datetime('now') ELSE completed_at END
               WHERE id = ?""",
            (target_status, clean_error, target_status, target_id),
        )
        conn.commit()


def finish_acquisition_cycle(target_id: str, status: str, error: str | None = None) -> None:
    clean_error = (error or "").strip()[:1000] or None
    with db_conn() as conn:
        conn.execute(
            """UPDATE acquisition_targets
               SET status = ?, last_error = ?, updated_at = datetime('now'),
                   completed_at = CASE WHEN ? = 'completed' THEN datetime('now') ELSE completed_at END
               WHERE id = ?""",
            (status, clean_error, status, target_id),
        )
        conn.commit()


def acquisition_context_for_job(job_id: str) -> dict | None:
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT at.*, j.acquisition_cycle
               FROM jobs j
               JOIN acquisition_targets at ON at.id = j.acquisition_target_id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


_UNKNOWN_SOURCE_RANK = {
    # Unknown bitrate is not promoted to lossless merely because the provider is
    # capable of it. Direct MP3 sites still precede opaque streaming formats.
    "freemp3cloud": 90,
    "jiosaavn": 85,
    "zvu4no": 80,
    "soulseek": 70,
    "soundcloud": 60,
    "youtube": 50,
    "monochrome": 40,
}


def automatic_candidate_sort_key(candidate: dict, priority_source: str | None = None) -> tuple:
    """Sort an eligible result from best known quality towards worst lossy."""
    source = (candidate.get("source") or "youtube").strip().lower()
    tier = int(candidate.get("quality_tier") or quality_tier_of_result(candidate) or 0)
    quality_rank = tier * 100 if tier else _UNKNOWN_SOURCE_RANK.get(source, 0)
    preferred = int(bool(priority_source) and source == priority_source.strip().lower())
    return (
        quality_rank,
        preferred,
        float(candidate.get("_acquisition_confidence") or 0),
        float(candidate.get("relevance_score") or 0),
    )


def rank_automatic_candidates(
    candidates: list[dict],
    expected_artist: str,
    expected_title: str,
    priority_source: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Gate identity first, then order eligible candidates by quality tier.

    The preferred source only breaks ties inside a quality tier. It cannot make
    a lossy candidate jump ahead of an allowed, confident lossless result.
    """
    eligible: list[dict] = []
    rejected: list[dict] = []
    query = f"{expected_artist} - {expected_title}".strip(" -")
    for original in candidates:
        candidate = dict(original)
        confidence, breakdown = compute_match_confidence(
            expected_artist,
            expected_title,
            candidate.get("title") or "",
            candidate_artist=(
                candidate.get("artist") or candidate.get("channel") or candidate.get("uploader")
            ),
            query=query,
        )
        candidate["_acquisition_confidence"] = confidence
        candidate["_acquisition_match_breakdown"] = breakdown
        if confidence < FALLBACK_MATCH_CONFIDENCE_FLOOR:
            rejected.append(candidate)
        else:
            eligible.append(candidate)
    eligible.sort(
        key=lambda result: automatic_candidate_sort_key(result, priority_source),
        reverse=True,
    )
    return eligible, rejected
