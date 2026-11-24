"""Автонаказания за повторные нарушения.

Нарушения: стоп-слова, бессмысленный текст, повтор одного и того же текста,
флуд (антиспам). Если за сутки набралось AUTOBAN_VIOLATIONS нарушений —
временный бан на 24 ч, а при повторном автобане в течение 30 дней — на
7 дней. Бан снимается сам (maintenance_loop), пользователь получает
уведомление и в момент бана, и при снятии. Платная разблокировка
работает и для временного бана.

Ручные баны администраторов остаются бессрочными и автоматикой не
трогаются.
"""
import logging

from app.database import add_violation, count_recent_temp_bans, log, pop_expired_temp_bans, temp_ban_user
from app.runtime_settings import get as get_setting
from app.utils import h

logger = logging.getLogger(__name__)

KIND_LABELS = {
    "blacklist": "запрещённые слова",
    "gibberish": "бессмысленный текст",
    "duplicate": "повтор одного и того же текста",
    "flood": "флуд",
}
FIRST_BAN_HOURS = 24
REPEAT_BAN_HOURS = 24 * 7


async def register_violation(bot, user_id: int, kind: str) -> bool:
    """Учитывает нарушение. Возвращает True, если пользователь получил бан."""
    if not get_setting("AUTOBAN_ENABLED"):
        return False
    from app.auth import is_admin
    if is_admin(user_id):
        return False
    try:
        count = await add_violation(user_id, kind)
        if count < int(get_setting("AUTOBAN_VIOLATIONS")):
            return False
        hours = REPEAT_BAN_HOURS if await count_recent_temp_bans(user_id) else FIRST_BAN_HOURS
        reason = f"повторные нарушения: {KIND_LABELS.get(kind, kind)}"
        until = await temp_ban_user(user_id, hours, reason)
        if until is None:
            return False  # уже в ручном бане
    except Exception:
        logger.exception("Ошибка автонаказания пользователя %s", user_id)
        return False

    from app.handlers.unlock import unlock_keyboard
    from app.tg_ui import tg_time
    period = "7 дней" if hours == REPEAT_BAN_HOURS else "24 часа"
    try:
        await bot.send_message(
            user_id,
            f"⛔ <b>Вы временно заблокированы на {period}</b>\n\n"
            f"Причина: {h(reason)}.\n"
            f"Блокировка снимется автоматически: {tg_time(until)}.",
            parse_mode="HTML",
            reply_markup=unlock_keyboard({"bot": True, "channel": False, "comments": False}),
        )
    except Exception:
        pass
    from app.alerts import notify_admins
    await notify_admins(f"⛔ Автобан пользователя <code>{user_id}</code> на {period}: {h(reason)}.", key=None)
    return True


async def release_expired_bans(bot) -> int:
    """Снимает истёкшие временные баны и сообщает об этом пользователям."""
    released = await pop_expired_temp_bans()
    for user_id in released:
        await log("autoban_expired", f"user {user_id}")
        try:
            await bot.send_message(user_id, "✅ Временная блокировка снята. Пожалуйста, соблюдайте правила 🙏\n\n/start — главное меню")
        except Exception:
            pass
    return len(released)
