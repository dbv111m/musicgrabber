"""
MusicGrabber Telegram Bot

Simple functional Telegram bot for searching and downloading music.
No classes, no over-engineering - just functions.
"""

import logging
import os
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import httpx

from constants import VERSION, MUSIC_DIR, DB_PATH
from settings import get_setting, get_setting_bool, set_setting

# Telegram settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = os.getenv("TELEGRAM_ALLOWED_USERS", "")

# User context storage (simple dict)
user_contexts = {}

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============================================================================
# Database helpers
# =============================================================================

def get_db_connection():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_telegram_tables():
    """Initialize Telegram-specific tables."""
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            settings_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            action TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Add telegram_chat_id to jobs if not exists
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN telegram_chat_id INTEGER")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()


def get_user_settings(chat_id: int) -> dict:
    """Get user settings from database."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT settings_json FROM telegram_users WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()
    conn.close()

    if row:
        try:
            return json.loads(row["settings_json"])
        except json.JSONDecodeError:
            pass

    # Default settings
    return {
        "search_source": "all",
        "convert_to_flac": True,
        "download_folder": "Singles",
        "min_bitrate": 0,
    }


def save_user_settings(chat_id: int, settings: dict):
    """Save user settings to database."""
    conn = get_db_connection()
    conn.execute(
        """INSERT OR REPLACE INTO telegram_users
           (chat_id, settings_json, last_active)
           VALUES (?, ?, CURRENT_TIMESTAMP)""",
        (chat_id, json.dumps(settings))
    )
    conn.commit()
    conn.close()


def update_user_last_active(chat_id: int, username: str = None, first_name: str = None):
    """Update user last active time."""
    conn = get_db_connection()
    if username or first_name:
        conn.execute(
            """UPDATE telegram_users
               SET last_active = CURRENT_TIMESTAMP,
                   username = COALESCE(?, username),
                   first_name = COALESCE(?, first_name)
               WHERE chat_id = ?""",
            (username, first_name, chat_id)
        )
    else:
        conn.execute(
            "UPDATE telegram_users SET last_active = CURRENT_TIMESTAMP WHERE chat_id = ?",
            (chat_id,)
        )
    conn.commit()
    conn.close()


def log_bot_action(chat_id: int, action: str, details: str = None):
    """Log bot action."""
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO bot_logs (chat_id, action, details) VALUES (?, ?, ?)",
        (chat_id, action, details)
    )
    conn.commit()
    conn.close()


# =============================================================================
# Access control
# =============================================================================

def is_user_allowed(chat_id: int) -> bool:
    """Check if user is allowed to use bot."""
    if not TELEGRAM_ALLOWED_USERS:
        return True  # No restriction

    try:
        allowed_ids = [int(x.strip()) for x in TELEGRAM_ALLOWED_USERS.split(",")]
        return chat_id in allowed_ids
    except ValueError:
        return True  # Invalid config, allow all


# =============================================================================
# Context management
# =============================================================================

def set_user_context(chat_id: int, key: str, value: any):
    """Set value in user context."""
    if chat_id not in user_contexts:
        user_contexts[chat_id] = {}
    user_contexts[chat_id][key] = value


def get_user_context(chat_id: int, key: str, default=None):
    """Get value from user context."""
    return user_contexts.get(chat_id, {}).get(key, default)


def clear_user_context(chat_id: int):
    """Clear user context."""
    if chat_id in user_contexts:
        del user_contexts[chat_id]


# =============================================================================
# API helpers
# =============================================================================

async def search_music(query: str, source: str, limit: int = 10) -> list:
    """Search music via API."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "http://localhost:8080/api/search",
                json={"query": query, "source": source, "limit": limit}
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []


