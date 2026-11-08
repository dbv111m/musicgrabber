"""
MusicGrabber, artist verification for automatic downloads.

Album imports, bulk "Artist - Title" imports and watched playlists know who the
track is by before a single byte arrives. The search leg already ranks results
by how well they match that identity, but ranking is a preference, not a
promise: when every candidate is wrong the best wrong one still wins, and until
now it was filed into the album folder with ARTIST set to whatever the uploader
or the video title happened to say.

This module is the post-download verdict. It is deliberately ordered by how
much each kind of evidence can be trusted, because a live check against the
real junk uploads showed that yt-dlp's ``artist``/``track`` fields are empty
for nearly everything except YouTube Music's auto-generated " - Topic" uploads,
and that uploader names and "Artist - Title" title strings are free text:

1. An AcoustID fingerprint resolved to a MusicBrainz recording identifies the
   audio itself. Its artist credit is the primary verdict: agree and the file
   is accepted, disagree and it is rejected, whatever the uploader claims.
2. Agreement with the MusicBrainz/Deezer expected duration (within 8 s and
   5%) is the second hard gate. A different artist's version of the same song
   is rarely the same length to within a few seconds.
3. Provider metadata (yt-dlp ``artist``, a Tidal/ISRC credit) is strong
   evidence when present, which in practice means Topic uploads.
4. Uploader and title-prefix agreement are weak: enough to prefer one
   candidate over another, never enough to accept on their own in strict
   mode. Nothing verifiable at all means the file is held for the user to
   force-accept rather than written into the album.

Pure functions, no database and no network, so the whole decision table is
unit-testable. ``downloads.py`` owns the consequences (trash, blacklist,
mismatch log, next candidate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from constants import (
    STRICT_ARTIST_MATCH_THRESHOLD,
    STRICT_DURATION_TOLERANCE_FRAC,
    STRICT_DURATION_TOLERANCE_SECS,
)
from matching import clean_artist, is_junk_artist, similarity
from metadata import parse_reject_keywords, title_has_version_annotation

# Applied in strict mode when the user has not written their own list. These
# are the words that, inside a version annotation, mean "not the recording on
# the album": covers and karaoke are other people, live/acoustic/remix are other
# takes, and the last few are the YouTube-native mutations of a studio track.
DEFAULT_REJECT_VERSION_KEYWORDS = (
    "cover", "karaoke", "tribute", "live", "acoustic", "unplugged", "remix",
    "nightcore", "slowed", "sped up", "8d", "instrumental", "ai cover", "suno",
)

# Channel-name decoration that is YouTube branding rather than part of the
# artist's name. Stripped from the end, repeatedly, so "X Official - Topic"
# collapses to "X".
_CHANNEL_BRANDING_RE = re.compile(
    r"\s*[-–—:|]?\s*\b(topic|vevo|official|music|records|tv|hq|channel|videos?|audio)\b\s*$",
    re.IGNORECASE,
)
_TOPIC_CHANNEL_RE = re.compile(r"\s[-–—]\s*topic\s*$", re.IGNORECASE)
_OFFICIAL_CHANNEL_RE = re.compile(r"(vevo|official)\b", re.IGNORECASE)

# "Nirvana (UK)" is a different act sharing a name; "Nirvana (Official)" is not.
_ARTIST_QUALIFIER_RE = re.compile(r"^(.+?)\s*[(\[]([^)\]]+)[)\]]\s*$")
_ARTIST_BRANDING_WORDS_RE = re.compile(
    r"\b(official|vevo|topic|feat|ft|featuring|band|music|lyrics|audio|video|hd|hq|visuali[sz]er)\b",
    re.IGNORECASE,
)

# Where several acts share one credit: "A & B", "A, B", "A feat. B", "A x B".
_CREDIT_SPLIT_RE = re.compile(
    r"\s*(?:,|;|/|&|\+|\bx\b|\bvs\.?\b|\band\b|\bwith\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*",
    re.IGNORECASE,
)

# Trailing tokens a longer credit may add to a shorter one and still be the
# same act: "Charlie Daniels" is "The Charlie Daniels Band".
_ENSEMBLE_TOKENS = frozenset({"band", "the", "orchestra", "group", "trio", "quartet", "ensemble"})

# A video title's segments: "Artist - Title (Official Audio)", "Artist | Title".
_TITLE_SEGMENT_RE = re.compile(r"\s+--\s+|\s+[-–—|:]\s+")
# A "[Label] " or "(Channel) " prefix some uploaders put before the artist.
_TITLE_LABEL_PREFIX_RE = re.compile(r"^\s*[(\[][^)\]]+[)\]]\s*")
# "02. Title", "2 - Title": a track number is not an artist.
_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[.)\-]?\s*$")
_BY_ARTIST_RE = re.compile(r"\bby\s+(.+)$", re.IGNORECASE)
# Bracketed tails of a title segment: "(Official Audio)", "[HD]", "(Lyrics)".
_TRAILING_ANNOTATION_RE = re.compile(r"(\s*[(\[][^)\]]*[)\]])+\s*$")


def strip_channel_branding(uploader: str | None) -> str:
    """Drop " - Topic", "VEVO", "Official" and friends from a channel name."""
    name = (uploader or "").strip()
    if not name:
        return ""
    # Squashed branding ("ShinedownVEVO") has no word boundary to find.
    name = re.sub(r"(?<=\w)(vevo|official)$", "", name, flags=re.IGNORECASE)
    # Bracketed branding ("Nirvana (Official)") is not a namesake qualifier.
    qualified = _ARTIST_QUALIFIER_RE.match(name)
    if qualified and all(_ARTIST_BRANDING_WORDS_RE.fullmatch(w) for w in qualified.group(2).split()):
        name = qualified.group(1).strip()
    while True:
        stripped = _CHANNEL_BRANDING_RE.sub("", name).strip()
        if stripped == name or not stripped:
            # Never strip a name down to nothing: a channel called "Music" or
            # "Live" is still the only name we have.
            return name if not stripped else stripped
        name = stripped


def is_topic_channel(uploader: str | None) -> bool:
    """True for YouTube Music's auto-generated "<Artist> - Topic" channels."""
    return bool(_TOPIC_CHANNEL_RE.search(uploader or ""))


