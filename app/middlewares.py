"""Middleware диспетчера: контроль чата/темы, проверка подписки/бана, отлов ошибок."""
import logging

from aiogram.types import CallbackQuery, Message

from app.config import ADMINS, DELETION_REVIEWERS
from app.runtime_settings import get as get_setting
from app.database import is_banned, update_user_subscription_status
from app.keyboards import get_subscription_keyboard
from app.services import check_subscription
from app.validators import (
    is_valid_moderators_chat,
    is_valid_admins_chat,
    validate_chat_for_moderation,
    validate_chat_for_admin_actions,
)

logger = logging.getLogger(__name__)


class ErrorHandlingMiddleware:
    """
    Ловит необработанные исключения в хендлерах.

    Без этого один неожиданный exception (например, Telegram API вернул
    непредвиденную ошибку) просто падает в логи aiogram без ответа
    пользователю — для него бот выглядит "зависшим". Здесь мы логируем
    полный traceback и, по возможности, вежливо отвечаем.
    """
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except Exception:
            logger.exception(f"Необработанная ошибка при обработке события: {event}")
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⚠️ Произошла ошибка при обработке запроса. Попробуйте ещё раз.")
            except Exception:
                logger.exception("Не удалось уведомить пользователя об ошибке")


class ChatValidationMiddleware:
    # Callback'и админ-панели должны работать для админа ВЕЗДЕ — и в личке,
    # и если он открыл /admin прямо в группе модераторов/администраторов.
    # РАНЬШЕ это правило применялось только в приватном чате (chat.type ==
    # "private"), поэтому если админ открывал /admin внутри группы
    # модераторов, все кнопки админ-панели (включая "Стоп-слова") ошибочно
    # попадали под проверку "тема модерации" и блокировались с сообщением
    # "⚠️ Это действие доступно только в теме модерации".
    # Callback'ы административной панели не должны зависеть от того,
    # в каком чате/теме администратор нажал кнопку. В частности, раздел
    # «Стоп-слова» должен одинаково работать из лички, админ-чата и любой
    # другой темы. Динамические callback'ы (страницы, удаление слова и т.п.)
    # тоже перечислены здесь, чтобы их не перехватывала проверка тем.
    ADMIN_PANEL_CALLBACKS = (
        "blacklist", "banned_users", "pub_blacklist",
        "add_pub_blacklist", "remove_pub_blacklist",
        "remove_blacklist_word_", "banned_page_", "pubblack_page_",
        "admin_stats", "admin_ai_stats", "admin_logs",
        "pending_posts", "pending_page_", "admin_panel",
        "broadcast", "manage_subscriptions", "list_subscriptions",
        "add_channel_subscription", "add_group_subscription",
        "remove_subscription", "refresh_subscriptions",
        "admin_publish_post", "admin_reject_post", "admin_backup_now",
        "auto_phrase", "add_auto_phrase", "remove_auto_phrase",
        "settings_", "settings_intro_comment", "export_users",
        "admin_ad_post", "ad_type", "ad_abort", "ad_replace:", "ad_combo:", "ad_duration:", "ad_duration_back:", "ad_pin:",
        "ad_pin_back:", "ad_confirm:", "ad_confirm_back:", "ad_cancel:",
        "ad_subscription_back:", "ad_broadcast_back:", "ad_broadcast_confirm:",
        "ad_delete_now:", "ad_unpin_now:", "ad_retry:",
    )

    @classmethod
    def is_admin_panel_callback(cls, event: CallbackQuery) -> bool:
        data = event.data or ""
        return any(data == prefix or data.startswith(prefix)
                   for prefix in cls.ADMIN_PANEL_CALLBACKS)

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            # Администратор, работающий с админ-панелью, не должен попадать
            # под ограничения тем модераторского/админского чата. Особенно
            # важно для «Стоп-слов»: кнопка открывается из /admin и все её
            # дочерние действия должны выполняться в том же контексте.
            if event.from_user.id in ADMINS and self.is_admin_panel_callback(event):
                return await handler(event, data)

        moderators_chat_id = get_setting("MODERATORS_CHAT_ID")
        admins_chat_id = get_setting("ADMINS_CHAT_ID")
        comments_chat_id = get_setting("COMMENTS_CHAT_ID")

        if isinstance(event, Message):
            if event.chat.type in ("group", "supergroup"):
                # Чат комментариев используется только для учёта активности
                # пользователей (счётчик комментариев в профиле) — модерация
                # тем здесь не нужна, просто пропускаем сообщение дальше.
                if comments_chat_id and event.chat.id == comments_chat_id:
                    return await handler(event, data)
                if event.chat.id in (moderators_chat_id, admins_chat_id):
                    if event.chat.id == moderators_chat_id and not is_valid_moderators_chat(event):
                        logger.warning(f"Сообщение из неправильной темы группы модераторов: {event.message_thread_id}")
                        return
                    elif event.chat.id == admins_chat_id and not is_valid_admins_chat(event):
                        logger.warning(f"Сообщение из неправильной темы группы администраторов: {event.message_thread_id}")
                        return
                else:
                    return

        elif isinstance(event, CallbackQuery):
            if event.message.chat.type in ("group", "supergroup"):
                if event.message.chat.id == moderators_chat_id:
                    if not await validate_chat_for_moderation(event):
                        await event.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
                        return
                elif event.message.chat.id == admins_chat_id:
                    if not await validate_chat_for_admin_actions(event):
                        await event.answer("⚠️ Это действие доступно только в теме администраторов", show_alert=True)
                        return
                else:
                    await event.answer("⚠️ Это действие недоступно в этой группе", show_alert=True)
                    return

        return await handler(event, data)


