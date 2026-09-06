"""
Сервисные функции, которым для работы нужен объект Bot (проверка подписки
через Telegram API, публикация в канал, отправка постов на ревью, удаление
постов по заявкам, поиск автора, приоритетное ускорение и т.д.). Отделены
от app.database, где лежат "чистые" операции с БД.
"""
import asyncio
from app.auth import is_admin as _is_admin
import logging
import re
from html import escape
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import aiosqlite

from app.config import DB_NAME, REQUIRED_SUBSCRIPTIONS, TIMEZONE, ADMINS, DELETION_REVIEWERS, LOCAL_AI_REQUIRED
from app.runtime_settings import get as get_setting
from app.loader import bot
from app.decision_engine import evaluate
from app.media_analysis import analyze_media, phash_distance
from app.database import (
    get_post_by_id, update_post_message_ids, try_finalize_post, try_finalize_post_revert,
    claim_post_for_publishing, finish_publishing, fail_publishing, get_stuck_publishing_posts, get_stuck_intro_comments,
    log, approve_and_schedule, increment_mention_count, get_orphaned_moderation_posts,
    get_stuck_manual_review_posts, set_post_auto_status, set_channel_message_id,
    get_post_channel_message_id, mark_post_deleted, create_deletion_request,
    get_deletion_request, claim_deletion_request, reopen_deletion_request, get_expired_deletion_requests,
    set_deletion_request_admin_refs, get_username_by_user_id, has_auto_approve_trigger,
    set_post_review_deadline, get_expired_manual_review_posts, get_total_pending_count,
    get_post_by_channel_message_id, mark_intro_comment_posted, finish_intro_comment, reset_intro_comment_claim,
    record_ai_feedback, record_ai_correction, adjust_user_trust, get_ai_stats,
    get_user_trust_score, get_recent_photo_hashes, set_ai_analysis,
    get_active_advertising_subscriptions, expire_advertising_subscriptions,
)
from app.keyboards import (
    moderation_keyboard, deletion_reviewer_keyboard, deletion_resolved_keyboard,
    intro_comment_keyboard, disabled_moderation_keyboard,
)
from app.scheduler import pick_schedule_slot, rebalance_daily_queue
from app.moderation_photo import check_photo_nsfw, photo_detector_available

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")
_TME_PUBLIC_RE = re.compile(r"t\.me/([A-Za-z0-9_]+)/(\d+)")
_TME_PRIVATE_RE = re.compile(r"t\.me/c/(\d+)/(\d+)")

_channel_username_cache: Optional[str] = None
_bot_username_cache: Optional[str] = None


async def check_subscription(user_id: int) -> Tuple[bool, List[Dict[str, Any]]]:
    """Проверяет подписку пользователя на обязательные каналы и группы
    (работает и с открытыми, и с закрытыми — закрытые тоже проверяются
    через get_chat_member, для этого боту достаточно быть их участником/админом)."""
    unsubscribed = []
    # Временные рекламные подписки живут отдельно от постоянных и
    # автоматически исчезают после оплаченного срока.
    try:
        await expire_advertising_subscriptions()
        temporary_subs = await get_active_advertising_subscriptions()
    except Exception:
        logger.exception("Не удалось загрузить временные рекламные подписки")
        temporary_subs = []

    subscriptions = list(REQUIRED_SUBSCRIPTIONS) + temporary_subs
    seen = set()
    for sub in subscriptions:
        key = (sub.get("type"), str(sub.get("id")))
        if key in seen:
            continue
        seen.add(key)
        # Telegram Bot API не предоставляет способ проверить,
        # подписан ли пользователь на другого бота. Такой объект
        # показывается как обязательный переход, но не блокирует
        # пользователя навсегда. Каналы/группы проверяются реально.
        if sub.get("type") == "bot":
            continue

        try:
            chat_id = int(sub["id"])
            try:
                chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if chat_member.status in ("member", "administrator", "creator"):
                    continue
                unsubscribed.append(sub)
            except TelegramForbiddenError:
                logger.error(f"Бот не имеет прав для проверки {sub['type']} {sub['name']} (ID: {chat_id})")
                unsubscribed.append(sub)
            except Exception as e:
                logger.error(f"Ошибка при проверке подписки на {sub['type']} {sub['id']}: {e}")
                unsubscribed.append(sub)
        except ValueError:
            try:
                chat = await bot.get_chat(chat_id=sub["id"])
                chat_member = await bot.get_chat_member(chat_id=chat.id, user_id=user_id)
                if chat_member.status in ("member", "administrator", "creator"):
                    continue
                unsubscribed.append(sub)
            except Exception as e:
                logger.error(f"Ошибка при проверке подписки на {sub['type']} {sub['id']}: {e}")
                unsubscribed.append(sub)

    return len(unsubscribed) == 0, unsubscribed


