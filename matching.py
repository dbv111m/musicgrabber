"""
MusicGrabber, song matching primitives.

Confidence-based fuzzy matching for picking the right candidate out of a
search result list. Designed to give Soulseek/slskd path-shaped filenames
the per-segment treatment they need; slskd results look like
``Various/[2003] Hits/Artist - Track.flac`` and you cannot just throw the
whole thing at a YouTube-style title scorer.

Returns 0.0-1.0 confidence with a useful breakdown for logs.

Heavily inspired by the SoulSync project's matching engine. The core ideas
adopted: SequenceMatcher fuzzy similarity, per-path-segment scoring, core
title fast path, version-aware penalty inside the score, junk artist gate.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable


_FEAT_PATTERNS = (
    re.compile(r'\s*\(feat\.?[^)]*\)', re.IGNORECASE),
    re.compile(r'\s*\(ft\.?[^)]*\)', re.IGNORECASE),
    re.compile(r'\s*\(featuring[^)]*\)', re.IGNORECASE),
    re.compile(r'\s+feat\.?\s.*$', re.IGNORECASE),
    re.compile(r'\s+ft\.?\s.*$', re.IGNORECASE),
    re.compile(r'\s+featuring\s.*$', re.IGNORECASE),
)

_TITLE_NOISE = (
    re.compile(r'\s*\(explicit\)', re.IGNORECASE),
    re.compile(r'\s*\(clean\)', re.IGNORECASE),
)

_ARTIST_FEAT = (
    re.compile(r'\s*\bfeat\.?.*$', re.IGNORECASE),
    re.compile(r'\s*\bft\.?\s.*$', re.IGNORECASE),
    re.compile(r'\s*\bfeaturing\b.*$', re.IGNORECASE),
)

# Words that mean a result is the wrong version of the song. SoulSync uses
# similar lists; tuned slightly for what slskd folks actually share.
_HEAVY_VERSION_KEYWORDS = (
    "remix", "rmx", "club mix", "vip mix", "extended mix",
    "live", "live at", "live from", "concert", "in concert",
    "acoustic", "unplugged", "stripped",
    "slowed", "reverb", "sped up", "speed up",
    "instrumental", "karaoke",
    "demo", "rough cut", "medley",
    "radio edit", "radio version", "single edit",
    "8d audio", "bass boosted", "nightcore",
)
_LIGHT_VERSION_KEYWORDS = ("remaster", "remastered")

_JUNK_ARTIST_TOKENS = frozenset({
    "various artists", "various artist", "va",
    "unknown artist", "unknown album", "unknown",
    "compilation",
})

# Path segments below this length are too generic to score against, "01" or
# "cd1" should not earn an artist-match bonus.
_MIN_SEGMENT_CHARS = 3


def _has_cjk(text: str) -> bool:
    """True if any character is in the CJK / Hiragana / Katakana / Hangul / Halfwidth ranges."""
    for c in text:
        cp = ord(c)
        if (0x2E80 <= cp <= 0x9FFF       # CJK radicals + unified ideographs
                or 0x3040 <= cp <= 0x30FF  # Hiragana + Katakana
                or 0xFF00 <= cp <= 0xFFEF  # Halfwidth/Fullwidth
                or 0xAC00 <= cp <= 0xD7AF):  # Hangul syllables
            return True
    return False


_LATIN_EXTENDED_MAP = str.maketrans({
    # NFKD doesn't decompose these because the diacritic is part of the
    # glyph itself, not a combining mark. Common in metal track names
    # ("ØUTSIDER", "Mötley Crüe", "Æquinox").
    'Ø': 'O', 'ø': 'o',
    'Æ': 'AE', 'æ': 'ae',
    'Œ': 'OE', 'œ': 'oe',
    'Þ': 'Th', 'þ': 'th',
    'Ð': 'D', 'ð': 'd',
    'ß': 'ss',
    'Ł': 'L', 'ł': 'l',
})


def _strip_diacritics(text: str) -> str:
    """NFKD fold accented Latin characters back to ASCII; leave CJK alone."""
    if not text:
        return ""
    if _has_cjk(text):
        return text
    text = text.translate(_LATIN_EXTENDED_MAP)
    decomposed = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in decomposed if not unicodedata.combining(c))


def normalise_string(text: str) -> str:
    """Lowercase, fold accents, collapse separators to spaces, drop punctuation.

    Keeps the dollar sign so A$AP Rocky still matches itself. CJK characters
    are preserved verbatim because slskd users routinely share Japanese and
    Korean tracks under their original kanji/hangul filenames.
    """
    if not text:
        return ""
    cjk = _has_cjk(text)
    text = _strip_diacritics(text).lower()
    text = re.sub(r'[._/&\-]+', ' ', text)
    if cjk:
        # Strip ASCII punctuation but keep non-ASCII letters (CJK, etc.)
        text = re.sub(r'[!-/:-@\[-`{-~]', '', text)
    else:
        text = re.sub(r'[^a-z0-9\s$]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def core_string(text: str) -> str:
    """Strip everything except letters and numbers, used for an exact-match
    shortcut where punctuation and spacing cannot be trusted."""
    return re.sub(r'[^a-z0-9]', '', normalise_string(text))


def clean_title(title: str) -> str:
    """Remove feat./ft. clauses and explicit/clean tags, then normalise.

    Important: do NOT strip remix/live/acoustic/version annotations here.
    Those are real differences between recordings and the version-aware
    similarity check below depends on them surviving.
    """
    if not title:
        return ""
    cleaned = title
    for pat in _FEAT_PATTERNS:
        cleaned = pat.sub('', cleaned)
    for pat in _TITLE_NOISE:
        cleaned = pat.sub('', cleaned)
    return normalise_string(cleaned)


def clean_artist(artist: str) -> str:
    """Strip feat. clauses from an artist name, leave & and "and" in place
    so "Daryl Hall & John Oates" survives."""
    if not artist:
        return ""
    cleaned = artist
    for pat in _ARTIST_FEAT:
        cleaned = pat.sub('', cleaned)
    return normalise_string(cleaned)


def is_junk_artist(text: str) -> bool:
    """True for Various Artists / VA / Unknown folder labels."""
    if not text:
        return False
    norm = normalise_string(text)
    if not norm:
        return False
    return norm in _JUNK_ARTIST_TOKENS


_TYPO_RATIO_FLOOR = 0.85


def similarity(a: str, b: str) -> float:
    """Fuzzy similarity 0.0-1.0 with a version-aware penalty.

    If one string is the prefix of the other and the trailing content names
    a different version (remix, live, acoustic), the score collapses so
    "Stay" never matches "Stay (Live in Tokyo)" above the confidence floor.

    Also guards against SequenceMatcher inflating short unrelated strings
    that happen to share characters; "silver" and "utsider" both contain
    s/i/e/r and would otherwise score 0.6 despite being different songs.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    base = SequenceMatcher(None, a, b).ratio()

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if longer.startswith(shorter):
        extra = longer[len(shorter):].strip(' -()[]')
        for kw in _HEAVY_VERSION_KEYWORDS:
            if kw in extra:
                # Heavy penalty: caps the score so a remix never beats the
                # original at the 0.55 threshold.
                return min(base, 0.30)
        for kw in _LIGHT_VERSION_KEYWORDS:
            if kw in extra:
                # Remaster is the same recording with different mastering.
                # Floor at 0.75 so the SequenceMatcher dip from the longer
                # string doesn't push it below threshold.
                return max(base, 0.75)

    # No-shared-token guard. SequenceMatcher rewards character overlap, but
    # for genuinely different words the only honest signal is high overlap
    # (typo territory, >= 0.85). Below that floor, with no shared tokens,
    # demote hard.
    a_tokens = {t for t in a.split() if t}
    b_tokens = {t for t in b.split() if t}
    if a_tokens and b_tokens and not (a_tokens & b_tokens) and base < _TYPO_RATIO_FLOOR:
        return min(base, 0.25)
    return base


