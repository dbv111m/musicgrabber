"""Conservative, read-only audio provenance auditing.

The audit describes what can be observed and what MusicGrabber previously
recorded.  It does not attempt the rather more theatrical trick of proving that
a lossless-looking file has never met a lossy encoder.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from audio_probe import AUDIO_EXTENSIONS, probe_file
from constants import DB_PATH, MUSIC_DIR
from db import db_conn
from settings import get_setting, get_trash_dir
from utils import spawn_daemon_thread, is_excluded_scan_dir


CRITERIA_VERSION = "1"
# A full NAS library is an excellent way to discover how many other endpoints
# also need that mount. Four readers keep a large audit practical; the priority
# gate below pauses them between files whenever an interactive library view
# needs the same storage.
PROBE_WORKERS = 4
PROBE_YIELD_SECONDS = 0.025
READ_ONLY_NOTICE = (
    "Dry-run report: no audio files were renamed, retagged, moved, deleted, "
    "replaced, or otherwise improved by vigorous staring."
)

CLASSIFICATIONS = {
    "known_lossy_transcode": "Known lossy transcode",
    "native_lossy": "Native lossy acquisition",
    "lossy_derivative": "Lossy derivative of recorded lossless",
    "recorded_lossless": "Recorded lossless acquisition",
    "historical_unknown": "Historical or unknown provenance",
    "unreadable": "Unreadable audio file",
}

LOSSLESS_CODECS = {
    "aiff",
    "alac",
    "ape",
    "flac",
    "pcm",
    "tak",
    "wav",
    "wave",
    "wavpack",
}
LOSSY_CODECS = {
    "aac",
    "ac3",
    "eac3",
    "mp3",
    "mp4a",
    "opus",
    "vorbis",
    "wma",
}
LOSSY_PROVIDERS = {
    "freemp3cloud",
    "jiosaavn",
    "mp3phoenix",
    "soundcloud",
    "youtube",
    "zvu4no",
}

_CODEC_ALIASES = {
    "m4a": "aac",
    "mp4a": "aac",
    "ogg": "vorbis",
    "oga": "vorbis",
    "wave": "wav",
    "wv": "wavpack",
}
_scan_lock = threading.Lock()
_active_users: set[str] = set()
_library_priority = threading.Condition()
_priority_readers = 0


def _norm_user(user_id: str | None) -> str:
    return user_id or ""


def _normalise_codec(value: str | None) -> str | None:
    if not value:
        return None
    codec = re.sub(r"[^a-z0-9]+", "", value.strip().lower())
    return _CODEC_ALIASES.get(codec, codec) or None


def _quality_kind(codec: str | None) -> str:
    if codec in LOSSLESS_CODECS:
        return "lossless"
    if codec in LOSSY_CODECS:
        return "lossy"
    return "unknown"


def _numbers(value: str | None) -> list[int]:
    return [int(number) for number in re.findall(r"\d+", value or "")]


def _bitrate_bucket(kbps: int) -> tuple[str, int | None]:
    if not kbps:
        return "lossy_unknown", None
    if kbps >= 300:
        return "lossy_320", 4
    if kbps >= 240:
        return "lossy_256", 3
    if kbps >= 170:
        return "lossy_192", 2
    return "lossy_128", 1


def _recorded_origin(info: dict) -> tuple[str | None, int, list[dict]]:
    """Return recorded/inferred origin codec, bitrate, and supporting evidence."""
    evidence: list[dict] = []
    structured_codec = _normalise_codec(info.get("source_codec"))
    structured_bitrate = int(info.get("source_bitrate_kbps") or 0)
    if structured_codec:
        evidence.append({
            "kind": "recorded_metadata",
            "field": "SOURCE_CODEC",
            "label": "Recorded acquisition codec",
            "value": structured_codec.upper(),
            "meaning": "MusicGrabber recorded this codec at acquisition time.",
        })
        if structured_bitrate:
            evidence.append({
                "kind": "recorded_metadata",
                "field": "SOURCE_BITRATE",
                "label": "Recorded acquisition bitrate",
                "value": f"{structured_bitrate} kbps",
                "meaning": "This is recorded provenance, not a fresh measurement.",
            })
        return structured_codec, structured_bitrate, evidence

    source_quality = (info.get("source_quality") or "").strip()
    from_match = re.search(r"\bfrom\s+([a-z0-9]+)", source_quality, re.I)
    if from_match:
        codec = _normalise_codec(from_match.group(1))
        from_part = re.split(r"\bfrom\b", source_quality, maxsplit=1, flags=re.I)[-1]
        bitrate = max(_numbers(from_part), default=0)
        evidence.append({
            "kind": "legacy_metadata",
            "field": "SOURCE_QUALITY",
            "label": "Legacy recorded acquisition quality",
            "value": source_quality,
            "meaning": "The older human-readable tag records a conversion origin.",
        })
        return codec, bitrate, evidence

    # Older MusicGrabber files often carry only SOURCE + SOURCE_QUALITY.  A
    # leading codec can still be used as recorded evidence, but only when the
    # SOURCE marker confirms this is one of ours.
    source = (info.get("source") or "").strip()
    leading = re.match(r"^\s*([a-z0-9]+)", source_quality, re.I)
    if source and leading:
        codec = _normalise_codec(leading.group(1))
        if codec in LOSSLESS_CODECS | LOSSY_CODECS:
            bitrate = max(_numbers(source_quality), default=0)
            evidence.append({
                "kind": "legacy_metadata",
                "field": "SOURCE_QUALITY",
                "label": "Legacy recorded acquisition quality",
                "value": source_quality,
                "meaning": "A MusicGrabber source marker accompanies this older quality tag.",
            })
            return codec, bitrate, evidence

    upper_quality = source_quality.upper()
    if source and ("LOSSLESS" in upper_quality or "HI_RES" in upper_quality):
        evidence.append({
            "kind": "legacy_metadata",
            "field": "SOURCE_QUALITY",
            "label": "Legacy recorded acquisition quality",
            "value": source_quality,
            "meaning": "The provider result was recorded as lossless; this is not forensic proof.",
        })
        return "flac", 0, evidence

    provider = source.lower()
    if provider in LOSSY_PROVIDERS:
        evidence.append({
            "kind": "provider_record",
            "field": "SOURCE",
            "label": "Recorded acquisition service",
            "value": source,
            "meaning": "This MusicGrabber provider path supplies lossy audio.",
        })
        return "unknown_lossy", 0, evidence

    return None, 0, evidence


def classify_provenance(info: dict) -> dict:
    """Classify one successfully probed file and explain every conclusion."""
    stored_codec = _normalise_codec(info.get("codec"))
    stored_kind = _quality_kind(stored_codec)
    stored_bitrate = int(info.get("bitrate_kbps") or 0)
    origin_codec, origin_bitrate, origin_evidence = _recorded_origin(info)
    origin_kind = (
        "lossy" if origin_codec == "unknown_lossy" else _quality_kind(origin_codec)
    )

    evidence = [{
        "kind": "observed_file",
        "field": "codec",
        "label": "Stored codec",
        "value": (stored_codec or "unknown").upper(),
        "meaning": (
            "Observed from the file structure. This describes storage, not the "
            "quality of every earlier generation."
        ),
    }]
    if stored_bitrate:
        evidence.append({
            "kind": "observed_file",
            "field": "bitrate_kbps",
            "label": "Observed average bitrate",
            "value": f"{stored_bitrate} kbps",
            "meaning": "Useful for describing a lossy file, never proof of a lossless origin.",
        })
    evidence.extend(origin_evidence)

    source = (info.get("source") or "").strip()
    if source and not any(item["field"] == "SOURCE" for item in evidence):
        evidence.append({
            "kind": "recorded_metadata",
            "field": "SOURCE",
            "label": "Recorded acquisition source",
            "value": source,
            "meaning": "MusicGrabber recorded the service used to acquire this file.",
        })

    if stored_kind == "lossless" and origin_kind == "lossy":
        classification = "known_lossy_transcode"
        effective_quality, effective_tier = _bitrate_bucket(origin_bitrate)
    elif stored_kind == "lossy" and origin_kind == "lossy":
        classification = "native_lossy"
        rates = [rate for rate in (stored_bitrate, origin_bitrate) if rate]
        effective_quality, effective_tier = _bitrate_bucket(min(rates) if rates else 0)
    elif stored_kind == "lossy" and origin_kind == "lossless":
        classification = "lossy_derivative"
        effective_quality, effective_tier = _bitrate_bucket(stored_bitrate)
    elif stored_kind == "lossless" and origin_kind == "lossless":
        classification = "recorded_lossless"
        effective_quality, effective_tier = "recorded_lossless", 5
    else:
        classification = "historical_unknown"
        if stored_kind == "lossy":
            # A lossy wrapper gives an honest upper bound even when its earlier
            # history is unknown.
            effective_quality, effective_tier = _bitrate_bucket(stored_bitrate)
        else:
            effective_quality, effective_tier = "unknown", None
        evidence.append({
            "kind": "missing_evidence",
            "field": "provenance",
            "label": "Provenance gap",
            "value": "No decisive acquisition codec was recorded",
            "meaning": "The file remains unknown rather than being promoted by appearance.",
        })

    caveats = [
        (
            "Codec, container, bitrate, tags, and any future spectral heuristic "
            "cannot prove that a file is genuinely lossless."
        )
    ]
    if classification == "recorded_lossless":
        caveats.append(
            "Recorded lossless means the acquisition metadata says lossless; it is not a forensic guarantee."
        )
    elif classification == "historical_unknown":
        caveats.append(
            "Missing or ambiguous history is deliberately left unknown."
        )

    return {
        "classification": classification,
        "classification_label": CLASSIFICATIONS[classification],
        "stored_quality": stored_kind,
        "effective_quality": effective_quality,
        "effective_tier": effective_tier,
        "origin_codec": origin_codec,
        "origin_bitrate_kbps": origin_bitrate,
        "evidence": evidence,
        "caveats": caveats,
    }


def _unreadable_result() -> dict:
    return {
        "classification": "unreadable",
        "classification_label": CLASSIFICATIONS["unreadable"],
        "stored_quality": "unknown",
        "effective_quality": "unknown",
        "effective_tier": None,
        "origin_codec": None,
        "origin_bitrate_kbps": 0,
        "evidence": [{
            "kind": "read_error",
            "field": "file",
            "label": "Inspection failed",
            "value": "Mutagen could not read this audio file",
            "meaning": "No quality or provenance conclusion was made.",
        }],
        "caveats": ["The file was not modified while inspection failed."],
    }


def prioritise_over_audio_audit(func):
    """Let an interactive library read pause the background audit.

    FastAPI runs these synchronous handlers in worker threads. ``wraps`` keeps
    their signatures intact for FastAPI's parameter inspection.
    """
    @wraps(func)
    def wrapped(*args, **kwargs):
        global _priority_readers
        with _library_priority:
            _priority_readers += 1
        try:
            return func(*args, **kwargs)
        finally:
            with _library_priority:
                _priority_readers -= 1
                _library_priority.notify_all()

    return wrapped


def _polite_probe(path: Path) -> dict | None:
    with _library_priority:
        while _priority_readers:
            _library_priority.wait(timeout=0.5)
    try:
        return probe_file(path)
    finally:
        time.sleep(PROBE_YIELD_SECONDS)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _excluded_roots(user_id: str | None, music_root: Path) -> set[Path]:
    candidates = {
        get_trash_dir(user_id),
        DB_PATH.parent / ".upgrade_quarantine",
    }
    staging = get_setting("slskd_downloads_path", "", user_id=user_id).strip()
    if staging:
        candidates.add(Path(staging))
    excluded: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved != music_root and _is_within(resolved, music_root):
            excluded.add(resolved)
    return excluded


def _gather_files(root: Path, excluded_roots: set[Path]) -> tuple[list[Path], int]:
    files: list[Path] = []
    excluded_count = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for dirname in dirnames:
            # NAS-generated bins live inside the share itself, so they pass every
            # "is it under the music root?" test with flying colours. Name check
            # first, before we waste a resolve() on somebody's deleted albums.
            if is_excluded_scan_dir(dirname):
                excluded_count += 1
                continue
            child = current_path / dirname
            try:
                if child.is_symlink():
                    excluded_count += 1
                    continue
                resolved = child.resolve()
            except OSError:
                excluded_count += 1
                continue
            if resolved in excluded_roots or not _is_within(resolved, root):
                excluded_count += 1
                continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs

        for filename in filenames:
            path = current_path / filename
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                if path.is_symlink() or not _is_within(path.resolve(), root):
                    excluded_count += 1
                    continue
            except OSError:
                excluded_count += 1
                continue
            files.append(path)
    files.sort(key=lambda item: str(item).casefold())
    return files, excluded_count


def _insert_audit_file(
    conn: sqlite3.Connection,
    run_id: str,
    uid: str,
    root: Path,
    path: Path,
    info: dict | None,
) -> bool:
    try:
        stat = path.stat()
    except OSError:
        stat = None

    result = classify_provenance(info) if info is not None else _unreadable_result()
    relpath = str(path.relative_to(root))
    payload = info or {}
    conn.execute(
        """
        INSERT INTO audio_audit_files (
            run_id, user_id, path, filename, file_size, mtime, container, codec,
            bitrate_kbps, duration, sample_rate_hz, bits_per_sample, channels,
            source, source_quality, source_codec, source_bitrate_kbps, file_id,
            artist, title, stored_quality, effective_quality, effective_tier,
            classification, classification_label, evidence_json, caveats_json,
            read_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            uid,
            relpath,
            path.name,
            stat.st_size if stat else None,
            stat.st_mtime if stat else None,
            payload.get("container"),
            payload.get("codec"),
            payload.get("bitrate_kbps"),
            payload.get("duration"),
            payload.get("sample_rate_hz"),
            payload.get("bits_per_sample"),
            payload.get("channels"),
            payload.get("source"),
            payload.get("source_quality"),
            result.get("origin_codec") or payload.get("source_codec"),
            result.get("origin_bitrate_kbps") or payload.get("source_bitrate_kbps"),
            payload.get("file_id"),
            payload.get("artist"),
            payload.get("title"),
            result["stored_quality"],
            result["effective_quality"],
            result["effective_tier"],
            result["classification"],
            result["classification_label"],
            json.dumps(result["evidence"], ensure_ascii=False),
            json.dumps(result["caveats"], ensure_ascii=False),
            None if info is not None else "Mutagen could not read this audio file",
        ),
    )
    return info is None