async def download_track(video_id: str, title: str, artist: str, source: str,
                        convert_to_flac: bool, chat_id: int) -> Optional[str]:
    """Queue track download and return job ID."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "http://localhost:8080/api/download",
                json={
                    "video_id": video_id,
                    "title": title,
                    "artist": artist,
                    "source": source,
                    "convert_to_flac": convert_to_flac
                }
            )
            response.raise_for_status()
            data = response.json()

            job_id = data.get("job_id")

            # Link job to chat
            conn = get_db_connection()
            conn.execute(
                "UPDATE jobs SET telegram_chat_id = ? WHERE id = ?",
                (chat_id, job_id)
            )
            conn.commit()
            conn.close()

            return job_id
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


async def get_job_status(job_id: str) -> Optional[dict]:
    """Get job status."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"http://localhost:8080/api/jobs/{job_id}")
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.error(f"Job status error: {e}")
    return None


async def get_user_jobs(chat_id: int, limit: int = 10) -> list:
    """Get user's jobs."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"http://localhost:8080/api/jobs?limit={limit}")
            if response.status_code == 200:
                data = response.json()
                # Filter by chat_id
                jobs = data.get("jobs", [])
                return [j for j in jobs if j.get("telegram_chat_id") == chat_id]
    except Exception as e:
        logger.error(f"Get jobs error: {e}")
    return []


async def get_stats() -> Optional[dict]:
    """Get global stats."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("http://localhost:8080/api/stats")
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.error(f"Stats error: {e}")
    return None


# =============================================================================
# Keyboard builders
# =============================================================================

def build_main_keyboard():
    """Build main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("🎵 Поиск музыки", callback_data="menu_search")],
        [InlineKeyboardButton("📋 Мои загрузки", callback_data="menu_queue")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_settings_keyboard(settings: dict):
    """Build settings menu keyboard."""
    source_labels = {
        "youtube": "YouTube",
        "soundcloud": "SoundCloud",
        "monochrome": "Monochrome",
        "all": "Все источники"
    }
    source_label = source_labels.get(settings["search_source"], "Все источники")

    quality_label = "FLAC" if settings["convert_to_flac"] else "Оригинал"

    keyboard = [
        [InlineKeyboardButton(f"🔊 Источник: {source_label}", callback_data="set_source")],
        [InlineKeyboardButton(f"🎼 Качество: {quality_label}", callback_data="set_quality")],
        [InlineKeyboardButton(f"📁 Папка: {settings['download_folder']}", callback_data="set_folder")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_results_keyboard(results: list, page: int = 0, per_page: int = 5):
    """Build search results keyboard."""
    keyboard = []
    start = page * per_page
    end = start + per_page

    for i, result in enumerate(results[start:end]):
        idx = start + i + 1
        source_emoji = {
            "youtube": "🔴",
            "soundcloud": "🟠",
            "monochrome": "🟣",
            "soulseek": "🟢",
        }.get(result.get("source", "youtube"), "⚪")

        quality = result.get("quality", "N/A")
        title = result.get("title", "Unknown")[:30]
        artist = result.get("artist", result.get("channel", "Unknown"))[:20]

        label = f"{idx}. {source_emoji} {artist} - {title} [{quality}]"
        callback = f"download_{start + i}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    # Navigation
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"page_{page-1}"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"page_{page+1}"))
    nav_row.append(InlineKeyboardButton("❌", callback_data="search_cancel"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="menu_main")])

    return InlineKeyboardMarkup(keyboard)


def build_queue_keyboard(jobs: list):
    """Build queue management keyboard."""
    keyboard = []
    for job in jobs[:5]:  # Max 5 jobs
        job_id = job["id"][:8]
        status = job["status"]
        title = job.get("title", "Unknown")[:25]

        status_emoji = {
            "queued": "⏳",
            "downloading": "⬇️",
            "completed": "✅",
            "failed": "❌",
        }.get(status, "❓")

        label = f"{status_emoji} {title}"
        if status in ["queued", "downloading"]:
            callback = f"cancel_{job_id}"
        elif status == "failed":
            callback = f"retry_{job_id}"
        else:
            callback = "noop"

        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="queue_refresh")])
    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="menu_main")])

    return InlineKeyboardMarkup(keyboard)


# =============================================================================
# Command handlers
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not is_user_allowed(chat_id):
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return

    update_user_last_active(chat_id, user.username, user.first_name)
    clear_user_context(chat_id)

    text = f"""🎵 <b>MusicGrabger Bot</b>

