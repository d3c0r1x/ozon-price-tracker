"""Интеграционный тест полного сценария Ozon-трекера.

В отличие от unit-тестов, здесь апдейты Telegram прогоняются через НАСТОЯЩИЙ
Dispatcher: router, middleware, хендлеры и БД бота (тот же код, что и в bot.py).
Исходящие вызовы Bot API перехватываются CapturingSession — сеть не нужна,
тест детерминирован (демо-клиент MockOzonClient).

Сценарий: /start → /track по ссылке → /list → /alert → /history → /stats.
Плюс проверка /untrack, /delalert, /check, /diag и ошибок формата команд.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import bot as botmod
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update, User
from db import Database
from middlewares import LoggingMiddleware, ThrottlingMiddleware
from ozon_api import MockOzonClient
from utils import TTLCache

USER_ID = 777
USERNAME = "live_tester"
FAKE_TOKEN = "12345:test-only-no-network"
OZON_ID = 1500516648
OZON_URL = f"https://www.ozon.ru/product/smartfon-{OZON_ID}/"


class CapturingSession(BaseSession):
    """Перехватывает исходящие вызовы Bot API и записывает их в self.calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None
    ):
        data = method.model_dump(exclude_none=True)
        self.calls.append({"method": type(method).__name__, "data": data})
        return _fake_result(method, data)

    async def close(self) -> None:
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


def _fake_result(method: TelegramMethod, data: dict):
    name = type(method).__name__
    if name == "SendMessage":
        return Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=USER_ID, type="private"),
            text=data.get("text", ""),
        )
    return True


def _user() -> User:
    return User(id=USER_ID, is_bot=False, first_name="Live", username=USERNAME)


def _chat() -> Chat:
    return Chat(id=USER_ID, type="private")


def _msg_update(text: str, message_id: int, update_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=message_id,
            date=datetime.now(),
            chat=_chat(),
            from_user=_user(),
            text=text,
        ),
    )


def _send_texts(session: CapturingSession) -> list[str]:
    return [c["data"].get("text", "") for c in session.calls if c["method"] == "SendMessage"]


# Роутер бота можно прикрепить только к ОДНОМУ Dispatcher'у (aiogram кидает
# RuntimeError при повторном include_router), поэтому Dispatcher создаётся один
# раз на весь тестовый модуль и переиспользуется во всех сценариях.
DP = Dispatcher()
DP.include_router(botmod.router)
# interval=0: в тесте апдейты идут без задержек, троттлинг не должен их дропать
DP.message.middleware(ThrottlingMiddleware(min_interval=0.0))
DP.update.middleware(LoggingMiddleware())


def _make_bot(session: CapturingSession) -> Bot:
    return Bot(
        token=FAKE_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )


def _reset_state(db_path: str) -> None:
    """Свежая БД, чистый кэш, демо-клиент Ozon (изоляция между тестами)."""
    botmod.ozon = MockOzonClient()
    botmod.product_cache = TTLCache(ttl_seconds=300.0)
    botmod.db = Database(db_path)


# ---------------------------------------------------------------- сценарии

def test_track_list_alert_history_flow(tmp_path) -> None:
    """/start → /track по ссылке → /list → /alert → /history → /stats + БД."""
    db_path = str(tmp_path / "ozon.db")

    async def run() -> None:
        _reset_state(db_path)
        await botmod.db.init()
        session = CapturingSession()
        bot = _make_bot(session)
        dp = DP

        upd = mid = 0
        mock_product = await MockOzonClient().get_product(OZON_ID)

        # /start — приветствие со списком команд
        await dp.feed_update(bot, _msg_update("/start", mid := mid + 1, upd := upd + 1))
        assert any("Ozon Price &amp; Stock Tracker" in t for t in _send_texts(session))
        assert any("/track" in t and "/alert" in t for t in _send_texts(session))

        # /track по ссылке — карточка товара из mock-клиента
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/track {OZON_URL}", mid := mid + 1, upd := upd + 1))
        track = _send_texts(session)
        assert any("Товар добавлен в отслеживание" in t for t in track)
        assert any(f"<code>{OZON_ID}</code>" in t for t in track)
        assert any(f"{mock_product['price']} ₽" in t for t in track)      # цена из mock
        assert any(f"Остаток: {mock_product['stock']} шт." in t for t in track)

        # повторный /track того же товара — «уже в вашем списке»
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/track {OZON_ID}", mid := mid + 1, upd := upd + 1))
        dup = _send_texts(session)
        assert any("уже в вашем списке" in t for t in dup)

        # /list — товар в списке
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/list", mid := mid + 1, upd := upd + 1))
        lst = _send_texts(session)
        assert any("Отслеживаемые товары" in t and str(OZON_ID) in t for t in lst)

        # /alert ID 5000 — персональный порог
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/alert {OZON_ID} 5000", mid := mid + 1, upd := upd + 1))
        alert = _send_texts(session)
        assert any("Уведомлю, когда цена товара" in t and "5000 ₽" in t for t in alert)

        # /history ID — одна запись истории
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/history {OZON_ID}", mid := mid + 1, upd := upd + 1))
        hist = _send_texts(session)
        assert any("История цен" in t and str(OZON_ID) in t for t in hist)
        assert any(f"<b>{mock_product['price']} ₽</b>" in t for t in hist)  # «время — цена, остаток»

        # /stats — сводка
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/stats", mid := mid + 1, upd := upd + 1))
        stats = _send_texts(session)
        assert any("Сводка по базе" in t and "Товаров: 1" in t for t in stats)

        # БД: товар, подписка, порог, история записаны
        item = await botmod.db.get_item(OZON_ID)
        assert item is not None and item["price"] == mock_product["price"]
        assert await botmod.db.users_for_product(OZON_ID) == [USER_ID]
        assert await botmod.db.get_threshold(USER_ID, OZON_ID) == 5000
        assert len(await botmod.db.history(OZON_ID)) == 1

        await bot.session.close()

    asyncio.run(run())