async def notify_admins_outcome(post_id: int, status: str, reason: Optional[str] = None) -> None:
    """Отправляет администраторам ГОТОВЫЙ результат — пост уже опубликован
    или отклонён. Админы больше не видят пост в ожидании решения (это
    только у модераторов, см. send_post_for_review) — сюда попадает только
    финальный итог, с полной информацией об авторе и кнопкой "кто это сделал"."""
    try:
        admins_chat_id = get_setting("ADMINS_CHAT_ID")
        if not admins_chat_id:
            return

        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("""
                SELECT p.text, p.photo, p.user_id, u.username, p.moderator_id
                FROM posts p
                LEFT JOIN users u ON p.user_id = u.user_id
                WHERE p.id=?
            """, (post_id,))
            row = await cur.fetchone()

            if not row:
                return

            text, photo, user_id, username, moderator_id = row

            mod_username = None
            if moderator_id == 0:
                mod_username = "🤖 автоматически (по расписанию)"
            elif moderator_id:
                cur2 = await db.execute("SELECT username FROM users WHERE user_id=?", (moderator_id,))
                mod_row = await cur2.fetchone()
                mod_username = f"@{mod_row[0]}" if mod_row and mod_row[0] else "неизвестно"

            if status == "published":
                header = f"✅ <b>Пост #{post_id} опубликован</b>"
                action_text = "👤 <b>Опубликовал:</b>"
                button_text = "👤 Кто опубликовал"
                callback_data = f"who_pub_{post_id}"
            elif status == "rejected":
                header = f"❌ <b>Пост #{post_id} отклонён</b>"
                action_text = "👤 <b>Отклонил:</b>"
                button_text = "👤 Кто отклонил"
                callback_data = f"who_rej_{post_id}"
            else:
                return

            admin_text = (
                f"{header}\n\n"
                f"📄 <b>Текст:</b>\n{escape(text or '')}\n\n"
                f"👤 <b>Автор:</b> @{escape(username or 'без username')}\n"
                f"🆔 <b>ID автора:</b> <code>{user_id}</code>\n"
                f"{action_text} {mod_username or 'неизвестно'}"
            )

            if status == "rejected" and reason:
                admin_text += f"\n📝 <b>Причина:</b> {reason}"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=button_text, callback_data=callback_data)],
                [InlineKeyboardButton(text="🤖 ИИ ошибся", callback_data=f"ai_error_{post_id}")],
            ])
            topic_id = get_setting("ADMINS_TOPIC_ID") or None

            try:
                if photo:
                    await bot.send_photo(
                        chat_id=admins_chat_id, message_thread_id=topic_id, photo=photo,
                        caption=admin_text, parse_mode="HTML", reply_markup=kb,
                    )
                else:
                    await bot.send_message(
                        chat_id=admins_chat_id, message_thread_id=topic_id,
                        text=admin_text, parse_mode="HTML", reply_markup=kb,
                    )
            except Exception as e:
                logger.warning(f"Не удалось отправить с темой в чат админов: {e}. Пробую без темы.")
                try:
                    if photo:
                        await bot.send_photo(admins_chat_id, photo, caption=admin_text, parse_mode="HTML", reply_markup=kb)
                    else:
                        await bot.send_message(admins_chat_id, admin_text, parse_mode="HTML", reply_markup=kb)
                except Exception as e2:
                    logger.error(f"Критическая ошибка отправки итога поста #{post_id} администраторам: {e2}")

    except Exception as e:
        logger.error(f"Ошибка отправки итога поста #{post_id} администраторам: {e}")


async def publish_post(post_id: int, moderator_id: int = 0) -> Tuple[bool, str]:
    """Надёжная публикация: DB-claim -> Telegram -> DB-finish.
    Если Telegram упал, пост возвращается в очередь с retry и никогда не
    помечается опубликованным до фактического успешного ответа Telegram."""
    post = await get_post_by_id(post_id)
    if not post:
        return False, "not_found"
    status = post[5]
    if status == "published":
        return False, "already_processed"
    if status not in ("moderation", "approved"):
        return False, "already_processed"
    if not await claim_post_for_publishing(post_id, moderator_id):
        return False, "already_processed"
    try:
        _, user_id, text, photo, _, _ = post
        chat_id = get_setting("MAIN_CHANNEL_ID")
        if photo:
            sent = await bot.send_photo(chat_id=chat_id, photo=photo, caption=escape(text or ""), parse_mode="HTML")
        else:
            sent = await bot.send_message(chat_id=chat_id, text=escape(text or ""), parse_mode="HTML")
        if not await finish_publishing(post_id, sent.message_id):
            await fail_publishing(post_id, "db_finalize_failed", retry=True)
            return False, "db_finalize_failed"
        await record_ai_feedback(post_id, "published", 2.0 if moderator_id == 0 else 4.0)
        await register_mentions_from_text(text)
        await notify_admins_outcome(post_id, "published")
        await log("publish", f"post #{post_id} published by {moderator_id or 'bot'}")
        return True, "published"
    except Exception as e:
        await fail_publishing(post_id, str(e), retry=True)
        logger.exception("Ошибка публикации поста #%s", post_id)
        return False, "telegram_publish_failed"


