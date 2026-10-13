#!/usr/bin/env python3
"""Score MusicBrainz release-selection strategies against the cached corpus.

Runs entirely offline against tests/fixtures/mb_corpus_cache.json, so you can
iterate on scoring as often as you like without MusicBrainz getting cross.

    tests/.venv/bin/python tests/tools/eval_mb_strategies.py
    tests/.venv/bin/python tests/tools/eval_mb_strategies.py --detail widen/improved

A strategy is a (pool, scorer) pair:
  pool   - which candidate releases we even get to look at
  scorer - how we rank them

Keeping those separate matters, because it tells us whether a win came from
looking at more candidates or from judging them better. Those need different
fixes and it would be easy to credit the wrong one.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
CORPUS = FIXTURES / "mb_corpus.json"
CACHE = FIXTURES / "mb_corpus_cache.json"

from metadata import _score_release_group  # noqa: E402


# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------

def _as_rg(rel: dict) -> dict:
    """Shape a release into what _score_release_group expects, as production does."""
    rg = dict(rel.get("release-group") or {})
    if not rg.get("artist-credit"):
        rg["artist-credit"] = rel.get("artist-credit") or []
    if rel.get("date") and not rg.get("first-release-date"):
        rg["_date"] = rel["date"]
    return rg


def score_baseline(rel: dict, artist: str) -> int:
    """Exactly what ships today."""
    return _score_release_group(_as_rg(rel), artist)


# A live bootleg is very often titled with the gig date, e.g.
# "2001-08-01: Hutchinson Field, Chicago, IL, USA". Nothing else looks like that.
_GIG_TITLE_RE = re.compile(r"^\s*\d{4}[-‐-―]\d{2}[-‐-―]\d{2}\s*[::]")


def score_improved(rel: dict, artist: str) -> int:
    """Baseline plus the signals the current scorer ignores entirely.

    `status` is the big one. MusicBrainz explicitly marks bootlegs and promos as
    such (12.5% and 4.2% of our cached releases respectively) and we have never
    once looked, which is how a Chicago crowd recording ends up outranking
    OK Computer.
    """
    score = _score_release_group(_as_rg(rel), artist)

    status = (rel.get("status") or "").lower()
    if status == "bootleg":
        score -= 25
    elif status in ("promotion", "pseudo-release", "withdrawn"):
        score -= 12

    title = (rel.get("title") or "").lower()
    if _GIG_TITLE_RE.match(rel.get("title") or ""):
        score -= 15
    # "Live at ...", "Live in ...", "(Live)" and friends. The release-group
    # secondary type usually catches these, but not every live album is tagged.
    if re.search(r"\blive\b", title):
        score -= 10

    return score


SCORERS = {"baseline": score_baseline, "improved": score_improved}


# --------------------------------------------------------------------------
# Recording-level selection
# --------------------------------------------------------------------------

# Version markers that mean "this is not the canonical studio take".
_ALT_TAKE_RE = re.compile(
    r"\b(live|remix|demo|instrumental|acoustic|karaoke|edit|reprise|"
    r"radio version|alternate|rehearsal|session)\b", re.I)


def score_recording(rec: dict, artist: str, scorer) -> float:
    """How likely is this the canonical studio recording of the song?

    The strongest signal turns out to be sheer release count. The studio take
    ends up on the album, the single, the greatest hits and 200 compilations,
    while a given live version appears on exactly one bootleg. Counting where a
    recording turned up is a decent proxy for "this is the one people mean".
    """
    rels = rec.get("releases") or []
    if not rels:
        return -9999.0

    best_release = max(scorer(rel, artist) for rel in rels)

    # Diminishing returns, so a recording on 200 comps doesn't beat one on 30.
    import math
    spread = math.log1p(len(rels)) * 3.0

    penalty = 0.0
    if _ALT_TAKE_RE.search(rec.get("title") or ""):
        penalty -= 12.0

    return best_release + spread + penalty


# --------------------------------------------------------------------------
# Pools
# --------------------------------------------------------------------------

MB_SCORE_FLOOR = 85  # production refuses anything shakier than this


def _recs(entry: dict, variant: str) -> list[dict]:
    return (entry.get(variant) or {}).get("recordings") or []


def _releases_of(recs: list[dict]) -> list[dict]:
    return [rel for rec in recs for rel in (rec.get("releases") or [])]


def _eligible(entry: dict, *variants: str) -> list[dict]:
    """Recordings from the named query variants, merged by recording id.

    The same recording turns up in several query variants carrying *different*
    release lists, because each query only returns the releases matching it. So
    we merge the release lists rather than keeping whichever copy arrived first;
    dropping the rest loses the studio album entirely, which cost me an hour and
    a perfectly good theory.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for variant in variants:
        for rec in _recs(entry, variant):
            if rec.get("score", 0) < MB_SCORE_FLOOR:
                continue
            rid = rec.get("id")
            existing = by_id.get(rid)
            if existing is None:
                merged = dict(rec)
                merged["releases"] = list(rec.get("releases") or [])
                by_id[rid] = merged
                order.append(rid)
                continue
            known = {r.get("id") for r in existing["releases"]}
            for rel in rec.get("releases") or []:
                if rel.get("id") not in known:
                    existing["releases"].append(rel)
                    known.add(rel.get("id"))
    return [by_id[rid] for rid in order]


