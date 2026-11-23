"""Middleware диспетчера: контроль чата/темы, проверка подписки/бана, отлов ошибок."""
import logging
import time

from aiogram.dispatcher.event.bases import CancelHandler, SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.config import DELETION_REVIEWERS
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

from app.auth import is_admin as _is_admin



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
        except (SkipHandler, CancelHandler):
            # Служебные исключения aiogram (передать событие следующему
            # обработчику / отменить) — это не ошибки.
            raise
        except TelegramBadRequest as exc:
            # Повторное нажатие той же кнопки (edit_text с тем же текстом)
            # и слишком старый callback — это не ошибки бота, пугать
            # пользователя сообщением об ошибке не нужно.
            message = str(exc).lower()
            if "message is not modified" in message or "query is too old" in message:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return None
            return await self._report(event)
        except Exception:
            return await self._report(event)

    async def _report(self, event):
        logger.exception("Необработанная ошибка при обработке события %s", type(event).__name__)
        try:
            if isinstance(event, CallbackQuery):
                await event.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⚠️ Произошла ошибка при обработке запроса. Попробуйте ещё раз.")
        except Exception:
            logger.exception("Не удалось уведомить пользователя об ошибке")


class ThrottlingMiddleware:
    """Защита от спама: не больше RATE_LIMIT_EVENTS событий за
    RATE_LIMIT_WINDOW секунд от одного пользователя в личке.

    При превышении пользователь один раз получает предупреждение, а
    дальнейшие сообщения до конца окна молча игнорируются — бот не тратит
    на флудера ни запросы к Telegram, ни обращения к БД. Админы, оплаты и
    сообщения в группах (комментарии, модерация) не ограничиваются.
    """

    def __init__(self, limit: int | None = None, window: float | None = None):
        import os
        self.limit = limit or int(os.getenv("RATE_LIMIT_EVENTS", "15") or 15)
        self.window = window or float(os.getenv("RATE_LIMIT_WINDOW", "10") or 10)
        self._hits: dict[int, list[float]] = {}
        self._warned_until: dict[int, float] = {}

    def _allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(user_id, ()) if now - t < self.window]
        hits.append(now)
        self._hits[user_id] = hits
        if len(self._hits) > 20_000:  # периодическая чистка неактивных
            for uid in [u for u, ts in self._hits.items() if not ts or now - ts[-1] > self.window]:
                self._hits.pop(uid, None)
                self._warned_until.pop(uid, None)
        return len(hits) <= self.limit

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or _is_payment_update(event) or _is_admin(user.id):
            return await handler(event, data)
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if chat is None or chat.type != "private":
            return await handler(event, data)

        if self._allowed(user.id):
            return await handler(event, data)

        now = time.monotonic()
        if self._warned_until.get(user.id, 0) <= now:
            self._warned_until[user.id] = now + self.window
            logger.info("Антиспам: пользователь %s превысил лимит", user.id)
            try:
                text = "⏳ Слишком много действий. Подождите несколько секунд."
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                else:
                    await event.answer(text)
            except Exception:
                pass
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        return None


class ChatValidationMiddleware:
    # Callback'и админ-панели должны работать для админа ВЕЗДЕ — и в личке,
    # и если он открыл /admin прямо в группе модераторов/администраторов.
    # РАНЬШЕ это правило применялось только в приватном чате (chat.type ==
    # "private"), поэтому если админ открывал /admin внутри группы
    # модераторов, все кнопки админ-панели (включая "Стоп-слова") ошибочно
    # попадали под проверку "тема модерации" и блокировались с сообщением
    # "⚠️ Это действие доступно только в теме модерации".
    ADMIN_PANEL_CALLBACKS = [
        "blacklist", "banned_users", "pub_blacklist",
        "add_pub_blacklist", "remove_pub_blacklist",
        "banned_page_", "pubblack_page_", "admin_stats",
        "pending_posts", "pending_page_", "admin_panel",
        "broadcast", "manage_subscriptions", "list_subscriptions",
        "add_channel_subscription", "add_group_subscription",
        "remove_subscription",
        "refresh_subscriptions", "admin_publish_post",
        "admin_reject_post", "admin_ad_post", "admin_backup_now",
        "auto_phrase", "settings_", "export_users",
        # Остальные кнопки админ-панели и мастера рекламы (/post) — раньше
        # они не были в списке и блокировались, если админ открыл панель
        # или /post не в личке.
        "admin_", "ad_", "remove_sub_", "remove_blacklist_word_",
        "remove_auto_phrase", "rm_auto_phrase_", "add_auto_phrase",
        "broadcast", "priority_pub_", "priority_rej_", "arejr_",
    ]

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            if event.from_user and _is_admin(event.from_user.id) and event.data:
                for cmd in self.ADMIN_PANEL_CALLBACKS:
                    if event.data.startswith(cmd):
                        return await handler(event, data)

        # Команда /post — административный мастер рекламы. Для администратора
        # она должна работать независимо от того, где команда отправлена:
        # в личке, в группе администраторов или в другой группе. Иначе
        # ChatValidationMiddleware ошибочно блокирует /post проверкой темы.
        if isinstance(event, Message) and event.from_user and _is_admin(event.from_user.id) and event.text:
            command = event.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command == "/post":
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
            if event.message is not None and event.message.chat.type in ("group", "supergroup"):
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


