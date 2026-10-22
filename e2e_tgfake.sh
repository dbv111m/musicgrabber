#!/usr/bin/env bash
# e2e кнопок бота через tgfake — локальный Telegram Bot API стенд без живого
# Telegram: https://github.com/EvilFreelancer/tgfake
# Требует: tgfake запущен на $TGFAKE_URL (./tgfake -addr 127.0.0.1:18820),
# приложение MusicGrabber доступно на $MUSICGRABBER_API_URL.
set -euo pipefail
TGFAKE_URL=${TGFAKE_URL:-http://127.0.0.1:18820}
MUSICGRABBER_API_URL=${MUSICGRABBER_API_URL:-http://127.0.0.1:8765}
DIR=$(cd "$(dirname "$0")" && pwd)
T=$TGFAKE_URL/sim

fail() { echo "FAIL: $1"; curl -s "$T/chat/4242?format=text" | tail -25; exit 1; }
tap() { curl -s -X POST "$T/callback" -d "{\"label\": \"$1\"}" >/dev/null; sleep 2; }
chat() { curl -s "$T/chat/4242?format=text"; }

# Автозапуск стенда, если не поднят (бинарь: ~/tools/tgfake, см. IDEAS.md)
TGFAKE_OWNED=""
if ! curl -s -m 2 "$T/state" >/dev/null 2>&1; then
  [ -x "$HOME/tools/tgfake/tgfake" ] || fail "нет ~/tools/tgfake/tgfake и стенд не поднят"
  nohup "$HOME/tools/tgfake/tgfake" -addr "${TGFAKE_URL#http://}" >/tmp/tgfake_e2e.log 2>&1 &
  TGFAKE_OWNED=$!
  sleep 2
  curl -s -m 3 "$T/state" >/dev/null || fail "tgfake не поднялся на $TGFAKE_URL"
fi
curl -s -X POST "$T/reset" >/dev/null
rm -f /tmp/e2e_bot.db
TELEGRAM_BOT_TOKEN=123456:AAEtestE2Etoken000000000000000 \
  TELEGRAM_API_URL=$TGFAKE_URL MUSICGRABBER_API_URL=$MUSICGRABBER_API_URL \
  MUSICGRABBER_API_KEY=$(grep -oP '(?<=^API_KEY=).*' "$DIR/.env" 2>/dev/null || true) \
  TELEGRAM_BOT_DB=/tmp/e2e_bot.db \
  python3 "$DIR/telegram_bot.py" >/tmp/e2e_bot.log 2>&1 &
BOT=$!
trap '[ -n "$BOT" ] && kill $BOT 2>/dev/null; [ -n "$TGFAKE_OWNED" ] && kill $TGFAKE_OWNED 2>/dev/null' EXIT
sleep 4

curl -s -X POST "$T/message" -d '{"text": "/start"}' >/dev/null
sleep 2
chat | grep -q 'Поиск музыки' || fail "/start не дал меню"

tap '❓ Помощь'
chat | grep -q 'Как пользоваться' || fail "кнопка Помощь не ответила (бага menu_help)"
chat | grep -q 'Поиск музыки' || fail "у Помощи нет клавиатуры главного меню"

tap '⚙️ Настройки'
chat | grep -q 'Источник' || fail "Настройки не открылись"

tap '🔊 Источник: Все источники'
chat | grep -q 'Источник: YouTube' || fail "переключение источника не сработало"

tap '🎼 Качество: Оригинал'
chat | grep -q 'Качество: FLAC' || fail "переключение FLAC не сработало"

tap '◀️ Назад'
chat | grep -q 'Выберите действие' || fail "Назад не вернул в меню"

# noop-строка (завершённая задача в /queue): чат не меняется, но не зависает
curl -s -X POST "$T/callback" -d '{"data": "noop"}' >/dev/null
sleep 2
chat | grep -q 'Выберите действие' || fail "noop сломал сообщение"

# --- Очередь: дедуп, досылка упущенного файла, очистка ---
# Берём реальную completed-джобу живого app + вторую джобу с тем же треком
# (эмуляция повторной загрузки) — очередь должна показать трек один раз.
API_KEY_E2E=${API_KEY_E2E:-$(grep -oP '(?<=^API_KEY=).*' "$DIR/.env" 2>/dev/null || true)}
JOB_IDS=$(curl -s -H "X-API-Key: $API_KEY_E2E" "$MUSICGRABBER_API_URL/api/jobs?limit=10" | python3 -c "
import json, sys
d = json.load(sys.stdin)
jobs = d if isinstance(d, list) else d.get('jobs', [])
done = [j['id'] for j in jobs if j.get('status') == 'completed']
other = [j['id'] for j in jobs if j.get('id') not in done]
print(' '.join((done[:1] + other[:1])[:2]))")
J1=${JOB_IDS%% *}; J2=${JOB_IDS##* }
[ -n "$J1" ] || fail "в app нет completed-джобы для теста очереди"
python3 - "$J1" "${J2:-$J1-dup}" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect('/tmp/e2e_bot.db')
# свежее всех — completed-джоба: дедуп оставит её, досылка пойдёт по ней
conn.execute("INSERT OR REPLACE INTO bot_jobs (job_id, chat_id, title, artist, delivered, created_at) "
             "VALUES (?, 4242, 'E2E Dup Track', 'E2E Artist', 0, '2026-01-02 00:00:00')", (sys.argv[1],))
conn.execute("INSERT OR REPLACE INTO bot_jobs (job_id, chat_id, title, artist, delivered, created_at) "
             "VALUES (?, 4242, 'E2E Dup Track', 'E2E Artist', 0, '2026-01-01 00:00:00')", (sys.argv[2],))
conn.commit(); conn.close()
PYEOF

tap '📋 Мои загрузки'
chat | grep -q 'Очистить' || fail "в очереди нет кнопки Очистить"
chat | grep -q 'Всего: 1' || fail "дедуп не сработал (ожидалась 1 запись)"
[ "$(chat | grep -c 'E2E Dup Track\]')" -eq 1 ] || fail "кнопка трека показана больше одного раза"
sleep 6  # досылка упущенного файла идёт фоновой задачей
[ "$(python3 -c "import sqlite3; print(sqlite3.connect('/tmp/e2e_bot.db').execute(\"SELECT count(*) FROM bot_logs WHERE action='redeliver'\").fetchone()[0])")" -ge 1 ] || fail "досылка completed-файла не сработала"
curl -s -X POST "$T/callback" -d '{"label": "🗑 Очистить"}' >/dev/null
sleep 2
chat | grep -q 'Очередь очищена' || fail "очистка очереди не сработала"
[ "$(python3 -c "import sqlite3; print(sqlite3.connect('/tmp/e2e_bot.db').execute('SELECT count(*) FROM bot_jobs WHERE chat_id=4242').fetchone()[0])")" -eq 0 ] || fail "bot_jobs не очищена"

echo "OK: e2e кнопок через tgfake пройден"