Версия: {VERSION}

Поиск и загрузка музыки в лучшем качестве!

Выберите действие в меню ниже:"""

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_main_keyboard())
    log_bot_action(chat_id, "start")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    text = """❓ <b>Помощь</b>

<b>Команды:</b>
/start - Главное меню
/search - Поиск музыки
/queue - Мои загрузки
/stats - Статистика
/settings - Настройки

<b>Как пользоваться:</b>
1. Нажмите "🎵 Поиск музыки"
2. Введите название трека
3. Выберите результат из списка
4. Файл автоматически загрузится и придёт в чат

<b>Источники:</b>
🔴 YouTube
🟠 SoundCloud
🟣 Monochrome (Lossless FLAC)
🟢 Soulseek (если настроен)

<b>Настройки:</b>
• Выбор источника поиска
• Качество (FLAC/оригинал)
• Папка для сохранения

Вопросы и предложения: https://github.com/dbv111m/musicgrabber"""

    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(update.effective_chat.id, "help")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /search command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    clear_user_context(chat_id)
    set_user_context(chat_id, "awaiting_search", True)

    text = """🔍 <b>Поиск музыки</b>

Введите название трека или "Исполнитель - Название"

Примеры:
• Arctic Monkeys
• Arctic Monkeys - Do I Wanna Know?
• qotsa go with the flow

Для отмены отправьте /cancel"""

    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "search_init")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /queue command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    jobs = await get_user_jobs(chat_id)

    if not jobs:
        text = "📋 <b>Мои загрузки</b>\n\nПока нет загрузок. Используйте /search для загрузки музыки."
        await update.message.reply_text(text, parse_mode="HTML")
        return

    text = f"📋 <b>Мои загрузки</b>\n\nВсего: {len(jobs)}"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_queue_keyboard(jobs))
    log_bot_action(chat_id, "queue_view")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    stats = await get_stats()
    if not stats:
        await update.message.reply_text("❌ Не удалось получить статистику.")
        return

    total = stats.get("total_jobs", 0)
    completed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    storage_gb = stats.get("storage_bytes", 0) / (1024**3)
    file_count = stats.get("file_count", 0)

    success_rate = (completed / total * 100) if total > 0 else 0

    text = f"""📊 <b>Статистика</b>

🎵 Всего загрузок: {total}
✅ Завершено: {completed} ({success_rate:.1f}%)
❌ Ошибок: {failed}

📦 Место на диске: {storage_gb:.2f} GB
🎧 Треков: {file_count}

Остальная статистика доступна в веб-интерфейсе."""

    await update.message.reply_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "stats_view")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command."""
    chat_id = update.effective_chat.id

    if not is_user_allowed(chat_id):
        return

    settings = get_user_settings(chat_id)

    text = """⚙️ <b>Настройки</b>

Выберите параметр для изменения:"""

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings_view")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command."""
    chat_id = update.effective_chat.id
    clear_user_context(chat_id)
    await update.message.reply_text("❌ Отменено.", reply_markup=build_main_keyboard())


# =============================================================================
# Message handlers
# =============================================================================

async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search query message."""
    chat_id = update.effective_chat.id
    query = update.message.text.strip()

    if not get_user_context(chat_id, "awaiting_search"):
        return  # Not in search mode

    if not query:
        await update.message.reply_text("❌ Пустой запрос. Попробуйте ещё раз:")
        return

    clear_user_context(chat_id)
    set_user_context(chat_id, "search_query", query)

    # Send searching message
    status_msg = await update.message.reply_text(f"🔍 Ищу: <b>{query}</b>...", parse_mode="HTML")

    # Get user settings
    settings = get_user_settings(chat_id)
    source = settings["search_source"]

    # Search
    results = await search_music(query, source, limit=20)

    if not results:
        await status_msg.edit_text("❌ Ничего не найдено. Попробуйте другой запрос.")
        return

    # Store results
    set_user_context(chat_id, "search_results", results)
    set_user_context(chat_id, "search_page", 0)

    # Update message with results
    total = len(results)
    shown = min(5, total)
    text = f"""🎵 <b>Найдено: {total}</b>

