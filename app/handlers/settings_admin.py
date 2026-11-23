"""
Настройки бота "на лету" и экспорт пользователей — доступно только двум
супер-администраторам (app.config.SUPER_ADMINS), а не всем ADMINS.
"""
import logging
import asyncio
import os
import tempfile

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, FSInputFile
from aiogram.fsm.context import FSMContext

from app.config import SUPER_ADMINS
from app.database import log, get_user_export_stats
from app.keyboards import (
    settings_list_keyboard, settings_categories_keyboard, settings_edit_cancel_keyboard, admin_menu,
    intro_comment_settings_keyboard, intro_comment_keyboard,
)
from app.states import SettingsState
from app import runtime_settings as rs
from app.services import get_bot_username
from app.utils import h

logger = logging.getLogger(__name__)

router = Router()

_INTRO_COMMENT_KEYS = (
    "INTRO_COMMENT_TEXT",
    "INTRO_COMMENT_BTN1_LABEL", "INTRO_COMMENT_BTN1_URL",
    "INTRO_COMMENT_BTN2_LABEL", "INTRO_COMMENT_BTN2_URL",
    "INTRO_COMMENT_BTN3_LABEL", "INTRO_COMMENT_BTN3_URL",
)


def _is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMINS


async def _preview_keyboard():
    """Превью клавиатуры первого комментария с реальными deep-link кнопками
    (post_id=0 — тестовый, чтобы админ видел кнопки "Узнать автора"/
    "Удалить пост" в превью в том же виде, что и в реальном комментарии)."""
    from app.keyboards import intro_comment_keyboard
    bot_username = await get_bot_username()
    return intro_comment_keyboard(0, bot_username)


# ================== ОБЩИЙ СПИСОК НАСТРОЕК ==================
def _short(value: str, limit: int = 18) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _category_keyboard(idx: int):
    _, keys = rs.SETTINGS_CATEGORIES[idx]
    items = [(key, f"{rs.get_label(key)}: {_short(rs.display_value(key))}") for key in keys]
    return settings_list_keyboard(items)


def _back_for(key: str) -> str:
    if key in _INTRO_COMMENT_KEYS:
        return "settings_intro_comment"
    idx = rs.category_of(key)
    return f"settings_cat_{idx}" if idx is not None else "settings_list"


@router.callback_query(F.data == "settings_list")
async def settings_list(cb: CallbackQuery, state: FSMContext):
    if not _is_super_admin(cb.from_user.id):
        return await cb.answer("🚫 Доступно только супер-администраторам.", show_alert=True)
    await state.clear()
    await cb.message.edit_text(
        "⚙️ <b>Настройки бота</b>\n\n"
        "Выберите раздел. Изменения применяются сразу, без перезапуска.",
        parse_mode='HTML',
        reply_markup=settings_categories_keyboard([title for title, _ in rs.SETTINGS_CATEGORIES]),
    )


@router.callback_query(F.data.startswith("settings_cat_"))
async def settings_category(cb: CallbackQuery, state: FSMContext):
    if not _is_super_admin(cb.from_user.id):
        return await cb.answer("🚫 Доступно только супер-администраторам.", show_alert=True)
    try:
        idx = int(cb.data.rsplit("_", 1)[1])
        title, _ = rs.SETTINGS_CATEGORIES[idx]
    except (ValueError, IndexError):
        return await cb.answer("❌ Раздел не найден.", show_alert=True)
    await state.clear()
    await cb.message.edit_text(
        f"⚙️ <b>{title}</b>\n\nНажмите на параметр, чтобы изменить его.",
        parse_mode='HTML',
        reply_markup=_category_keyboard(idx),
    )


# ================== РАЗДЕЛ: ПЕРВЫЙ КОММЕНТАРИЙ ПОД ПОСТОМ ==================
@router.callback_query(F.data == "settings_intro_comment")
async def settings_intro_comment(cb: CallbackQuery):
    if not _is_super_admin(cb.from_user.id):
        return await cb.answer("🚫 Доступно только супер-администраторам.", show_alert=True)

    await cb.message.edit_text(
        "💬 <b>Первый комментарий под постом</b>\n\n"
        "Бот автоматически оставляет этот комментарий первым под каждым "
        "новым постом в канале (требуется, чтобы у канала была подключена "
        "группа обсуждений и она же была указана как "
        "<code>COMMENTS_CHAT_ID</code>).\n\n"
        "Выберите, что изменить:",
        parse_mode='HTML',
        reply_markup=intro_comment_settings_keyboard()
    )
    # Превью — отдельным сообщением, чтобы админ сразу видел, как это
    # выглядит в реальности (текст + кликабельные кнопки), а не только
    # значения полей текстом.
    try:
        await cb.message.answer(
            "👁 <b>Превью:</b>",
            parse_mode='HTML',
        )
        await cb.message.answer(
            rs.get("INTRO_COMMENT_TEXT"),
            reply_markup=await _preview_keyboard(),
        )
    except Exception:
        logger.exception("Не удалось отправить превью первого комментария")


