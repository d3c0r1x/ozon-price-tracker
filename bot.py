"""Telegram-бот "Ozon Price & Stock Tracker".

Стек: aiogram v3, httpx, curl_cffi (имитация Chrome), aiosqlite, apscheduler.

Функции:
  - /track URL_или_ID — начать отслеживание товара Ozon;
  - /list, /untrack, /history — управление списком и историей цен;
  - /alert ID ЦЕНА — персональный порог «уведомить, если цена <= N ₽»;
  - /delalert ID — удалить порог;
  - /check — принудительная проверка всех отслеживаемых товаров;
  - /diag — диагностика доступа к API Ozon; /stats — сводка по базе;
  - /cleanup ДНИ — очистка истории старше N дней (админ).

Планировщик (apscheduler) периодически проверяет цены и шлёт уведомления:
цена упала, достигнут порог, товар снова в наличии, товар заканчивается.
Кулдаун уведомлений по одному товару — OZON_ALERT_COOLDOWN_HOURS.

Запуск:  python bot.py   (предварительно задайте OZON_BOT_TOKEN)
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from alerts import should_notify
from db import Database
from middlewares import LoggingMiddleware, ThrottlingMiddleware
from ozon_api import MockOzonClient, OzonClient, parse_product_url
from utils import TTLCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(config.BASE_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

router = Router()
db = Database(config.DB_PATH)
ozon: OzonClient | MockOzonClient | None = None
bot: Bot | None = None
product_cache = TTLCache(ttl_seconds=config.CACHE_TTL_SECONDS)


# ---------------------------------------------------------------- команды

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот <b>Ozon Price &amp; Stock Tracker</b>.\n\n"
        "Команды:\n"
        "• /track <b>ССЫЛКА или ID</b> — отслеживать товар Ozon\n"
        "• /list — мои товары\n"
        "• /untrack <b>ID</b> — удалить из отслеживания\n"
        "• /history <b>ID</b> — история цен\n"
        "• /alert <b>ID ЦЕНА</b> — уведомить, когда цена опустится до N ₽\n"
        "• /delalert <b>ID</b> — удалить порог\n"
        "• /check — проверить цены прямо сейчас\n"
        "• /diag — диагностика доступа к API Ozon\n"
        "• /stats — сводка по базе\n"
        "• /cleanup <b>ДНИ</b> — очистить историю старше N дней (админ)\n\n"
        "Бот периодически проверяет цены и пришлёт уведомление, если цена упала, "
        "выросла, товар снова в наличии, заканчивается или достигнут порог."
    )


@router.message(Command("track"))
async def cmd_track(message: Message) -> None:
    ozon_id = parse_product_url((message.text or "").replace("/track", "", 1))
    if ozon_id is None:
        await message.answer("Формат: /track ССЫЛКА_ИЛИ_ID (например, /track 1500516648)")
        return
    try:
        product = await product_cache.get_or_set(ozon_id, lambda: _fetch_product(ozon_id))
    except Exception as exc:  # сеть, антибот и т.п.
        await message.answer(f"⚠️ Не удалось получить данные Ozon: {exc}")
        return
    if product is None:
        await message.answer(f"Товар Ozon с ID <b>{ozon_id}</b> не найден.")
        return
    await db.upsert_item(product)
    await db.track(message.from_user.id, ozon_id)
    price = int(product.get("price") or 0)
    old_price = int(product.get("old_price") or 0)
    price_txt = f"<s>{old_price} ₽</s> <b>{price} ₽</b>" if old_price else f"<b>{price} ₽</b>"
    await message.answer(
        "✅ Товар добавлен в отслеживание:\n"
        f"<b>{_html.escape(str(product.get('title')), quote=False)}</b>\n"
        f"ID: <code>{ozon_id}</code>\n"
        f"Цена: {price_txt}\n"
        f"Остаток: {product.get('stock', 0)} шт.\n"
        f"Рейтинг: {product.get('rating') or '—'}"
    )


async def _fetch_product(ozon_id: int):
    """Обёртка для TTL-кэша: обращается к клиенту Ozon (mock или реальному)."""
    return await ozon.get_product(ozon_id)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    items = await db.list_tracked(message.from_user.id)
    if not items:
        await message.answer("У вас пока нет отслеживаемых товаров. /track ССЫЛКА_ИЛИ_ID")
        return
    lines = []
    for i in items:
        price = i["price"] or 0
        stock = i["stock"] or 0
        stock_txt = "нет в наличии" if stock == 0 else f"остаток {stock} шт."
        lines.append(
            f"• <code>{i['ozon_id']}</code> — {_html.escape(i['title'][:40], quote=False)}: "
            f"<b>{price} ₽</b>, {stock_txt}"
        )
    await message.answer("📦 <b>Отслеживаемые товары:</b>\n" + "\n".join(lines))


@router.message(Command("untrack"))
async def cmd_untrack(message: Message) -> None:
    ozon_id = parse_product_url((message.text or "").replace("/untrack", "", 1))
    if ozon_id is None:
        await message.answer("Формат: /untrack ID")
        return
    removed = await db.untrack(message.from_user.id, ozon_id)
    if removed:
        await message.answer(f"Товар <code>{ozon_id}</code> удалён из отслеживания.")
    else:
        await message.answer(f"Товар <code>{ozon_id}</code> не был в вашем списке.")


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    ozon_id = parse_product_url((message.text or "").replace("/history", "", 1))
    if ozon_id is None:
        await message.answer("Формат: /history ID")
        return
    rows = await db.history(ozon_id, limit=15)
    if not rows:
        await message.answer(f"История для <code>{ozon_id}</code> пуста.")
        return
    lines = [
        f"{r['checked_at'][:16]} — <b>{r['price']} ₽</b>"
        + (f", остаток {r['stock']} шт." if r["stock"] else ", нет в наличии")
        for r in rows
    ]
    await message.answer(f"📈 <b>История цен</b> (ID <code>{ozon_id}</code>):\n" + "\n".join(lines))


@router.message(Command("alert"))
async def cmd_alert(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].isdigit():
        await message.answer("Формат: /alert ID ЦЕНА (например, /alert 1500516648 5000)")
        return
    ozon_id = parse_product_url(parts[1])
    if ozon_id is None:
        await message.answer("Не удалось распознать ID товара.")
        return
    threshold = int(parts[2])
    await db.set_threshold(message.from_user.id, ozon_id, threshold)
    await message.answer(f"✅ Уведомлю, когда цена товара <code>{ozon_id}</code> опустится до {threshold} ₽ или ниже.")


@router.message(Command("delalert"))
async def cmd_delalert(message: Message) -> None:
    ozon_id = parse_product_url((message.text or "").replace("/delalert", "", 1))
    if ozon_id is None:
        await message.answer("Формат: /delalert ID")
        return
    removed = await db.del_threshold(message.from_user.id, ozon_id)
    if removed:
        await message.answer(f"Порог для <code>{ozon_id}</code> удалён.")
    else:
        await message.answer(f"Порог для <code>{ozon_id}</code> не был задан.")


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    """Принудительная проверка только товаров пользователя."""
    items = await db.list_tracked(message.from_user.id)
    if not items:
        await message.answer("У вас пока нет отслеживаемых товаров.")
        return
    await message.answer("🔍 Проверяю цены…")
    changed = 0
    for item in items:
        ozon_id = item["ozon_id"]
        product = await _fetch_product(ozon_id)
        if product is None:
            continue
        await db.upsert_item(product)
        item_row = await db.get_item(ozon_id)
        threshold = await db.get_threshold(message.from_user.id, ozon_id)
        notify, msgs = should_notify(
            int(product["price"] or 0), int(product["stock"] or 0),
            last_price=item_row["last_price"], last_stock=item_row["last_stock"],
            last_alert_at=item_row["last_alert_at"], threshold=threshold,
            cooldown_hours=config.ALERT_COOLDOWN_HOURS,
            low_stock_threshold=config.LOW_STOCK_THRESHOLD,
        )
        if notify:
            await db.update_last_notified(ozon_id, int(product["price"] or 0), int(product["stock"] or 0))
            await message.answer(
                f"<b>{_html.escape(str(product['title'])[:60], quote=False)}</b>\n"
                + "\n".join(msgs)
            )
            changed += 1
    await message.answer(f"Проверка завершена. Изменений: {changed}.")


@router.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    results = await ozon.diagnose()
    lines = [f"• <b>{name}</b>: {status}" for name, status in results]
    await message.answer("🩺 <b>Диагностика Ozon:</b>\n" + "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    s = await db.stats()
    await message.answer(
        "📊 <b>Сводка по базе:</b>\n"
        f"• Товаров: {s['items']}\n"
        f"• Подписок: {s['tracked']}\n"
        f"• Записей истории: {s['history']}\n"
        f"• Порогов: {s['thresholds']}\n"
        f"• Пользователей: {s['users']}"
    )


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    if config.ADMIN_IDS and message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ Команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else config.HISTORY_KEEP_DAYS
    deleted = await db.cleanup_history(days)
    await message.answer(f"🧹 Удалено записей истории старше {days} дн.: {deleted}.")


# ----------------------------------------------------- фоновая проверка цен

async def scheduled_check() -> None:
    """Периодическая проверка всех отслеживаемых товаров и рассылка алертов."""
    ids = await db.all_tracked_ids()
    if not ids:
        return
    logger.info("Плановая проверка: %d товаров", len(ids))
    for ozon_id in ids:
        try:
            product = await ozon.get_product(ozon_id)
        except Exception as exc:
            logger.warning("Ошибка проверки %s: %s", ozon_id, exc)
            continue
        if product is None:
            continue
        await db.upsert_item(product)
        item = await db.get_item(ozon_id)
        price = int(product["price"] or 0)
        stock = int(product["stock"] or 0)
        for user_id in await db.users_for_product(ozon_id):
            threshold = await db.get_threshold(user_id, ozon_id)
            notify, msgs = should_notify(
                price, stock,
                last_price=item["last_price"], last_stock=item["last_stock"],
                last_alert_at=item["last_alert_at"], threshold=threshold,
                cooldown_hours=config.ALERT_COOLDOWN_HOURS,
                low_stock_threshold=config.LOW_STOCK_THRESHOLD,
            )
            if notify:
                await db.update_last_notified(ozon_id, price, stock)
                try:
                    await bot.send_message(
                        user_id,
                        f"<b>{_html.escape(str(product['title'])[:60], quote=False)}</b>\n"
                        + "\n".join(msgs),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as exc:
                    logger.warning("Не удалось отправить уведомление %s: %s", user_id, exc)


# ------------------------------------------------------------------- main

async def main() -> None:
    global ozon, bot
    if not config.BOT_TOKEN:
        logger.error("OZON_BOT_TOKEN не задан (см. .env.example)")
        return
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(ThrottlingMiddleware(min_interval=config.THROTTLE_MIN_INTERVAL))
    dp.message.middleware(LoggingMiddleware())
    dp.include_router(router)

    await db.init()

    if config.DEMO_MODE:
        ozon = MockOzonClient()
        logger.info("Ozon-клиент: демо-режим (данные выдуманные)")
    else:
        ozon = OzonClient()
        logger.info("Ozon-клиент: реальный composer-api (транспорт %s)", config.HTTP_CLIENT)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_check,
        "interval",
        minutes=config.CHECK_INTERVAL_MINUTES,
        id="ozon_check",
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Бот запущен. Демо-режим: %s. Проверка каждые %d мин. Кулдаун алертов: %.1f ч",
        config.DEMO_MODE, config.CHECK_INTERVAL_MINUTES, config.ALERT_COOLDOWN_HOURS,
    )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await ozon.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
