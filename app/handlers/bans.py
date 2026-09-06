from aiogram import F, Router
from aiogram.types import *
from datetime import datetime

import aiosqlite

from app.loader import bot, logger
from app.auth import is_admin as _is_admin
from app.config import (
    ADMINS, DB_NAME,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== COMMANDS FOR ADMINS ==================
@router.message(F.text.startswith("/ban"))
async def ban_command(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 2:
        return await msg.answer(
            "❌ <b>Использование:</b> <code>/ban &lt;user_id&gt; [причина]</code>\n\n"
            "<i>Примеры:</i>\n"
            "<code>/ban 123456789 спам</code>\n"
            "<code>/ban 123456789 нарушение правил</code>",
            parse_mode='HTML'
        )
    
    try:
        user_id = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "Нарушение правил"
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
            user_exists = await cur.fetchone() is not None
        
        if not user_exists:
            return await msg.answer(f"❌ Пользователь с ID <code>{user_id}</code> не найден в базе.", parse_mode='HTML')
        
        await ban_user(user_id, reason, msg.from_user)
        
        try:
            from app.handlers.unlock import unlock_keyboard
            await bot.send_message(
                user_id,
                f"🚫 <b>Вы были заблокированы!</b>\n\n"
                f"📝 <b>Причина:</b> {reason}\n"
                f"👮 <b>Администратор:</b> @{msg.from_user.username or 'без username'}\n"
                f"🆔 <b>ID администратора:</b> {msg.from_user.id}\n\n"
                f"🔒 <b>Вы больше не можете использовать меню бота</b>\n\n"
                f"📞 <b>Для разблокировки:</b> Свяжитесь с @theaugustine, "
                f"либо разблокируйте себя самостоятельно за звёзды ниже 👇",
                parse_mode='HTML',
                reply_markup=unlock_keyboard({"bot": True, "channel": False, "comments": False}),
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id} о блокировке: {e}")
        
        await msg.answer(
            f"✅ Пользователь <code>{user_id}</code> заблокирован.\n"
            f"📝 <b>Причина:</b> {reason}",
            parse_mode='HTML'
        )
        
    except ValueError:
        await msg.answer("❌ Неверный формат ID пользователя. ID должен быть числом.")

@router.message(F.text.startswith("/unban"))
async def unban_command(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer(
            "❌ <b>Использование:</b> <code>/unban &lt;user_id&gt;</code>\n\n"
            "<i>Пример:</i>\n"
            "<code>/unban 123456789</code>",
            parse_mode='HTML'
        )
    
    try:
        user_id = int(parts[1])
        
        if not await is_banned(user_id):
            return await msg.answer(f"❌ Пользователь <code>{user_id}</code> не заблокирован.", parse_mode='HTML')
        
        await unban_user(user_id)
        
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Вы были разблокированы!</b>\n\n"
                "🔓 Теперь вы снова можете использовать бота.\n"
                f"👮 <b>Администратор:</b> @{msg.from_user.username or 'без username'}\n",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id} о разблокировке: {e}")
        
        await msg.answer(f"✅ Пользователь <code>{user_id}</code> разблокирован.", parse_mode='HTML')
        
    except ValueError:
        await msg.answer("❌ Неверный формат ID пользователя. ID должен быть числом.")


# ================== ЗАБЛОКИРОВАННЫЕ ПОЛЬЗОВАТЕЛИ ==================
@router.callback_query(F.data == "banned_users")
async def show_banned_users(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await show_banned_users_page(cb, page=1)

async def show_banned_users_page(cb: CallbackQuery, page: int):
    banned_users, total = await get_banned_users(page=page, per_page=5)
    total_pages = (total + 4) // 5
    
    if not banned_users:
        text = "👤 <b>Нет заблокированных пользователей</b>"
        await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())
        return
    
    text_lines = [f"🚫 <b>Заблокированные пользователи (стр. {page}/{total_pages}):</b>\n\n"]
    
    start_idx = (page - 1) * 5 + 1
    for i, (user_id, reason, ban_time, admin_username, username) in enumerate(banned_users, start_idx):
        try:
            time_str = datetime.fromisoformat(ban_time).strftime('%d.%m.%Y %H:%M')
        except:
            time_str = ban_time
        
        text_lines.append(f"<b>{i}. 🆔 <code>{user_id}</code></b>")
        text_lines.append(f"   📛 @{username or 'без username'}")
        text_lines.append(f"   📝 <b>Причина:</b> {reason}")
        if admin_username:
            text_lines.append(f"   👮 <b>Админ:</b> @{admin_username}")
        text_lines.append(f"   🕐 <b>Заблокирован:</b> {time_str}\n")
    
    text = "\n".join(text_lines)
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await cb.message.edit_text(
        text, 
        parse_mode='HTML', 
        reply_markup=pagination_keyboard(page, total_pages, "banned", "blacklist")
    )

@router.callback_query(F.data.startswith("banned_page_"))
async def banned_page_handler(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        page = int(cb.data.split("_")[2])
        await show_banned_users_page(cb, page)
    except (ValueError, IndexError):
        await cb.answer("❌ Ошибка при загрузке страницы", show_alert=True)

