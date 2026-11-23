"""Реклама: мастер создания, публикация сейчас или по расписанию, и
менеджер действующей рекламы.

Мастер (/post или «➕ Новая реклама»):
  тип → пост → (срок + закреп | комбо → подписка → рассылки) → сводка →
  «🚀 Опубликовать сейчас» или «🕐 Запланировать».

Комбо:
  24 часа -> 24ч публикация + 24ч закреп;
  48 часов -> 48ч + 48ч;
  72 часа -> 72ч + 72ч + 1 рассылка в боте;
  Неделя -> 7 дней + 72ч закреп + обязательная подписка на 24ч;
  Неделя+ -> 7 дней + 72ч закреп + обязательная подписка на 72ч + 3 рассылки.

Время рассылок задаётся либо кнопками «через N ч после публикации», либо
точной датой. Хранение относительного времени позволяет запланировать
публикацию на потом без пересчёта расписания.

Менеджер («📢 Реклама» в админ-панели): список запланированной и
действующей рекламы, карточка с продлением, снятием закрепа, удалением,
досрочной публикацией и отменой.
"""
import logging
import re
from datetime import datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.advertising_service import COMBOS, hours_label as _hours_label, pin_label as _pin_label, publish_ad
from app.auth import is_admin
from app.config import TIMEZONE
from app.database import (
    add_advertising_broadcast,
    cancel_advertising_post,
    cancel_scheduled_advertising_post,
    clear_advertising_scheduled_extras,
    count_pending_ad_broadcasts,
    create_advertising_post,
    extend_advertising_post,
    finish_advertising_expiry,
    get_ad,
    list_ads,
    log,
    reopen_advertising_post,
    schedule_advertising_post,
    set_advertising_combo_key,
    set_advertising_control,
    set_advertising_error,
    set_advertising_preview,
    set_advertising_subscription_target,
    set_advertising_type,
    update_advertising_duration,
    update_advertising_pin,
    update_advertising_source,
)
from app.keyboards import (
    advertising_combo_keyboard,
    advertising_duration_keyboard,
    advertising_pin_keyboard,
    advertising_retry_keyboard,
    advertising_subscription_back_keyboard,
    advertising_type_back_keyboard,
    advertising_type_keyboard,
)
from app.loader import bot
from app.runtime_settings import get as get_setting
from app.states import AdvertisingState
from app.tg_ui import DANGER, PRIMARY, SUCCESS, btn, show_screen, tg_time, tg_time_iso
from app.utils import h

logger = logging.getLogger(__name__)
router = Router()

DT_RE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s+(\d{1,2})[:.](\d{2})\s*$")
BROADCAST_OFFSETS = (1, 3, 6, 12, 24, 48, 72, 120)


def _is_admin(obj) -> bool:
    return is_admin(obj.from_user.id)


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _parse_dt(text: str) -> datetime | None:
    """«06.09.2026 18:30», «06.09 18:30» (текущий год), «6/9/26 18.30»."""
    m = DT_RE.match(text or "")
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    year = int(year) if year else _now().year
    if year < 100:
        year += 2000
    try:
        return datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=TIMEZONE)
    except ValueError:
        return None


# ------------------------------------------------------------------
# Расписание рассылок: элементы {"offset": часы} или {"abs": iso}
# ------------------------------------------------------------------
def _normalize_times(raw) -> list[dict]:
    result = []
    for item in raw or []:
        if isinstance(item, str):  # формат до обновления — абсолютное время
            result.append({"abs": item})
        elif isinstance(item, dict):
            result.append(item)
    return result


def _resolve(item: dict, publish_at: datetime) -> datetime:
    if "offset" in item:
        return publish_at + timedelta(hours=float(item["offset"]))
    return datetime.fromisoformat(item["abs"])


def _describe(item: dict) -> str:
    if "offset" in item:
        return f"через {_hours_label(int(item['offset']))} после публикации"
    return tg_time(datetime.fromisoformat(item["abs"]))


def _check_times(times: list[dict], publish_at: datetime, duration_hours: int) -> str | None:
    end = publish_at + timedelta(hours=duration_hours)
    previous = None
    for i, item in enumerate(times, 1):
        when = _resolve(item, publish_at)
        if when <= publish_at:
            return f"Рассылка {i} раньше публикации."
        if when <= _now() + timedelta(minutes=1):
            return f"Время рассылки {i} уже прошло."
        if when > end:
            return f"Рассылка {i} выходит за оплаченный срок публикации."
        if previous and when <= previous:
            return f"Рассылка {i} должна быть позже предыдущей."
        previous = when
    return None


# ------------------------------------------------------------------
# Клавиатуры (относятся только к рекламе)
# ------------------------------------------------------------------
def _confirm_kb(ad_id: int, ad_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Опубликовать сейчас", f"ad_confirm:{ad_id}", style=SUCCESS)],
        [btn("🕐 Запланировать", f"ad_schedule:{ad_id}", style=PRIMARY)],
        [btn("↩️ Назад", f"ad_confirm_back:{ad_id}:{ad_type}"), btn("✏️ Заменить пост", f"ad_replace:{ad_id}")],
        [btn("❌ Отмена", f"ad_cancel:{ad_id}", style=DANGER)],
    ])


