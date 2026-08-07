"""Асинхронный клиент к публичным API Ozon с антибот-устойчивостью.

Честный статус (проверено эмпирически, 2026-08-08):
  - www.ozon.ru/api/composer-api.bx/page/json/v2 -> HTTP 307 всегда:
    это редирект-петля антибота (к URL добавляется &__rr=N и так до бесконечности),
    если у клиента нет валидного region-cookie. Обычный curl/httpx/curl_cffi
    без прогретых cookie проходят через edge, но упираются в проверку региона.
  - Заголовки и имитация Chrome-отпечатка (curl_cffi impersonate) помогают
    пройти edge-фильтр по отпечатку, но регион-проверку не обходят.

Вывод: с IP/окружения без валидного region-cookie реальные цены Ozon
получить нельзя. Рабочие варианты:
  1) Прокси с прогретым cookie (OZON_PROXY) — поддержан в транспортах;
  2) Демо-режим (OZON_DEMO_MODE=1) — стабильные выдуманные данные;
  3) Официальный Seller API Ozon с токеном продавца.

Клиент реализует максимум на уровне HTTP-клиента:
  - транспорт curl_cffi (имитация Chrome) по умолчанию, httpx как fallback;
  - браузерные заголовки + x-o3-app-name (ожидает приложение Ozon);
  - обнаружение редирект-петли 307 и понятная диагностика (см. diagnose());
  - retry на 429/5xx/сетевые ошибки с экспоненциальным backoff (stdlib);
  - поддержка прокси.

Парсинг ответа: Ozon отдаёт «composer» — словарь widgetStates, где каждый
виджет это JSON-строка. Карточка товара живёт в виджете с префиксом
"webProductPage" (структура нестабильна — парсер ищет по ключам устойчиво).
Цены в виджете — строки рублей ("7990.00") или числа в копейках — парсер
нормализует в целые рубли.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import config

logger = logging.getLogger(__name__)

COMPOSER_API_URL = "https://www.ozon.ru/api/composer-api.bx/page/json/v2"
WWW_URL = "https://www.ozon.ru/"

# Заголовки «как у приложения Ozon». User-Agent и sec-ch-ua при curl_cffi
# выставляет имитация Chrome (ручной UA сломал бы отпечаток).
BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.ozon.ru/",
    "Origin": "https://www.ozon.ru",
    "x-o3-app-name": "rich",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Повторяем на 429 (rate limit) и 5xx; 307 — регион-блок, ретраить бессмысленно.
_RETRY_STATUSES = {429, *range(500, 600)}


def _backoff(attempt: int, min_delay: float = 0.5) -> float:
    """Экспоненциальная задержка: 0.5, 1.0, 2.0 … (потолок 10 c)."""
    return min(min_delay * (2 ** (attempt - 1)), 10.0)


def parse_product_url(text: str) -> int | None:
    """Достаёт ID товара Ozon из ссылки или из голого числа.

    Поддерживаемые формы:
      - https://www.ozon.ru/product/name-1500516648/
      - https://www.ozon.ru/product/1500516648/
      - ozon.ru/product/.../1500516648/  (ID в конце пути)
      - голое число: 1500516648
    Возвращает None, если ID не найден.
    """
    text = text.strip()
    m = re.search(r"/product/(?:[^/]+-)?(\d{5,})/?", text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    return None


# ------------------------------------------------------------- транспорты

try:
    from curl_cffi.requests import AsyncSession as CurlCffiSession

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    CurlCffiSession = None
    HAS_CURL_CFFI = False


class HttpxTransport:
    """Транспорт на httpx: без редиректов (их не бывает), без имитации отпечатка."""

    def __init__(self, timeout: float = 20.0, proxy: str = "") -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=timeout, proxy=proxy or None)

    async def get(self, url: str, *, params=None, headers=None) -> tuple[int, str]:
        resp = await self._client.get(url, params=params, headers=headers)
        return resp.status_code, resp.text

    async def aclose(self) -> None:
        await self._client.aclose()


class CurlCffiTransport:
    """Транспорт на curl_cffi: имитация TLS/HTTP2-отпечатка Chrome.

    allow_redirects=False: редирект-петлю антибота (307 + __rr) мы ловим сами
    и превращаем в понятную диагностику, а не крутим её бесконечно.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        impersonate: str = "chrome",
        proxies: dict | None = None,
    ) -> None:
        self._session = CurlCffiSession(
            impersonate=impersonate,
            timeout=timeout,
            proxies=proxies,
        )

    async def get(self, url: str, *, params=None, headers=None) -> tuple[int, str]:
        resp = await self._session.get(
            url, params=params, headers=headers, allow_redirects=False
        )
        return resp.status_code, resp.text

    async def aclose(self) -> None:
        closer = getattr(self._session, "aclose", None) or self._session.close
        await closer()


