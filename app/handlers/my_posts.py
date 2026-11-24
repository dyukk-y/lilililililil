"""«📋 Мои посты»: список своих постов со статусами и отмена поста,
пока он не опубликован.

Статусы «на проверке» и «одобрен, ждёт времени публикации» намеренно
показываются одинаково («⏳ В очереди») — бот по-прежнему не раскрывает,
прошёл ли пост автоматическую проверку.
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.database import cancel_post_by_author, get_user_posts, log
from app.tg_ui import DANGER, btn, tg_time_iso
from app.utils import h, shorten

router = Router()
PAGE = 8

_STATUS = {
    "moderation": "⏳ В очереди",
    "approved": "⏳ В очереди",
    "publishing": "⏳ Публикуется",
    "published": "✅ Опубликован",
    "rejected": "❌ Отклонён",
    "deleted": "🗑 Удалён",
    "cancelled": "🚫 Отменён вами",
}


async def _render(cb: CallbackQuery, page: int) -> None:
    from app.services import channel_post_link
    from app.tg_ui import show_screen

    rows, total = await get_user_posts(cb.from_user.id, PAGE, page * PAGE)
    kb: list = []
    if not rows:
        text = "📋 <b>Мои посты</b>\n\nВы ещё ничего не предлагали. Нажмите «✍️ Предложить пост» в меню."
    else:
        lines = [f"📋 <b>Мои посты</b> · всего {total}\n"]
        for post_id, text_, photo, time_, status, reason, channel_msg, source in rows:
            icon = "📷 " if photo else ""
            line = (f"<b>#{post_id}</b> · {_STATUS.get(status, status)} · {tg_time_iso(time_, '%d.%m %H:%M')}\n"
                    f"{icon}<i>{h(shorten(text_ or '', 60))}</i>")
            if status == "rejected" and reason:
                line += f"\n└ Причина: {h(shorten(reason, 120))}"
            lines.append(line)
            if status == "published" and channel_msg:
                link = await channel_post_link(channel_msg)
                if link:
                    kb.append([btn(f"🔗 #{post_id} в канале", url=link)])
            if status in ("moderation", "approved") and source == "bot":
                kb.append([btn(f"🚫 Отменить #{post_id}", f"mypost_cancel_{post_id}", style=DANGER)])
        text = "\n\n".join(lines)
        nav = []
        if page > 0:
            nav.append(btn("◀️ Новее", f"my_posts_{page - 1}"))
        if (page + 1) * PAGE < total:
            nav.append(btn("Старее ▶️", f"my_posts_{page + 1}"))
        if nav:
            kb.append(nav)
    kb.append([btn("🏠 Главное меню", "menu")])
    await show_screen(cb.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "my_posts")
@router.callback_query(F.data.startswith("my_posts_"))
async def my_posts(cb: CallbackQuery):
    try:
        page = int(cb.data.rsplit("_", 1)[1]) if cb.data != "my_posts" else 0
    except ValueError:
        page = 0
    await cb.answer()
    await _render(cb, max(0, page))


@router.callback_query(F.data.startswith("mypost_cancel_"))
async def ask_cancel(cb: CallbackQuery):
    post_id = int(cb.data.rsplit("_", 1)[1])
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"🚫 Да, отменить #{post_id}", f"mypost_yes_{post_id}", style=DANGER)],
        [btn("↩️ Нет, оставить", "my_posts")],
    ]))


@router.callback_query(F.data.startswith("mypost_yes_"))
async def confirm_cancel(cb: CallbackQuery):
    from app.services import _disable_moderator_card
    post_id = int(cb.data.rsplit("_", 1)[1])
    if not await cancel_post_by_author(post_id, cb.from_user.id):
        await cb.answer("⚠️ Пост уже опубликован или обработан — отменить нельзя.", show_alert=True)
    else:
        await _disable_moderator_card(post_id, "cancelled")
        await log("post_cancelled", f"user {cb.from_user.id} cancelled post #{post_id}")
        await cb.answer(f"Пост #{post_id} отменён")
    await _render(cb, 0)