def is_official_channel(uploader: str | None) -> bool:
    """True for VEVO and self-described official channels (not Topic)."""
    return bool(_OFFICIAL_CHANNEL_RE.search(uploader or ""))


def normalise_artist(name: str | None) -> str:
    """Casefold, fold diacritics, drop punctuation, "feat." clauses, "&"/"and"
    and a leading "The", so spelling variants of one act compare equal."""
    text = clean_artist(name or "")
    if not text:
        return ""
    text = re.sub(r"\band\b", " ", text)
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_artist_credits(name: str | None) -> list[str]:
    """Normalised individual acts in a credit string, junk labels removed."""
    raw = (name or "").strip()
    if not raw:
        return []
    credits = []
    for part in _CREDIT_SPLIT_RE.split(raw):
        norm = normalise_artist(part)
        if norm and not is_junk_artist(part) and norm not in credits:
            credits.append(norm)
    return credits


def _ensemble_variant(shorter: str, longer: str) -> bool:
    """True when `longer` is `shorter` plus only ensemble words or a second name part."""
    short_tokens = shorter.split()
    long_tokens = longer.split()
    if not short_tokens or not re.search(rf"\b{re.escape(shorter)}\b", longer):
        return False
    extra = [t for t in long_tokens if t not in short_tokens]
    if not extra:
        return True
    if all(t in _ENSEMBLE_TOKENS for t in extra):
        return True
    # "Charlie Daniels" inside "The Charlie Daniels Band" is fine; a one-word
    # name inside a longer one ("Genesis" in "Genesis Owusu") is a different act.
    return len(short_tokens) >= 2


def artist_match_score(expected: str | None, candidate: str | None) -> float:
    """How confidently `candidate` names the same act as `expected`, 0.0-1.0.

    1.0 is the same name (branding, "The", "&"/"and", diacritics and
    punctuation ignored); 0.9 is a collaboration where one side lists extra
    guests; 0.85 is an ensemble variant or shared primary credit; anything that
    only resembles the name fuzzily scores low unless it is a plain typo.
    """
    exp = normalise_artist(expected)
    cand_raw = strip_channel_branding(candidate)
    cand = normalise_artist(cand_raw)
    if not exp or not cand:
        return 0.0
    if is_junk_artist(cand_raw):
        return 0.0
    if cand == exp or cand.replace(" ", "") == exp.replace(" ", ""):
        return 1.0

    qualified = _ARTIST_QUALIFIER_RE.match((candidate or "").strip())
    if (qualified
            and normalise_artist(qualified.group(1)) == exp
            and not _ARTIST_BRANDING_WORDS_RE.search(qualified.group(2))):
        # Same name, different act: "nearly the right band" is the wrong band.
        return 0.0

    exp_credits = set(split_artist_credits(expected))
    cand_credits = set(split_artist_credits(cand_raw))
    if exp_credits and cand_credits:
        if exp_credits <= cand_credits or cand_credits <= exp_credits:
            return 0.9
        if exp_credits & cand_credits:
            return 0.85

    if _ensemble_variant(exp, cand) or _ensemble_variant(cand, exp):
        return 0.85

    # "Hall & Oates" for "Daryl Hall & John Oates": every word of the shorter
    # name is in the longer one. Two words at least, or "Genesis" would vouch
    # for "Genesis Owusu".
    exp_tokens, cand_tokens = set(exp.split()), set(cand.split())
    shorter, longer = (exp_tokens, cand_tokens) if len(exp_tokens) <= len(cand_tokens) else (cand_tokens, exp_tokens)
    if len(shorter) >= 2 and shorter <= longer:
        return 0.85

    if _is_typo_of(exp, cand):
        return 0.85
    # Resemblance is not identity: "baby sound lab" is not a misspelling of
    # "dababy", and "the unrighteous brothers" is a parody, not a typo.
    return min(similarity(exp, cand), 0.5)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance; names are short, so the plain O(n*m) version will do."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def _is_typo_of(a: str, b: str) -> bool:
    """Same number of words, each pair identical or one keystroke apart (two for
    long words), sharing a first letter. "kraftwork" is Kraftwerk;
    "unrighteous" is not "righteous", whatever SequenceMatcher thinks."""
    a_tokens, b_tokens = a.split(), b.split()
    if not a_tokens or len(a_tokens) != len(b_tokens):
        return False
    for x, y in zip(a_tokens, b_tokens, strict=True):
        if x == y:
            continue
        if len(x) < 4 or len(y) < 4 or x[0] != y[0]:
            return False
        if _edit_distance(x, y) > (2 if min(len(x), len(y)) >= 8 else 1):
            return False
    return True


