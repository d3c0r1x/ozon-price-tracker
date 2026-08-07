"""Тесты P10: БД (aiosqlite) и чистая логика алертов.

Запуск: python -m pytest tests -q
"""
import asyncio
from datetime import datetime, timedelta, timezone

from alerts import should_notify
from db import Database
from ozon_api import MockOzonClient

OZON_ID = 1500516648


def _product(ozon_id: int = OZON_ID, price: int = 6990, stock: int = 12) -> dict:
    return {
        "id": ozon_id,
        "title": f"Товар {ozon_id}",
        "price": price,
        "old_price": 9990,
        "stock": stock,
        "rating": 4.5,
    }


# ---------------------------------------------------------------- БД

def test_db_roundtrip(tmp_path) -> None:
    """Полный цикл: карточка, трекинг, история, пороги, отписка, статистика."""
    async def run() -> None:
        db = Database(str(tmp_path / "tracker.db"))
        await db.init()
        await db.upsert_item(_product())
        await db.track(111, OZON_ID)
        assert await db.list_tracked(111)
        assert await db.history(OZON_ID)
        assert await db.users_for_product(OZON_ID) == [111]
        assert await db.all_tracked_ids() == [OZON_ID]

        # пороговый алерт
        await db.set_threshold(111, OZON_ID, 5000)
        assert await db.get_threshold(111, OZON_ID) == 5000
        await db.set_threshold(111, OZON_ID, 4000)  # перезапись
        assert await db.get_threshold(111, OZON_ID) == 4000
        assert await db.del_threshold(111, OZON_ID) is True
        assert await db.get_threshold(111, OZON_ID) is None

        assert await db.untrack(111, OZON_ID) is True
        s = await db.stats()
        assert s["items"] >= 1 and s["history"] >= 1 and s["users"] == 0

    asyncio.run(run())


def test_db_upsert_updates_price_and_writes_history(tmp_path) -> None:
    """Повторный апсерт обновляет цену и добавляет запись в историю."""
    async def run() -> None:
        db = Database(str(tmp_path / "tracker.db"))
        await db.init()
        await db.upsert_item(_product(price=6990))
        await db.upsert_item(_product(price=5990))
        item = await db.get_item(OZON_ID)
        assert item["price"] == 5990
        assert len(await db.history(OZON_ID)) == 2

    asyncio.run(run())


def test_cleanup_history(tmp_path) -> None:
    """История старше N дней удаляется, свежая остаётся."""
    async def run() -> None:
        import aiosqlite
        db = Database(str(tmp_path / "tracker.db"))
        await db.init()
        async with aiosqlite.connect(db.path) as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
            await conn.execute(
                "INSERT INTO price_history (ozon_id, price, stock, checked_at) "
                "VALUES (?, 100, 5, ?)", (OZON_ID, old),
            )
            await conn.commit()
        await db.upsert_item(_product())  # свежая запись
        deleted = await db.cleanup_history(keep_days=30)
        assert deleted == 1
        assert len(await db.history(OZON_ID)) == 1

    asyncio.run(run())


# --------------------------------------------------------------- алерты

def test_should_notify_price_drop() -> None:
    now = datetime.now(timezone.utc)
    notify, msgs = should_notify(5990, 10, last_price=6990, last_stock=10,
                                 last_alert_at=None, now=now)
    assert notify and len(msgs) == 1 and "упала" in msgs[0]


def test_should_notify_back_in_stock() -> None:
    """Было 0 на складе -> стало 5: уведомляем о возврате в наличии."""
    now = datetime.now(timezone.utc)
    notify, msgs = should_notify(6990, 5, last_price=6990, last_stock=0,
                                 last_alert_at=None, now=now)
    assert notify
    assert any("снова в наличии" in m for m in msgs)


def test_should_notify_threshold_and_cooldown() -> None:
    now = datetime.now(timezone.utc)
    # порог достигнут
    notify, msgs = should_notify(4500, 10, last_price=6990, last_stock=10,
                                 last_alert_at=None, threshold=5000, now=now)
    assert notify and any("Порог достигнут" in m for m in msgs)
    # кулдаун: уведомляли час назад — молчим
    alert_1h_ago = (now - timedelta(hours=1)).isoformat()
    notify, _ = should_notify(4500, 10, last_price=6990, last_stock=10,
                              last_alert_at=alert_1h_ago, threshold=5000,
                              now=now, cooldown_hours=6)
    assert not notify
    # кулдаун истёк
    alert_7h_ago = (now - timedelta(hours=7)).isoformat()
    notify, _ = should_notify(4500, 10, last_price=6990, last_stock=10,
                              last_alert_at=alert_7h_ago, threshold=5000,
                              now=now, cooldown_hours=6)
    assert notify


def test_should_notify_no_change_silent() -> None:
    """Цена и остаток не изменились — тишина."""
    now = datetime.now(timezone.utc)
    notify, msgs = should_notify(6990, 10, last_price=6990, last_stock=10,
                                 last_alert_at=None, now=now)
    assert not notify and msgs == []


def test_should_notify_low_stock() -> None:
    """Остаток уменьшился и ниже порога — уведомляем о том, что заканчивается."""
    now = datetime.now(timezone.utc)
    notify, msgs = should_notify(6990, 2, last_price=6990, last_stock=10,
                                 last_alert_at=None, now=now,
                                 low_stock_threshold=5)
    assert notify and any("заканчивается" in m for m in msgs)