async def reject_post(post_id: int, moderator_id: Optional[int] = None, reason: str = "") -> bool:
    """Атомарно отклоняет пост и выполняет общие побочные действия.

    Все точки отклонения (модератор, админ, приоритетная заявка и таймаут)
    проходят через одну функцию, поэтому гонка с публикацией не приводит к
    двойной обработке или повторному уведомлению.
    """
    post = await get_post_by_id(post_id)
    if not post or post[5] not in ("moderation", "approved"):
        return False

    # None означает автоматическое решение (таймаут), а реальный Telegram ID
    # сохраняется для ручного решения.
    ok = await try_finalize_post(
        post_id,
        moderator_id if moderator_id is not None else 0,
        "rejected",
        reason or "Не указана",
    )
    if not ok:
        return False

    user_id = post[1]
    try:
        await record_ai_feedback(post_id, "rejected", -5.0)
    except Exception:
        logger.exception("Не удалось записать AI feedback для отклонённого поста #%s", post_id)

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"❌ <b>Пост #{post_id} отклонён.</b>\n\n"
                f"📝 Причина: {escape(reason or 'Не указана')}"
            ),
            parse_mode="HTML",
        )
    except Exception:
        # Пользователь мог заблокировать бота — это не должно отменять уже
        # успешно зафиксированное решение в БД.
        logger.info("Не удалось уведомить автора поста #%s об отклонении", post_id)

    try:
        await notify_admins_outcome(post_id, "rejected", reason or "Не указана")
    except Exception:
        logger.exception("Не удалось отправить итог отклонения поста #%s администраторам", post_id)

    await log(
        "reject",
        f"post #{post_id} rejected by {moderator_id if moderator_id is not None else 'bot'}: {reason or 'Не указана'}",
    )
    return True


async def mark_ai_error(post_id: int, admin_id: int) -> bool:
    if not _is_admin(admin_id):
        return False
    ok = await record_ai_correction(post_id, admin_id)
    if ok:
        await log("ai_correction", f"post #{post_id}: admin {admin_id} marked AI error")
    return ok


# ================== АВТОМОДЕРАЦИЯ / АВТОПУБЛИКАЦИЯ / РУЧНАЯ МОДЕРАЦИЯ ==================
async def register_mentions_from_text(text: str) -> None:
    """При публикации поста ищет в тексте @упоминания зарегистрированных
    пользователей и увеличивает им счётчик упоминаний в профиле."""
    if not text:
        return
    for username in set(_MENTION_RE.findall(text)):
        await increment_mention_count(username)


