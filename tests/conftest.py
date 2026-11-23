"""Общая подготовка тестов.

Переменные окружения выставляются ДО импорта приложения: app.config читает
их при импорте. Сеть не используется — Bot получает фейковую сессию,
которая записывает все запросы к Telegram API и отвечает правдоподобными
объектами.
"""
import itertools
import os
import sys
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio

_TMP = tempfile.mkdtemp(prefix="smotrbot_tests_")
os.environ.update({
    "BOT_TOKEN": "123456789:TEST_token_abcdefghijklmnopqrstuvwxyz",
    "MAIN_CHANNEL_ID": "-1001000000001",
    "MODERATORS_CHAT_ID": "-1001000000002",
    "ADMINS_CHAT_ID": "-1001000000003",
    "COMMENTS_CHAT_ID": "-1001000000004",
    "ADMINS": "1001",
    "SUPER_ADMINS": "1001",
    "DELETION_REVIEWERS": "1002",
    "DB_NAME": os.path.join(_TMP, "test.db"),
    "BACKUP_DIR": os.path.join(_TMP, "backups"),
    "LOG_FILE": os.path.join(_TMP, "bot.log"),
    "DONATION_TOPIC_ID": "0",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import TelegramMethod  # noqa: E402
from aiogram.types import Chat, Message, User  # noqa: E402

ADMIN_ID = 1001
USER_ID = 2001
_msg_ids = itertools.count(100)


class FakeSession(BaseSession):
    """Записывает вызовы API и возвращает минимально правдоподобный ответ."""

    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.chat_member_status: dict[tuple[int, int], str] = {}
        self.fail_methods: dict[str, Exception] = {}

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name in self.fail_methods:
            raise self.fail_methods[name]
        returning = method.__returning__
        if name in ("SendMessage", "SendPhoto", "CopyMessage", "SendInvoice", "EditMessageText",
                    "EditMessageCaption"):
            chat_id = getattr(method, "chat_id", None) or 1
            if name == "CopyMessage":
                from aiogram.types import MessageId
                return MessageId(message_id=next(_msg_ids))
            return Message(
                message_id=next(_msg_ids), date=datetime.now(),
                chat=Chat(id=int(chat_id), type="private" if int(chat_id) > 0 else "supergroup"),
                text=getattr(method, "text", None), caption=getattr(method, "caption", None),
            )
        if name == "GetChatMember":
            from aiogram.types import ChatMemberMember, ChatMemberBanned
            status = self.chat_member_status.get((int(method.chat_id), int(method.user_id)), "member")
            user = User(id=int(method.user_id), is_bot=False, first_name="U")
            if status == "kicked":
                return ChatMemberBanned(user=user, until_date=0)
            return ChatMemberMember(user=user)
        if name == "GetMe":
            return User(id=123456789, is_bot=True, first_name="Bot", username="test_bot")
        if returning is bool or name.startswith(("Answer", "Delete", "Edit", "Pin", "Unpin", "Unban")):
            return True
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    def names(self) -> list[str]:
        return [type(c).__name__ for c in self.calls]

    def of(self, name: str) -> list:
        return [c for c in self.calls if type(c).__name__ == name]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def app_ready():
    import main  # noqa: F401  (регистрирует роутеры)
    from app.database import init_db
    from app.runtime_settings import load_settings
    from app.middlewares import setup_middlewares
    from app.loader import dp

    await init_db()
    await load_settings()
    setup_middlewares(dp)
    for router in main.ROUTERS:
        dp.include_router(router)
    return dp


@pytest.fixture()
def fake(app_ready):
    from app.loader import bot
    from app.middlewares import invalidate_subscription_cache
    session = FakeSession()
    bot.session = session
    invalidate_subscription_cache()
    return session