# Положительный результат проверки подписки кэшируется ненадолго: без этого
# на КАЖДОЕ сообщение/нажатие кнопки бот делал по запросу get_chat_member на
# каждую обязательную подписку (и запись в БД), что заметно тормозит бота и
# упирается в лимиты Telegram API при росте аудитории. Отрицательный
# результат не кэшируется — подписавшийся пользователь сразу проходит дальше.
SUBSCRIPTION_CACHE_TTL_SECONDS = 60
_subscription_ok_until: dict[int, float] = {}


def invalidate_subscription_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _subscription_ok_until.clear()
    else:
        _subscription_ok_until.pop(user_id, None)


def _is_payment_update(event) -> bool:
    return isinstance(event, Message) and event.successful_payment is not None


def _is_unlock_callback(event) -> bool:
    return isinstance(event, CallbackQuery) and bool(event.data) and event.data.startswith("unlock_")


class SubscriptionMiddleware:
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        # Подтверждение оплаты обрабатывается ВСЕГДА: деньги уже списаны.
        # Раньше successful_payment от заблокированного пользователя (а он
        # как раз платит за разблокировку!) или от отписавшегося отбрасывался
        # этим middleware — звёзды списывались, а услуга не применялась.
        if _is_payment_update(event):
            return await handler(event, data)

        if _is_admin(user.id) or user.id in DELETION_REVIEWERS:
            return await handler(event, data)

        if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
            return await handler(event, data)

        if (
            isinstance(event, CallbackQuery)
            and event.message is not None
            and event.message.chat.type in ("group", "supergroup")
        ):
            return await handler(event, data)

        user_id = user.id

        # Кнопки «🔓 Разблокировать …» должны работать именно у
        # заблокированных пользователей (и у забаненных в канале, которые
        # физически не могут выполнить обязательную подписку на него).
        if _is_unlock_callback(event):
            return await handler(event, data)

        is_start = isinstance(event, Message) and bool(event.text) and event.text.startswith("/start")

        if await is_banned(user_id):
            # /start сам показывает причину блокировки и кнопки разблокировки.
            if is_start:
                return await handler(event, data)
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Вы заблокированы.", show_alert=True)
            elif isinstance(event, Message):
                from app.handlers.unlock import unlock_keyboard
                await event.answer(
                    "🚫 Вы заблокированы в боте.",
                    reply_markup=unlock_keyboard({"bot": True, "channel": False, "comments": False}),
                )
            return

        if is_start:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_subscription":
            invalidate_subscription_cache(user_id)
            return await handler(event, data)

        if _subscription_ok_until.get(user_id, 0) > time.monotonic():
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
                if event.message is not None:
                    try:
                        await event.message.edit_text(text, parse_mode="HTML", reply_markup=get_subscription_keyboard(unsubscribed_required))
                    except Exception:
                        await event.message.answer(text, parse_mode="HTML", reply_markup=get_subscription_keyboard(unsubscribed_required))
            else:
                await event.answer(text, parse_mode="HTML", reply_markup=get_subscription_keyboard(unsubscribed_required))
            return

        _subscription_ok_until[user_id] = time.monotonic() + SUBSCRIPTION_CACHE_TTL_SECONDS
        if len(_subscription_ok_until) > 50_000:
            now = time.monotonic()
            for uid in [u for u, t in _subscription_ok_until.items() if t <= now]:
                _subscription_ok_until.pop(uid, None)

        if not is_subscribed:
            await update_user_subscription_status(user_id, True)

        return await handler(event, data)


def setup_middlewares(dp) -> None:
    error_middleware = ErrorHandlingMiddleware()
    throttling_middleware = ThrottlingMiddleware()
    chat_validation_middleware = ChatValidationMiddleware()
    subscription_middleware = SubscriptionMiddleware()

    # Порядок важен: ошибка должна ловиться "снаружи" всех остальных.
    for observer in (dp.message, dp.callback_query):
        observer.middleware(error_middleware)
        observer.middleware(throttling_middleware)
        observer.middleware(chat_validation_middleware)
        observer.middleware(subscription_middleware)