async def send_post_for_review(post_id: int, scheduled_time: Optional[datetime],
                                manual_reason: str = "") -> None:
    """Карточка нового поста для МОДЕРАТОРОВ — единственное место, где
    решается судьба поста до публикации (кнопки "Опубликовать сейчас"/
    "Отказать"). Администраторы теперь ничего не видят на этом этапе —
    только итог, когда пост уже опубликован или отклонён (см.
    notify_admins_outcome), без действий с их стороны.

    - если scheduled_time задан — пост автоматически одобрен и ждёт своего
      времени публикации (модератор может опубликовать раньше или отклонить);
    - если scheduled_time=None — пост ждёт РУЧНОГО решения модератора
      (фото, либо текст без ключевых фраз для автопубликации; manual_reason
      поясняет, почему именно).
    Имя/юзернейм автора в этой карточке никогда не указывается — модератор
    видит только текст/фото поста."""
    post = await get_post_by_id(post_id)
    if not post:
        return
    _, user_id, text, photo, _, _ = post

    # Подтягиваем диагностику, чтобы модератор сразу видел состояние
    # автоматических проверок, но не видел личные данные автора.
    ai_score = None
    ai_conf = None
    ai_decision = None
    ai_reason = ""
    ocr_text = ""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT ai_score, ai_confidence, ai_decision, ai_reason, ocr_text "
            "FROM posts WHERE id=?", (post_id,)
        )
        analysis = await cur.fetchone()
        if analysis:
            ai_score, ai_conf, ai_decision, ai_reason, ocr_text = analysis

    if scheduled_time:
        formatted_time = scheduled_time.strftime("%d.%m.%Y %H:%M")
        header = (
            f"🤖 <b>ПОСТ #{post_id} · ГОТОВ К ПУБЛИКАЦИИ</b>\n"
            f"<blockquote>🕐 <b>По расписанию:</b> {formatted_time} (Новосибирск)</blockquote>\n"
        )
    else:
        timeout_hours = get_setting("MODERATION_TIMEOUT_HOURS")
        header = (
            f"🔎 <b>ПОСТ #{post_id} · НУЖНО РЕШЕНИЕ</b>\n"
            f"<blockquote>⏳ Автоотклонение через <b>{timeout_hours} ч.</b>\n"
            f"Причина ручной проверки: <b>{escape(manual_reason or 'дополнительная проверка')}</b></blockquote>\n"
        )

    if ai_score is not None:
        if float(ai_conf or 0) >= 0.85:
            ai_badge = "🟢 Высокая"
        elif float(ai_conf or 0) >= 0.68:
            ai_badge = "🟡 Средняя"
        else:
            ai_badge = "🔴 Низкая"
        ai_block = (
            f"<blockquote>🤖 <b>Проверка ИИ</b>\n"
            f"Оценка: <b>{float(ai_score):.0f}/100</b> · Уверенность: <b>{float(ai_conf or 0):.0%}</b>\n"
            f"Надёжность: <b>{ai_badge}</b>\n"
            f"Решение: <b>{'автоматически' if ai_decision == 'auto' else 'ручная проверка'}</b></blockquote>\n"
        )
    else:
        ai_block = "<blockquote>🤖 <b>Проверка ИИ</b> · анализ ещё не сохранён</blockquote>\n"

    mod_text = f"{header}{ai_block}<blockquote>📝 <b>Текст публикации</b>\n{escape(text or 'Без текста')}</blockquote>"
    if ocr_text:
        mod_text += f"\n<blockquote>🔤 <b>Текст на фото</b>\n{escape(ocr_text[:700])}</blockquote>"
    if ai_reason:
        mod_text += f"\n<blockquote>💡 <b>Комментарий ИИ</b>\n{escape(ai_reason[:700])}</blockquote>"

    async def _send(chat_id: int, topic_id: int, keyboard, review_text: str):
        if not chat_id:
            return None
        try:
            if photo:
                return await bot.send_photo(
                    chat_id=chat_id, message_thread_id=topic_id or None, photo=photo,
                    caption=review_text, parse_mode="HTML", reply_markup=keyboard,
                )
            return await bot.send_message(
                chat_id=chat_id, message_thread_id=topic_id or None, text=review_text,
                parse_mode="HTML", reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить пост #{post_id} в чат {chat_id} с темой: {e}. Пробую без темы.")
            try:
                if photo:
                    return await bot.send_photo(
                        chat_id=chat_id, photo=photo, caption=review_text,
                        parse_mode="HTML", reply_markup=keyboard,
                    )
                return await bot.send_message(
                    chat_id=chat_id, text=review_text, parse_mode="HTML", reply_markup=keyboard,
                )
            except Exception as e2:
                logger.error(f"Критическая ошибка отправки поста #{post_id} в чат {chat_id}: {e2}")
                return None

    mod_msg = await _send(
        get_setting("MODERATORS_CHAT_ID"), get_setting("MODERATORS_TOPIC_ID"),
        moderation_keyboard(post_id), mod_text,
    )
    if mod_msg:
        await update_post_message_ids(post_id, moderators_message_id=mod_msg.message_id)


async def process_new_post(post_id: int) -> None:
    """Конвейер для постов, прошедших автомодерацию полностью: подбираем
    слот публикации, переводим в очередь. Пользователю НИЧЕГО дополнительно
    не пишем здесь — единое сообщение "отправлено на модерацию, вы в
    очереди" уже отправлено сразу при приёме поста (см. handlers/posting.py) —
    бот сознательно не раскрывает пользователю сам факт автопубликации."""
    scheduled_time = await pick_schedule_slot()
    await approve_and_schedule(post_id, scheduled_time)
    await send_post_for_review(post_id, scheduled_time)
    await log("auto_approve", f"post #{post_id} scheduled at {scheduled_time.isoformat()}")


async def process_new_post_manual_review(post_id: int, manual_reason: str = "") -> None:
    """Конвейер для постов, требующих решения человека (фото, либо текст без
    ключевых фраз для автопубликации). Пользователю дополнительно ничего не
    пишем (см. process_new_post) — только карточка модераторам/админам."""
    await send_post_for_review(post_id, scheduled_time=None, manual_reason=manual_reason)
    await log("manual_review_required", f"post #{post_id} requires manual review: {manual_reason}")


async def route_new_post(post_id: int, photo_checked: bool = False) -> None:
    """Единый production decision engine. Жёсткие проверки выполняются до
    этой функции; здесь принимается только маршрут auto/manual."""
    post = await get_post_by_id(post_id)
    if not post:
        return
    _, user_id, text, photo, _, _ = post
    has_trigger = await has_auto_approve_trigger(text)
    trust = await get_user_trust_score(user_id)
    media_ok = True
    media_reason = ""
    ocr_text = ""

    if photo:
        try:
            buf = await bot.download(photo)
            media_bytes = buf.read()
            phash, ocr_text, ocr_ok, media_reason = await analyze_media(media_bytes)
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE posts SET photo_hash=?, ocr_text=? WHERE id=?", (phash, ocr_text[:3000], post_id))
                await db.commit()
            if not ocr_ok:
                media_ok = False
            recent = await get_recent_photo_hashes(phash)
            if any(phash_distance(phash, old) <= 4 for old in recent if old):
                # Повтор фотографии в течение 72 часов не запрещаем навсегда,
                # но отправляем человеку для проверки. После окна повтор разрешён.
                media_ok = False
                media_reason = "фото уже встречалось недавно"
            detector_ok = await asyncio.to_thread(photo_detector_available)
            if not detector_ok:
                media_ok = False
                media_reason = "локальный анализатор фото недоступен"
            else:
                is_explicit, nsfw_class, nsfw_score = await screen_photo(photo)
                if is_explicit:
                    media_ok = False
                    media_reason = f"обнаружен потенциально запрещённый контент ({nsfw_class}, {nsfw_score:.2f})"
        except Exception as e:
            logger.exception("Ошибка анализа медиа поста #%s", post_id)
            media_ok = False
            media_reason = "ошибка анализа фото"

    analysis_text = text
    if ocr_text:
        analysis_text += "\n[ТЕКСТ С ФОТО]: " + ocr_text
    decision = await evaluate(analysis_text, trust=trust, has_trigger=has_trigger, media_ok=media_ok)
    final_reason = decision.reason
    if media_reason:
        final_reason += f"; {media_reason}"
    await set_ai_analysis(post_id, decision.score, decision.confidence, decision.decision, final_reason, ocr_text)
    await log("decision_engine", f"post #{post_id}: score={decision.score}; confidence={decision.confidence:.2f}; trust={trust:.1f}; decision={decision.decision}; reason={final_reason}")

    if get_setting("AI_SHADOW_MODE"):
        # Теневой режим: анализ и статистика сохраняются, но реальный маршрут
        # остаётся ручным. Удобно для безопасной калибровки порога.
        await set_post_auto_status(post_id, "shadow_manual")
        deadline = datetime.now(TIMEZONE) + timedelta(hours=get_setting("MODERATION_TIMEOUT_HOURS"))
        await set_post_review_deadline(post_id, deadline)
        await process_new_post_manual_review(post_id, "теневой режим ИИ: реальное решение не изменено")
        return

    if decision.decision == "auto" and media_ok:
        await process_new_post(post_id)
    else:
        await set_post_auto_status(post_id, "pending_manual_review")
        deadline = datetime.now(TIMEZONE) + timedelta(hours=get_setting("MODERATION_TIMEOUT_HOURS"))
        await set_post_review_deadline(post_id, deadline)
        reason = f"ИИ {decision.score}/100, уверенность {decision.confidence:.0%}: {final_reason}"
        await process_new_post_manual_review(post_id, reason)


async def recover_pending_posts() -> None:
    """Вызывается при старте бота: дообрабатывает посты, которые не были
    доведены до конца конвейера — например, если процесс упал между
    вставкой записи в БД и выбором маршрута, либо между выбором маршрута и
    отправкой карточки модераторам. Ничего не теряется — посты уже в БД
    (SQLite + WAL), просто нужно доиграть их обработку до конца."""
    orphaned = await get_orphaned_moderation_posts()
    for post_id in orphaned:
        logger.info(f"Восстанавливаю маршрутизацию поста #{post_id} после перезапуска бота")
        try:
            await route_new_post(post_id)
        except Exception:
            logger.exception(f"Не удалось восстановить обработку поста #{post_id}")

    for post_id in await get_stuck_intro_comments():
        try:
            await reset_intro_comment_claim(post_id)
        except Exception:
            logger.exception("Не удалось освободить зависший claim первого комментария #%s", post_id)

    stuck_publishing = await get_stuck_publishing_posts()
    for post_id in stuck_publishing:
        try:
            await fail_publishing(post_id, "stuck_after_restart", retry=True)
        except Exception:
            logger.exception("Не удалось восстановить публикацию #%s", post_id)

    stuck = await get_stuck_manual_review_posts()
    for post_id in stuck:
        logger.info(f"Дошлю карточку поста #{post_id} модераторам после перезапуска бота")
        try:
            await send_post_for_review(post_id, scheduled_time=None)
        except Exception:
            logger.exception(f"Не удалось дослать карточку поста #{post_id} модераторам")


async def moderation_expiry_loop() -> None:
    """Фоновая задача: раз в 5 минут автоматически отклоняет посты на
    ручной модерации, чей общий срок (MODERATION_TIMEOUT_HOURS, по
    умолчанию 24 ч.) истёк без решения модератора."""
    while True:
        try:
            for post_id in await get_expired_manual_review_posts():
                try:
                    timeout_hours = get_setting("MODERATION_TIMEOUT_HOURS")
                    ok = await reject_post(
                        post_id, moderator_id=None,
                        reason=f"Автоматический отказ: решение не было принято в течение {timeout_hours} ч.",
                    )
                    if ok:
                        await log("moderation_expired", f"post #{post_id} auto-rejected (timeout)")
                except Exception:
                    logger.exception(f"Ошибка при автоотклонении просроченного поста #{post_id}")
        except Exception:
            logger.exception("Ошибка в цикле проверки просроченной модерации")
        await asyncio.sleep(300)


# ================== NSFW-ПРОВЕРКА ФОТО ==================
async def screen_photo(photo_file_id: str) -> Tuple[bool, Optional[str], float]:
    """Скачивает фото по file_id и прогоняет через NudeNet.
    Возвращает (is_explicit, class_name, score) — см. app/moderation_photo.py."""
    try:
        buf = await bot.download(photo_file_id)
        image_bytes = buf.read()
    except Exception:
        logger.exception(f"Не удалось скачать фото {photo_file_id} для NSFW-проверки")
        return False, None, 0.0

    return await check_photo_nsfw(image_bytes)


# ================== ПРИОРИТЕТНОЕ УСКОРЕНИЕ ПРОВЕРКИ (Telegram Stars) ==================
async def apply_priority_boost(post_id: int, payer_id: int) -> None:
    post = await get_post_by_id(post_id)
    if not post:
        return
    if post[5] == "approved":
        await approve_and_schedule(post_id, datetime.now(TIMEZONE))
    await _notify_admins_priority(post_id, payer_id)
    await log("priority_boost", f"post #{post_id} boosted by user {payer_id}")


async def publish_priority_post(post_id: int, admin_id: int) -> Tuple[bool, str]:
    """Публикация прямо из ЛС администратора, без поиска поста в группе."""
    if not _is_admin(admin_id):
        return False, "forbidden"
    post = await get_post_by_id(post_id)
    if not post:
        return False, "not_found"
    if post[5] not in ("moderation", "approved"):
        return False, "already_processed"
    return await publish_post(post_id, moderator_id=admin_id)


async def _notify_admins_priority(post_id: int, payer_id: int) -> None:
    post = await get_post_by_id(post_id)
    if not post:
        return
    text_preview = escape((post[2] or "")[:150])
    username = await get_username_by_user_id(payer_id)
    text = (
        "🚀 <b>Приоритетная публикация</b>\n\n"
        f"👤 Пользователь: @{escape(username) if username else 'без username'}\n"
        f"🆔 ID: <code>{payer_id}</code>\n"
        f"📌 Пост: <b>#{post_id}</b>\n\n"
        f"📄 <b>Текст:</b>\n{text_preview}"
    )
    from app.keyboards import priority_admin_keyboard
    kb = priority_admin_keyboard(post_id)
    for admin_id in ADMINS:
        try:
            if post[3]:
                await bot.send_photo(admin_id, post[3], caption=text, parse_mode="HTML", reply_markup=kb)
            else:
                await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s о приоритетной публикации: %s", admin_id, e)


# ================== ПОИСК ПОСТА ПО ССЫЛКЕ/ПЕРЕСЛАННОМУ СООБЩЕНИЮ ==================
async def _get_channel_username() -> str:
    global _channel_username_cache
    if _channel_username_cache is not None:
        return _channel_username_cache
    try:
        chat = await bot.get_chat(get_setting("MAIN_CHANNEL_ID"))
        _channel_username_cache = chat.username or ""
    except Exception as e:
        logger.warning(f"Не удалось получить username основного канала: {e}")
        _channel_username_cache = ""
    return _channel_username_cache


async def get_bot_username() -> str:
    """Username самого бота — нужен для deep-link кнопок ("Узнать автора"/
    "Удалить пост" под первым комментарием в группе обсуждений: клик по
    ним должен открыть диалог именно с этим ботом)."""
    global _bot_username_cache
    if _bot_username_cache is not None:
        return _bot_username_cache
    try:
        me = await bot.get_me()
        _bot_username_cache = me.username or ""
    except Exception as e:
        logger.warning(f"Не удалось получить username бота: {e}")
        _bot_username_cache = ""
    return _bot_username_cache


async def resolve_channel_post_id(msg) -> Optional[int]:
    """Определяет ID сообщения в основном канале по пересланному посту или
    по ссылке t.me — используется и в заявке на удаление, и в поиске автора.
    Возвращает None, если распознать не удалось или пост из другого чата."""
    main_channel_id = get_setting("MAIN_CHANNEL_ID")

    origin = getattr(msg, "forward_origin", None)
    if origin is not None and getattr(origin, "type", None) == "channel":
        if origin.chat and origin.chat.id == main_channel_id:
            return origin.message_id

    # legacy-поля на случай другой версии Bot API/aiogram
    forward_chat = getattr(msg, "forward_from_chat", None)
    if forward_chat and forward_chat.id == main_channel_id and getattr(msg, "forward_from_message_id", None):
        return msg.forward_from_message_id

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return None

    m = _TME_PRIVATE_RE.search(text)
    if m:
        internal_id, message_id = m.group(1), int(m.group(2))
        try:
            candidate_chat_id = int(f"-100{internal_id}")
        except ValueError:
            return None
        return message_id if candidate_chat_id == main_channel_id else None

    m = _TME_PUBLIC_RE.search(text)
    if m:
        username, message_id = m.group(1), int(m.group(2))
        channel_username = await _get_channel_username()
        if channel_username and username.lower() == channel_username.lower():
            return message_id
        return None

    return None


# ================== ПЕРВЫЙ КОММЕНТАРИЙ ПОД ПОСТОМ В КАНАЛЕ ==================
async def post_intro_comment(post_id: int, discussion_message_id: int) -> None:
    """Отправляет вводный комментарий ('Будьте вежливы...' + кнопки
    Предложить пост/Купить звёзды/Купить VPN) в группу обсуждений — как
    ОТВЕТ на автоматически пересланное туда сообщение поста, из-за чего он
    отображается в канале как первый комментарий под постом."""
    comments_chat_id = get_setting("COMMENTS_CHAT_ID")
    if not comments_chat_id:
        return

    claimed = await mark_intro_comment_posted(post_id)
    if not claimed:
        return  # уже отправлено (например, обработчик сработал повторно)

    bot_username = await get_bot_username()

    try:
        await bot.send_message(
            chat_id=comments_chat_id,
            text=get_setting("INTRO_COMMENT_TEXT"),
            reply_to_message_id=discussion_message_id,
            reply_markup=intro_comment_keyboard(post_id, bot_username),
        )
        await finish_intro_comment(post_id)
        await log("intro_comment", f"post #{post_id}: посажен первый комментарий")
    except Exception:
        await reset_intro_comment_claim(post_id)
        logger.exception(f"Не удалось отправить первый комментарий под пост #{post_id}")


async def handle_channel_auto_forward(msg) -> None:
    """Вызывается на каждое сообщение в чате комментариев. Если это
    автоматическая пересылка поста из основного канала (создаётся Telegram
    сама, когда у канала подключена группа обсуждений) — сажает под ней
    вводный комментарий. Не имеет отношения к учёту реальных комментариев
    пользователей (см. handlers/comments.py) — это отдельный, более узкий
    случай."""
    if not getattr(msg, "is_automatic_forward", False):
        return

    channel_message_id = await resolve_channel_post_id(msg)
    if not channel_message_id:
        return

    post = await get_post_by_channel_message_id(channel_message_id)
    if not post:
        return

    await post_intro_comment(post[0], msg.message_id)


# ================== МАСКИРОВКА ДАННЫХ АВТОРА ==================
def mask_user_id(user_id: int) -> str:
    s = str(user_id)
    if len(s) <= 4:
        return "•" * len(s)
    return s[:2] + "•" * (len(s) - 4) + s[-2:]


def mask_username(username: Optional[str]) -> str:
    if not username:
        return "не установлен"
    if len(username) <= 2:
        return username[0] + "•"
    return username[0] + "•" * (len(username) - 2) + username[-1]


# ================== УДАЛЕНИЕ ОПУБЛИКОВАННОГО ПОСТА ==================
async def delete_published_post(post_id: int, reason: str, decided_by: Optional[int]) -> Tuple[bool, str]:
    post = await get_post_by_id(post_id)
    if not post:
        return False, "not_found"
    _, user_id, text, photo, _, status = post
    if status != "published":
        return False, f"already_{status}"

    channel_message_id = await get_post_channel_message_id(post_id)
    if not channel_message_id:
        return False, "missing_channel_message_id"
    try:
        await bot.delete_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id)
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение #{channel_message_id} из канала (пост #{post_id}): {e}")
        return False, "telegram_delete_failed"
    await mark_post_deleted(post_id)

    try:
        await bot.send_message(
            user_id,
            f"🗑 Ваш пост был удалён из канала по заявке.\n\n📝 <b>Причина:</b> {reason}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {user_id} об удалении поста: {e}")

    await log("post_delete", f"post #{post_id} deleted, decided_by={decided_by}: {reason}")

    # Освободившийся слот в дневной квоте (если пост был опубликован сегодня)
    # тут же подтягивается ребалансировкой — вместо него в этот день должен
    # опубликоваться следующий пост из очереди, если он есть.
    try:
        moved = await rebalance_daily_queue()
        if moved:
            logger.info(f"После удаления поста #{post_id} ребалансировка перенесла на сегодня {moved} пост(ов)")
    except Exception:
        logger.exception(f"Ошибка ребалансировки очереди после удаления поста #{post_id}")

    return True, "ok"