Показано: {shown}
Запрос: {query}

Выберите трек для загрузки:"""

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=build_results_keyboard(results))
    log_bot_action(chat_id, "search", f"query: {query}, results: {total}")


# =============================================================================
# Callback handlers
# =============================================================================

async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu callback."""
    query = update.callback_query
    await query.answer()

    text = """🎵 <b>MusicGrabber Bot</b>

Выберите действие:"""
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_keyboard())


async def callback_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search menu callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    clear_user_context(chat_id)
    set_user_context(chat_id, "awaiting_search", True)

    text = """🔍 <b>Поиск музыки</b>

Введите название трека или "Исполнитель - Название"

Для отмены отправьте /cancel"""

    await query.edit_message_text(text, parse_mode="HTML")


async def callback_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle queue menu callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    jobs = await get_user_jobs(chat_id)

    if not jobs:
        text = "📋 <b>Мои загрузки</b>\n\nПока нет загрузок."
        await query.edit_message_text(text, parse_mode="HTML")
        return

    text = f"📋 <b>Мои загрузки</b>\n\nВсего: {len(jobs)}"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_queue_keyboard(jobs))


async def callback_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stats menu callback."""
    query = update.callback_query
    await query.answer()

    stats = await get_stats()
    if not stats:
        await query.edit_message_text("❌ Не удалось получить статистику.")
        return

    total = stats.get("total_jobs", 0)
    completed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    storage_gb = stats.get("storage_bytes", 0) / (1024**3)
    file_count = stats.get("file_count", 0)
    success_rate = (completed / total * 100) if total > 0 else 0

    text = f"""📊 <b>Статистика</b>

🎵 Всего: {total}
✅ Завершено: {completed} ({success_rate:.1f}%)
❌ Ошибок: {failed}
📦 Место: {storage_gb:.2f} GB
🎧 Треков: {file_count}"""

    await query.edit_message_text(text, parse_mode="HTML")


async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings menu callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    settings = get_user_settings(chat_id)

    text = """⚙️ <b>Настройки</b>

Выберите параметр:"""
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))


async def callback_set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle source setting callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    settings = get_user_settings(chat_id)

    # Cycle through sources
    sources = ["youtube", "soundcloud", "monochrome", "all"]
    current_idx = sources.index(settings["search_source"]) if settings["search_source"] in sources else 0
    next_idx = (current_idx + 1) % len(sources)
    settings["search_source"] = sources[next_idx]

    save_user_settings(chat_id, settings)

    source_labels = {
        "youtube": "YouTube 🔴",
        "soundcloud": "SoundCloud 🟠",
        "monochrome": "Monochrome 🟣",
        "all": "Все источники 🎯"
    }

    text = f"🔊 <b>Источник: {source_labels[sources[next_idx]]}</b>"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings", f"source: {sources[next_idx]}")


async def callback_set_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quality setting callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    settings = get_user_settings(chat_id)

    # Toggle FLAC
    settings["convert_to_flac"] = not settings["convert_to_flac"]
    save_user_settings(chat_id, settings)

    quality = "FLAC 🎼" if settings["convert_to_flac"] else "Оригинал 📻"

    text = f"🎼 <b>Качество: {quality}</b>"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_settings_keyboard(settings))
    log_bot_action(chat_id, "settings", f"flac: {settings['convert_to_flac']}")


async def callback_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle download callback."""
    query = update.callback_query
    await query.answer("⏳ Добавлено в очередь...")

    chat_id = query.message.chat_id
    result_idx = int(query.data.split("_")[1])

    # Get result
    results = get_user_context(chat_id, "search_results", [])
    if result_idx >= len(results):
        await query.edit_message_text("❌ Ошибка: результат не найден.")
        return

    result = results[result_idx]

    # Get settings
    settings = get_user_settings(chat_id)

    # Download
    job_id = await download_track(
        video_id=result["video_id"],
        title=result.get("title", "Unknown"),
        artist=result.get("artist") or result.get("channel", "Unknown"),
        source=result.get("source", "youtube"),
        convert_to_flac=settings["convert_to_flac"],
        chat_id=chat_id
    )

    if not job_id:
        await query.edit_message_text("❌ Ошибка при добавлении в очередь.")
        return

    text = f"""⏳ <b>Добавлено в очередь</b>

🎵 {result.get('title', 'Unknown')}
🎤 {result.get('artist') or result.get('channel', 'Unknown')}

Используйте /queue для отслеживания прогресса."""

    await query.edit_message_text(text, parse_mode="HTML")
    log_bot_action(chat_id, "download", f"{result.get('title')}, job: {job_id}")


