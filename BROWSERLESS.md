# Музыкаграббер + общий browserless (CDP)

## Что изменилось

Ранее каждый контейнер, которому нужен был headless-браузер, тянул свой Chromium внутри образа:

- `musicgrabber` — `playwright install chromium` + Google Chrome/Chromium + SeleniumBase (~1.1 GB уникальных слоёв, образ 3.33 GB)
- `rsshub` — `diygod/rsshub:chromium-bundled` (1.94 GB)

Теперь браузер вынесен во внешний сервис **browserless** (`ghcr.io/browserless/chromium:latest`). Все потребители подключаются к нему по Chrome DevTools Protocol (CDP) через `connect_over_cdp`.

## Архитектура

```
+------------------+      HTTP/CDP      +------------------------+
|  musicgrabber    |  port 3000 (host)  |  browserless           |
|  (connect_over_cdp|------------------>|  ghcr.io/browserless   |
|   Spotify/Amazon)|                    |  chromium:latest       |
+------------------+                    |  CONCURRENT=2          |
                                        +------------------------+
                                                 ^
                                                 |
                                        +------------------------+
                                        |  rsshub                |
                                        |  BROWSER_ENDPOINT      |
                                        |  http://browserless:3000|
                                        +------------------------+
```

## Сервисы

### browserless (`/opt/rsshub/docker-compose.yml`)

- Образ: `ghcr.io/browserless/chromium:latest`
- Порт: `3000:3000` (доступен хосту и другим compose-проектам через `host.docker.internal`)
- `CONCURRENT=2` — максимум двух одновременных сессий, остальные в очереди
- `TIMEOUT=120000`
- `shm_size: 1gb`

### musicgrabber (`/opt/musicgrabber/docker-compose.yml`)

- `BROWSER_ENDPOINT=http://host.docker.internal:3000`
- `extra_hosts: ["host.docker.internal:host-gateway"]` — из контейнера попадает на браузер хоста
- `shm_size: 256mb` (было 2gb — больше не нужно, браузер внешний)
- Образ réduit: **3.33 GB → 1.26 GB**

### rsshub (`/opt/rsshub/docker-compose.yml`)

- `diygod/rsshub:chromium-bundled` → `diygod/rsshub:latest`
- `BROWSER_ENDPOINT=http://browserless:3000`
- Образ réduit: **1.94 GB → 646 MB**

## Что поправлено в коде

| Файл | Было | Стало |
|------|------|-------|
| `spotify_browser.py:93` | `p.chromium.launch(headless=True, args=[...])` | `p.chromium.connect_over_cdp(os.environ.get("BROWSER_ENDPOINT", "http://host.docker.internal:3000"))` |
| `amazonpl.py:358` | `p.chromium.launch(headless=True)` | `p.chromium.connect_over_cdp(os.environ.get("BROWSER_ENDPOINT", "http://host.docker.internal:3000"))` |
| `Dockerfile` | `playwright install chromium --with-deps`, Chrome/Chromium, SeleniumBase, xauth/tk8.6, UC driver | удалено |
| `docker-compose.yml` | `shm_size: 2gb` | `shm_size: 256mb`, `BROWSER_ENDPOINT`, `extra_hosts` |

## Ограничения

- `amazonalbum.py` использует SeleniumBase с локальным Chrome. Без Chrome в образе он корректно упадёт с `SeleniumBase unavailable` (уже есть try/except) — функционал наименования альбомов Amazon временно недоступен до переноса на browserless.
- `freemp3cloud` (JS-рендер) пока не подключён к browserless — можно добавить по тому же принципу (`connect_over_cdp`).

## Операции

```bash
# Перезапуск browserless
docker compose -f /opt/rsshub/docker-compose.yml restart browserless

# Логи browserless
docker compose -f /opt/rsshub/docker-compose.yml logs browserless

# Проверить, что браузер отвечает
curl -s http://127.0.0.1:3000/

# Пересобрать musicgrabber после правок
docker compose -f /opt/musicgrabber/docker-compose.yml up -d --build
```

## Диск

До: `musicgrabber` 3.33 GB + `rsshub:chromium-bundled` 1.94 GB + кэш сборки ~5 GB.
После: `musicgrabber` 1.26 GB + `rsshub` 646 MB + `browserless` 3.65 GB (один на всех).

Чистый выигрыш на образах: ~1.7 GB. Главное — ребилды больше не качают 5-гигабайтные хром-слои, и каждый новый браузерный сервис = +0 копий Chromium.