def test_untrack_delalert_check_diag(tmp_path) -> None:
    """/untrack, /delalert, /check без изменений, /diag и повторный /track."""
    db_path = str(tmp_path / "ozon.db")

    async def run() -> None:
        _reset_state(db_path)
        await botmod.db.init()
        session = CapturingSession()
        bot = _make_bot(session)
        dp = DP

        upd = mid = 0
        await dp.feed_update(bot, _msg_update(f"/track {OZON_ID}", mid := mid + 1, upd := upd + 1))
        await dp.feed_update(bot, _msg_update(f"/alert {OZON_ID} 5000", mid := mid + 1, upd := upd + 1))

        # /check — цена не менялась → изменений 0
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/check", mid := mid + 1, upd := upd + 1))
        chk = _send_texts(session)
        assert any("Проверяю цены" in t for t in chk)
        assert any("Изменений: 0" in t for t in chk)

        # /diag — диагностика демо-режима
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/diag", mid := mid + 1, upd := upd + 1))
        assert any("Диагностика Ozon" in t and "demo-mode" in t for t in _send_texts(session))

        # /delalert — порог удалён
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/delalert {OZON_ID}", mid := mid + 1, upd := upd + 1))
        assert any("Порог для" in t and "удалён." in t for t in _send_texts(session))
        assert await botmod.db.get_threshold(USER_ID, OZON_ID) is None

        # /delalert ещё раз — «не был задан»
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/delalert {OZON_ID}", mid := mid + 1, upd := upd + 1))
        assert any("не был задан" in t for t in _send_texts(session))

        # /untrack — удаление, повтор — «не был в вашем списке»
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/untrack {OZON_ID}", mid := mid + 1, upd := upd + 1))
        assert any("удалён из отслеживания" in t for t in _send_texts(session))
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/untrack {OZON_ID}", mid := mid + 1, upd := upd + 1))
        assert any("не был в вашем списке" in t for t in _send_texts(session))
        assert await botmod.db.users_for_product(OZON_ID) == []

        await bot.session.close()

    asyncio.run(run())


def test_command_errors_and_empty_states(tmp_path) -> None:
    """Ошибки формата команд и пустые состояния."""
    db_path = str(tmp_path / "ozon.db")

    async def run() -> None:
        _reset_state(db_path)
        await botmod.db.init()
        session = CapturingSession()
        bot = _make_bot(session)
        dp = DP

        upd = mid = 0

        # /track без аргумента
        await dp.feed_update(bot, _msg_update("/track", mid := mid + 1, upd := upd + 1))
        assert any("Формат: /track" in t for t in _send_texts(session))

        # /track с мусором — не ссылка
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/track совсем не ссылка", mid := mid + 1, upd := upd + 1))
        assert any("Формат: /track" in t for t in _send_texts(session))

        # /alert с плохим форматом
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/alert 123", mid := mid + 1, upd := upd + 1))
        assert any("Формат: /alert" in t for t in _send_texts(session))

        # /list без товаров — подсказка
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/list", mid := mid + 1, upd := upd + 1))
        assert any("У вас пока нет отслеживаемых товаров" in t for t in _send_texts(session))

        # /history без товара — «пуста»
        session.calls.clear()
        await dp.feed_update(bot, _msg_update(f"/history {OZON_ID}", mid := mid + 1, upd := upd + 1))
        assert any("История для" in t and "пуста" in t for t in _send_texts(session))

        # /check без товаров
        session.calls.clear()
        await dp.feed_update(bot, _msg_update("/check", mid := mid + 1, upd := upd + 1))
        assert any("нет отслеживаемых товаров" in t for t in _send_texts(session))

        await bot.session.close()

    asyncio.run(run())
