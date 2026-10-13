"""
MusicGrabber - Long-form YouTube Splitting

Detects a single long YouTube video (DJ mixes, "CD1/CD2" style compilation
uploads, and the like) as a split candidate, using YouTube's own chapters
first, then a cue sheet in the video description, then one hunted out of the
comments, falling back to manual entry. A confirmed split downloads the
video's audio once and cuts it into individually tagged tracks with ffmpeg,
landing them in Albums/Artist/Album/ the same way a normal album download does.
"""

import json
import re
import sqlite3
import subprocess
import uuid
from typing import Optional

import httpx

from constants import (
    LONGFORM_SPLIT_THRESHOLD_SECONDS,
    TIMEOUT_YTDLP_INFO,
    TIMEOUT_YTDLP_COMMENTS,
    TIMEOUT_LONGFORM_DOWNLOAD,
    TIMEOUT_FFMPEG_CONVERT,
    COVER_ART_TIMEOUT,
)
from coverart import ensure_album_cover_files, _guess_cover_mime
from db import db_conn
from metadata import apply_metadata_to_file
from settings import get_albums_dir
from utils import sanitize_filename, cap_filename_stem, set_file_permissions
from youtube import _ytdlp_base_args, run_ytdlp_with_retries


# "Artist - Title" splitter, same shape as the one bulk-import lines are parsed
# with (app.py's clean_bulk_import_line path) so chapter/cue titles like
# "Above & Beyond - Sun & Moon" separate the same way a pasted line would.
_ARTIST_TITLE_RE = re.compile(r'^(.+?)\s*[-–—]\s*(.+)$')

# Cue-sheet-shaped line: an optional leading track number, an optional "[" or
# "(" before the timestamp (YouTube auto-links bare "H:MM:SS" in descriptions
# regardless of what wraps it, and creators wrap it in either), HH:MM:SS or
# MM:SS, then the title. Deliberately permissive (real cue sheets are
# hand-typed and inconsistent) but always anchored on the timestamp so it
# never mistakes ordinary prose for a cue point.
_CUE_LINE_RE = re.compile(
    r'^\s*(?:\d+[.)]\s*)?[\[(]?(\d{1,2}(?::\d{2}){1,2})[\])]?\s*[-–—:]?\s*(.+?)\s*$'
)


def _split_artist_title(text: str) -> tuple[Optional[str], str]:
    """Split "Artist - Title" text. Returns (None, text) when it isn't shaped that way."""
    match = _ARTIST_TITLE_RE.match(text.strip())
    if not match:
        return None, text.strip()
    artist, title = match.groups()
    return artist.strip(), title.strip()


def _parse_timestamp(raw: str) -> float:
    seconds = 0
    for part in raw.split(':'):
        seconds = seconds * 60 + int(part)
    return float(seconds)


def parse_cue_sheet_text(text: str) -> list[dict]:
    """Scan freeform text (a comment body, or a pasted cue sheet) for cue points.

    Returns an ordered list of {"start_seconds": float, "title": str}, one per
    matched line. Callers fill in "end_seconds" (the next segment's start, or
    the video duration for the last one) and split "Artist - Title" themselves.
    """
    points = []
    for line in (text or "").splitlines():
        match = _CUE_LINE_RE.match(line)
        if not match:
            continue
        raw_ts, title = match.groups()
        title = title.strip(" -–—:")
        if not title:
            continue
        points.append({"start_seconds": _parse_timestamp(raw_ts), "title": title})
    return points


def _fill_end_times(cue_points: list[dict], duration: float) -> list[dict]:
    segments = []
    for i, point in enumerate(cue_points):
        end = cue_points[i + 1]["start_seconds"] if i + 1 < len(cue_points) else duration
        segments.append({"start_seconds": point["start_seconds"], "end_seconds": end, "title": point["title"]})
    return segments


def _segments_from_chapters(chapters: list[dict]) -> list[dict]:
    segments = []
    for chapter in chapters:
        title = (chapter.get("title") or "").strip()
        if not title:
            continue
        segments.append({
            "start_seconds": float(chapter.get("start_time") or 0),
            "end_seconds": float(chapter.get("end_time") or 0),
            "title": title,
        })
    return segments