def pool_today(entry: dict) -> list[dict]:
    """Production: limit=1, so only the first recording exists at all."""
    recs = _recs(entry, "plain")
    if not recs or recs[0].get("score", 0) < MB_SCORE_FLOOR:
        return []
    return recs[:1]


def pool_plain(entry: dict) -> list[dict]:
    return _eligible(entry, "plain")


def pool_filtered(entry: dict) -> list[dict]:
    return _eligible(entry, "filtered")


def pool_union(entry: dict) -> list[dict]:
    return _eligible(entry, "plain", "filtered")


def pool_union_alias(entry: dict) -> list[dict]:
    """Union, falling back to the alias queries when the plain ones find nothing.

    This is the non-English case: a romanised title matches no recording title
    but does match an alias, so without this the track gets no metadata at all.
    We only fall back, rather than always merging, because alias matching is
    looser and we would rather not invite it in when we already have a hit.
    """
    out = pool_union(entry)
    return out or _eligible(entry, "alias", "alias_filtered")


def pool_union_alias1(entry: dict) -> list[dict]:
    """As union+alias, but the fallback fires only the plain alias query.

    Each extra query costs a second of rate-limit sleep, so it is worth knowing
    whether the fourth request earns its keep.
    """
    out = pool_union(entry)
    return out or _eligible(entry, "alias")


POOLS = {
    "today": pool_today,
    "plain": pool_plain,
    "filtered": pool_filtered,
    "union": pool_union,
    "union+alias1": pool_union_alias1,
    "union+alias": pool_union_alias,
}


def select_flat(recs: list[dict], artist: str, scorer, title: str = ""):
    """Every release from every recording, judged purely on release merit."""
    rels = _releases_of(recs)
    if not rels:
        return None
    return max(rels, key=lambda rel: scorer(rel, artist))


def select_prod(recs: list[dict], artist: str, scorer, title: str = ""):
    """Exactly what metadata.lookup_musicbrainz now does.

    Imported rather than reimplemented, so the harness cannot quietly drift away
    from the code it is supposed to be measuring.
    """
    from metadata import _score_recording_canonicity, _release_score_for
    if not recs:
        return None
    best = max(recs, key=lambda rec: _score_recording_canonicity(rec, artist, title))
    rels = best.get("releases") or []
    if not rels:
        return None
    return max(rels, key=lambda rel: _release_score_for(rel, artist))


def select_bestrec(recs: list[dict], artist: str, scorer, title: str = ""):
    """Pick the canonical recording first, then its best release.

    Two stages, because they are two different questions: "which take of this
    song is the real one" and "which pressing of it should we tag against".
    """
    if not recs:
        return None
    best = max(recs, key=lambda rec: score_recording(rec, artist, scorer))
    rels = best.get("releases") or []
    if not rels:
        return None
    return max(rels, key=lambda rel: scorer(rel, artist))


SELECTORS = {"flat": select_flat, "bestrec": select_bestrec, "prod": select_prod}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def pick(entry: dict, artist: str, pool_name: str, scorer_name: str,
         selector_name: str, title: str = "") -> dict | None:
    recs = POOLS[pool_name](entry)
    return SELECTORS[selector_name](recs, artist, SCORERS[scorer_name], title)


def is_correct(track: dict, picked: dict | None) -> bool:
    if not picked:
        return False
    title = (picked.get("title") or "").lower()
    return any(pat.lower() in title for pat in track["accept"])


def evaluate(corpus, cache, pool_name, scorer_name, selector_name):
    rows = []
    for track in corpus["tracks"]:
        key = f"{track['artist']}␟{track['title']}"
        entry = cache.get(key, {})
        picked = pick(entry, track["artist"], pool_name, scorer_name, selector_name,
                      track["title"])
        rows.append({
            "track": track,
            "picked": picked,
            "ok": is_correct(track, picked),
            "empty": picked is None,
        })
    return rows


