from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import *


from app.config import (
    ADMINS, SUPER_ADMINS, REQUIRED_SUBSCRIPTIONS,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()


from app.auth import is_admin as _is_admin

# ================== ADMIN PANEL ==================
async def _admin_panel_text() -> str:
    users_count = await get_users_count()
    banned_users, _ = await get_banned_users(page=1, per_page=1)
    banned_count = len(banned_users) if banned_users else 0
    blacklist, _ = await get_publication_blacklist(page=1, per_page=100)
    blacklist_count = len(blacklist) if blacklist else 0
    subscription_count = len(REQUIRED_SUBSCRIPTIONS)

    return (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🚫 Заблокировано: <b>{banned_count}</b>\n"
        f"📝 Стоп-слов (маты/оскорбления/спам): <b>{blacklist_count}</b>\n"
        f"📢 Обязательных подписок: <b>{subscription_count}</b>\n\n"
        f"🤖 <i>Посты с ключевыми фразами (см. «Фразы для автопубликации») "
        f"публикуются автоматически по расписанию; всё остальное — фото и "
        f"текст без таких фраз — ждёт решения модератора (до 24 часов).</i>\n\n"
        f"<i>Выберите действие:</i>"
    )


@router.message(Command("admin"))
async def admin_panel_command(msg: Message):
    if not _is_admin(msg.from_user.id):
        return await msg.answer("🚫 У вас нет доступа к этой команде.")
    
    await msg.answer(
        await _admin_panel_text(), parse_mode='HTML',
        reply_markup=admin_menu(msg.from_user.id in SUPER_ADMINS)
    )

@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await cb.message.edit_text(
        await _admin_panel_text(), parse_mode='HTML',
        reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS)
    )

@router.callback_query(F.data == "blacklist")
async def blacklist_panel(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    banned_users, _ = await get_banned_users(page=1, per_page=1)
    banned_count = len(banned_users) if banned_users else 0
    blacklist, _ = await get_publication_blacklist(page=1, per_page=1)
    blacklist_count = len(blacklist) if blacklist else 0
    
    text = (
        f"🚫 <b>Заблокированные пользователи и стоп-слова</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"👤 Заблокированных пользователей: <b>{banned_count}</b>\n"
        f"📝 Стоп-слов (маты/оскорбления/спам): <b>{blacklist_count}</b>\n\n"
        f"<i>Стоп-слова автоматически отклоняют пост ещё до отправки в очередь "
        f"на публикацию. Список можно свободно редактировать ниже.</i>\n\n"
        f"<i>Выберите действие:</i>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())
