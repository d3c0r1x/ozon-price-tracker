"""Локальная SQLite-БД через aiosqlite: товары Ozon, подписки, история цен.

Продвинутый уровень:
  - индексы на колонки, по которым идёт поиск (ozon_id, checked_at);
  - last_alert_at в карточке — кулдаун уведомлений (см. alerts.py);
  - таблица alert_thresholds — персональный порог «уведомить ниже N ₽»;
  - cleanup_history() — плановая очистка истории старше N дней;
  - stats() — сводка по базе для команды /stats.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        """Создаёт таблицы и индексы при первом запуске."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    ozon_id       INTEGER PRIMARY KEY,
                    title         TEXT,
                    price         INTEGER,      -- текущая цена, рубли
                    old_price     INTEGER,      -- цена до скидки, рубли
                    stock         INTEGER,      -- остаток, шт.
                    rating        REAL,
                    last_checked  TEXT,
                    last_price    INTEGER,      -- цена, о которой уже уведомили
                    last_stock    INTEGER,      -- остаток, о котором уже уведомили
                    last_alert_at TEXT,         -- когда последний раз уведомили
                    created_at    TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked (
                    user_id INTEGER,
                    ozon_id INTEGER,
                    PRIMARY KEY (user_id, ozon_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_thresholds (
                    user_id    INTEGER,
                    ozon_id    INTEGER,
                    threshold  INTEGER,      -- уведомить, если цена <= threshold
                    created_at TEXT,
                    PRIMARY KEY (user_id, ozon_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ozon_id    INTEGER,
                    price      INTEGER,
                    stock      INTEGER,
                    checked_at TEXT
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_ozon ON price_history (ozon_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracked_ozon ON tracked (ozon_id)"
            )
            await db.commit()

    async def upsert_item(self, product: dict) -> None:
        """Сохраняет/обновляет карточку товара и пишет запись в историю цен."""
        ozon_id = int(product["id"])
        price = int(product.get("price") or 0)
        old_price = int(product.get("old_price") or price) if product.get("old_price") else None
        stock = int(product.get("stock") or 0)
        rating = product.get("rating")
        title = str(product.get("title") or "")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO items
                    (ozon_id, title, price, old_price, stock, rating, last_checked,
                     last_price, last_stock, last_alert_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(ozon_id) DO UPDATE SET
                    title = excluded.title,
                    price = excluded.price,
                    old_price = excluded.old_price,
                    stock = excluded.stock,
                    rating = excluded.rating,
                    last_checked = excluded.last_checked
                """,
                (
                    ozon_id, title, price, old_price, stock, rating, _now(),
                    price, stock, _now(),
                ),
            )
            await db.execute(
                "INSERT INTO price_history (ozon_id, price, stock, checked_at) "
                "VALUES (?, ?, ?, ?)",
                (ozon_id, price, stock, _now()),
            )
            await db.commit()

    async def update_last_notified(self, ozon_id: int, price: int, stock: int) -> None:
        """После отправки уведомления запоминает текущие значения и время."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE items SET last_price = ?, last_stock = ?, last_alert_at = ? "
                "WHERE ozon_id = ?",
                (price, stock, _now(), ozon_id),
            )
            await db.commit()

    async def get_item(self, ozon_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM items WHERE ozon_id = ?", (ozon_id,)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_last_alert(self, ozon_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT last_alert_at FROM items WHERE ozon_id = ?", (ozon_id,)
            )
            row = await cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------- трекинг

    async def track(self, user_id: int, ozon_id: int) -> bool:
        """Добавляет товар в отслеживание; False — если уже отслеживался."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO tracked (user_id, ozon_id) VALUES (?, ?)",
                (user_id, ozon_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def is_tracked(self, user_id: int, ozon_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM tracked WHERE user_id = ? AND ozon_id = ? LIMIT 1",
                (user_id, ozon_id),
            )
            return await cur.fetchone() is not None

    async def untrack(self, user_id: int, ozon_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM tracked WHERE user_id = ? AND ozon_id = ?",
                (user_id, ozon_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_tracked(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT i.ozon_id, i.title, i.price, i.old_price, i.stock, i.rating
                FROM tracked t JOIN items i ON i.ozon_id = t.ozon_id
                WHERE t.user_id = ?
                ORDER BY i.created_at DESC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def users_for_product(self, ozon_id: int) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id FROM tracked WHERE ozon_id = ?", (ozon_id,)
            )
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def all_tracked_ids(self) -> list[int]:
        """Все ozon_id, которые отслеживает хотя бы один пользователь."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT DISTINCT ozon_id FROM tracked")
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    # ----------------------------------------------------- пороговые алерты

    async def set_threshold(self, user_id: int, ozon_id: int, threshold: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO alert_thresholds (user_id, ozon_id, threshold, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, ozon_id) DO UPDATE SET threshold = excluded.threshold
                """,
                (user_id, ozon_id, threshold, _now()),
            )
            await db.commit()

    async def del_threshold(self, user_id: int, ozon_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM alert_thresholds WHERE user_id = ? AND ozon_id = ?",
                (user_id, ozon_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_threshold(self, user_id: int, ozon_id: int) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT threshold FROM alert_thresholds WHERE user_id = ? AND ozon_id = ?",
                (user_id, ozon_id),
            )
            row = await cur.fetchone()
        return row[0] if row else None

    # ----------------------------------------------------------- история

    async def history(self, ozon_id: int, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM price_history WHERE ozon_id = ? ORDER BY id DESC LIMIT ?",
                (ozon_id, limit),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def cleanup_history(self, keep_days: int) -> int:
        """Удаляет историю цен старше keep_days дней, возвращает число строк."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
            timespec="seconds"
        )
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM price_history WHERE checked_at < ?", (cutoff,)
            )
            await db.commit()
            return cur.rowcount

    async def stats(self) -> dict:
        """Сводка по базе: товары, подписки, история, пороги, пользователи."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM items") as cur:
                items = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM tracked") as cur:
                tracked = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM price_history") as cur:
                history = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM alert_thresholds") as cur:
                thresholds = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM tracked"
            ) as cur:
                users = (await cur.fetchone())[0]
        return {
            "items": items, "tracked": tracked, "history": history,
            "thresholds": thresholds, "users": users,
        }