def duration_similarity(a_secs: float | None, b_secs: float | None) -> float:
    """Closeness of two durations on 0.0-1.0. Neutral 0.5 when either is unknown."""
    if not a_secs or not b_secs or a_secs <= 0 or b_secs <= 0:
        return 0.5
    if abs(a_secs - b_secs) <= 5:
        return 1.0
    diff_ratio = abs(a_secs - b_secs) / max(a_secs, b_secs)
    return max(0.0, 1.0 - diff_ratio * 5)


def query_requests_variant(query: str) -> bool:
    """True if the user's own query asks for a remix/live/acoustic.

    Used so we don't penalise a remix result when the user explicitly typed
    'mamma mia remix'.
    """
    if not query:
        return False
    q = query.lower()
    return any(kw in q for kw in _HEAVY_VERSION_KEYWORDS)


def split_path_segments(filename: str) -> list[str]:
    """Split a Soulseek/slskd filename on path separators and the YouTube
    pipe delimiter, drop empty segments, return raw segments (caller normalises)."""
    if not filename:
        return []
    return [s for s in re.split(r'[\\/|]+', filename) if s.strip()]


def _segment_match(target_norm: str, segments_norm: Iterable[str]) -> float:
    """Best per-segment similarity for a normalised target.

    Word-boundary substring match scores 1.0; falls back to SequenceMatcher.
    """
    if not target_norm or len(target_norm) < 2:
        return 0.0
    target_re = re.compile(r'\b' + re.escape(target_norm) + r'\b')
    best = 0.0
    for seg in segments_norm:
        if not seg or len(seg) < _MIN_SEGMENT_CHARS:
            continue
        if target_re.search(seg):
            return 1.0
        ratio = SequenceMatcher(None, target_norm, seg).ratio()
        if ratio > best:
            best = ratio
    return best