# ================== РЕДАКТИРОВАНИЕ ОДНОЙ НАСТРОЙКИ ==================
@router.callback_query(F.data.startswith("settings_edit_"))
async def settings_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _is_super_admin(cb.from_user.id):
        return await cb.answer("🚫 Доступно только супер-администраторам.", show_alert=True)

    key = cb.data[len("settings_edit_"):]
    if key not in rs.SETTINGS_SCHEMA:
        return await cb.answer("❌ Неизвестная настройка.", show_alert=True)

    await state.set_state(SettingsState.wait_new_value)
    await state.update_data(key=key)

    value_type = rs.SETTINGS_SCHEMA[key][0]
    if key.endswith("_URL"):
        type_hint = "ссылка, начинается с https://"
    else:
        type_hint = {
            "int": "целое число, например 100",
            "float": "число, например 0.87",
            "list": "ID через запятую, например 111111,222222",
        }.get(value_type.__name__, "текст")

    await cb.message.edit_text(
        f"⚙️ <b>{rs.get_label(key)}</b>\n\n"
        f"Текущее значение: <code>{h(rs.display_value(key))}</code>\n\n"
        f"Пришлите новое значение ({type_hint}):",
        parse_mode='HTML',
        reply_markup=settings_edit_cancel_keyboard(_back_for(key))
    )


@router.message(SettingsState.wait_new_value)
async def settings_edit_process(msg: Message, state: FSMContext):
    if not _is_super_admin(msg.from_user.id):
        return

    data = await state.get_data()
    key = data.get("key")
    await state.clear()

    if not key:
        return await msg.answer("❌ Ошибка: настройка не найдена, начните заново.")

    if not msg.text:
        await state.set_state(SettingsState.wait_new_value)
        await state.update_data(key=key)
        return await msg.answer("❌ Пришлите значение обычным текстом.", reply_markup=settings_edit_cancel_keyboard(_back_for(key)))

    ok, result_msg = await rs.set_value(key, msg.text.strip())

    if not ok:
        # Оставляем пользователя в режиме ввода — можно сразу прислать
        # исправленное значение, не открывая настройку заново.
        await state.set_state(SettingsState.wait_new_value)
        await state.update_data(key=key)
        return await msg.answer(
            f"❌ {h(result_msg)}",
            reply_markup=settings_edit_cancel_keyboard(_back_for(key))
        )

    await log("settings_change", f"admin {msg.from_user.id}: {key} = {rs.display_value(key)}")

    if key in _INTRO_COMMENT_KEYS:
        await msg.answer(
            f"✅ Настройка «{rs.get_label(key)}» изменена на <code>{h(rs.display_value(key))}</code>",
            parse_mode='HTML',
            reply_markup=intro_comment_settings_keyboard()
        )
        # Свежее превью сразу после изменения — видно результат немедленно.
        try:
            await msg.answer(
                rs.get("INTRO_COMMENT_TEXT"),
                reply_markup=await _preview_keyboard(),
            )
        except Exception:
            logger.exception("Не удалось отправить превью первого комментария после изменения")
        return

    idx = rs.category_of(key)
    await msg.answer(
        f"✅ Настройка «{rs.get_label(key)}» изменена на <code>{h(rs.display_value(key))}</code>",
        parse_mode='HTML',
        reply_markup=_category_keyboard(idx) if idx is not None else admin_menu(True),
    )


# ================== ЭКСПОРТ ПОЛЬЗОВАТЕЛЕЙ В EXCEL ==================
@router.callback_query(F.data == "export_users")
async def export_users(cb: CallbackQuery):
    if not _is_super_admin(cb.from_user.id):
        return await cb.answer("🚫 Доступно только супер-администраторам.", show_alert=True)

    await cb.answer("⏳ Формирую файл...")

    try:
        path = await _build_users_excel()
        try:
            await cb.message.answer_document(
                FSInputFile(path, filename="users.xlsx"),
                caption="📥 Список пользователей (по убыванию потраченных ⭐)",
            )
        finally:
            # Временный файл удаляем всегда, даже если отправка не удалась.
            try:
                os.remove(path)
            except OSError:
                pass
        await log("export_users", f"admin {cb.from_user.id} exported users list")
    except Exception as e:
        logger.exception("Ошибка при экспорте пользователей в Excel")
        await cb.message.answer(f"❌ Не удалось сформировать файл: {h(e)}")


async def _build_users_excel() -> str:
    rows = await get_user_export_stats()
    # openpyxl работает синхронно — на большой базе это блокировало бы весь
    # бот на время сборки файла, поэтому выносим в отдельный поток.
    return await asyncio.to_thread(_build_users_excel_sync, rows)


def _build_users_excel_sync(rows) -> str:
    from datetime import date
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Пользователи"

    headers = ["ID", "Юзернейм", "Дней в боте", "Постов написано", "Потрачено звёзд"]
    ws.append(headers)
    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    today = date.today()
    for user_id, username, reg_date, post_count, stars_spent in rows:
        days_in_bot = ""
        if reg_date:
            try:
                reg = date.fromisoformat(reg_date[:10])
                days_in_bot = (today - reg).days
            except ValueError:
                days_in_bot = ""
        ws.append([
            user_id,
            f"@{username}" if username else "—",
            days_in_bot,
            post_count,
            stars_spent,
        ])

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial")

    column_widths = [14, 22, 14, 16, 18]
    for i, width in enumerate(column_widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = width

    ws.freeze_panes = "A2"

    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="smotrbot_users_")
    os.close(fd)
    wb.save(path)
    return path