def title_artist_mention_score(expected: str | None, video_title: str | None) -> float:
    """Best artist score among the places a video title names its performer.

    Looks at every separator-delimited segment except one that is merely the
    song title, plus a trailing "by Artist". A segment that is a track number
    or the title in quotes is not a performer and scores nothing.
    """
    title = (video_title or "").strip()
    if not title or not expected:
        return 0.0
    stripped = _TITLE_LABEL_PREFIX_RE.sub("", title)
    best = 0.0
    for segment in _TITLE_SEGMENT_RE.split(stripped or title):
        # "(Official Audio)", "[HD]", "(Lyrics)": annotation, not a namesake.
        segment = _TRAILING_ANNOTATION_RE.sub("", segment).strip().strip("\"'“”‘’")
        if not segment or _TRACK_NUMBER_RE.match(segment):
            continue
        best = max(best, artist_match_score(expected, segment))
    by = _BY_ARTIST_RE.search(title)
    if by:
        best = max(best, artist_match_score(expected, by.group(1)))
    return best


def duration_within_tolerance(actual_secs: float | None, expected_secs: float | None) -> tuple[bool | None, float]:
    """(agrees, allowed_secs). `agrees` is None when either side is unknown."""
    try:
        actual = float(actual_secs or 0)
        expected = float(expected_secs or 0)
    except (TypeError, ValueError):
        return None, 0.0
    if actual <= 0 or expected <= 0:
        return None, 0.0
    # Within 8 s and within 5% of the expected length, whichever is tighter; a
    # two-second floor absorbs MusicBrainz lengths rounded to the second.
    allowed = max(2.0, min(STRICT_DURATION_TOLERANCE_SECS, expected * STRICT_DURATION_TOLERANCE_FRAC))
    return abs(actual - expected) <= allowed, allowed


def effective_reject_keywords(configured: str | None, *, strict: bool) -> list[str]:
    """The user's reject list, or the strict-mode default when they wrote none."""
    keywords = parse_reject_keywords(configured or "")
    if keywords or not strict:
        return keywords
    return list(DEFAULT_REJECT_VERSION_KEYWORDS)


def unwanted_version_keyword(candidate_title: str | None, requested_title: str | None,
                             keywords: list[str]) -> str | None:
    """The keyword a candidate's version annotation trips, or None.

    Whole words only, and only inside an annotation such as "(Live)" or
    "- Acoustic", so "Demolition Man" survives "demo". Any keyword that the
    requested title itself contains is dropped first: asking for the acoustic
    version gets the acoustic version.
    """
    if not keywords:
        return None
    wanted = (requested_title or "").lower()
    applicable = [
        kw for kw in keywords
        if not re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", wanted)
    ]
    if not applicable:
        return None
    return title_has_version_annotation(candidate_title or "", applicable)


def channel_trust(expected_artist: str | None, uploader: str | None) -> int:
    """Ranking preference for a channel that is the artist's own: 2 for a
    matching "<Artist> - Topic" upload, 1 for a matching VEVO/official channel,
    0 otherwise."""
    if not expected_artist or not uploader:
        return 0
    if artist_match_score(expected_artist, uploader) < STRICT_ARTIST_MATCH_THRESHOLD:
        return 0
    if is_topic_channel(uploader):
        return 2
    if is_official_channel(uploader):
        return 1
    return 0


