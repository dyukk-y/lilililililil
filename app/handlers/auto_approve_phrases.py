"""Управление списком ключевых фраз, при наличии которых текстовый пост
считается кандидатом на полностью автоматическую публикацию."""
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from app.config import ADMINS
from app.database import (
    get_auto_approve_phrases, add_auto_approve_phrase, remove_auto_approve_phrase, log,
)
from app.keyboards import auto_phrase_menu, auto_phrase_cancel_menu, admin_menu
from app.states import AutoPhraseState

router = Router()


@router.callback_query(F.data == "auto_phrase_list")
async def auto_phrase_list(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    await show_auto_phrase_page(cb, page=1)


async def show_auto_phrase_page(cb: CallbackQuery, page: int):
    phrases, total = await get_auto_approve_phrases(page=page, per_page=10)
    total_pages = (total + 9) // 10 or 1

    if not phrases:
        text = "🔑 Список ключевых фраз пуст"
        return await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu(cb.from_user.id in ADMINS))

    lines = [
        f"🔑 Ключевые фразы для автопубликации (стр. {page}/{total_pages}):\n",
        "Если текст поста содержит хотя бы одну из этих фраз и проходит "
        "остальные проверки — он публикуется автоматически. Если ни одной "
        "фразы нет — пост уходит на ручную модерацию.\n",
    ]
    start_idx = (page - 1) * 10 + 1
    for i, (phrase, added_by, added_time) in enumerate(phrases, start_idx):
        lines.append(f"{i}. <code>{phrase}</code>")

    await cb.message.edit_text(
        "\n".join(lines), parse_mode='HTML', reply_markup=auto_phrase_menu(page, total_pages)
    )


@router.callback_query(F.data.startswith("auto_phrase_page_"))
async def auto_phrase_page_handler(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    try:
        page = int(cb.data.split("_")[3])
        await show_auto_phrase_page(cb, page)
    except (ValueError, IndexError):
        await cb.answer("❌ Ошибка при загрузке страницы", show_alert=True)


@router.callback_query(F.data == "add_auto_phrase")
async def add_auto_phrase_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)

    await state.set_state(AutoPhraseState.wait_phrase)
    await cb.message.edit_text(
        "🔑 Добавление ключевой фразы\n\n"
        "Отправьте фразу — если текст поста будет её содержать (без учёта "
        "регистра), пост станет кандидатом на автопубликацию.\n\n"
        "Примеры: «что за», «расскажите про», «понравилась»",
        parse_mode='HTML',
        reply_markup=auto_phrase_cancel_menu()
    )


@router.message(AutoPhraseState.wait_phrase)
async def process_add_auto_phrase(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return

    phrase = msg.text.strip()
    if len(phrase) < 2:
        return await msg.answer("❌ Фраза должна содержать минимум 2 символа.")

    success = await add_auto_approve_phrase(phrase, msg.from_user.id)
    await state.clear()

    if success:
        await msg.answer(
            f"✅ Фраза добавлена: <code>{phrase}</code>",
            parse_mode='HTML',
            reply_markup=auto_phrase_menu()
        )
        await log("auto_phrase_add", f"admin {msg.from_user.id} added '{phrase}'")
    else:
        await msg.answer(
            f"❌ Фраза <code>{phrase}</code> уже есть в списке.",
            parse_mode='HTML',
            reply_markup=auto_phrase_menu()
        )


@router.callback_query(F.data == "remove_auto_phrase")
async def remove_auto_phrase_start(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)

    phrases, _ = await get_auto_approve_phrases(page=1, per_page=100)
    if not phrases:
        return await cb.answer("📋 Список фраз пуст.", show_alert=True)

    keyboard = [
        [InlineKeyboardButton(text=phrase, callback_data=f"rm_auto_phrase_{i}")]
        for i, (phrase, _, _) in enumerate(phrases)
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="auto_phrase_list")])

    await cb.message.edit_text(
        "🗑️ Удаление фразы\n\nВыберите фразу для удаления:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    # Список фраз для этого сеанса удаления храним в самих callback_data
    # (индекс), поэтому здесь же пересчитаем и сохраним "карту" через
    # повторный запрос при клике — см. process_remove_auto_phrase.


@router.callback_query(F.data.startswith("rm_auto_phrase_"))
async def process_remove_auto_phrase(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)

    try:
        index = int(cb.data.split("_")[3])
    except (ValueError, IndexError):
        return await cb.answer("❌ Ошибка", show_alert=True)

    phrases, _ = await get_auto_approve_phrases(page=1, per_page=100)
    if index < 0 or index >= len(phrases):
        return await cb.answer("❌ Фраза не найдена (список изменился, попробуйте снова).", show_alert=True)

    phrase = phrases[index][0]
    await remove_auto_approve_phrase(phrase, cb.from_user.id)
    await log("auto_phrase_remove", f"admin {cb.from_user.id} removed '{phrase}'")

    await cb.answer(f"✅ Фраза «{phrase}» удалена", show_alert=True)
    await show_auto_phrase_page(cb, page=1)
