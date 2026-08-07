"""Тесты P10: парсинг ссылок Ozon, парсер widgetStates, клиент, mock.

Запуск: python -m pytest tests -q
"""
import asyncio
import json

from ozon_api import (
    MockOzonClient,
    OzonClient,
    extract_product,
    parse_product_url,
)

OZON_ID = 1500516648


class FakeTransport:
    """Транспорт-заглушка: отдаёт заранее заданные ответы по порядку."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url: str, *, params=None, headers=None) -> tuple[int, str]:
        self.calls.append((url, params))
        if not self.responses:
            raise RuntimeError("FakeTransport: пустой список ответов")
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------ ссылки и ID

def test_parse_product_url_forms() -> None:
    """Разные формы ссылок Ozon и голый ID приводятся к одному числу."""
    assert parse_product_url(f"https://www.ozon.ru/product/name-{OZON_ID}/") == OZON_ID
    assert parse_product_url(f"https://www.ozon.ru/product/{OZON_ID}/") == OZON_ID
    assert parse_product_url(f"https://ozon.ru/product/another-slug-{OZON_ID}/") == OZON_ID
    assert parse_product_url(str(OZON_ID)) == OZON_ID
    assert parse_product_url("ozon.ru/product/hello") is None
    assert parse_product_url("не ссылка") is None
    assert parse_product_url("") is None


# ----------------------------------------------------- парсер widgetStates

def _composer_response(widget_key: str, widget: dict) -> str:
    return json.dumps(
        {"widgetStates": {widget_key: json.dumps(widget)}},
        ensure_ascii=False,
    )


def test_extract_product_from_web_product_page() -> None:
    """Карточка из виджета webProductPage со строковыми ценами в рублях."""
    payload = {
        "product": {
            "id": OZON_ID,
            "title": "Смартфон MockPhone",
            "price": {"price": "6990.00", "oldPrice": "9990.00"},
            "stockCount": 12,
            "rating": {"value": 4.6},
        }
    }
    product = extract_product(json.loads(_composer_response("webProductPage123", payload)))
    assert product is not None
    assert product["id"] == OZON_ID
    assert product["title"] == "Смартфон MockPhone"
    assert product["price"] == 6990
    assert product["old_price"] == 9990
    assert product["stock"] == 12
    assert product["rating"] == 4.6


def test_extract_product_kopeck_prices_and_stocks_total() -> None:
    """Цены в копейках числами и остаток в stocks.total тоже понимаются."""
    payload = {
        "product": {
            "id": OZON_ID,
            "title": "Товар",
            "price": {"value": 699000},          # копейки
            "old_price": {"value": 999000},
            "stocks": {"total": 0},              # нет в наличии
        }
    }
    product = extract_product(json.loads(_composer_response("webProductPage1", payload)))
    assert product is not None
    assert product["price"] == 6990
    assert product["old_price"] == 9990
    assert product["stock"] == 0


def test_extract_product_missing_returns_none() -> None:
    """Нет товара (пустой ответ/ошибка) — возвращаем None без исключения."""
    assert extract_product({}) is None
    assert extract_product({"widgetStates": {"other": "{}"}}) is None
    assert extract_product({"widgetStates": {"webProductPage1": "{}"}}) is None
    bad = json.dumps({"widgetStates": {"webProductPage1": "не json"}})
    assert extract_product(json.loads(bad)) is None


# -------------------------------------------------------------- клиент

def test_client_gets_product_via_fake_transport() -> None:
    """HTTP 200 + корректный composer-ответ -> карточка товара."""
    payload = {
        "product": {
            "id": OZON_ID,
            "title": "Реальный товар",
            "price": {"price": "4999.00"},
            "stockCount": 5,
        }
    }
    transport = FakeTransport([(200, _composer_response("webProductPage9", payload))])
    client = OzonClient(transport=transport, max_retries=2)
    product = asyncio.run(client.get_product(OZON_ID))
    assert product is not None
    assert product["price"] == 4999
    assert product["title"] == "Реальный товар"
    # параметр url корректный
    params = transport.calls[0][1]
    assert params == {"url": f"/product/{OZON_ID}/"}


def test_client_handles_region_block_307() -> None:
    """HTTP 307 (редирект-петля антибота) -> None, без исключений и ретраев."""
    transport = FakeTransport([(307, "redirect")])
    client = OzonClient(transport=transport, max_retries=3)
    product = asyncio.run(client.get_product(OZON_ID))
    assert product is None
    assert len(transport.calls) == 1  # 307 не ретраим


def test_client_retries_429_then_succeeds() -> None:
    """На 429 делаем повтор и получаем данные."""
    payload = {"product": {"id": OZON_ID, "title": "T", "price": {"price": "100.00"}}}
    transport = FakeTransport([
        (429, "rate limit"),
        (200, _composer_response("webProductPage2", payload)),
    ])
    client = OzonClient(transport=transport, max_retries=3)
    product = asyncio.run(client.get_product(OZON_ID))
    assert product is not None and product["price"] == 100
    assert len(transport.calls) == 2


def test_mock_client_fields() -> None:
    """Демо-режим: карточка с ожидаемыми полями и диагностика."""
    product = asyncio.run(MockOzonClient().get_product(OZON_ID))
    assert product["id"] == OZON_ID
    assert product["price"] > 0
    assert product["old_price"] > product["price"]
    assert product["stock"] >= 0
    assert "mock" in product["title"].lower()