async def callback_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle page navigation callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    page = int(query.data.split("_")[1])

    results = get_user_context(chat_id, "search_results", [])
    search_query = get_user_context(chat_id, "search_query", "")

    set_user_context(chat_id, "search_page", page)

    total = len(results)
    shown = min((page + 1) * 5, total)

    text = f"""🎵 <b>Найдено: {total}</b>

Показано: {shown}
Запрос: {search_query}

Выберите трек:"""

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_results_keyboard(results, page))


async def callback_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search cancel callback."""
    query = update.callback_query
    await query.answer()

    clear_user_context(query.message.chat_id)
    await query.edit_message_text("❌ Поиск отменен.", reply_markup=build_main_keyboard())


async def callback_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle no-op callback."""
    await update.callback_query.answer()


# =============================================================================
# Bot setup
# =============================================================================

def setup_bot():
    """Setup bot application."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return None

    # Init database tables
    init_telegram_tables()

    # Create application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_query))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(callback_main_menu, pattern="^menu_main$"))
    app.add_handler(CallbackQueryHandler(callback_search, pattern="^menu_search$"))
    app.add_handler(CallbackQueryHandler(callback_queue, pattern="^menu_queue$"))
    app.add_handler(CallbackQueryHandler(callback_stats, pattern="^menu_stats$"))
    app.add_handler(CallbackQueryHandler(callback_settings, pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(callback_set_source, pattern="^set_source$"))
    app.add_handler(CallbackQueryHandler(callback_set_quality, pattern="^set_quality$"))
    app.add_handler(CallbackQueryHandler(callback_download, pattern="^download_\\d+$"))
    app.add_handler(CallbackQueryHandler(callback_page, pattern="^page_\\d+$"))
    app.add_handler(CallbackQueryHandler(callback_search_cancel, pattern="^search_cancel$"))
    app.add_handler(CallbackQueryHandler(callback_noop, pattern="^set_folder$|^cancel_|^retry_|^queue_refresh$"))

    return app


def run_bot():
    """Run bot (blocking)."""
    app = setup_bot()
    if not app:
        logger.error("Failed to setup bot")
        return

    logger.info("Starting MusicGrabber Telegram bot...")
    app.run_polling(allowed_updates=["message", "callback_query"])


async def run_bot_async():
    """Run bot asynchronously (for integration with FastAPI)."""
    app = setup_bot()
    if not app:
        logger.error("Failed to setup bot")
        return

    logger.info("Starting MusicGrabber Telegram bot (async)...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)


def run_bot_process():
    """Run bot in separate process (for background execution)."""
    import multiprocessing
    process = multiprocessing.Process(target=run_bot, daemon=True)
    process.start()
    logger.info("Telegram bot started in background process")
    return process


# =============================================================================
# For running standalone
# =============================================================================

if __name__ == "__main__":
    run_bot()