def run_audit_scan(user_id: str | None, run_id: str | None = None) -> dict:
    """Build and atomically publish one complete read-only snapshot."""
    uid = _norm_user(user_id)
    run_id = run_id or str(uuid.uuid4())
    configured_root = Path(
        get_setting("music_dir", str(MUSIC_DIR), user_id=user_id)
    ).expanduser()

    try:
        root = configured_root.resolve()
        if not root.is_dir():
            raise RuntimeError(f"Music directory is not available: {configured_root}")

        excluded_roots = _excluded_roots(user_id, root)
        paths, excluded_count = _gather_files(root, excluded_roots)

        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO audio_audit_runs (
                    id, user_id, music_root, criteria_version, status,
                    total_files, excluded_paths
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    music_root=excluded.music_root,
                    criteria_version=excluded.criteria_version,
                    status='running',
                    total_files=excluded.total_files,
                    excluded_paths=excluded.excluded_paths,
                    error=NULL
                """,
                (run_id, uid, str(root), CRITERIA_VERSION, len(paths), excluded_count),
            )
            conn.commit()

        unreadable = 0
        batch: list[tuple[int, Path, dict | None]] = []
        worker_count = min(PROBE_WORKERS, max(1, len(paths)))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="audio-audit",
        ) as executor:
            for index, (path, info) in enumerate(
                zip(paths, executor.map(_polite_probe, paths)),
                start=1,
            ):
                batch.append((index, path, info))
                if len(batch) < 50 and index < len(paths):
                    continue
                with db_conn() as conn:
                    for _, batch_path, batch_info in batch:
                        unreadable += int(
                            _insert_audit_file(
                                conn, run_id, uid, root, batch_path, batch_info
                            )
                        )
                    conn.execute(
                        """
                        UPDATE audio_audit_runs
                        SET scanned_files=?, classified_files=?, unreadable_files=?
                        WHERE id=? AND user_id=?
                        """,
                        (index, index - unreadable, unreadable, run_id, uid),
                    )
                    conn.commit()
                batch.clear()

        with db_conn() as conn:
            conn.execute(
                "UPDATE audio_audit_runs SET is_current=0 WHERE user_id=?",
                (uid,),
            )
            conn.execute(
                """
                UPDATE audio_audit_runs
                SET status='completed', is_current=1,
                    completed_at=CURRENT_TIMESTAMP
                WHERE id=? AND user_id=?
                """,
                (run_id, uid),
            )
            # Keep the current report plus two predecessors for diagnosis. They
            # are not exposed as selectable reports yet.
            old_ids = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT id FROM audio_audit_runs
                    WHERE user_id=? AND is_current=0 AND status!='running'
                    ORDER BY started_at DESC LIMIT -1 OFFSET 2
                    """,
                    (uid,),
                ).fetchall()
            ]
            if old_ids:
                conn.executemany(
                    "DELETE FROM audio_audit_runs WHERE id=? AND user_id=?",
                    [(old_id, uid) for old_id in old_ids],
                )
            conn.commit()
        return get_audit_status(user_id)
    except Exception as exc:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO audio_audit_runs (
                    id, user_id, music_root, criteria_version, status, error,
                    completed_at
                ) VALUES (?, ?, ?, ?, 'failed', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    status='failed', error=excluded.error,
                    completed_at=CURRENT_TIMESTAMP
                """,
                (run_id, uid, str(configured_root), CRITERIA_VERSION, str(exc)),
            )
            conn.commit()
        return get_audit_status(user_id)
    finally:
        with _scan_lock:
            _active_users.discard(uid)


def start_audit_scan(user_id: str | None) -> dict:
    uid = _norm_user(user_id)
    with _scan_lock:
        if uid in _active_users:
            status = get_audit_status(user_id)
            return {
                "status": "already_running",
                "scan": status.get("running"),
            }
        _active_users.add(uid)

    run_id = str(uuid.uuid4())
    root = get_setting("music_dir", str(MUSIC_DIR), user_id=user_id)
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE audio_audit_runs
            SET status='failed', error='Scan interrupted before completion',
                completed_at=CURRENT_TIMESTAMP
            WHERE user_id=? AND status='running'
            """,
            (uid,),
        )
        conn.execute(
            """
            INSERT INTO audio_audit_runs
                (id, user_id, music_root, criteria_version, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (run_id, uid, root, CRITERIA_VERSION),
        )
        conn.commit()
    spawn_daemon_thread(run_audit_scan, user_id, run_id)
    return {"status": "started", "scan": {"id": run_id, "status": "running"}}


def _run_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    total = int(result.get("total_files") or 0)
    scanned = int(result.get("scanned_files") or 0)
    result["progress_percent"] = round(scanned * 100 / total) if total else 0
    return result


def _summary_for_run(conn: sqlite3.Connection, run_id: str) -> dict:
    counts = {
        row["classification"]: row["count"]
        for row in conn.execute(
            """
            SELECT classification, COUNT(*) AS count
            FROM audio_audit_files WHERE run_id=?
            GROUP BY classification
            """,
            (run_id,),
        ).fetchall()
    }
    return {
        "total": sum(counts.values()),
        "counts": {
            key: int(counts.get(key, 0))
            for key in CLASSIFICATIONS
        },
    }


def _options_for_run(conn: sqlite3.Connection, run_id: str) -> dict:
    options: dict[str, list[str]] = {}
    for field in ("container", "codec", "source", "effective_quality"):
        options[field] = [
            row[0]
            for row in conn.execute(
                f"""
                SELECT DISTINCT {field} FROM audio_audit_files
                WHERE run_id=? AND {field} IS NOT NULL AND {field}!=''
                ORDER BY {field} COLLATE NOCASE
                """,
                (run_id,),
            ).fetchall()
        ]
    return options


def get_audit_status(user_id: str | None) -> dict:
    uid = _norm_user(user_id)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        # A daemon thread cannot survive a process/container restart. Turn any
        # orphaned row into an honest failure so the UI does not remain stuck on
        # "running" with a disabled button until the heat death of the universe.
        if uid not in _active_users:
            conn.execute(
                """
                UPDATE audio_audit_runs
                SET status='failed',
                    error='Scan interrupted before completion',
                    completed_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND status='running'
                """,
                (uid,),
            )
            conn.commit()
        running = conn.execute(
            """
            SELECT * FROM audio_audit_runs
            WHERE user_id=? AND status='running'
            ORDER BY started_at DESC LIMIT 1
            """,
            (uid,),
        ).fetchone()
        current = conn.execute(
            """
            SELECT * FROM audio_audit_runs
            WHERE user_id=? AND is_current=1 AND status='completed'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (uid,),
        ).fetchone()
        failed = conn.execute(
            """
            SELECT * FROM audio_audit_runs
            WHERE user_id=? AND status='failed'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (uid,),
        ).fetchone()
        current_dict = _run_dict(current)
        summary = _summary_for_run(conn, current["id"]) if current else None
        options = _options_for_run(conn, current["id"]) if current else {}
    return {
        "criteria_version": CRITERIA_VERSION,
        "read_only_notice": READ_ONLY_NOTICE,
        "running": _run_dict(running),
        "current": current_dict,
        "last_failed": _run_dict(failed),
        "summary": summary,
        "options": options,
    }


def _current_run_id(conn: sqlite3.Connection, uid: str) -> str | None:
    row = conn.execute(
        """
        SELECT id FROM audio_audit_runs
        WHERE user_id=? AND is_current=1 AND status='completed'
        ORDER BY completed_at DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()
    return row[0] if row else None


def _filtered_where(filters: dict) -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    for field in (
        "classification",
        "container",
        "codec",
        "source",
        "effective_quality",
    ):
        value = (filters.get(field) or "").strip()
        if value:
            clauses.append(f"{field}=?")
            params.append(value)
    lossless_only = (filters.get("lossless_only") or "").strip()
    if lossless_only == "stored":
        clauses.append("stored_quality='lossless'")
    elif lossless_only == "recorded":
        clauses.append("effective_quality='recorded_lossless'")
    return clauses, params


def _decode_file_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    item["caveats"] = json.loads(item.pop("caveats_json") or "[]")
    return item


def get_audit_files(
    user_id: str | None,
    filters: dict | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    uid = _norm_user(user_id)
    filters = filters or {}
    page = max(1, page)
    per_page = min(100, max(1, per_page))
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        run_id = _current_run_id(conn, uid)
        if not run_id:
            return {"items": [], "total": 0, "page": 1, "pages": 0, "run_id": None}
        clauses, params = _filtered_where(filters)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM audio_audit_files WHERE run_id=?{where}",
            (run_id, *params),
        ).fetchone()[0]
        pages = (total + per_page - 1) // per_page
        rows = conn.execute(
            f"""
            SELECT * FROM audio_audit_files
            WHERE run_id=?{where}
            ORDER BY path COLLATE NOCASE
            LIMIT ? OFFSET ?
            """,
            (run_id, *params, per_page, (page - 1) * per_page),
        ).fetchall()
    return {
        "items": [_decode_file_row(row) for row in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "run_id": run_id,
    }


def get_audit_export(user_id: str | None, filters: dict | None = None) -> dict:
    """Return a complete filtered dry-run report suitable for JSON or CSV."""
    uid = _norm_user(user_id)
    filters = filters or {}
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        run_id = _current_run_id(conn, uid)
        if not run_id:
            return {
                "metadata": {
                    "criteria_version": CRITERIA_VERSION,
                    "read_only_notice": READ_ONLY_NOTICE,
                    "filters": filters,
                },
                "files": [],
            }
        run = conn.execute(
            "SELECT * FROM audio_audit_runs WHERE id=? AND user_id=?",
            (run_id, uid),
        ).fetchone()
        clauses, params = _filtered_where(filters)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT * FROM audio_audit_files
            WHERE run_id=?{where}
            ORDER BY path COLLATE NOCASE
            """,
            (run_id, *params),
        ).fetchall()
    return {
        "metadata": {
            "scan": dict(run),
            "criteria_version": CRITERIA_VERSION,
            "read_only_notice": READ_ONLY_NOTICE,
            "filters": filters,
        },
        "files": [_decode_file_row(row) for row in rows],
    }
