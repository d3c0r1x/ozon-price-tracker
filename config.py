"""Конфигурация бота через переменные окружения (stdlib os.getenv)."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = os.getenv("OZON_BOT_TOKEN", "")
DB_PATH = os.getenv("OZON_DB_PATH", os.path.join(BASE_DIR, "tracker.db"))
# Демо-режим: не ходит в сеть, отдаёт выдуманные данные (Ozon защищён антиботом)
DEMO_MODE = os.getenv("OZON_DEMO_MODE", "0") == "1"
# Периодичность проверки цен (минуты). Ozon меняет цены чаще WB, поэтому по
# умолчанию 6 часов, а не сутки.
CHECK_INTERVAL_MINUTES = int(os.getenv("OZON_CHECK_INTERVAL_MINUTES", "360"))
# Остаток меньше или равен порогу считаем «товар заканчивается»
LOW_STOCK_THRESHOLD = int(os.getenv("OZON_LOW_STOCK_THRESHOLD", "5"))

# --- Транспорт HTTP (антибот-устойчивость, см. ozon_api.py) ---
#   curl_cffi — имитация TLS/HTTP2-отпечатка Chrome (по умолчанию)
#   httpx     — стандартный асинхронный клиент
HTTP_CLIENT = os.getenv("OZON_HTTP_CLIENT", "curl_cffi")
# Прокси: http://user:pass@host:port или socks5://host:1080
PROXY = os.getenv("OZON_PROXY", "")
MAX_RETRIES = int(os.getenv("OZON_MAX_RETRIES", "3"))

# --- Продвинутый уровень ---
# Кулдаун уведомлений по одному товару (часы)
ALERT_COOLDOWN_HOURS = float(os.getenv("OZON_ALERT_COOLDOWN_HOURS", "6"))
# Хранить историю цен N дней
HISTORY_KEEP_DAYS = int(os.getenv("OZON_HISTORY_KEEP_DAYS", "30"))
# Минимальный интервал между сообщениями пользователя (секунды)
THROTTLE_MIN_INTERVAL = float(os.getenv("OZON_THROTTLE_MIN_INTERVAL", "0.7"))
# ID администраторов для /cleanup (через запятую; пусто = доступно всем)
ADMIN_IDS = [int(x) for x in os.getenv("OZON_ADMIN_IDS", "").split(",") if x.strip().isdigit()]
# TTL кэша карточек Ozon (секунды)
CACHE_TTL_SECONDS = float(os.getenv("OZON_CACHE_TTL_SECONDS", "300"))