async def submit_deletion_request(post_id: int, requester_id: int, reason: str) -> Tuple[str, Optional[int]]:
    """
    Возвращает (result, request_id), где result:
    'auto_deleted' — инициатор оказался автором или упомянут в посте, удалено сразу
    'pending' — заявка ушла на рассмотрение администрации
    'not_found' / 'not_published' / 'error'
    """
    post = await get_post_by_id(post_id)
    if not post:
        return "not_found", None
    _, author_id, text, photo, _, status = post
    if status != "published":
        return "not_published", None

    is_author = (requester_id == author_id)
    is_mentioned = False
    if text and not is_author:
        mentioned_usernames = {u.lower() for u in _MENTION_RE.findall(text)}
        requester_username = (await get_username_by_user_id(requester_id) or "").lower()
        is_mentioned = bool(requester_username) and requester_username in mentioned_usernames

    if is_author or is_mentioned:
        ok, _ = await delete_published_post(post_id, reason, decided_by=requester_id)
        return ("auto_deleted" if ok else "error"), None

    timeout_hours = get_setting("DELETION_REQUEST_TIMEOUT_HOURS")
    request_id = await create_deletion_request(post_id, requester_id, reason, timeout_hours)
    await _notify_deletion_reviewers(request_id)
    return "pending", request_id


