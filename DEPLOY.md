# Деплой на VPS

Парсер — фоновый процесс, работает по циклу и шлёт результаты в Telegram.

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
- **Интервал** задаётся в `docker-compose.yml` (`--interval-minutes 5`), можно поменять на любой.
- **Режим «один прогон»** — если нужен разовый запуск, замените команду на `python main.py --console` или запустите без `--loop`.

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
ExecStart=/opt/parser/venv/bin/python main.py --loop --interval-minutes 5
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
