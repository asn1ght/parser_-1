# ============================
#  config.py — настройки парсера
# ============================
import os
from dotenv import load_dotenv

load_dotenv()

AVITO_API_LOGIN = os.getenv("login", "").strip()
AVITO_API_TOKEN = os.getenv("token_avito", "").strip()

# ====== ТЕЛЕГРАМ ======


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [
    chat_id.strip()
    for chat_id in os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
    if chat_id.strip()
]
if not TELEGRAM_CHAT_IDS:
    single = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    TELEGRAM_CHAT_IDS = [single] if single else []
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_IDS[0] if TELEGRAM_CHAT_IDS else ""

# ====== ФИЛЬТРЫ ПОИСКА ======
FILTERS = {
    "max_price": int(os.getenv("MAX_PRICE", "1000000")),
    "max_mileage": int(os.getenv("MAX_MILEAGE", "300000")),
    "location": "sankt-peterburg",
    "radius": 300,             # Радиус поиска, км
    "drom_region": "region78", # Санкт-Петербург на Drom
    "drom_api_region_id": 191, # Санкт-Петербург в REST-App Drom API
    "drom_lookback_minutes": int(os.getenv("DROM_LOOKBACK_MINUTES", "30")),
    "drom_lookback_hours": int(os.getenv("DROM_LOOKBACK_HOURS", "24")),
    "autoru_api_region_id": 191,
    "autoru_lookback_hours": int(os.getenv("AUTORU_LOOKBACK_HOURS", "2")),
    "avito_region_id": 653240, # Санкт-Петербург в REST-App API
    "avito_lookback_hours": int(os.getenv("AVITO_LOOKBACK_HOURS", "2")),
    "avito_lookback_minutes": int(os.getenv("AVITO_LOOKBACK_MINUTES", "30")),
    "market_history_days": int(os.getenv("MARKET_HISTORY_DAYS", "30")),
    "market_min_samples": int(os.getenv("MARKET_MIN_SAMPLES", "5")),
    "market_max_price": int(os.getenv("MARKET_MAX_PRICE", "2500000")),
    "market_max_mileage": int(os.getenv("MARKET_MAX_MILEAGE", "500000")),
    "market_context_limit": int(os.getenv("MARKET_CONTEXT_LIMIT", "100")),
    "market_context_max_searches_per_run": int(os.getenv("MARKET_CONTEXT_MAX_SEARCHES_PER_RUN", "6")),
    "min_market_discount_pct": float(os.getenv("MIN_MARKET_DISCOUNT_PCT", "10")),
    "min_profit": 100000,      # Минимальная желаемая прибыль, руб
}

# ====== НАСТРОЙКИ ПАРСИНГА ======
SCRAPING = {
    "max_pages_per_site": int(os.getenv("MAX_PAGES_PER_SITE", "5")),
    "delay_min": 3,             # Минимальная задержка между запросами, сек
    "delay_max": 8,             # Максимальная задержка, сек
    "max_quality_checks_per_run": int(os.getenv("MAX_QUALITY_CHECKS_PER_RUN", "30")),
    "top_count": int(os.getenv("TOP_COUNT", "50")),
    "listing_api_limit": int(os.getenv("LISTING_FETCH_LIMIT", "250")),
}

# ====== РАСПИСАНИЕ РАБОТЫ ======
SCHEDULE = {
    "quiet_start_hour": int(os.getenv("QUIET_START_HOUR", "23")),   # тишина с этого часа
    "first_run_hour": int(os.getenv("FIRST_RUN_HOUR", "8")),        # первый утренний отчёт
    "run_interval_hours": int(os.getenv("RUN_INTERVAL_HOURS", "3")), # каждые N часов
    "archive_lookback_hours": int(os.getenv("ARCHIVE_LOOKBACK_HOURS", "168")),
    "archive_listing_api_limit": int(os.getenv("ARCHIVE_LISTING_LIMIT", "1000")),
    "morning_lookback_hours": int(os.getenv("MORNING_LOOKBACK_HOURS", "10")),  # утром собрать за ночь
    "regular_lookback_hours": int(os.getenv("REGULAR_LOOKBACK_HOURS", "3")),   # обычное окно сбора
}

# ====== СТОИМОСТЬ ПРЕДПРОДАЖНОЙ ПОДГОТОВКИ ======
PREP_COSTS = {
    "chemical_cleaning": 3000,   # Химчистка салона (расходники)
    "polishing": 2000,           # Полировка кузова (расходники)
    "small_repair": 5000,        # Мелкий ремонт (лампочка, коврики и т.п.)
}

# ====== СТОИМОСТЬ ПРОДАЖИ ======
SELLING_COSTS = {
    "avito_listing": 1500,       # Платное размещение на Авито
    "fuel_inspection": 2000,     # Бензин на показы и осмотры
}