def _basename_no_ext(filename: str) -> str:
    """Last path segment with the extension stripped, suitable for title scoring."""
    if not filename:
        return ""
    last = re.split(r'[\\/]+', filename)[-1]
    return re.sub(r'\.[^.]+$', '', last)


def _strip_track_number(name: str) -> str:
    """Strip leading "01 - ", "01. ", "01_" track number prefixes."""
    return re.sub(r'^\s*\d{1,3}[\s.\-_]+', '', name)


# Title weight kept at 0.55 so a strong title match plus a decent artist
# clears 0.55 even if duration is unknown:
#   0.85 * 0.55 + 0.7 * 0.30 + 0.5 * 0.10 + 0 * 0.05 = 0.7275
_W_TITLE = 0.55
_W_ARTIST = 0.30
_W_DURATION = 0.10
_W_ALBUM = 0.05

CORE_TITLE_FAST_PATH = 0.90
WRONG_VERSION_PENALTY = 0.4
TITLE_GATE_THRESHOLD = 0.4  # Title sim below this scales whole confidence down
ARTIST_GATE_THRESHOLD = 0.5  # Artist sim below this does the same: right song, wrong act

# A bracketed qualifier on an artist name marks a different act sharing it:
# Deezer's "Nirvana (UK)" is the 1960s British band, not the one from Aberdeen,
# Washington. Branding in brackets ("(Official)", "(VEVO)") is not a new act.
_ARTIST_QUALIFIER_RE = re.compile(r"^(.+?)\s*[(\[]([^)\]]+)[)\]]\s*$")
_ARTIST_BRANDING_RE = re.compile(r"\b(official|vevo|topic|feat|ft|featuring|band|music)\b", re.I)

# Named mixes and edits: "(Devonshire Mix)", "[Butch Vig Edit]", or Deezer's
# bracketless "Tum Hi Ho Bollywood Dance Mix". The heavy keyword list only knows
# the common ones ("club mix", "radio edit"). "Original Mix" and "Album Mix"
# are the release itself, so they pass.
_NAMED_MIX_RE = re.compile(r"\b(mix|edit|dub)\s*([)\]]|$)", re.I)
_CANONICAL_MIX_RE = re.compile(r"\b(original|album|main)\s+(mix|edit)\b", re.I)

# Two songs joined into one track: "One More Time / Aerodynamic".
_JOINED_TITLES_RE = re.compile(r"\S\s+/\s+\S")

# Where a title says who the song is by: a leading "Artist - " segment (or
# "Various - Artist - "), or "... by Artist".
_TITLE_SEPARATOR_RE = re.compile(r"\s+[-\u2013|:]\s+")


