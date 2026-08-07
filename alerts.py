"""Логика «уведомлять или нет» — чистая функция, легко тестируемая.

Отделена от Telegram и БД, чтобы unit-тестами покрыть все ветки:
падение и рост цены, товар снова в наличии (частый сценарий Ozon),
достижение персонального порога, кулдаун между уведомлениями по одному
товару.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def should_notify(
    price: int,
    stock: int,
    last_price: Optional[int],
    last_stock: Optional[int],
    last_alert_at: Optional[str],
    *,
    threshold: Optional[int] = None,
    cooldown_hours: float = 6.0,
    low_stock_threshold: int = 5,
    now: Optional[datetime] = None,
) -> tuple[bool, list[str]]:
    """Возвращает (нужно_ли_уведомлять, список_сообщений).

    Уведомляем, если:
      - цена изменилась относительно последней известной (упала или выросла);
      - цена опустилась до персонального порога (или ниже);
      - товар снова в наличии (был 0, стал > 0);
      - товар заканчивается (остаток <= low_stock_threshold и уменьшился).
    Но не чаще одного раза в cooldown_hours по одному товару.
    """
    messages: list[str] = []

    if last_price is not None and price < last_price:
        messages.append(
            f"📉 <b>Цена упала!</b> Было {last_price} ₽ → стало {price} ₽"
        )
    elif last_price is not None and price > last_price:
        messages.append(
            f"📈 <b>Цена выросла.</b> Было {last_price} ₽ → стало {price} ₽"
        )

    if threshold is not None and price <= threshold:
        messages.append(
            f"🎯 <b>Порог достигнут!</b> Цена {price} ₽ (ваш порог: {threshold} ₽)"
        )

    if last_stock == 0 and stock > 0:
        messages.append(f"✅ <b>Товар снова в наличии!</b> Осталось {stock} шт.")
    if last_stock is not None and last_stock > 0 and 0 < stock <= low_stock_threshold and stock < last_stock:
        messages.append(f"⚠️ <b>Товар заканчивается!</b> Осталось {stock} шт.")

    if not messages:
        return False, []

    if last_alert_at:
        try:
            last_alert_dt = datetime.fromisoformat(last_alert_at)
            now_dt = now or datetime.now(last_alert_dt.tzinfo)
            if now_dt - last_alert_dt < timedelta(hours=cooldown_hours):
                return False, []  # кулдаун ещё не истёк
        except ValueError:
            pass  # битая дата — не мешаем уведомлению
    return True, messages