def summarise(rows):
    total = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    empty = sum(1 for r in rows if r["empty"])
    by_cat = {}
    for r in rows:
        cat = r["track"]["category"]
        c = by_cat.setdefault(cat, [0, 0])
        c[1] += 1
        if r["ok"]:
            c[0] += 1
    return ok, total, empty, by_cat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", help="show per-track detail for POOL/SCORER, e.g. union/improved")
    ap.add_argument("--verify", action="store_true",
                    help="check every expectation is actually reachable in the cached data")
    ap.add_argument("--cache", action="append",
                    help="cache file to evaluate; repeatable. Several snapshots average out "
                         "MusicBrainz's non-deterministic search ordering.")
    args = ap.parse_args()

    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    paths = [Path(p) for p in (args.cache or [CACHE])]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"No cache at {missing[0]}. Run tests/tools/refresh_mb_corpus.py first.")
        return 1
    caches = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    cache = caches[0]

    if args.verify:
        # An expectation nobody can reach is a bug in the corpus, not in the code.
        # Without this check a wrong `accept` pattern quietly punishes every
        # strategy equally and we'd conclude the code was hopeless.
        bad = 0
        for track in corpus["tracks"]:
            key = f"{track['artist']}␟{track['title']}"
            entry = cache.get(key, {})
            titles = set()
            for variant in ("plain", "filtered", "alias", "alias_filtered"):
                # Respect the score floor, or we "verify" against recordings that
                # production would reject anyway and congratulate ourselves for it.
                eligible = [r for r in _recs(entry, variant)
                            if r.get("score", 0) >= MB_SCORE_FLOOR]
                for rel in _releases_of(eligible):
                    titles.add((rel.get("title") or "").lower())
            hit = any(pat.lower() in t for pat in track["accept"] for t in titles)
            if not hit:
                bad += 1
                print(f"UNREACHABLE  {track['artist']} - {track['title']}")
                print(f"             accept={track['accept']}")
                print(f"             {len(titles)} distinct releases cached, e.g. "
                      f"{sorted(titles)[:4]}")
        print(f"\n{len(corpus['tracks']) - bad}/{len(corpus['tracks'])} expectations reachable"
              f"{'; fix the corpus before trusting any score below' if bad else ''}")
        if bad:
            return 1
        print()

    cats = sorted({t["category"] for t in corpus["tracks"]})
    n = len(caches)
    print(f"Averaged over {n} snapshot{'s' if n > 1 else ''}: {', '.join(p.name for p in paths)}\n")
    header = (f"{'strategy':34s} {'correct':>13s} {'none':>5s}  "
              + "  ".join(f"{c[:11]:>11s}" for c in cats))
    print(header)
    print("-" * len(header))

    for pool_name in POOLS:
        for selector_name in SELECTORS:
            for scorer_name in SCORERS:
                per_snap, empties, cat_tot = [], [], {}
                for snap in caches:
                    rows = evaluate(corpus, snap, pool_name, scorer_name, selector_name)
                    ok, total, empty, by_cat = summarise(rows)
                    per_snap.append(ok)
                    empties.append(empty)
                    for c, (got, tot) in by_cat.items():
                        acc = cat_tot.setdefault(c, [0, 0])
                        acc[0] += got
                        acc[1] += tot
                cells = []
                for c in cats:
                    got, tot = cat_tot.get(c, [0, 0])
                    cells.append(f"{got / n:.1f}/{tot // n}".rjust(11))
                mean = sum(per_snap) / n
                spread = f"±{(max(per_snap) - min(per_snap)) / 2:.1f}" if n > 1 else "     "
                label = f"{pool_name}/{selector_name}/{scorer_name}"
                marker = ("  <- ships today"
                          if pool_name == "today" and scorer_name == "baseline"
                          and selector_name == "flat" else "")
                print(f"{label:34s} {mean:5.1f}/{total:<3d}{spread:>5s} "
                      f"{sum(empties) / n:5.1f}  " + "  ".join(cells) + marker)

    if args.detail:
        pool_name, selector_name, scorer_name = args.detail.split("/")
        print(f"\nPer-track detail for {args.detail}:\n")
        base = {id(r["track"]): r
                for r in evaluate(corpus, cache, "today", "baseline", "flat")}
        for r in evaluate(corpus, cache, pool_name, scorer_name, selector_name):
            t = r["track"]
            mark = "ok  " if r["ok"] else ("NONE" if r["empty"] else "WRONG")
            picked = (r["picked"] or {}).get("title") if r["picked"] else None
            was = base[id(t)]
            was_title = (was["picked"] or {}).get("title") if was["picked"] else None
            changed = "" if was_title == picked else f"   (was: {was_title!r})"
            print(f"  {mark:5s} {t['artist']} - {t['title']}")
            print(f"        -> {picked!r}{changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