def score_track_against_filename(
    expected_artist: str,
    expected_title: str,
    filename: str,
    *,
    expected_album: str | None = None,
    candidate_duration_s: float | None = None,
    expected_duration_s: float | None = None,
    query: str | None = None,
) -> tuple[float, list[str]]:
    """Score a slskd-style path against expected artist/title/album.

    Returns (confidence 0.0-1.0, breakdown for logs).

    The filename should be the full slskd path (with backslashes or forward
    slashes); each path segment is scored independently for artist/album.
    """
    breakdown: list[str] = []

    if not filename:
        return 0.0, ["empty_filename"]

    raw_segments = split_path_segments(filename)
    segments_norm = [normalise_string(s) for s in raw_segments]
    segments_norm = [s for s in segments_norm if s]

    # Junk artist gate: reject "Various Artists" / "VA" / "Unknown" folders.
    # We check segments other than the basename itself, since a track called
    # "Unknown" is fine but a folder called "Various Artists" is not.
    for raw in raw_segments[:-1]:
        if is_junk_artist(raw):
            breakdown.append(f"junk_folder={raw!r}")
            return 0.0, breakdown

    # Title comes from the basename; track-number prefix and extension stripped.
    basename = _strip_track_number(_basename_no_ext(filename))
    candidate_title_norm = normalise_string(basename)
    candidate_title_clean = clean_title(basename)
    expected_title_clean = clean_title(expected_title or "")

    artist_norm = clean_artist(expected_artist or "")

    # ----- Artist score -----
    artist_score = 0.0
    if artist_norm:
        artist_score = _segment_match(artist_norm, segments_norm)
        # Penalise pure-similarity matches that didn't hit a word boundary,
        # since "muse" ≈ "museum" by ratio but is plainly wrong.
        if 0 < artist_score < 1.0:
            artist_score *= 0.7
    breakdown.append(f"artist={artist_score:.2f}")

    # ----- Title fast path: core string exact match -----
    expected_title_core = core_string(expected_title or "")
    candidate_title_core = core_string(basename)
    if (expected_title_core and candidate_title_core
            and expected_title_core == candidate_title_core
            and artist_score >= 0.75):
        confidence = CORE_TITLE_FAST_PATH + (artist_score * (1.0 - CORE_TITLE_FAST_PATH))
        breakdown.append(f"core_title_match→{confidence:.3f}")
        return confidence, breakdown

    # ----- Title score: SequenceMatcher with version-aware penalty -----
    title_score = similarity(expected_title_clean, candidate_title_clean)
    breakdown.append(f"title={title_score:.2f}")

    # ----- Duration score -----
    duration_score = duration_similarity(expected_duration_s, candidate_duration_s)
    breakdown.append(f"duration={duration_score:.2f}")

    # ----- Album score (optional) -----
    album_score = 0.0
    if expected_album:
        album_norm = normalise_string(expected_album)
        if album_norm:
            album_score = _segment_match(album_norm, segments_norm)
            breakdown.append(f"album={album_score:.2f}")

    confidence = (
        title_score * _W_TITLE
        + artist_score * _W_ARTIST
        + duration_score * _W_DURATION
        + album_score * _W_ALBUM
    )

    # Title hard gate. A great artist match should not prop up a candidate
    # for a different song; below the gate we scale confidence proportionally
    # to the title evidence so wrong-track results sink past every right-track
    # one regardless of how shiny the album folder looks.
    if title_score < TITLE_GATE_THRESHOLD:
        confidence *= (title_score / TITLE_GATE_THRESHOLD) if TITLE_GATE_THRESHOLD else 0.0
        breakdown.append(f"title_gate×{title_score / TITLE_GATE_THRESHOLD:.2f}")

    # Wrong-version demotion: if the user did NOT ask for a variant but the
    # candidate filename clearly is one, knock it down. Demotes rather than
    # rejects so we still return a result if everything else is even worse.
    if not query_requests_variant(query or expected_title or ""):
        candidate_lower = candidate_title_norm
        if any(kw in candidate_lower for kw in _HEAVY_VERSION_KEYWORDS):
            confidence *= WRONG_VERSION_PENALTY
            breakdown.append(f"wrong_version×{WRONG_VERSION_PENALTY}")

    breakdown.append(f"final={confidence:.3f}")
    return confidence, breakdown