async def _notify_deletion_reviewers(request_id: int) -> None:
    req = await get_deletion_request(request_id)
    if not req:
        return
    _, post_id, requester_id, reason, _status, _created, _expire, _decided_by, _decided_time, _refs = req

    post = await get_post_by_id(post_id)
    post_text_preview = escape((post[2] or "")[:500]) if post else ""
    requester_username = await get_username_by_user_id(requester_id)

    channel_username = await _get_channel_username()
    channel_msg_id = await get_post_channel_message_id(post_id)
    if channel_username and channel_msg_id:
        link_line = f"🔗 <b>Ссылка на пост:</b> https://t.me/{channel_username}/{channel_msg_id}\n"
    else:
        link_line = f"📌 <b>Пост:</b> #{post_id}\n"

    timeout_hours = get_setting("DELETION_REQUEST_TIMEOUT_HOURS")
    text = (
        f"🗑 <b>Заявка на удаление поста</b>\n\n"
        f"{link_line}"
        f"👤 <b>Инициатор:</b> @{escape(requester_username) if requester_username else 'без username'}\n"
        f"🆔 <b>ID инициатора:</b> <code>{requester_id}</code>\n"
        f"📝 <b>Причина:</b> {escape(reason or '')}\n\n"
        f"📄 <b>Текст поста:</b>\n{post_text_preview}\n\n"
        f"⏳ Если никто не ответит в течение {timeout_hours} ч. — заявка отклонится автоматически."
    )

    refs = []
    for reviewer_id in DELETION_REVIEWERS:
        try:
            sent = await bot.send_message(reviewer_id, text, parse_mode="HTML",
                                           reply_markup=deletion_reviewer_keyboard(request_id))
            refs.append(f"{reviewer_id}:{sent.message_id}")
        except Exception as e:
            logger.warning(f"Не удалось отправить заявку на удаление #{request_id} рецензенту {reviewer_id}: {e}")

    if refs:
        await set_deletion_request_admin_refs(request_id, ",".join(refs))


