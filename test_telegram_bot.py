"""Self-check: every callback_data emitted by the bot's keyboards must be
matched by some registered CallbackQueryHandler, otherwise the button press
spins forever (Telegram shows a loader until query.answer()).

Run: python3 test_telegram_bot.py
"""
import os
import re
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("TELEGRAM_BOT_DB", "/tmp/test_telegram_bot.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telegram_bot as tb  # noqa: E402


def all_callback_datas() -> set:
    datas = set()

    def collect(markup):
        for row in markup.inline_keyboard:
            for btn in row:
                datas.add(btn.callback_data)

    collect(tb.build_main_keyboard())
    collect(tb.build_settings_keyboard({
        "search_source": "youtube", "convert_to_flac": False,
        "download_folder": "Singles",
    }))

    base = [{"video_id": "v", "title": "t", "artist": "a", "source": "youtube"},
            {"video_id": "v2", "title": "t2", "channel": "c", "source": "soundcloud",
             "quality": "HI_RES", "source_url": "https://x/y"}]
    for page in range(3):
        collect(tb.build_results_keyboard(base * 3, page))

    jobs = [{"id": f"job{i:032d}", "title": "x", "status": s}
            for i, s in enumerate(("queued", "downloading", "completed", "failed", "unknown"))]
    collect(tb.build_queue_keyboard(jobs))
    return datas


def handler_patterns(app) -> list:
    patterns = []
    for handlers in app.handlers.values():
        for h in handlers:
            p = getattr(h, "pattern", None)
            if p:
                patterns.append(p if isinstance(p, re.Pattern) else re.compile(p))
    return patterns


def main():
    app = tb.setup_bot()
    assert app, "setup_bot() returned None with fake token"

    patterns = handler_patterns(app)
    assert patterns, "no CallbackQueryHandler patterns registered"

    unmatched = sorted(
        d for d in all_callback_datas()
        if not any(p.match(d) for p in patterns)  # PTB matches with re.match
    )
    assert not unmatched, f"callback_data без хендлера (кнопка зависает): {unmatched}"

    assert app.concurrent_updates, "concurrent_updates выключен: медленный хендлер блокирует остальные кнопки"

    print(f"OK: все {len(all_callback_datas())} видов callback_data покрыты хендлерами, concurrent_updates включён")


if __name__ == "__main__":
    main()
