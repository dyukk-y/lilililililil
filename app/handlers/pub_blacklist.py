from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext
from datetime import datetime

import aiosqlite

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

# ================== ЧЕРНЫЙ СПИСОК ПУБЛИКАЦИЙ ==================
@router.callback_query(F.data == "pub_blacklist")
async def show_pub_blacklist(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await show_pub_blacklist_page(cb, page=1)

async def show_pub_blacklist_page(cb: CallbackQuery, page: int):
    """Показать страницу черного списка публикаций"""
    blacklist, total = await get_publication_blacklist(page=page, per_page=5)
    total_pages = (total + 4) // 5
    
    if not blacklist:
        text = "📝 <b>Список стоп-слов пуст</b>"
        await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())
        return
    
    text_lines = [f"📋 <b>Стоп-слова — маты/оскорбления/спам (стр. {page}/{total_pages}):</b>\n\n"]
    
    start_idx = (page - 1) * 5 + 1
    for i, (keyword, added_by, added_time) in enumerate(blacklist, start_idx):
        try:
            time_str = datetime.fromisoformat(added_time).strftime('%d.%m.%Y %H:%M')
        except:
            time_str = added_time or "неизвестно"
        
        admin_info = ""
        if added_by:
            try:
                async with aiosqlite.connect(DB_NAME) as db:
                    cur = await db.execute("SELECT username FROM users WHERE user_id=?", (added_by,))
                    admin_row = await cur.fetchone()
                    if admin_row and admin_row[0]:
                        admin_info = f"@{admin_row[0]}"
                    else:
                        admin_info = f"<code>{added_by}</code>"
            except:
                admin_info = f"<code>{added_by}</code>"
        else:
            admin_info = "неизвестно"
        
        text_lines.append(f"<b>{i}. 🔤 <code>{keyword}</code></b>")
        text_lines.append(f"   👤 Добавил: {admin_info}")
        text_lines.append(f"   🕐 Время: {time_str}\n")
    
    text = "\n".join(text_lines)
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await cb.message.edit_text(
        text, 
        parse_mode='HTML', 
        reply_markup=pub_blacklist_menu(page, total_pages)
    )

@router.callback_query(F.data.startswith("pubblack_page_"))
async def pubblack_page_handler(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        page = int(cb.data.split("_")[2])
        await show_pub_blacklist_page(cb, page)
    except (ValueError, IndexError):
        await cb.answer("❌ Ошибка при загрузке страницы", show_alert=True)

# ================== ДОБАВЛЕНИЕ В ЧЕРНЫЙ СПИСОК ==================
@router.callback_query(F.data == "add_pub_blacklist")
async def add_pub_blacklist(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BlacklistState.wait_keyword)
    await cb.message.edit_text(
        "📝 <b>Добавление стоп-слова</b>\n\n"
        "Отправьте слово или фразу, которую хотите добавить в стоп-список "
        "(бот уже содержит базовый набор матов и оскорблений — это для "
        "своих дополнений: спам-слов, конкретных юзернеймов и т.п.).\n\n"
        "<i>Примеры:</i>\n"
        "• @spammer - для блокировки упоминаний юзернейма\n"
        "• плохое слово - для блокировки конкретного слова\n"
        "• запрещенная фраза - для блокировки конкретной фразы\n\n"
        "⚠️ <b>Внимание:</b> Регистр не учитывается. Пост с таким словом/фразой "
        "будет автоматически отклонён ещё до постановки в очередь публикации.",
        parse_mode='HTML',
        reply_markup=blacklist_cancel_menu()
    )

@router.message(BlacklistState.wait_keyword)
async def process_pub_blacklist_keyword(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    
    keyword = msg.text.strip()
    if len(keyword) < 2:
        await msg.answer("❌ Ключевое слово должно содержать минимум 2 символа.")
        return
    
    success = await add_to_publication_blacklist(keyword, msg.from_user.id)
    
    if success:
        await msg.answer(
            f"✅ Добавлено в черный список публикаций: <code>{keyword}</code>\n\n"
            f"📝 Теперь посты, содержащие это слово/фразу, будут автоматически отклоняться.",
            parse_mode='HTML',
            reply_markup=blacklist_menu()
        )
        await log("blacklist_add", f"admin {msg.from_user.id} added '{keyword}'")
    else:
        await msg.answer(
            f"❌ Ключевое слово <code>{keyword}</code> уже есть в черном списке.",
            parse_mode='HTML',
            reply_markup=blacklist_menu()
        )
    
    await state.clear()

# ================== УДАЛЕНИЕ ИЗ ЧЕРНОГО СПИСКА ==================
@router.callback_query(F.data == "remove_pub_blacklist")
async def remove_pub_blacklist(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    blacklist, total = await get_publication_blacklist(page=1, per_page=100)
    
    if not blacklist:
        return await cb.answer("📋 Черный список публикаций пуст.", show_alert=True)
    
    keyboard = []
    for i, (keyword, added_by, added_time) in enumerate(blacklist, 1):
        keyboard.append([
            InlineKeyboardButton(
                text=f"{i}. {keyword}",
                callback_data=f"remove_blacklist_word_{keyword}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="pub_blacklist")])
    
    await cb.message.edit_text(
        "🗑️ <b>Удаление слова из черного списка публикаций</b>\n\n"
        "Выберите слово для удаления:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@router.callback_query(F.data.startswith("remove_blacklist_word_"))
async def process_remove_blacklist_word(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    keyword = cb.data.replace("remove_blacklist_word_", "")
    
    await remove_from_publication_blacklist(keyword)
    
    await cb.answer(f"✅ Слово '{keyword}' удалено из черного списка", show_alert=True)
    
    await show_pub_blacklist_page(cb, page=1)

