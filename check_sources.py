#!/usr/bin/env python3
"""Проверка музыкальных источников: сайт + поиск + докачка.

В отличие от servicecheck (доступность сайта), здесь проверяется вся цепочка:
search_fn источника возвращает живые результаты, и первый результат реально
докачивается (первые байты аудио / yt-dlp --simulate для страничных источников).

Запуск (внутри контейнера приложения, где есть все модули):
    docker cp check_sources.py music-grabber:/tmp/
    docker exec music-grabber python /tmp/check_sources.py [source ...]
Без аргументов — все источники; soulseek пропускается, если выключен.
"""
import subprocess
import sys

import httpx

import servicecheck
from search import SOURCE_REGISTRY
from settings import get_setting, get_setting_bool

QUERY = "the beatles let it be"
RANGE_BYTES = 65536
# Страничные источники: ссылка ведёт на страницу, а не на аудио — проверяем yt-dlp.
PAGE_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com")


def site_leg(sid: str) -> tuple[bool, str]:
    entry = servicecheck.check_source(sid, force=True)
    return entry.get("healthy", False), entry.get("reason", "")


def fetch_leg(sid: str, results: list) -> tuple[bool, str]:
    if not results:
        return False, "поиск пуст"
    if sid == "monochrome":
        # У Monochrome есть своя проверка раздачи аудио — она точнее Range-пробы.
        from monochrome import download_leg_healthy
        return download_leg_healthy()
    url = results[0].get("url") or results[0].get("source_url") or ""
    if not url:
        return False, "в результате нет ссылки"
    if any(h in url for h in PAGE_HOSTS):
        return ytdlp_simulate(url)
    try:
        resp = httpx.get(
            url,
            headers={"Range": f"bytes=0-{RANGE_BYTES - 1}"},
            timeout=30,
            follow_redirects=True,
        )
        ok = resp.status_code in (200, 206) and len(resp.content) > 1000
        return ok, f"HTTP {resp.status_code}, {len(resp.content)} байт"
    except Exception as exc:
        return False, f"fetch: {exc}"


def ytdlp_simulate(url: str) -> tuple[bool, str]:
    cmd = ["yt-dlp", "--simulate", "-f", "bestaudio", "--no-playlist", url]
    # Куки синкаются приложением в /data/cookies.txt (youtube_cookies в настройках).
    if get_setting("youtube_cookies", "").strip():
        cmd[1:1] = ["--cookies", "/data/cookies.txt"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "yt-dlp timeout"
    if proc.returncode == 0:
        return True, "format ok"
    last = (proc.stderr.strip().splitlines() or [""])[-1]
    return False, f"yt-dlp: {last[:80]}"


def main() -> int:
    only = set(sys.argv[1:])
    print(f"Запрос: {QUERY!r}\n")
    any_fail = False
    for sid, meta in SOURCE_REGISTRY.items():
        if only and sid not in only:
            continue
        if not get_setting_bool(f"source_{sid}_enabled", meta.get("default_enabled", True)):
            print(f"{meta['label']:12} SKIP (выключен в настройках)")
            continue
        if sid == "soulseek":
            from slskd import slskd_enabled
            if not slskd_enabled():
                print(f"{meta['label']:12} SKIP (выключен)")
                continue
        site_ok, site_reason = site_leg(sid)
        try:
            results = meta["search_fn"](QUERY, limit=3)
            search_err = ""
        except Exception as exc:
            results, search_err = [], str(exc)[:80]
        dl_ok, dl_reason = fetch_leg(sid, results)
        site = "ok" if site_ok else f"FAIL({site_reason[:30]})"
        dl = "ok" if dl_ok else f"FAIL({dl_reason[:50]})"
        print(f"{meta['label']:12} сайт:{site:35} поиск:{len(results):2}  докачка:{dl}")
        any_fail = any_fail or not (site_ok and results and dl_ok)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