class SubscriptionMiddleware:
    async def __call__(self, handler, event, data):
        if hasattr(event, "from_user") and (
            event.from_user.id in ADMINS or event.from_user.id in DELETION_REVIEWERS
        ):
            return await handler(event, data)

        if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.message.chat.type in ("group", "supergroup"):
            return await handler(event, data)

        user_id = event.from_user.id

        if await is_banned(user_id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Вы заблокированы.", show_alert=True)
            elif isinstance(event, Message):
                from app.handlers.unlock import unlock_keyboard
                await event.answer(
                    "🚫 Вы заблокированы в боте.",
                    reply_markup=unlock_keyboard({"bot": True, "channel": False, "comments": False}),
                )
            return

        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_subscription":
            return await handler(event, data)

        is_subscribed, unsubscribed = await check_subscription(user_id)
        unsubscribed_required = [sub for sub in unsubscribed if sub["type"] in ("channel", "group")]

        if unsubscribed_required:
            text = (
                "📢 <b>Для использования бота необходимо подписаться:</b>\n\n"
                "👇 <i>Нажмите на кнопки ниже, чтобы перейти и подписаться, затем нажмите «Я подписался»:</i>"
            )
            if isinstance(event, CallbackQuery):
                await event.answer("⚠️ Вы не подписаны на обязательные ресурсы.", show_alert=True)
                await event.message.edit_text(text, parse_mode="HTML", reply_markup=get_subscription_keyboard(unsubscribed_required))
            else:
                await event.answer(text, parse_mode="HTML", reply_markup=get_subscription_keyboard(unsubscribed_required))
            return

        if not is_subscribed:
            await update_user_subscription_status(user_id, True)

        return await handler(event, data)


def setup_middlewares(dp) -> None:
    error_middleware = ErrorHandlingMiddleware()
    chat_validation_middleware = ChatValidationMiddleware()
    subscription_middleware = SubscriptionMiddleware()

    # Порядок важен: ошибка должна ловиться "снаружи" всех остальных.
    for observer in (dp.message, dp.callback_query):
        observer.middleware(error_middleware)
        observer.middleware(chat_validation_middleware)
        observer.middleware(subscription_middleware)