async def _update_deletion_reviewer_messages(admin_refs: Optional[str], button_label: str) -> None:
    if not admin_refs:
        return
    for ref in admin_refs.split(","):
        if ":" not in ref:
            continue
        chat_id_str, message_id_str = ref.split(":", 1)
        try:
            chat_id, message_id = int(chat_id_str), int(message_id_str)
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id,
                reply_markup=deletion_resolved_keyboard(button_label),
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение о заявке в чате {ref}: {e}")


async def resolve_deletion_request(request_id: int, decided_by: int, approve: bool) -> Tuple[bool, str]:
    """Атомарно фиксирует решение рецензента (первый клик выигрывает)."""
    status = "approved" if approve else "rejected"
    claimed = await claim_deletion_request(request_id, decided_by, status)
    if not claimed:
        return False, "already_decided"

    req = await get_deletion_request(request_id)
    if not req:
        return False, "not_found"
    _, post_id, requester_id, reason, _status, _created, _expire, _decided_by, _decided_time, admin_refs = req

    if approve:
        ok, delete_reason = await delete_published_post(post_id, reason, decided_by)
        if ok:
            button_label = "✅ Удалено"
        else:
            await reopen_deletion_request(request_id)
            button_label = "⚠️ Ошибка удаления — повторить"
            try:
                await bot.send_message(requester_id, f"⚠️ Заявка на удаление поста #{post_id} одобрена, но Telegram не дал удалить пост. Заявка возвращена на повторную обработку.")
            except Exception:
                pass
    else:
        button_label = "❌ Отклонено"
        try:
            await bot.send_message(
                requester_id,
                f"❌ Ваша заявка на удаление поста #{post_id} отклонена администрацией.",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить инициатора заявки #{request_id}: {e}")

    await _update_deletion_reviewer_messages(admin_refs, button_label)
    await log("deletion_decision", f"request #{request_id} post #{post_id}: {status} by {decided_by}")
    return True, status


async def _expire_deletion_request(request_id: int) -> None:
    claimed = await claim_deletion_request(request_id, decided_by=0, status="expired")
    if not claimed:
        return
    req = await get_deletion_request(request_id)
    if not req:
        return
    _, post_id, requester_id, _reason, _status, _created, _expire, _decided_by, _decided_time, admin_refs = req

    timeout_hours = get_setting("DELETION_REQUEST_TIMEOUT_HOURS")
    try:
        await bot.send_message(
            requester_id,
            f"⌛ Заявка на удаление поста #{post_id} автоматически отклонена — "
            f"администрация не ответила в течение {timeout_hours} ч.",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить инициатора об истечении заявки #{request_id}: {e}")

    await _update_deletion_reviewer_messages(admin_refs, "⌛ Истекло")
    await log("deletion_expired", f"request #{request_id} post #{post_id} expired")


async def deletion_expiry_loop() -> None:
    """Фоновая задача: раз в 5 минут закрывает заявки на удаление, на
    которые никто из рецензентов не ответил за DELETION_REQUEST_TIMEOUT_HOURS."""
    while True:
        try:
            for request_id in await get_expired_deletion_requests():
                try:
                    await _expire_deletion_request(request_id)
                except Exception:
                    logger.exception(f"Ошибка при истечении заявки на удаление #{request_id}")
        except Exception:
            logger.exception("Ошибка в цикле проверки истёкших заявок на удаление")
        await asyncio.sleep(300)