def duration_string_to_seconds(value) -> float | None:
    """"3:57" / "1:02:03" / 237 -> seconds, or None when unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    parts = str(value).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        secs = nums[0]
    elif len(nums) == 2:
        secs = nums[0] * 60 + nums[1]
    elif len(nums) == 3:
        secs = nums[0] * 3600 + nums[1] * 60 + nums[2]
    else:
        return None
    return float(secs) if secs > 0 else None


@dataclass
class ArtistVerdict:
    """The outcome of `evaluate_artist_match`.

    decision is "accept" (keep the file), "reject" (a confident contrary
    identification: bin it, blacklist the result, try the next candidate) or
    "hold" (nothing contradicted the request but nothing verified it either:
    bin it, log a mismatch the user can force-accept, try the next candidate).
    """
    decision: str
    reason: str
    evidence: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.decision == "accept"

    @property
    def rejected(self) -> bool:
        return self.decision == "reject"


def evaluate_artist_match(
    expected_artist: str | None,
    *,
    recording_artist: str | None = None,
    provider_artist: str | None = None,
    uploader: str | None = None,
    video_title: str | None = None,
    actual_duration_secs: float | None = None,
    expected_duration_secs: float | None = None,
    strict: bool = True,
) -> ArtistVerdict:
    """Decide whether a downloaded file is by the artist that was asked for.

    recording_artist is the artist credit of a confident AcoustID/ISRC
    identification of the audio (None when there was none). provider_artist is
    structured metadata from the source (yt-dlp's ``artist`` field, present on
    Topic uploads). uploader and video_title are free text. Durations are in
    seconds; expected comes from MusicBrainz/Deezer for the requested recording.

    strict=False keeps the fingerprint verdict (a confident contrary
    identification is still wrong) but otherwise accepts, as the application
    always did.
    """
    evidence: list[str] = []
    threshold = STRICT_ARTIST_MATCH_THRESHOLD
    if not normalise_artist(expected_artist):
        return ArtistVerdict("accept", "no expected artist to verify against", evidence)

    # 1. The audio itself has been identified. Identity beats every string.
    if recording_artist and not is_junk_artist(recording_artist):
        score = artist_match_score(expected_artist, recording_artist)
        evidence.append(f"fingerprint_artist={recording_artist!r} score={score:.2f}")
        if score >= threshold:
            return ArtistVerdict("accept", f"fingerprint identifies '{recording_artist}'", evidence)
        return ArtistVerdict(
            "reject",
            f"fingerprint identifies '{recording_artist}', not '{expected_artist}'",
            evidence,
        )

    # 2. Length against the recording we asked for.
    duration_verified = False
    agrees, allowed = duration_within_tolerance(actual_duration_secs, expected_duration_secs)
    if agrees is not None:
        evidence.append(
            f"duration={float(actual_duration_secs):.0f}s expected={float(expected_duration_secs):.0f}s "
            f"tolerance=±{allowed:.0f}s"
        )
        if agrees:
            duration_verified = True
        elif strict:
            off_by = abs(float(actual_duration_secs) - float(expected_duration_secs))
            return ArtistVerdict(
                "reject",
                f"duration {float(actual_duration_secs):.0f}s is {off_by:.0f}s off the expected "
                f"{float(expected_duration_secs):.0f}s",
                evidence,
            )

    # 3. Structured metadata from the provider. Rare, but trustworthy when present.
    if provider_artist and not is_junk_artist(provider_artist):
        score = artist_match_score(expected_artist, provider_artist)
        evidence.append(f"provider_artist={provider_artist!r} score={score:.2f}")
        if score >= threshold:
            return ArtistVerdict("accept", f"source metadata credits '{provider_artist}'", evidence)
        if strict:
            return ArtistVerdict(
                "reject",
                f"source metadata credits '{provider_artist}', not '{expected_artist}'",
                evidence,
            )

    # 4. Free text: who uploaded it and what the title says.
    uploader_score = artist_match_score(expected_artist, uploader) if uploader else 0.0
    title_score = title_artist_mention_score(expected_artist, video_title)
    if uploader:
        evidence.append(f"uploader={strip_channel_branding(uploader)!r} score={uploader_score:.2f}")
    if video_title:
        evidence.append(f"title_mention score={title_score:.2f}")
    weak_agrees = max(uploader_score, title_score) >= threshold

    if duration_verified and weak_agrees:
        return ArtistVerdict("accept", "duration matches and the uploader or title names the artist", evidence)
    if not strict:
        return ArtistVerdict("accept", "strict artist matching is off", evidence)
    if duration_verified:
        return ArtistVerdict(
            "hold",
            f"duration matches but neither uploader nor title names '{expected_artist}'",
            evidence,
        )
    if weak_agrees:
        return ArtistVerdict(
            "hold",
            f"only the uploader or title claims this is '{expected_artist}'; no fingerprint or duration to confirm it",
            evidence,
        )
    return ArtistVerdict(
        "hold",
        f"nothing confirms this is '{expected_artist}'",
        evidence,
    )
