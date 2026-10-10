# Деплой на VPS

Парсер — фоновый процесс: собирает и перепроверяет объявления по расписанию, затем отправляет одну ежедневную сводку в Telegram.

## Быстрый старт (Docker)

На сервере (Ubuntu/Debian):

```bash
# 1. Установить Docker и compose-плагин
curl -fsSL https://get.docker.com | sh

# 2. Склонировать проект
git clone <ссылка-на-репозиторий> parser && cd parser

# 3. Создать .env с секретами (НЕ коммитить в git!)
cp .env.example .env
nano .env   # заполнить login, token_avito, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# 4. Собрать и запустить
docker compose up -d --build

# 5. Посмотреть логи
docker compose logs -f parser
```

Остановка/перезапуск:

```bash
docker compose restart parser   # перезапустить
docker compose down             # остановить
```

## Что важно

- **Секреты** — только в `.env` на сервере. В git не попадают (см. `.gitignore`).
- **База** `.market.sqlite3` и кэш `.market_context_cache.json` хранятся в volume `parser-data` (путь `/data` внутри контейнера) и переживают редеплои.
- **Интервал и расписание** задаются переменными окружения в `.env` (`FIRST_RUN_HOUR`, `RUN_INTERVAL_HOURS`, `QUIET_START_HOUR`, `MORNING_LOOKBACK_HOURS`, `REGULAR_LOOKBACK_HOURS`). Расписание и REST-App date-параметры используют фиксированное московское время UTC+3 независимо от зоны хоста. По умолчанию сбор выполняется в 8:00/11:00/14:00/17:00/20:00, а одна Telegram-сводка отправляется после последнего прохода в 20:00; тишина 23:00–8:00.
- **Рыночные аналоги** ищутся на той же площадке: начальная выборка — последние 30 дней и соседние годы, затем при нехватке качественных аналогов окно расширяется до 60–90 дней и допуск пробега постепенно расширяется. Минимум — 3 качественных аналога; скидка должна достигать `MIN_MARKET_DISCOUNT_PCT`. Поиск использует кэш и ограничение `MARKET_CONTEXT_MAX_SEARCHES_PER_RUN`; продавец, условия цены, состояние, поколение/комплектация (если поля доступны), пробег и выбросы по-прежнему фильтруются.
- **Статус объявления**: документация REST-App не описывает универсальное поле «активно/снято с продажи». Каталог повторно оценивает сохранённые объявления, наблюдавшиеся API в пределах настроенного срока истории, но не может гарантировать, что площадка не сняла конкретное объявление после последнего получения.
- **Разовый запуск** — `python main.py --once --console` (или без `--console` для отправки в Telegram).

## Без Docker (systemd)

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
cd /opt/parser
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Создать `/etc/systemd/system/aliceai-parser.service`:

```ini
[Unit]
Description=AliceAI car arbitrage parser
After=network-online.target

[Service]
WorkingDirectory=/opt/parser
EnvironmentFile=/opt/parser/.env
ExecStart=/opt/parser/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aliceai-parser
journalctl -u aliceai-parser -f
```

## Примечание про REST-App

У REST-App суточный лимит запросов. При исчерпании все площадки вернут «Exceeded the daily limit» — парсер это переживёт (откат на накопленную базу), но свежие данные появятся после сброса лимита или пополнения баланса.
