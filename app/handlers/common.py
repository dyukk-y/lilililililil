"""Общие команды и «страховочные» обработчики. Роутер подключается
ПОСЛЕДНИМ: сюда попадает только то, что не обработал никто другой."""
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.keyboards import main_menu
from app.utils import main_menu_text

logger = logging.getLogger(__name__)
# commands_router подключается ПЕРВЫМ — иначе внутри сценария (например,
# при вводе текста поста) /cancel воспринимался как текст поста.
commands_router = Router()
# router подключается ПОСЛЕДНИМ — «страховочные» обработчики.
router = Router()

HELP_TEXT = (
    "ℹ️ <b>Как пользоваться ботом</b>\n\n"
    "✍️ <b>Предложить пост</b> — текст или фото с подписью. Обязательно "
    "добавьте 🧑 или 👩. Перед отправкой бот покажет предпросмотр.\n"
    "⚡ <b>Ускорить проверку</b> — кнопка под сообщением об отправке.\n"
    "📌 <b>Закрепить пост</b> — кнопка в уведомлении о публикации.\n"
    "🗑 <b>Удалить запись</b> и 🔎 <b>узнать автора</b> — в разделе «Помощь».\n\n"
    "Команды: /start — меню, /cancel — отменить текущее действие."
)


@commands_router.message(Command("cancel"), F.chat.type == "private")
async def cancel_command(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current and current.startswith("AdvertisingState"):
        # У мастера рекламы своя отмена (помечает черновик отменённым).
        raise SkipHandler()
    had_state = current is not None
    await state.clear()
    await msg.answer(
        ("❌ Действие отменено.\n\n" if had_state else "") + main_menu_text(msg.from_user.first_name),
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@commands_router.message(Command("help"), F.chat.type == "private")
async def help_command(msg: Message):
    await msg.answer(HELP_TEXT, parse_mode="HTML", reply_markup=main_menu())


@router.message(F.chat.type == "private", StateFilter(None))
async def fallback_message(msg: Message):
    """Сообщение вне сценария — раньше бот молчал, и казалось, что он
    завис. Теперь подсказываем, что делать."""
    if msg.successful_payment:
        return
    await msg.answer(
        "🤔 Не совсем понял. Чтобы предложить пост, нажмите «✍️ Предложить пост».",
        reply_markup=main_menu(),
    )


@router.callback_query()
async def fallback_callback(cb: CallbackQuery):
    """Кнопка без обработчика (например, из очень старого сообщения) —
    раньше она бесконечно «крутилась»."""
    logger.info("Необработанная кнопка: %r", cb.data)
    await cb.answer("⌛ Эта кнопка устарела. Откройте меню заново: /start", show_alert=True)