def _best_cue_sheet_comment(comments: list[dict]) -> list[dict]:
    """Pick the comment with the most cue-sheet-shaped lines. A real cue sheet
    has many; a comment that just happens to mention one timestamp has one."""
    best: list[dict] = []
    for comment in comments:
        candidate = parse_cue_sheet_text(comment.get("text") or "")
        if len(candidate) > len(best):
            best = candidate
    return best if len(best) >= 2 else []


def _run_ytdlp_json(url: str, extra_args: list[str], timeout: int, *, user_id: Optional[str], operation: str) -> Optional[dict]:
    # --no-playlist matters here: without it, a "list=RD..." auto-mix param
    # (which YouTube tacks onto ordinary watch URLs) makes yt-dlp resolve the
    # whole - potentially endless - auto-generated mix instead of just this
    # one video, which is the entire reason this function gets called.
    cmd = ["yt-dlp", *_ytdlp_base_args(user_id), "--dump-json", "--no-warnings",
           "--skip-download", "--no-playlist", *extra_args, url]
    result, timed_out = run_ytdlp_with_retries(cmd, timeout, operation=operation)
    if timed_out or result is None or result.returncode != 0:
        return None
    try:
        # --write-comments can print warnings ahead of the JSON on some
        # versions; the JSON payload is always the last line.
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def detect_longform_candidate(url: str, user_id: Optional[str] = None) -> dict:
    """Probe a YouTube URL for long-form split eligibility. No audio is downloaded."""
    info = _run_ytdlp_json(url, [], TIMEOUT_YTDLP_INFO, user_id=user_id, operation="Long-form video info lookup")
    if not info:
        return {"eligible": False, "error": "Could not look up that video"}

    duration = float(info.get("duration") or 0)
    if duration < LONGFORM_SPLIT_THRESHOLD_SECONDS:
        return {"eligible": False}

    chapters = info.get("chapters") or []
    desc_points = parse_cue_sheet_text(info.get("description") or "")
    if chapters:
        raw_segments = _segments_from_chapters(chapters)
        detected_via = "chapters"
    elif len(desc_points) >= 2:
        # Timestamps linked in a video's description are not necessarily
        # YouTube "chapters" (those need the uploader to opt in / meet extra
        # requirements), but a hand-written "(0:00:00) Artist - Title" cue
        # sheet in the description is the uploader's own words and free to
        # check, since it's already sitting in the info we just fetched.
        raw_segments = _fill_end_times(desc_points, duration)
        detected_via = "description"
    else:
        # Bounded: 200 top-level comments is plenty to catch a pinned/popular
        # cue sheet without turning "Fetch" into a multi-minute wait. Replies
        # aren't worth the extra round trip; a cue sheet is always its own comment.
        comment_info = _run_ytdlp_json(
            url,
            ["--write-comments", "--extractor-args", "youtube:max_comments=200,200,0,0"],
            TIMEOUT_YTDLP_COMMENTS,
            user_id=user_id,
            operation="Long-form comment cue-sheet lookup",
        )
        comments = (comment_info or {}).get("comments") or []
        cue_points = _best_cue_sheet_comment(comments)
        raw_segments = _fill_end_times(cue_points, duration) if cue_points else []
        detected_via = "comments" if raw_segments else "manual"

    segments = []
    for seg in raw_segments:
        artist, title = _split_artist_title(seg["title"])
        segments.append({
            "start_seconds": seg["start_seconds"],
            "end_seconds": seg["end_seconds"],
            "title": title,
            "artist": artist,
        })

    return {
        "eligible": True,
        "video_id": info.get("id"),
        "video_title": info.get("title") or "Untitled",
        "thumbnail": info.get("thumbnail"),
        "duration": duration,
        "detected_via": detected_via,
        "segments": segments,
    }


def validate_segments(segments: list[dict], duration: Optional[float] = None) -> Optional[str]:
    """Sanity-check a user-confirmed segment table. Returns an error string, or None if fine."""
    if not segments:
        return "At least one segment is required"
    for i, seg in enumerate(segments, 1):
        if not (seg.get("title") or "").strip():
            return f"Segment {i} needs a title"
        if seg["end_seconds"] <= seg["start_seconds"]:
            return f"Segment {i}'s end time must be after its start time"
        if duration and seg["end_seconds"] > duration + 1:
            return f"Segment {i} runs past the end of the source video"
    return None