def _title_credits_artist(candidate_title: str, expected_artist_norm: str) -> bool:
    """Does the title say the song is by this artist, rather than just name them?"""
    if not expected_artist_norm:
        return False
    segments = _TITLE_SEPARATOR_RE.split(candidate_title)
    for segment in segments[:-1]:
        if expected_artist_norm in clean_artist(segment):
            return True
    by = re.search(r"\bby\s+(.+)$", candidate_title, re.I)
    return bool(by and clean_artist(by.group(1)).startswith(expected_artist_norm))


def compute_match_confidence(
    expected_artist: str | None,
    expected_title: str | None,
    candidate_title: str,
    candidate_artist: str | None = None,
    *,
    candidate_duration_s: float | None = None,
    expected_duration_s: float | None = None,
    query: str | None = None,
) -> tuple[float, list[str]]:
    """Match confidence for flat-title sources (YouTube, Monochrome, etc).

    Unlike score_track_against_filename this does not split paths; the
    candidate_title is treated as a single string and candidate_artist as
    the channel/uploader. SequenceMatcher does the fuzzy heavy lifting and
    similarity() applies the version-aware penalty so a remix candidate
    cannot win against a plain-title query.

    Returns (confidence 0.0-1.0, breakdown for logs).
    """
    breakdown: list[str] = []

    if not (expected_title or expected_artist):
        # Nothing to match against; punt with a neutral confidence.
        return 0.5, ["no_query_info"]

    if candidate_artist and is_junk_artist(candidate_artist):
        breakdown.append("junk_artist")
        return 0.0, breakdown

    expected_title_clean = clean_title(expected_title or "")
    candidate_title_clean = clean_title(candidate_title or "")
    expected_artist_norm = clean_artist(expected_artist or "")

    # YouTube titles routinely look like "Artist - Title (Official Video)";
    # strip the leading artist clause when present so the title comparison
    # actually compares titles.
    candidate_for_title_compare = candidate_title_clean
    if expected_artist_norm and candidate_title_clean:
        prefix = f"{expected_artist_norm} "
        if candidate_title_clean.startswith(prefix):
            stripped = candidate_title_clean[len(prefix):].strip()
            if stripped:
                candidate_for_title_compare = stripped

    # Core title fast path: alphanumeric exact match.
    expected_core = core_string(expected_title or "")
    candidate_core = core_string(candidate_for_title_compare)
    full_candidate_core = core_string(candidate_title_clean)

    if expected_core and expected_core == candidate_core:
        title_score = 1.0
    elif expected_core and expected_core == full_candidate_core:
        title_score = 1.0
    elif expected_core and len(expected_core) >= 4 and expected_core in full_candidate_core:
        # Expected title appears verbatim inside a longer candidate title
        # (typical "Artist - Title (Official Audio)" shape).
        title_score = 0.92
    else:
        title_score = max(
            similarity(expected_title_clean, candidate_for_title_compare),
            similarity(expected_title_clean, candidate_title_clean),
        )
    breakdown.append(f"title={title_score:.2f}")

    # Artist scoring; "X - Topic" style auto-channels get the suffix stripped
    # before comparison since the suffix is YouTube branding, not part of
    # the artist name.
    if expected_artist_norm:
        candidate_artist_norm = clean_artist(candidate_artist or "")
        # Strip YouTube-specific channel suffixes ("Topic", "VEVO") that
        # are branding rather than part of the artist name.
        if candidate_artist_norm.endswith(" topic"):
            candidate_artist_norm = candidate_artist_norm[:-len(" topic")].strip()
        if candidate_artist_norm.endswith("vevo"):
            candidate_artist_norm = candidate_artist_norm[:-4].strip()

        # Squashed comparisons handle "BritneySpearsVEVO" vs "Britney Spears"
        # where normalisation removes the space-camelCase boundary.
        expected_squashed = expected_artist_norm.replace(" ", "")
        candidate_squashed = candidate_artist_norm.replace(" ", "")

        qualified = _ARTIST_QUALIFIER_RE.match((candidate_artist or "").strip())

        if not candidate_artist_norm:
            artist_score = 0.0
        elif candidate_artist_norm == expected_artist_norm:
            artist_score = 1.0
        elif expected_squashed and expected_squashed == candidate_squashed:
            artist_score = 1.0
        elif (qualified
                and clean_artist(qualified.group(1)) == expected_artist_norm
                and not _ARTIST_BRANDING_RE.search(qualified.group(2))):
            # Same name, different act. Treated as no match at all, because
            # "nearly the right band" is still the wrong band.
            artist_score = 0.0
            breakdown.append("artist_namesake")
        elif (expected_artist_norm in candidate_artist_norm
                or candidate_artist_norm in expected_artist_norm
                or expected_squashed in candidate_squashed
                or candidate_squashed in expected_squashed):
            artist_score = 0.95
        else:
            sim = similarity(expected_artist_norm, candidate_artist_norm)
            # Artist credited in the candidate title also counts; YouTube
            # uploaders often park the artist in the title rather than the
            # channel ("Various - Artist - Title"). Credited, not merely
            # mentioned: "Sample a Song (Parody of Daft Punk One More Time)"
            # names Daft Punk without being by them.
            if _title_credits_artist(candidate_title or "", expected_artist_norm):
                artist_score = max(sim, 0.95)
            elif " - " in (candidate_title or ""):
                # "Artist - Title" posted by somebody else: compare the
                # artist half of the title too, so a typo there ("Kraftwork")
                # still gets credit the uploader's name never could.
                title_artist = clean_artist(candidate_title.split(" - ", 1)[0])
                artist_score = max(sim, similarity(expected_artist_norm, title_artist))
            else:
                artist_score = sim
        breakdown.append(f"artist={artist_score:.2f}")
    else:
        artist_score = 0.5

    duration_score = duration_similarity(expected_duration_s, candidate_duration_s)

    confidence = (
        title_score * 0.55
        + artist_score * 0.30
        + duration_score * 0.15
    )

    # Title hard gate: a strong artist or duration match should not save a
    # candidate that is clearly a different song. Wrong title is the loudest
    # possible "this is not the song you wanted" signal.
    if title_score < TITLE_GATE_THRESHOLD:
        confidence *= (title_score / TITLE_GATE_THRESHOLD) if TITLE_GATE_THRESHOLD else 0.0
        breakdown.append(f"title_gate×{title_score / TITLE_GATE_THRESHOLD:.2f}")

    # Artist hard gate, the same idea: the right title by the wrong artist is a
    # cover or a namesake, and a lossless cover is still not the song. Only
    # when we know who the candidate is; an unknown artist is not a wrong one.
    candidate_artist_known = bool(expected_artist_norm and clean_artist(candidate_artist or ""))
    if candidate_artist_known and artist_score < ARTIST_GATE_THRESHOLD:
        confidence *= artist_score / ARTIST_GATE_THRESHOLD
        breakdown.append(f"artist_gate×{artist_score / ARTIST_GATE_THRESHOLD:.2f}")

    # Wrong-version demotion: only when query is plain and similarity()
    # didn't already catch the version suffix structurally.
    plain_query = not query_requests_variant(query or expected_title or "")
    if plain_query and title_score >= 0.6:
        # similarity() already scored remix/live cases low; this branch
        # catches cases where the version word sits in a position
        # similarity() did not detect (e.g. infix, or the candidate title
        # starts with the artist).
        heavy = any(kw in candidate_title_clean for kw in _HEAVY_VERSION_KEYWORDS)
        named_mix = bool(_NAMED_MIX_RE.search(candidate_title or "")
                         and not _CANONICAL_MIX_RE.search(candidate_title or ""))
        joined = bool(_JOINED_TITLES_RE.search(candidate_title or ""))
        if heavy or named_mix or joined:
            if (not any(kw in expected_title_clean for kw in _HEAVY_VERSION_KEYWORDS)
                    and not _NAMED_MIX_RE.search(expected_title or "")
                    and not _JOINED_TITLES_RE.search(expected_title or "")):
                confidence *= WRONG_VERSION_PENALTY
                breakdown.append(f"wrong_version×{WRONG_VERSION_PENALTY}")

    breakdown.append(f"final={confidence:.3f}")
    return confidence, breakdown


def parse_query(query: str) -> tuple[str, str]:
    """Best-effort split of "Artist - Title" queries.

    Returns (artist, title); falls back to ("", query) when no separator.
    """
    if not query:
        return "", ""
    for sep in (" - ", " – ", " | "):
        if sep in query:
            artist, title = query.split(sep, 1)
            return artist.strip(), title.strip()
    return "", query.strip()
