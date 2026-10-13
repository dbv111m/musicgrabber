#!/usr/bin/env python3
"""Build the small offline fixture used by tests/test_release_selection.py.

Six tracks, one per failure mode, fetched with the same trimming as the tuning
corpus but keeping the media block so the test can also pin track numbers.

    tests/.venv/bin/python tests/tools/make_lookup_fixture.py

The alias query is only cached for tracks that actually need it, since production
only fires it when the other two searches come back empty. No point shipping 25
recordings nobody will ever read.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_mb_corpus import (  # noqa: E402
    build_queries, fetch, RATE_LIMIT_SECS, FIXTURES,
)

OUT = FIXTURES / "mb_lookup_fixture.json"

# One per failure mode: compilation trap, bootleg trap, romanised non-English,
# mainstream control, latin-script non-English, native-script non-English.
TRACKS = [
    ("Daft Punk", "Around the World"),
    ("Nirvana", "Smells Like Teen Spirit"),
    ("YOASOBI", "Yoru ni Kakeru"),
    ("Massive Attack", "Teardrop"),
    ("Rammstein", "Du hast"),
    ("Кино", "Группа крови"),
]

MB_SCORE_FLOOR = 85


def main() -> int:
    out: dict = {}
    for artist, title in TRACKS:
        queries = build_queries(artist, title)
        entry = {}
        for variant in ("plain", "filtered"):
            entry[variant] = fetch(queries[variant])
            print(f"  {artist} - {title} ({variant}): "
                  f"{len(entry[variant].get('recordings') or [])} recs", flush=True)
            time.sleep(RATE_LIMIT_SECS)

        eligible = [r for v in ("plain", "filtered")
                    for r in (entry[v].get("recordings") or [])
                    if r.get("score", 0) >= MB_SCORE_FLOOR]
        if not eligible:
            entry["alias"] = fetch(queries["alias"])
            print(f"  {artist} - {title} (alias fallback): "
                  f"{len(entry['alias'].get('recordings') or [])} recs", flush=True)
            time.sleep(RATE_LIMIT_SECS)

        out[f"{artist}␟{title}"] = entry

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nWrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, {len(out)} tracks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