def create_longform_split(url: str, video_title: str, album_artist: str, segments: list[dict], user_id: Optional[str] = None) -> str:
    """Insert the split + segment rows and hand off to the background worker. Returns the split id."""
    split_id = str(uuid.uuid4())[:8]
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO longform_splits
               (id, source_url, video_title, album_artist, total_segments, user_id, progress_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (split_id, url, video_title, album_artist, len(segments), user_id),
        )
        for index, seg in enumerate(segments):
            conn.execute(
                """INSERT INTO longform_split_segments
                   (split_id, segment_index, start_seconds, end_seconds, title, artist)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (split_id, index, seg["start_seconds"], seg["end_seconds"], seg["title"].strip(),
                 (seg.get("artist") or "").strip() or None),
            )
        conn.commit()

    from utils import spawn_daemon_thread
    spawn_daemon_thread(process_longform_split, split_id)
    return split_id


def _touch_split(conn, split_id: str, **fields) -> None:
    set_clauses = ["progress_at = datetime('now')"]
    params = []
    for col, val in fields.items():
        set_clauses.append(f"{col} = ?")
        params.append(val)
    params.append(split_id)
    conn.execute(f"UPDATE longform_splits SET {', '.join(set_clauses)} WHERE id = ?", params)


def _touch_segment(conn, segment_id: int, **fields) -> None:
    set_clauses = [f"{col} = ?" for col in fields]
    params = list(fields.values()) + [segment_id]
    conn.execute(f"UPDATE longform_split_segments SET {', '.join(set_clauses)} WHERE id = ?", params)


def _split_cancelled(split_id: str) -> bool:
    with db_conn() as conn:
        row = conn.execute("SELECT cancel_requested FROM longform_splits WHERE id = ?", (split_id,)).fetchone()
        return bool(row and row[0])


def process_longform_split(split_id: str) -> None:
    """Background worker: download the master once, cut it into segments, tag each."""
    from downloads import (
        _make_staging_dir, _clear_staging_dir, _validate_audio_integrity,
        probe_audio_quality, _extract_source_format_from_info,
        trigger_navidrome_scan, trigger_jellyfin_scan, trigger_plex_scan,
    )

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        split = conn.execute("SELECT * FROM longform_splits WHERE id = ?", (split_id,)).fetchone()
        if not split:
            return
        split = dict(split)
        segment_rows = conn.execute(
            "SELECT * FROM longform_split_segments WHERE split_id = ? ORDER BY segment_index",
            (split_id,),
        ).fetchall()
        segment_rows = [dict(r) for r in segment_rows]

    user_id = split.get("user_id")
    album_artist = split["album_artist"]
    video_title = split["video_title"]
    is_compilation = album_artist.strip().lower() == "various artists"
    staging = None

    try:
        with db_conn() as conn:
            _touch_split(conn, split_id, status="downloading")
            conn.commit()

        staging = _make_staging_dir(f"longform-{split_id}", user_id=user_id)

        info = _run_ytdlp_json(
            split["source_url"], [], TIMEOUT_YTDLP_INFO, user_id=user_id,
            operation=f"Long-form re-check before downloading {split_id}",
        ) or {}
        source_codec, source_bitrate = _extract_source_format_from_info(info)

        download_cmd = [
            "yt-dlp", *_ytdlp_base_args(user_id),
            "-f", "bestaudio", "--no-playlist", "--no-warnings",
            "-o", str(staging / "master.%(ext)s"),
            split["source_url"],
        ]
        result, timed_out = run_ytdlp_with_retries(
            download_cmd, TIMEOUT_LONGFORM_DOWNLOAD, operation=f"Long-form master download for {split_id}",
        )
        if timed_out or result is None or result.returncode != 0:
            raise Exception("Could not download the source video's audio")

        master_candidates = [p for p in staging.iterdir() if p.stem == "master"]
        if not master_candidates:
            raise Exception("Downloaded master file went missing")
        master_path = master_candidates[0]

        override_dir = get_albums_dir(user_id) / sanitize_filename(album_artist) / sanitize_filename(video_title)
        override_dir.mkdir(parents=True, exist_ok=True)

        thumb_bytes, thumb_mime = None, None
        thumbnail_url = info.get("thumbnail")
        if thumbnail_url:
            try:
                resp = httpx.get(thumbnail_url, timeout=COVER_ART_TIMEOUT, follow_redirects=True)
                if resp.status_code == 200 and resp.content:
                    thumb_bytes = resp.content
                    thumb_mime = _guess_cover_mime(resp.content, resp.headers.get("content-type"))
            except Exception:
                pass
        if thumb_bytes:
            ensure_album_cover_files(str(override_dir), thumb_bytes, thumb_mime)

        with db_conn() as conn:
            _touch_split(conn, split_id, status="splitting", override_dir=str(override_dir),
                         master_staging_path=str(master_path))
            conn.commit()

        completed, failed = 0, 0
        total = len(segment_rows)
        for seg in segment_rows:
            if _split_cancelled(split_id):
                with db_conn() as conn:
                    _touch_segment(conn, seg["id"], status="failed", error="Cancelled by user")
                    conn.commit()
                failed += 1
                continue

            with db_conn() as conn:
                _touch_segment(conn, seg["id"], status="cutting")
                conn.commit()

            index = seg["segment_index"]
            title = seg["title"]
            artist = (seg.get("artist") or "").strip() or album_artist
            stem = cap_filename_stem(f"{str(index + 1).zfill(2)} - {sanitize_filename(title)}")
            segment_path = override_dir / f"{stem}.flac"

            try:
                cut_result = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", str(master_path),
                     "-ss", str(seg["start_seconds"]), "-to", str(seg["end_seconds"]),
                     "-c:a", "flac", str(segment_path)],
                    capture_output=True, text=True, timeout=TIMEOUT_FFMPEG_CONVERT,
                )
                if cut_result.returncode != 0:
                    raise Exception(f"ffmpeg cut failed: {cut_result.stderr.strip()[:300]}")

                ok, reason, _ = _validate_audio_integrity(segment_path)
                if not ok:
                    segment_path.unlink(missing_ok=True)
                    raise Exception(f"Integrity check failed: {reason}")

                quality_label, _ = probe_audio_quality(segment_path, source_info=(source_codec, source_bitrate))
                apply_metadata_to_file(
                    segment_path,
                    artist,
                    title,
                    album=video_title,
                    track_number=index + 1,
                    track_total=total,
                    album_art_bytes=thumb_bytes,
                    album_art_mime=thumb_mime,
                    album_artist=album_artist,
                    source="youtube",
                    source_quality=quality_label,
                    source_codec=source_codec,
                    source_bitrate_kbps=source_bitrate,
                    compilation=is_compilation,
                )
                set_file_permissions(segment_path)

                with db_conn() as conn:
                    _touch_segment(conn, seg["id"], status="completed", final_path=str(segment_path))
                    conn.commit()
                completed += 1
            except Exception as e:
                with db_conn() as conn:
                    _touch_segment(conn, seg["id"], status="failed", error=str(e))
                    conn.commit()
                failed += 1

            with db_conn() as conn:
                _touch_split(conn, split_id, completed_segments=completed, failed_segments=failed)
                conn.commit()

        final_status = "completed" if failed == 0 else "completed_with_errors"
        with db_conn() as conn:
            _touch_split(conn, split_id, status=final_status)
            conn.execute("UPDATE longform_splits SET completed_at = datetime('now') WHERE id = ?", (split_id,))
            conn.commit()

        trigger_navidrome_scan(user_id=user_id)
        trigger_jellyfin_scan(user_id=user_id)
        trigger_plex_scan(user_id=user_id)

    except Exception as e:
        with db_conn() as conn:
            _touch_split(conn, split_id, status="failed", error=str(e))
            conn.execute("UPDATE longform_splits SET completed_at = datetime('now') WHERE id = ?", (split_id,))
            conn.commit()
    finally:
        _clear_staging_dir(staging)


def cancel_longform_split(split_id: str) -> bool:
    with db_conn() as conn:
        cursor = conn.execute(
            "UPDATE longform_splits SET cancel_requested = 1 WHERE id = ? AND status NOT IN ('completed', 'completed_with_errors', 'failed', 'cancelled')",
            (split_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