def _broadcast_kb(ad_id: int, duration: int, times: list[dict]) -> InlineKeyboardMarkup:
    last_offset = max([t["offset"] for t in times if "offset" in t] or [0])
    options = [o for o in BROADCAST_OFFSETS if last_offset < o < duration]
    buttons = [btn(f"+{_hours_label(o)}", f"ad_bt:{ad_id}:{o}") for o in options]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([btn("↩️ Назад", f"ad_broadcast_back:{ad_id}"), btn("❌ Отмена", f"ad_cancel:{ad_id}", style=DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _schedule_kb(ad_id: int) -> InlineKeyboardMarkup:
    now = _now()
    options = [("через 1 ч", now + timedelta(hours=1)), ("через 3 ч", now + timedelta(hours=3))]
    for label, day_shift, hour in (("сегодня 19:00", 0, 19), ("сегодня 21:00", 0, 21),
                                   ("завтра 10:00", 1, 10), ("завтра 19:00", 1, 19)):
        when = (now + timedelta(days=day_shift)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if when > now + timedelta(minutes=10):
            options.append((label, when))
    buttons = [btn(label, f"ad_sched_at:{ad_id}:{int(when.timestamp())}") for label, when in options]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("↩️ Назад к сводке", f"ad_show_confirm:{ad_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ad_card_kb(ad: dict, link: str | None) -> InlineKeyboardMarkup:
    ad_id = ad["id"]
    rows = []
    if ad["status"] == "published":
        if link:
            rows.append([btn("🔗 Открыть в канале", url=link)])
        rows.append([btn("⏱ +24 ч", f"ad_extend:{ad_id}:24"), btn("⏱ +7 дней", f"ad_extend:{ad_id}:168")])
        if ad["pin_expires_at"]:
            rows.append([btn("📍 Снять закреп", f"ad_unpin_now:{ad_id}")])
        rows.append([btn("🗑 Удалить сейчас", f"ad_delete_now:{ad_id}", style=DANGER)])
    elif ad["status"] in ("scheduled", "error"):
        label = "🔄 Повторить публикацию" if ad["status"] == "error" else "🚀 Опубликовать сейчас"
        rows.append([btn(label, f"ad_publish_scheduled:{ad_id}", style=SUCCESS)])
        rows.append([btn("❌ Отменить публикацию", f"ad_unschedule:{ad_id}", style=DANGER)])
    rows.append([btn("⬅️ К списку рекламы", "ad_hub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _done_kb(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📋 Управлять", f"ad_card:{ad_id}", style=PRIMARY)],
        [btn("📢 Вся реклама", "ad_hub")],
    ])


# ------------------------------------------------------------------
# Менеджер рекламы
# ------------------------------------------------------------------
_STATUS_ICON = {"scheduled": "🕐", "published": "🟢", "error": "⚠️"}


def _short_dt(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(TIMEZONE).strftime("%d.%m %H:%M") if iso else ""
    except (TypeError, ValueError):
        return ""


async def _hub_text_and_kb():
    ads = await list_ads()
    rows = [[btn("➕ Новая реклама", "ad_new", style=PRIMARY)]]
    if not ads:
        text = "📢 <b>Реклама</b>\n\nСейчас нет запланированной или действующей рекламы."
    else:
        lines = ["📢 <b>Реклама</b>\n"]
        for ad in ads:
            icon = _STATUS_ICON.get(ad["status"], "•")
            pinned = bool(ad["pin_expires_at"])
            if ad["status"] == "error":
                when = short = "ошибка публикации"
            elif ad["status"] == "scheduled":
                when = f"выйдет {tg_time_iso(ad['publish_at'], '%d.%m %H:%M')}"
                short = f"выйдет {_short_dt(ad['publish_at'])}"
            else:
                when = f"до {tg_time_iso(ad['expires_at'], '%d.%m %H:%M')}"
                short = f"до {_short_dt(ad['expires_at'])}"
            lines.append(f"{icon} <b>#{ad['id']}</b> · {_hours_label(int(ad['duration_hours']))}"
                         f"{' · 📌' if pinned else ''} · {when}")
            rows.append([btn(f"{icon} #{ad['id']} · {short}{' 📌' if pinned else ''}", f"ad_card:{ad['id']}")])
        text = "\n".join(lines) + "\n\n<i>Нажмите на рекламу, чтобы управлять ею.</i>"
    rows.append([btn("⬅️ В админ-панель", "admin_panel")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_hub(message: Message) -> None:
    text, kb = await _hub_text_and_kb()
    await show_screen(message, text, reply_markup=kb)


@router.callback_query(F.data.in_({"admin_ad_post", "ad_hub"}))
async def ad_hub(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    await cb.answer()
    await _show_hub(cb.message)


async def _ad_card_text(ad: dict) -> str:
    combo = COMBOS.get(ad["combo_key"] or "")
    mode = f"🎁 Комбо «{combo['label']}»" if combo else "📝 Пост"
    lines = [f"📢 <b>Реклама #{ad['id']}</b> · {mode}\n"]
    if ad["status"] == "error":
        lines.append("⚠️ Публикация не удалась (Telegram отказал). Можно повторить.")
        lines.append(f"⏱ Срок: {_hours_label(int(ad['duration_hours']))}")
    elif ad["status"] == "scheduled":
        lines.append(f"🕐 Публикация: {tg_time_iso(ad['publish_at'])}")
        lines.append(f"⏱ Срок: {_hours_label(int(ad['duration_hours']))}")
    else:
        lines.append(f"🟢 Опубликована: {tg_time_iso(ad['published_at'])}")
        lines.append(f"⏱ Удалится: {tg_time_iso(ad['expires_at'])}")
    if ad["pin_expires_at"]:
        lines.append(f"📌 Закреп до: {tg_time_iso(ad['pin_expires_at'])}")
    elif ad["status"] == "scheduled" and ad["pin_duration_hours"]:
        lines.append(f"📌 Закреп: {_pin_label(int(ad['pin_duration_hours']))}")
    if ad["subscription_name"]:
        lines.append(f"🔐 Обязательная подписка: {h(ad['subscription_name'])} на {ad['subscription_hours']} ч.")
    pending = await count_pending_ad_broadcasts(ad["id"])
    if pending:
        lines.append(f"📨 Рассылок впереди: {pending}")
    return "\n".join(lines)


async def _show_ad_card(message: Message, ad_id: int) -> bool:
    from app.services import channel_post_link
    ad = await get_ad(ad_id)
    if not ad or ad["status"] not in ("scheduled", "published", "error"):
        return False
    link = await channel_post_link(ad["channel_message_id"]) if ad["channel_message_id"] else None
    await show_screen(message, await _ad_card_text(ad), reply_markup=_ad_card_kb(ad, link))
    return True


@router.callback_query(F.data.startswith("ad_card:"))
async def ad_card(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    if await _show_ad_card(cb.message, ad_id):
        return await cb.answer()
    await cb.answer("Эта реклама уже завершена.", show_alert=True)
    await _show_hub(cb.message)


@router.callback_query(F.data.startswith("ad_extend:"))
async def ad_extend(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    _, ad_id_s, hours_s = cb.data.split(":")
    ad_id, hours = int(ad_id_s), int(hours_s)
    if not await extend_advertising_post(ad_id, hours):
        return await cb.answer("⚠️ Реклама уже не активна.", show_alert=True)
    await log("advertising_extend", f"admin {cb.from_user.id}: ad #{ad_id} +{hours}h")
    await cb.answer(f"✅ Продлено на {_hours_label(hours)}")
    await _show_ad_card(cb.message, ad_id)


@router.callback_query(F.data.startswith("ad_unschedule:"))
async def ad_unschedule(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_ad(ad_id)
    if ad and ad["status"] == "error":
        await reopen_advertising_post(ad_id)
        await cancel_advertising_post(ad_id)
    elif not await cancel_scheduled_advertising_post(ad_id):
        return await cb.answer("⚠️ Реклама уже опубликована или отменена.", show_alert=True)
    await log("advertising_unschedule", f"admin {cb.from_user.id}: ad #{ad_id}")
    await cb.answer("Публикация отменена")
    await _show_hub(cb.message)


@router.callback_query(F.data.startswith("ad_publish_scheduled:"))
async def ad_publish_scheduled(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await cb.answer("⏳ Публикую...")
    ad = await get_ad(ad_id)
    if ad and ad["status"] == "error":
        await reopen_advertising_post(ad_id)  # error -> draft
    result = await publish_ad(ad_id)
    if not result.ok:
        return await cb.message.answer("⚠️ Не удалось опубликовать: реклама уже обработана или Telegram отказал.")
    await _show_ad_card(cb.message, ad_id)


@router.callback_query(F.data.startswith("ad_delete_now:"))
async def delete_ad_now(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_ad(ad_id)
    if not ad or ad["status"] != "published" or not ad["channel_message_id"]:
        return await cb.answer("⚠️ Рекламный пост уже удалён.", show_alert=True)
    channel_id = get_setting("MAIN_CHANNEL_ID")
    try:
        try:
            await bot.unpin_chat_message(channel_id, message_id=ad["channel_message_id"])
        except Exception:
            pass
        await bot.delete_message(channel_id, ad["channel_message_id"])
    except Exception as e:
        if "message to delete not found" not in str(e).lower():
            logger.warning("Не удалось досрочно удалить рекламу #%s: %s", ad_id, e)
            return await cb.answer("Не удалось удалить пост.", show_alert=True)
    await finish_advertising_expiry(ad_id, deleted=True)
    await log("advertising_delete", f"admin {cb.from_user.id}: ad #{ad_id}")
    await cb.answer("🗑 Удалено")
    await _show_hub(cb.message)


@router.callback_query(F.data.startswith("ad_unpin_now:"))
async def unpin_ad_now(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_ad(ad_id)
    if not ad or not ad["channel_message_id"]:
        return await cb.answer("⚠️ Рекламный пост не найден.", show_alert=True)
    try:
        await bot.unpin_chat_message(get_setting("MAIN_CHANNEL_ID"), message_id=ad["channel_message_id"])
    except Exception:
        return await cb.answer("Не удалось снять закреп.", show_alert=True)
    # Раньше срок закрепа оставался в БД, и бот потом снимал его повторно.
    await finish_advertising_expiry(ad_id, deleted=False)
    await cb.answer("📍 Закреп снят")
    await _show_ad_card(cb.message, ad_id)


# ------------------------------------------------------------------
# Мастер создания
# ------------------------------------------------------------------
def _master_text() -> str:
    return (
        "📢 <b>Новая реклама</b>\n\n"
        "📝 <b>Пост</b> — срок публикации и закрепа выбираются отдельно.\n"
        "🎁 <b>Комбо</b> — готовый пакет с дополнительными услугами."
    )


async def _start_master(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdvertisingState.wait_type)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.answer(_master_text(), parse_mode="HTML", reply_markup=advertising_type_keyboard())
    else:
        await target.answer(_master_text(), parse_mode="HTML", reply_markup=advertising_type_keyboard())


@router.callback_query(F.data == "ad_new")
async def ad_new(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    await _start_master(cb, state)


@router.message(Command("post"))
async def admin_post_command(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return await msg.answer("🚫 У вас нет доступа к этой команде.")
    await _start_master(msg, state)


@router.callback_query(F.data == "ad_type:post")
async def choose_ad_type_post(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    # При переключении с «Комбо» на «Пост» старые параметры комбо не переносятся.
    await state.set_state(AdvertisingState.wait_post)
    await state.update_data(
        ad_type="post", combo_key=None, subscription=None,
        subscription_hours=0, broadcast_times=[], broadcast_count=0,
    )
    await cb.answer()
    await cb.message.edit_text(
        "📝 <b>Пост</b>\n\nОтправьте рекламный пост одним сообщением — текст, фото или "
        "видео. Форматирование и ссылки сохранятся.",
        parse_mode="HTML", reply_markup=advertising_type_back_keyboard(),
    )


@router.callback_query(F.data == "ad_type:combo")
async def choose_ad_type_combo(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    await state.set_state(AdvertisingState.wait_post)
    await state.update_data(ad_type="combo", broadcast_times=[])
    await cb.answer()
    lines = "\n".join(
        f"• <b>{c['label']}</b> — {_hours_label(c['duration'])}, закреп {_pin_label(c['pin'])}"
        + (f", подписка {c['subscription']} ч." if c["subscription"] else "")
        + (f", рассылок: {c['broadcasts']}" if c["broadcasts"] else "")
        for c in COMBOS.values()
    )
    await cb.message.edit_text(
        f"🎁 <b>Комбо</b>\n\n{lines}\n\nОтправьте рекламный пост одним сообщением.",
        parse_mode="HTML", reply_markup=advertising_type_back_keyboard(),
    )


@router.callback_query(F.data == "ad_type_back")
async def back_to_ad_type(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    await state.set_state(AdvertisingState.wait_type)
    await cb.answer()
    await cb.message.edit_text(_master_text(), parse_mode="HTML", reply_markup=advertising_type_keyboard())


async def _cancel_ad(ad_id, state: FSMContext, cb_or_msg):
    if ad_id:
        await cancel_advertising_post(int(ad_id))
    await state.clear()
    if isinstance(cb_or_msg, CallbackQuery):
        await cb_or_msg.answer("Отменено")
        await cb_or_msg.message.edit_text("❌ Создание рекламной публикации отменено.")
    else:
        await cb_or_msg.answer("❌ Создание рекламной публикации отменено.")


@router.callback_query(F.data == "ad_abort")
async def abort_advertising(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    data = await state.get_data()
    await _cancel_ad(data.get("ad_id"), state, cb)


@router.message(Command("cancel"), AdvertisingState.wait_type)
@router.message(Command("cancel"), AdvertisingState.wait_post)
@router.message(Command("cancel"), AdvertisingState.wait_duration)
@router.message(Command("cancel"), AdvertisingState.wait_pin)
@router.message(Command("cancel"), AdvertisingState.wait_combo)
@router.message(Command("cancel"), AdvertisingState.wait_subscription)
@router.message(Command("cancel"), AdvertisingState.wait_broadcast_time)
@router.message(Command("cancel"), AdvertisingState.wait_confirm)
@router.message(Command("cancel"), AdvertisingState.wait_publish_time)
async def advertising_cancel_command(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    await _cancel_ad(data.get("ad_id"), state, msg)


async def _own_draft(ad_id: int, user_id: int) -> dict | None:
    ad = await get_ad(ad_id)
    if not ad or ad["admin_id"] != user_id or ad["status"] != "draft":
        return None
    return ad


@router.message(AdvertisingState.wait_post)
async def receive_advertising_post(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    ad_id = data.get("ad_id")
    ad_type = data.get("ad_type", "post")
    try:
        if ad_id and not await _own_draft(int(ad_id), msg.from_user.id):
            ad_id = None
        if not ad_id:
            ad_id = await create_advertising_post(msg.from_user.id, msg.chat.id, msg.message_id, 24, ad_type)
        else:
            old = await get_ad(int(ad_id))
            if old and old["preview_chat_id"] and old["preview_message_id"]:
                try:
                    await bot.delete_message(old["preview_chat_id"], old["preview_message_id"])
                except Exception:
                    pass
            await update_advertising_source(int(ad_id), msg.chat.id, msg.message_id)
            await set_advertising_type(int(ad_id), ad_type)

        preview = await bot.copy_message(chat_id=msg.chat.id, from_chat_id=msg.chat.id, message_id=msg.message_id)
        await set_advertising_preview(int(ad_id), msg.chat.id, preview.message_id)
        await state.update_data(ad_id=int(ad_id), ad_type=ad_type)

        if ad_type == "combo":
            control = await msg.answer("👆 Так пост выйдет в канале.\n\n🎁 Выберите пакет:",
                                       reply_markup=advertising_combo_keyboard(int(ad_id)))
            await state.set_state(AdvertisingState.wait_combo)
        else:
            control = await msg.answer("👆 Так пост выйдет в канале.\n\n⏱ На сколько публиковать?",
                                       reply_markup=advertising_duration_keyboard(int(ad_id)))
            await state.set_state(AdvertisingState.wait_duration)
        await set_advertising_control(int(ad_id), msg.chat.id, control.message_id)
    except Exception as e:
        if ad_id:
            await set_advertising_error(int(ad_id), str(e))
        logger.exception("Не удалось подготовить рекламный пост #%s", ad_id)
        await state.clear()
        await msg.answer("⚠️ Не удалось подготовить рекламный пост. Попробуйте /post ещё раз.")


@router.callback_query(F.data.startswith("ad_replace:"))
async def replace_ad_source(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await _own_draft(ad_id, cb.from_user.id)
    if not ad:
        return await cb.answer("⚠️ Черновик уже недоступен.", show_alert=True)
    await state.update_data(ad_id=ad_id, ad_type=ad["ad_type"] or "post")
    await state.set_state(AdvertisingState.wait_post)
    await cb.answer()
    await cb.message.edit_text("✏️ Отправьте новый рекламный пост.", reply_markup=advertising_type_back_keyboard())


@router.callback_query(F.data.startswith("ad_duration:"))
async def choose_ad_duration(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_duration.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    _, ad_id_s, hours_s = cb.data.split(":")
    ad_id, hours = int(ad_id_s), int(hours_s)
    if not await _own_draft(ad_id, cb.from_user.id):
        return await cb.answer("⚠️ Черновик не найден.", show_alert=True)
    await update_advertising_duration(ad_id, hours)
    await set_advertising_combo_key(ad_id, None)
    await state.update_data(ad_id=ad_id, duration_hours=hours, ad_type="post", combo_key=None)
    await state.set_state(AdvertisingState.wait_pin)
    await cb.answer()
    await cb.message.edit_text(
        f"⏱ Публикация: <b>{_hours_label(hours)}</b>\n\n📌 На сколько закрепить?",
        parse_mode="HTML", reply_markup=advertising_pin_keyboard(ad_id, hours),
    )


@router.callback_query(F.data.startswith("ad_duration_back:"))
@router.callback_query(F.data.startswith("ad_pin_back:"))
async def back_to_duration(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await state.set_state(AdvertisingState.wait_duration)
    await cb.answer()
    await cb.message.edit_text("⏱ На сколько публиковать?", reply_markup=advertising_duration_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_pin:"))
async def choose_ad_pin(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_pin.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    _, ad_id_s, pin_s = cb.data.split(":")
    ad_id, pin_hours = int(ad_id_s), int(pin_s)
    ad = await _own_draft(ad_id, cb.from_user.id)
    if not ad:
        return await cb.answer("⚠️ Черновик не найден.", show_alert=True)
    if pin_hours > int(ad["duration_hours"]):
        return await cb.answer("⚠️ Закреп не может быть дольше публикации.", show_alert=True)
    await update_advertising_pin(ad_id, pin_hours)
    await state.update_data(ad_id=ad_id, pin_hours=pin_hours, ad_type="post", broadcast_times=[])
    await state.set_state(AdvertisingState.wait_confirm)
    await cb.answer()
    await _show_confirmation(cb.message, state, ad_id, edit=True)


@router.callback_query(F.data.startswith("ad_combo:"))
async def choose_ad_combo(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_combo.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    _, ad_id_s, combo_key = cb.data.split(":")
    ad_id = int(ad_id_s)
    combo = COMBOS.get(combo_key)
    if not combo or not await _own_draft(ad_id, cb.from_user.id):
        return await cb.answer("⚠️ Комбо или черновик недоступны.", show_alert=True)

    await update_advertising_duration(ad_id, combo["duration"])
    await update_advertising_pin(ad_id, combo["pin"])
    await set_advertising_type(ad_id, "combo")
    await set_advertising_combo_key(ad_id, combo_key)
    # При смене пакета старые настройки не должны «протечь» в новый выбор.
    await clear_advertising_scheduled_extras(ad_id)
    await set_advertising_subscription_target(ad_id, None)
    await state.update_data(
        ad_id=ad_id, duration_hours=combo["duration"], pin_hours=combo["pin"],
        combo_key=combo_key, ad_type="combo", broadcast_count=combo["broadcasts"],
        broadcast_times=[], subscription=None, subscription_hours=combo["subscription"],
    )
    await cb.answer()

    if combo["subscription"]:
        await state.set_state(AdvertisingState.wait_subscription)
        await cb.message.edit_text(
            f"🎁 <b>{combo['label']}</b>\n\n"
            "🔐 Пришлите канал или бота для обязательной подписки: "
            "<code>@username</code> или <code>https://t.me/username</code>.\n\n"
            "<i>Для канала бот должен быть его участником, чтобы проверять подписку.</i>",
            parse_mode="HTML", reply_markup=advertising_subscription_back_keyboard(ad_id),
        )
    elif combo["broadcasts"]:
        await _ask_broadcast_time(cb.message, state, ad_id, edit=True)
    else:
        await state.set_state(AdvertisingState.wait_confirm)
        await _show_confirmation(cb.message, state, ad_id, edit=True)


@router.message(AdvertisingState.wait_subscription)
async def receive_subscription_target(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    ad_id = int(data["ad_id"])
    hours = int(data.get("subscription_hours", 0))
    raw = (msg.text or "").strip()
    token = raw.rsplit("/", 1)[-1] if raw.startswith(("https://t.me/", "http://t.me/")) else raw
    if token.startswith("@"):
        username = token
    elif re.fullmatch(r"[A-Za-z0-9_]{4,64}", token):
        username = "@" + token
    else:
        return await msg.answer("❌ Нужен @username или ссылка https://t.me/username.")

    try:
        chat = await bot.get_chat(username)
    except Exception as e:
        logger.warning("Реклама: не удалось найти %s: %s", username, e)
        return await msg.answer("❌ Не нашёл такой канал/бот. Проверьте username и попробуйте ещё раз.")

    chat_type = getattr(chat, "type", "")
    url = raw if raw.startswith("http") else f"https://t.me/{username.lstrip('@')}"
    if chat_type in ("channel", "supergroup", "group"):
        try:
            member = await bot.get_chat_member(chat.id, bot.id)
            if member.status not in ("administrator", "creator", "member"):
                raise ValueError(member.status)
        except Exception:
            return await msg.answer(
                "❌ Бот не состоит в этом канале/группе и не сможет проверять подписку. "
                "Добавьте бота и пришлите ссылку ещё раз."
            )
        sub = {"type": "channel" if chat_type == "channel" else "group", "id": str(chat.id),
               "username": username, "name": getattr(chat, "title", None) or username, "url": url}
    else:
        # Подписку на другого бота Bot API проверить не позволяет — такой
        # ресурс показывается как обязательный переход без проверки.
        sub = {"type": "bot", "id": str(getattr(chat, "id", username)), "username": username,
               "name": getattr(chat, "first_name", None) or getattr(chat, "title", None) or username, "url": url}

    await set_advertising_subscription_target(ad_id, sub, hours)
    await state.update_data(subscription=sub)
    await msg.answer(f"✅ Подписка: <b>{escape(str(sub['name']))}</b> на {hours} ч.", parse_mode="HTML")
    if int(data.get("broadcast_count", 0)):
        await _ask_broadcast_time(msg, state, ad_id)
    else:
        await state.set_state(AdvertisingState.wait_confirm)
        await _show_confirmation(msg, state, ad_id)


async def _ask_broadcast_time(target: Message, state: FSMContext, ad_id: int, edit: bool = False):
    data = await state.get_data()
    times = _normalize_times(data.get("broadcast_times"))
    count = int(data.get("broadcast_count", 0))
    duration = int(data.get("duration_hours", 0))
    await state.set_state(AdvertisingState.wait_broadcast_time)
    done = "".join(f"\n✅ {i}. {_describe(t)}" for i, t in enumerate(times, 1))
    text = (
        f"📨 <b>Рассылка {len(times) + 1} из {count}</b>{done}\n\n"
        "Когда отправить рассылку подписчикам бота? Выберите кнопкой (отсчёт от "
        "момента публикации) или пришлите точную дату: <code>06.09 18:30</code>."
    )
    kb = _broadcast_kb(ad_id, duration, times)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


async def _add_broadcast(target: Message, state: FSMContext, item: dict, edit: bool) -> None:
    data = await state.get_data()
    ad_id = int(data["ad_id"])
    times = _normalize_times(data.get("broadcast_times")) + [item]
    duration = int(data.get("duration_hours", 0))
    # Предварительная проверка «как если бы публиковали сейчас»; финальная —
    # при публикации/планировании с реальным временем выхода.
    if "abs" in item:
        error = _check_times([t for t in times if "abs" in t], _now(), duration)
        if error:
            return await target.answer(f"❌ {error}")
    await state.update_data(broadcast_times=times)
    if len(times) < int(data.get("broadcast_count", 0)):
        return await _ask_broadcast_time(target, state, ad_id, edit=edit)
    await state.set_state(AdvertisingState.wait_confirm)
    await _show_confirmation(target, state, ad_id, edit=edit)


@router.callback_query(F.data.startswith("ad_bt:"))
async def broadcast_quick_time(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_broadcast_time.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    offset = int(cb.data.split(":")[2])
    await cb.answer()
    await _add_broadcast(cb.message, state, {"offset": offset}, edit=True)


@router.message(AdvertisingState.wait_broadcast_time)
async def receive_broadcast_time(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    when = _parse_dt(msg.text or "")
    if not when:
        return await msg.answer("❌ Не понял дату. Пример: <code>06.09 18:30</code> или кнопка выше.", parse_mode="HTML")
    await _add_broadcast(msg, state, {"abs": when.isoformat()}, edit=False)


@router.callback_query(F.data.startswith("ad_broadcast_back:"))
async def back_from_broadcasts(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    data = await state.get_data()
    times = _normalize_times(data.get("broadcast_times"))
    await cb.answer()
    if times:  # шаг назад — убрать последнюю рассылку
        await state.update_data(broadcast_times=times[:-1])
        return await _ask_broadcast_time(cb.message, state, ad_id, edit=True)
    combo = COMBOS.get(data.get("combo_key") or "")
    if combo and combo["subscription"]:
        await state.set_state(AdvertisingState.wait_subscription)
        return await cb.message.edit_text("🔐 Пришлите канал или бота для обязательной подписки.",
                                          reply_markup=advertising_subscription_back_keyboard(ad_id))
    await state.set_state(AdvertisingState.wait_combo)
    await cb.message.edit_text("🎁 Выберите пакет:", reply_markup=advertising_combo_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_subscription_back:"))
async def back_from_subscription(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await state.set_state(AdvertisingState.wait_combo)
    await cb.answer()
    await cb.message.edit_text("🎁 Выберите пакет:", reply_markup=advertising_combo_keyboard(ad_id))


async def _confirmation_text(ad: dict, data: dict) -> str:
    combo = COMBOS.get(ad["combo_key"] or "")
    lines = [
        "🧾 <b>Проверьте рекламу</b>\n",
        f"🎁 Комбо: <b>{combo['label']}</b>" if combo else "📝 Режим: <b>пост</b>",
        f"⏱ Публикация: <b>{_hours_label(int(ad['duration_hours']))}</b>",
        f"📌 Закреп: <b>{_pin_label(int(ad['pin_duration_hours'] or 0))}</b>",
    ]
    sub = data.get("subscription")
    if sub:
        hours = combo["subscription"] if combo else 0
        lines.append(f"🔐 Подписка: <b>{escape(str(sub.get('name') or 'ресурс'))}</b> на {hours} ч.")
    for i, t in enumerate(_normalize_times(data.get("broadcast_times")), 1):
        lines.append(f"📨 Рассылка {i}: {_describe(t)}")
    lines.append("\nПост выше выйдет без изменений. Опубликовать сейчас или запланировать?")
    return "\n".join(lines)


async def _show_confirmation(target: Message, state: FSMContext, ad_id: int, edit: bool = False):
    ad = await get_ad(ad_id)
    data = await state.get_data()
    text = await _confirmation_text(ad, data)
    kb = _confirm_kb(ad_id, ad["ad_type"] or "post")
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("ad_show_confirm:"))
async def show_confirm_again(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    if not await _own_draft(ad_id, cb.from_user.id):
        return await cb.answer("⚠️ Черновик уже недоступен.", show_alert=True)
    await state.set_state(AdvertisingState.wait_confirm)
    await cb.answer()
    await _show_confirmation(cb.message, state, ad_id, edit=True)


@router.callback_query(F.data.startswith("ad_confirm_back:"))
async def back_from_confirmation(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    _, ad_id_s, kind = cb.data.split(":")
    ad_id = int(ad_id_s)
    ad = await _own_draft(ad_id, cb.from_user.id)
    if not ad:
        return await cb.answer("⚠️ Черновик уже недоступен.", show_alert=True)
    await cb.answer()
    if kind == "combo":
        data = await state.get_data()
        combo = COMBOS.get(ad["combo_key"] or "") or COMBOS["24"]
        times = _normalize_times(data.get("broadcast_times"))
        if combo["broadcasts"] and times:
            await state.update_data(broadcast_times=times[:-1])
            return await _ask_broadcast_time(cb.message, state, ad_id, edit=True)
        if combo["subscription"]:
            await state.set_state(AdvertisingState.wait_subscription)
            return await cb.message.edit_text("🔐 Пришлите канал или бота для обязательной подписки.",
                                              reply_markup=advertising_subscription_back_keyboard(ad_id))
        await state.set_state(AdvertisingState.wait_combo)
        return await cb.message.edit_text("🎁 Выберите пакет:", reply_markup=advertising_combo_keyboard(ad_id))
    await state.set_state(AdvertisingState.wait_pin)
    await cb.message.edit_text("📌 На сколько закрепить?",
                               reply_markup=advertising_pin_keyboard(ad_id, int(ad["duration_hours"])))


def _validate_ready(ad: dict, data: dict) -> str | None:
    if ad["ad_type"] != "combo":
        return None
    combo = COMBOS.get(ad["combo_key"] or "")
    if not combo:
        return "Не выбран пакет."
    if len(_normalize_times(data.get("broadcast_times"))) != combo["broadcasts"]:
        return "Не все рассылки настроены."
    if combo["subscription"] and not data.get("subscription"):
        return "Не настроена обязательная подписка."
    return None


async def _store_broadcasts(ad_id: int, data: dict, publish_at: datetime, duration: int) -> str | None:
    """Проверяет расписание относительно реального времени выхода и
    записывает его в БД. Старые записи удаляются — иначе после неудачной
    попытки и повтора остались бы прежние времена."""
    times = _normalize_times(data.get("broadcast_times"))
    error = _check_times(times, publish_at, duration)
    if error:
        return error
    await clear_advertising_scheduled_extras(ad_id)
    for idx, item in enumerate(times, 1):
        await add_advertising_broadcast(ad_id, idx, _resolve(item, publish_at))
    return None


@router.callback_query(F.data.startswith("ad_confirm:"))
async def confirm_advertising_publish(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_confirm.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await _own_draft(ad_id, cb.from_user.id)
    if not ad:
        return await cb.answer("⚠️ Черновик уже обработан.", show_alert=True)
    data = await state.get_data()
    error = _validate_ready(ad, data) or await _store_broadcasts(ad_id, data, _now(), int(ad["duration_hours"]))
    if error:
        return await cb.answer(f"⚠️ {error}", show_alert=True)

    await cb.answer("⏳ Публикую...")
    await cb.message.edit_text("⏳ Публикую рекламный пост в канал...")
    result = await publish_ad(ad_id)
    if not result.ok:
        await reopen_advertising_post(ad_id)
        return await cb.message.edit_text(
            "❌ Не удалось опубликовать рекламный пост.\n\n"
            "Пост и настройки сохранены — можно безопасно повторить.",
            reply_markup=advertising_retry_keyboard(ad_id),
        )
    await state.clear()
    ad = await get_ad(ad_id)
    text = "✅ <b>Реклама опубликована</b>\n\n" + await _ad_card_text(ad)
    if result.warnings:
        text += "\n\n⚠️ " + "; ".join(result.warnings) + "."
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_done_kb(ad_id))


@router.callback_query(F.data.startswith("ad_schedule:"))
async def schedule_picker(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await _own_draft(ad_id, cb.from_user.id)
    if not ad:
        return await cb.answer("⚠️ Черновик уже обработан.", show_alert=True)
    error = _validate_ready(ad, await state.get_data())
    if error:
        return await cb.answer(f"⚠️ {error}", show_alert=True)
    await state.set_state(AdvertisingState.wait_publish_time)
    await cb.answer()
    await cb.message.edit_text(
        "🕐 <b>Когда опубликовать?</b>\n\nВыберите вариант или пришлите дату и время: "
        "<code>06.09 18:30</code> (время Новосибирска).",
        parse_mode="HTML", reply_markup=_schedule_kb(ad_id),
    )


async def _do_schedule(target: Message, state: FSMContext, ad_id: int, user_id: int, when: datetime, edit: bool):
    ad = await _own_draft(ad_id, user_id)
    if not ad:
        return await target.answer("⚠️ Черновик уже обработан.")
    if when <= _now() + timedelta(minutes=2):
        return await target.answer("❌ Время публикации должно быть в будущем.")
    if when > _now() + timedelta(days=60):
        return await target.answer("❌ Запланировать можно не дальше чем на 60 дней вперёд.")
    data = await state.get_data()
    error = await _store_broadcasts(ad_id, data, when, int(ad["duration_hours"]))
    if error:
        return await target.answer(f"❌ {error} Измените расписание рассылок.")
    if not await schedule_advertising_post(ad_id, when):
        return await target.answer("⚠️ Черновик уже обработан.")
    await log("advertising_schedule", f"admin {user_id}: ad #{ad_id} at {when.isoformat()}")
    await state.clear()
    ad = await get_ad(ad_id)
    text = "🕐 <b>Реклама запланирована</b>\n\n" + await _ad_card_text(ad)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=_done_kb(ad_id))
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=_done_kb(ad_id))


@router.callback_query(F.data.startswith("ad_sched_at:"))
async def schedule_quick(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    _, ad_id_s, ts = cb.data.split(":")
    await cb.answer()
    when = datetime.fromtimestamp(int(ts), TIMEZONE)
    await _do_schedule(cb.message, state, int(ad_id_s), cb.from_user.id, when, edit=True)


@router.message(AdvertisingState.wait_publish_time)
async def schedule_manual(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    when = _parse_dt(msg.text or "")
    if not when:
        return await msg.answer("❌ Не понял дату. Пример: <code>06.09 18:30</code>.", parse_mode="HTML")
    data = await state.get_data()
    await _do_schedule(msg, state, int(data["ad_id"]), msg.from_user.id, when, edit=False)


@router.callback_query(F.data.startswith("ad_retry:"))
async def retry_advertising(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await reopen_advertising_post(ad_id)
    ad = await _own_draft(ad_id, cb.from_user.id)
    if not ad:
        return await cb.answer("⚠️ Черновик не найден.", show_alert=True)
    await state.update_data(ad_id=ad_id, ad_type=ad["ad_type"] or "post")
    await state.set_state(AdvertisingState.wait_confirm)
    await cb.answer()
    await _show_confirmation(cb.message, state, ad_id, edit=True)


@router.callback_query(F.data.startswith("ad_cancel:"))
async def cancel_advertising_callback(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    if await _own_draft(ad_id, cb.from_user.id):
        await cancel_advertising_post(ad_id)
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.edit_text("❌ Рекламная публикация отменена.")


# ------------------------------------------------------------------
# Публикация по расписанию (вызывается из advertising_expiry_loop)
# ------------------------------------------------------------------
async def publish_due_scheduled_ads() -> int:
    from app.database import get_due_scheduled_ads
    published = 0
    for ad_id in await get_due_scheduled_ads():
        ad = await get_ad(ad_id)
        result = await publish_ad(ad_id)
        if result.ok:
            published += 1
            text = f"✅ Запланированная реклама #{ad_id} опубликована."
            if result.warnings:
                text += "\n⚠️ " + "; ".join(result.warnings) + "."
        elif result.error != "already_processed":
            text = (f"❌ Не удалось опубликовать запланированную рекламу #{ad_id}. "
                    "Откройте «📢 Реклама» в админ-панели, чтобы повторить.")
        else:
            continue
        if ad and ad["admin_id"]:
            try:
                await bot.send_message(ad["admin_id"], text, reply_markup=_done_kb(ad_id) if result.ok else None)
            except Exception:
                logger.info("Не удалось уведомить админа %s о рекламе #%s", ad["admin_id"], ad_id)
    return published