def _make_transport() -> HttpxTransport | CurlCffiTransport:
    """Выбирает транспорт по OZON_HTTP_CLIENT (curl_cffi по умолчанию)."""
    if config.HTTP_CLIENT == "curl_cffi":
        if not HAS_CURL_CFFI:
            logger.warning(
                "OZON_HTTP_CLIENT=curl_cffi, но библиотека не установлена. "
                "Падаем обратно на httpx. Установите: pip install curl_cffi"
            )
        else:
            proxies = None
            if config.PROXY:
                proxies = {"http": config.PROXY, "https": config.PROXY}
            return CurlCffiTransport(proxies=proxies)
    return HttpxTransport(proxy=config.PROXY)


# ---------------------------------------------------------- парсер виджетов

def _iter_widget_states(data: dict):
    """Итерирует (ключ_виджета, распарсенный_json) из widgetStates.

    Ozon отдаёт виджеты как JSON-строки; некоторые виджеты бывают просто
    строками без JSON — такие пропускаем.
    """
    for key, raw in (data.get("widgetStates") or {}).items():
        if not isinstance(raw, str):
            continue
        try:
            yield key, json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue


def _unwrap_price(value):
    """Раскрывает цену, которая бывает числом, строкой или объектом {value: ...}."""
    if isinstance(value, dict):
        for key in ("value", "price", "amount"):
            if key in value and value[key] is not None:
                return value[key]
        return None
    return value


