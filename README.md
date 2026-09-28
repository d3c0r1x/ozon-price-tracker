# Ozon Price & Stock Tracker

> **Prototype / supporting project.**
>
> Это ранний marketplace-прототип, который больше не является отдельным представителем портфолио. Логика мониторинга цены/остатка позже использовалась и развивалась в [Ozon Seller Bot](https://github.com/d3c0r1x/ozon-seller-bot) и [Smart Shopper](https://github.com/d3c0r1x/smart-shopper).
>
> Репозиторий оставлен публичным как техническая история, а не как отдельный продукт.

## Что делает

Telegram-бот:

- отслеживает товары Ozon;
- сохраняет историю цены;
- следит за остатками;
- использует порог low-stock;
- отправляет уведомления;
- имеет cooldown для alerts;
- периодически очищает старые данные.

## Структура

```
bot.py          # commands + scheduler
ozon_api.py     # Ozon client
alerts.py       # alert decisions
db.py           # SQLite/history
utils.py        # cache/retry
middlewares.py  # throttling/logging
config.py       # settings
tests/          # adapter, DB, flow tests
```

## Быстрый запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
start.bat
```

Для просмотра без сети:

```
OZON_DEMO_MODE=1
```

## Конфигурация

Основные параметры:

| Переменная | Назначение |
|---|---|
| `OZON_BOT_TOKEN` | Telegram token |
| `OZON_DEMO_MODE` | offline demo |
| `OZON_HTTP_CLIENT` | `curl_cffi` или `httpx` |
| `OZON_PROXY` | proxy |
| `OZON_CHECK_INTERVAL_MINUTES` | polling interval |
| `OZON_ALERT_COOLDOWN_HOURS` | alert cooldown |
| `OZON_HISTORY_KEEP_DAYS` | retention |
| `OZON_LOW_STOCK_THRESHOLD` | low-stock threshold |
| `OZON_CACHE_TTL_SECONDS` | cache TTL |

## Пример сценария

Пользователь добавляет товар через Telegram, затем scheduler периодически:

1. запрашивает актуальные данные;
2. сравнивает их с предыдущим snapshot;
3. принимает решение об alert;
4. сохраняет новый snapshot.

## Тесты

```bash
pytest -q
```

## Почему проект поддерживается только как прототип

Самостоятельный tracker слишком узок по сравнению с последующими проектами.

Сейчас его назначение — показать конкретный этап:

```
simple marketplace polling
        ↓
alerts + persistence
        ↓
larger Ozon / marketplace products
```

## AI-assisted development

AI использовался как ускоритель реализации и тестовых идей. Ценность этого репозитория именно в том, что он показывает развитие конкретной функции до более крупных систем.

## Лицензия

MIT.
