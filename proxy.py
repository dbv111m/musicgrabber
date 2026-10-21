"""Резидентный прокси для источников, которым он нужен (zvu4no и прочие).

Выключатель — настройка proxy_enabled (env PROXY_ENABLED), адрес — proxy_url
(env PROXY_URL). Источники передают get_proxy() в proxy= своих httpx-клиентов;
None (выключено/пусто) означает «напрямую», как раньше.
"""
from settings import get_setting


def get_proxy() -> str | None:
    if get_setting("proxy_enabled", "false").lower() in ("true", "1", "yes"):
        url = get_setting("proxy_url", "")
        if url:
            return url
    return None