def _to_rubles(value) -> int | None:
    """Нормализует цену Ozon в целые рубли.

    Ozon в разных виджетах отдаёт и строки рублей ("7990.00", "7 990"), и
    копейки числами (799000). Умеем оба варианта.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # числа с плавающей точкой — рубли ("7990.5"); целые > 10000 — копейки
        if isinstance(value, float):
            return int(round(value))
        return value if value < 100000 else value // 100
    s = str(value).replace("\u00a0", " ").replace(" ", "").replace(",", ".").strip()
    if not s or not s.replace(".", "").isdigit():
        return None
    num = float(s)
    # "7990.00" — рубли; если точка интерпретирована как разделитель тысяч
    # (например "7.990,00" после удаления пробелов) — это копейки-строка
    if num < 100000 and "." in s and len(s.split(".")[-1]) <= 2:
        return int(round(num))
    if num >= 100000:
        return int(num // 100)
    return int(round(num))


def _product_from_widget(widget: dict) -> dict | None:
    """Устойчиво извлекает карточку товара из JSON-виджета Ozon.

    Поля ищем по нескольким известным вариантам ключей (структура Ozon
    нестабильна): id/ext_id, title/name, price/priceWithSale, stockCount/stocks.
    """
    product = widget.get("product") if isinstance(widget, dict) else None
    if not isinstance(product, dict):
        return None

    pid = product.get("id") or product.get("ext_id")
    if pid is None:
        return None

    title = product.get("title") or product.get("name") or ""
    price_block = product.get("price") or {}
    price = _to_rubles(_unwrap_price(price_block.get("price") or price_block.get("value")))
    old_price = _to_rubles(_unwrap_price(
        price_block.get("oldPrice")
        or price_block.get("old_price")
        or product.get("old_price")
        or product.get("oldPrice")
    ))

    # Остаток: в разных местах (stockCount, stocks.total, или в товарной карточке)
    stock = _to_rubles(product.get("stockCount"))
    if stock is None:
        stocks = product.get("stocks") or {}
        stock = _to_rubles(stocks.get("total") if isinstance(stocks, dict) else None)
    if stock is None:
        stock = _to_rubles(product.get("available"))

    rating = None
    rating_raw = product.get("rating")
    if isinstance(rating_raw, dict):
        rating = rating_raw.get("value") or rating_raw.get("average")
    elif isinstance(rating_raw, (int, float)):
        rating = rating_raw

    return {
        "id": int(pid),
        "title": str(title),
        "price": price,
        "old_price": old_price,
        "stock": stock,
        "rating": float(rating) if rating is not None else None,
    }


def extract_product(data: dict) -> dict | None:
    """Достаёт карточку товара из ответа composer-api (widgetStates).

    Ищем виджет, имя которого начинается с webProductPage (или содержащее
    product). Возвращает словарь с полями id/title/price/old_price/stock/rating
    или None.
    """
    for key, widget in _iter_widget_states(data):
        if not key.startswith("webProductPage"):
            continue
        product = _product_from_widget(widget)
        if product is not None:
            return product
    # запасной вариант: ищем в любом виджете, где есть product
    for key, widget in _iter_widget_states(data):
        if isinstance(widget, dict) and widget.get("product"):
            product = _product_from_widget(widget)
            if product is not None:
                return product
    return None


# --------------------------------------------------------------- клиент

class OzonClient:
    """Клиент к публичному composer-api Ozon с retry и диагностикой."""

    def __init__(self, transport=None, max_retries: int | None = None) -> None:
        self._transport = transport if transport is not None else _make_transport()
        self._max_retries = max_retries if max_retries is not None else config.MAX_RETRIES

    async def _get(self, url: str, *, params=None, retries: int | None = None) -> tuple[int, str]:
        """GET с retry на 429/5xx и сетевые ошибки; возвращает (status, text)."""
        max_retries = self._max_retries if retries is None else retries
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                status, text = await self._transport.get(
                    url, params=params, headers=BROWSER_HEADERS
                )
            except Exception as exc:  # сетевая ошибка любого транспорта
                last_exc = exc
                if attempt == max_retries:
                    raise
                await asyncio.sleep(_backoff(attempt))
                continue

            if status in _RETRY_STATUSES and attempt < max_retries:
                await asyncio.sleep(_backoff(attempt))
                continue
            return status, text

        raise last_exc if last_exc is not None else RuntimeError("unreachable")

    async def get_product(self, ozon_id: int) -> dict | None:
        """Карточка товара по ID; None — товар не найден или ответ недоступен."""
        params = {"url": f"/product/{ozon_id}/"}
        status, text = await self._get(COMPOSER_API_URL, params=params)
        if status == 307:
            logger.warning(
                "Ozon composer-api -> HTTP 307 для %s: регион-блок "
                "(редирект-петля антибота). Нужен OZON_PROXY или демо-режим.",
                ozon_id,
            )
            return None
        if status != 200:
            logger.warning("Ozon composer-api -> HTTP %s для %s", status, ozon_id)
            return None
        try:
            return extract_product(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Ozon composer-api вернул не-JSON для %s", ozon_id)
            return None

    async def diagnose(self) -> list[tuple[str, str]]:
        """Проверяет доступность эндпоинтов Ozon (для команды /diag)."""
        checks = [
            ("www.ozon.ru", WWW_URL, None),
            ("composer-api (product)", COMPOSER_API_URL, {"url": "/product/1500516648/"}),
        ]
        results: list[tuple[str, str]] = []
        for name, url, params in checks:
            try:
                status, text = await self._get(url, params=params, retries=1)
                if status == 307:
                    results.append((name, "HTTP 307 — регион-блок (редирект-петля антибота)"))
                elif status == 429:
                    results.append((name, "HTTP 429 — rate limit"))
                elif status == 200:
                    results.append((name, "HTTP 200 — доступен"))
                else:
                    results.append((name, f"HTTP {status} — недоступен"))
            except Exception as exc:
                results.append((name, f"ошибка: {type(exc).__name__}: {exc}"))
        return results

    async def aclose(self) -> None:
        await self._transport.aclose()


# ------------------------------------------------------------ демо-режим

class MockOzonClient:
    """Демо-режим: не ходит в сеть, отдаёт выдуманную карточку товара."""

    async def get_product(self, ozon_id: int) -> dict:
        return {
            "id": ozon_id,
            "title": f"Тестовый товар {ozon_id} (mock)",
            "price": 6990,
            "old_price": 9990,
            "stock": 12,
            "rating": 4.6,
        }

    async def diagnose(self) -> list[tuple[str, str]]:
        return [("demo-mode", "данные выдуманные (OZON_DEMO_MODE=1)")]

    async def aclose(self) -> None:
        return None
