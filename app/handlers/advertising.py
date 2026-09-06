"""Административный мастер рекламных публикаций.

Режимы:
- Пост: срок публикации и закрепа выбираются отдельно.
- Комбо: готовый пакет с автоматическими дополнительными услугами.

Комбо:
24 часа -> 24ч публикация + 24ч закреп;
48 часов -> 48ч + 48ч;
72 часа -> 72ч + 72ч + 1 рассылка в боте;
Неделя -> 7 дней + 72ч закреп + обязательная подписка на 24ч;
Неделя+ -> 7 дней + 72ч закреп + обязательная подписка на 72ч + 3 рассылки в боте.
"""
from datetime import datetime, timedelta
from html import escape
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import ADMINS, TIMEZONE
from app.database import (
    cancel_advertising_post,
    create_advertising_post,
    get_advertising_post,
    log,
    publish_advertising_post,
    set_advertising_control,
    set_advertising_error,
    set_advertising_preview,
    update_advertising_duration,
    update_advertising_pin,
    update_advertising_source,
    set_advertising_type,
    finish_advertising_expiry,
    set_advertising_subscription_target,
    clear_advertising_scheduled_extras,
    add_advertising_broadcast,
    activate_advertising_subscription,
)
from app.keyboards import (
    advertising_type_keyboard,
    advertising_duration_keyboard,
    advertising_pin_keyboard,
    advertising_combo_keyboard,
    advertising_confirm_keyboard,
    advertising_done_keyboard,
    advertising_broadcast_confirm_keyboard,
    advertising_type_back_keyboard, advertising_subscription_back_keyboard,
    advertising_broadcast_back_keyboard, advertising_retry_keyboard,
)
from app.loader import bot, logger
from app.runtime_settings import get as get_setting
from app.states import AdvertisingState

router = Router()

COMBOS = {
    "24": {"label": "24 часа", "duration": 24, "pin": 24, "subscription": 0, "broadcasts": 0},
    "48": {"label": "48 часов", "duration": 48, "pin": 48, "subscription": 0, "broadcasts": 0},
    "72": {"label": "72 часа", "duration": 72, "pin": 72, "subscription": 0, "broadcasts": 1},
    "week": {"label": "Неделя", "duration": 168, "pin": 72, "subscription": 24, "broadcasts": 0},
    "week_plus": {"label": "Неделя+", "duration": 168, "pin": 72, "subscription": 72, "broadcasts": 3},
}

DT_RE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\s+(\d{1,2}):(\d{2})\s*$")


def _is_admin(obj) -> bool:
    return obj.from_user.id in ADMINS


def _hours_label(hours: int) -> str:
    return "7 дней" if hours == 168 else f"{hours} ч."


def _pin_label(hours: int) -> str:
    if not hours:
        return "без закрепа"
    return "72 ч." if hours == 72 else ("7 дней" if hours == 168 else f"{hours} ч.")


def _master_text() -> str:
    return (
        "📢 <b>Реклама</b>\n\n"
        "Выберите направление размещения:\n\n"
        "📝 <b>Пост</b> — срок публикации и закрепа выбираются отдельно.\n"
        "🎁 <b>Комбо</b> — готовый пакет с дополнительными услугами."
    )


def _combo_details(combo_key: str) -> str:
    c = COMBOS[combo_key]
    lines = [f"⏱ Публикация: <b>{_hours_label(c['duration'])}</b>", f"📌 Закреп: <b>{_pin_label(c['pin'])}</b>"]
    if c["subscription"]:
        lines.append(f"🔐 Обязательная подписка: <b>{c['subscription']} ч.</b>")
    if c["broadcasts"]:
        lines.append(f"📨 Рассылка в боте: <b>{c['broadcasts']} раз(а)</b>")
    return "\n".join(lines)


async def _start_master(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdvertisingState.wait_type)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await target.message.answer(_master_text(), reply_markup=advertising_type_keyboard())
    else:
        await target.answer(_master_text(), reply_markup=advertising_type_keyboard())


@router.callback_query(F.data == "admin_ad_post")
async def admin_ad_post_button(cb: CallbackQuery, state: FSMContext):
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
    # При переключении с «Комбо» на «Пост» нельзя переносить старые
    # параметры комбо в новый сценарий.
    await state.set_state(AdvertisingState.wait_post)
    await state.update_data(
        ad_type="post", combo_key=None, subscription=None,
        subscription_hours=0, broadcast_times=[], broadcast_index=1, broadcast_count=0,
    )
    await cb.answer()
    await cb.message.edit_text(
        "📝 <b>Пост</b>\n\nОтправьте рекламный пост следующим сообщением.\n"
        "Telegram-медиа, форматирование, ссылки и подписи будут сохранены.\n\n"
        "После получения выберете срок публикации и срок закрепа.",
        parse_mode="HTML", reply_markup=advertising_type_back_keyboard(),
    )


@router.callback_query(F.data == "ad_type:combo")
async def choose_ad_type_combo(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    await state.set_state(AdvertisingState.wait_post)
    await state.update_data(ad_type="combo")
    await cb.answer()
    await cb.message.edit_text(
        "🎁 <b>Комбо</b>\n\nОтправьте рекламный пост следующим сообщением.\n"
        "После получения выберете готовый пакет.\n\n"
        "<b>72 часа</b> — дополнительно 1 рассылка в боте.\n"
        "<b>Неделя</b> — обязательная подписка на 24 часа.\n"
        "<b>Неделя+</b> — обязательная подписка на 72 часа + 3 рассылки.",
        parse_mode="HTML", reply_markup=advertising_type_back_keyboard(),
    )


@router.callback_query(F.data == "ad_type_back")
async def back_to_ad_type(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    await state.set_state(AdvertisingState.wait_type)
    await cb.answer()
    await cb.message.edit_text(_master_text(), reply_markup=advertising_type_keyboard())


async def _cancel_ad(ad_id, state: FSMContext, cb_or_msg):
    if ad_id:
        await cancel_advertising_post(int(ad_id))
    await state.clear()
    if isinstance(cb_or_msg, CallbackQuery):
        await cb_or_msg.answer("Отменено")
        await cb_or_msg.message.edit_text("❌ <b>Создание рекламной публикации отменено.</b>", parse_mode="HTML")
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
async def advertising_cancel_command(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    await _cancel_ad(data.get("ad_id"), state, msg)


@router.message(AdvertisingState.wait_post)
async def receive_advertising_post(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    ad_id = data.get("ad_id")
    ad_type = data.get("ad_type", "post")
    try:
        if ad_id:
            ad = await get_advertising_post(int(ad_id))
            if not ad or ad[1] != msg.from_user.id or ad[15] != "draft":
                ad_id = None
        if not ad_id:
            ad_id = await create_advertising_post(msg.from_user.id, msg.chat.id, msg.message_id, 24, ad_type)
        else:
            old = await get_advertising_post(int(ad_id))
            if old and old[4] and old[5]:
                try:
                    await bot.delete_message(old[4], old[5])
                except Exception:
                    pass
            await update_advertising_source(int(ad_id), msg.chat.id, msg.message_id)
            await set_advertising_type(int(ad_id), ad_type)

        preview = await bot.copy_message(chat_id=msg.chat.id, from_chat_id=msg.chat.id, message_id=msg.message_id)
        await set_advertising_preview(int(ad_id), msg.chat.id, preview.message_id)
        await state.update_data(ad_id=int(ad_id), source_message_id=msg.message_id, source_chat_id=msg.chat.id, ad_type=ad_type)

        if ad_type == "combo":
            control = await msg.answer("👆 <b>Предпросмотр</b>\n\nВыберите комбо:", parse_mode="HTML", reply_markup=advertising_combo_keyboard(int(ad_id)))
            await state.set_state(AdvertisingState.wait_combo)
        else:
            control = await msg.answer("👆 <b>Предпросмотр</b>\n\n⏱ <b>На сколько публиковать пост?</b>", parse_mode="HTML", reply_markup=advertising_duration_keyboard(int(ad_id)))
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
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or ad[15] != "draft":
        return await cb.answer("⚠️ Черновик уже недоступен.", show_alert=True)
    await state.update_data(ad_id=ad_id, ad_type=ad[17] or "post")
    await state.set_state(AdvertisingState.wait_post)
    await cb.answer()
    await cb.message.edit_text("✏️ <b>Отправьте новый рекламный пост.</b>", parse_mode="HTML", reply_markup=advertising_type_back_keyboard())


@router.callback_query(F.data.startswith("ad_duration:"))
async def choose_ad_duration(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_duration.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    _, ad_id_s, hours_s = cb.data.split(":")
    ad_id, hours = int(ad_id_s), int(hours_s)
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or ad[15] != "draft":
        return await cb.answer("⚠️ Черновик не найден.", show_alert=True)
    await update_advertising_duration(ad_id, hours)
    await state.update_data(ad_id=ad_id, duration_hours=hours, ad_type="post")
    await state.set_state(AdvertisingState.wait_pin)
    await cb.answer()
    await cb.message.edit_text(
        f"⏱ <b>Публикация: {_hours_label(hours)}</b>\n\n📌 <b>На какое время нужен закреп?</b>",
        parse_mode="HTML", reply_markup=advertising_pin_keyboard(ad_id, hours),
    )


@router.callback_query(F.data.startswith("ad_duration_back:"))
async def back_to_duration(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await state.set_state(AdvertisingState.wait_duration)
    await cb.answer()
    await cb.message.edit_text("⏱ <b>На сколько публиковать пост?</b>", parse_mode="HTML", reply_markup=advertising_duration_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_pin:"))
async def choose_ad_pin(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_pin.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    _, ad_id_s, pin_s = cb.data.split(":")
    ad_id, pin_hours = int(ad_id_s), int(pin_s)
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or ad[15] != "draft":
        return await cb.answer("⚠️ Черновик не найден.", show_alert=True)
    duration = int(ad[9])
    if pin_hours > duration:
        return await cb.answer("⚠️ Закреп не может быть дольше публикации.", show_alert=True)
    await update_advertising_pin(ad_id, pin_hours)
    await state.update_data(ad_id=ad_id, duration_hours=duration, pin_hours=pin_hours, ad_type="post")
    await state.set_state(AdvertisingState.wait_confirm)
    await cb.answer()
    await _show_confirmation(cb, ad_id, duration, pin_hours, "post")


@router.callback_query(F.data.startswith("ad_pin_back:"))
async def back_to_ad_duration(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await state.set_state(AdvertisingState.wait_duration)
    await cb.answer()
    await cb.message.edit_text("⏱ <b>На сколько публиковать пост?</b>", parse_mode="HTML", reply_markup=advertising_duration_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_combo:"))
async def choose_ad_combo(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_combo.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    _, ad_id_s, combo_key = cb.data.split(":")
    ad_id = int(ad_id_s)
    combo = COMBOS.get(combo_key)
    ad = await get_advertising_post(ad_id)
    if not combo or not ad or ad[1] != cb.from_user.id or ad[15] != "draft":
        return await cb.answer("⚠️ Комбо или черновик недоступны.", show_alert=True)

    await update_advertising_duration(ad_id, combo["duration"])
    await update_advertising_pin(ad_id, combo["pin"])
    await set_advertising_type(ad_id, "combo")
    # При смене пакета старые временные настройки не должны "протечь"
    # в новый выбор. До публикации дочерние записи всё равно не активны.
    await clear_advertising_scheduled_extras(ad_id)
    await set_advertising_subscription_target(ad_id, None)
    await state.update_data(
        ad_id=ad_id, duration_hours=combo["duration"], pin_hours=combo["pin"],
        combo_key=combo_key, ad_type="combo", broadcast_count=combo["broadcasts"],
    )
    await cb.answer()

    if combo["subscription"]:
        await state.update_data(subscription_hours=combo["subscription"])
        await state.set_state(AdvertisingState.wait_subscription)
        await cb.message.edit_text(
            f"🎁 <b>Комбо: {combo['label']}</b>\n\n{_combo_details(combo_key)}\n\n"
            "🔐 <b>Отправьте канал или бота для обязательной подписки.</b>\n"
            "Можно прислать <code>@username</code> или ссылку <code>https://t.me/username</code>.\n\n"
            "Для канала бот должен иметь право проверять подписку.",
            parse_mode="HTML", reply_markup=advertising_subscription_back_keyboard(ad_id),
        )
        return

    if combo["broadcasts"]:
        await _start_broadcast_schedule(cb, state, ad_id, combo["broadcasts"])
    else:
        await state.set_state(AdvertisingState.wait_confirm)
        await _show_confirmation(cb, ad_id, combo["duration"], combo["pin"], "combo", combo["label"])


async def _start_broadcast_schedule(cb: CallbackQuery, state: FSMContext, ad_id: int, count: int):
    await state.update_data(broadcast_times=[], broadcast_index=1, broadcast_count=count)
    await state.set_state(AdvertisingState.wait_broadcast_time)
    await cb.message.edit_text(
        f"📨 <b>Расписание рассылок</b>\n\n"
        f"Нужно указать дату и время для <b>{count}</b> рассылок.\n"
        "Часовой пояс: <b>Новосибирск (UTC+7)</b>.\n\n"
        "Формат: <code>06.09.2026 18:30</code>\n"
        "Вводите рассылки по одной, по порядку.",
        parse_mode="HTML", reply_markup=advertising_broadcast_back_keyboard(ad_id),
    )


@router.message(AdvertisingState.wait_subscription)
async def receive_subscription_target(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    ad_id = int(data["ad_id"])
    hours = int(data.get("subscription_hours", 0))
    raw = (msg.text or "").strip()
    username = ""
    token = raw
    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
        token = raw.rsplit("/", 1)[-1]
    if token.startswith("@"):
        username = token
    elif re.fullmatch(r"[A-Za-z0-9_]{4,64}", token):
        username = "@" + token
    else:
        return await msg.answer("❌ Нужен @username или ссылка https://t.me/username.")

    try:
        chat = await bot.get_chat(username)
        chat_type = getattr(chat, "type", "")
        if chat_type in ("channel", "supergroup", "group"):
            sub_type = "channel" if chat_type == "channel" else "group"
            sub_id = str(chat.id)
            name = getattr(chat, "title", None) or username
            url = raw if raw.startswith("http") else f"https://t.me/{username.lstrip('@')}"
            # Реальная проверка возможна только если бот имеет доступ к чату.
            try:
                member = await bot.get_chat_member(chat.id, bot.id)
                if member.status not in ("administrator", "creator", "member"):
                    return await msg.answer("❌ Бот не имеет доступа к этому каналу/группе и не сможет проверять подписку.")
            except Exception:
                return await msg.answer("❌ Не удалось проверить доступ бота к каналу/группе. Добавьте бота в канал/группу и попробуйте снова.")
        else:
            # Для другого бота Telegram не даёт Bot API способа проверить факт
            # старта/"подписки" пользователя. Сохраняем его как обязательный
            # переход, но не выдаём ложную гарантию автоматической проверки.
            sub_type = "bot"
            sub_id = str(getattr(chat, "id", username))
            name = getattr(chat, "first_name", None) or getattr(chat, "title", None) or username
            url = raw if raw.startswith("http") else f"https://t.me/{username.lstrip('@')}"

        sub = {"type": sub_type, "id": sub_id, "username": username, "name": name, "url": url}
        await set_advertising_subscription_target(ad_id, sub, hours)
        await state.update_data(subscription=sub)
        combo_key = data.get("combo_key")
        count = int(data.get("broadcast_count", 0))
        if count:
            await state.set_state(AdvertisingState.wait_broadcast_time)
            await state.update_data(broadcast_times=[], broadcast_index=1)
            await msg.answer(
                f"✅ Цель обязательной подписки сохранена: <b>{name}</b>\n\n"
                f"🔐 Срок: <b>{hours} ч.</b>\n\n"
                "📨 Теперь укажите дату и время первой рассылки.\n"
                "Часовой пояс: <b>Новосибирск (UTC+7)</b>.\n"
                "Формат: <code>06.09.2026 18:30</code>",
                parse_mode="HTML", reply_markup=advertising_broadcast_back_keyboard(ad_id),
            )
        else:
            await state.set_state(AdvertisingState.wait_confirm)
            c = COMBOS[combo_key]
            await msg.answer("✅ Цель обязательной подписки сохранена.")
            await _show_confirmation_message(msg, ad_id, c["duration"], c["pin"], "combo", c["label"], sub)
    except Exception as e:
        logger.exception("Ошибка добавления рекламной обязательной подписки")
        await msg.answer(f"❌ Не удалось определить канал/бота: {escape(str(e))}")


@router.message(AdvertisingState.wait_broadcast_time)
async def receive_broadcast_time(msg: Message, state: FSMContext):
    if not _is_admin(msg):
        return
    data = await state.get_data()
    ad_id = int(data["ad_id"])
    count = int(data.get("broadcast_count", 0))
    index = int(data.get("broadcast_index", 1))
    m = DT_RE.match((msg.text or ""))
    if not m:
        return await msg.answer("❌ Неверный формат. Пример: <code>06.09.2026 18:30</code>", parse_mode="HTML")
    day, month, year, hour, minute = map(int, m.groups())
    try:
        scheduled = datetime(year, month, day, hour, minute, tzinfo=TIMEZONE)
    except ValueError:
        return await msg.answer("❌ Такой даты не существует.")
    now = datetime.now(TIMEZONE)
    duration = int(data.get("duration_hours", 0))
    if scheduled <= now + timedelta(minutes=1):
        return await msg.answer("❌ Время рассылки должно быть в будущем.")
    if scheduled > now + timedelta(hours=duration):
        return await msg.answer("❌ Рассылка должна попасть внутрь оплаченного срока публикации.")
    times = list(data.get("broadcast_times", []))
    if times:
        prev = datetime.fromisoformat(times[-1])
        if scheduled <= prev:
            return await msg.answer("❌ Следующая рассылка должна быть позже предыдущей.")
    times.append(scheduled.isoformat())
    if index < count:
        await state.update_data(broadcast_times=times, broadcast_index=index + 1)
        return await msg.answer(
            f"✅ Рассылка {index}/{count}: <b>{scheduled.strftime('%d.%m.%Y %H:%M')}</b> (Новосибирск)\n\n"
            f"Введите дату и время <b>рассылки {index + 1}/{count}</b>.",
            parse_mode="HTML", reply_markup=advertising_broadcast_back_keyboard(ad_id),
        )
    await state.update_data(broadcast_times=times)
    combo_key = data.get("combo_key")
    c = COMBOS[combo_key]
    await state.set_state(AdvertisingState.wait_confirm)
    await msg.answer(
        "📨 <b>Расписание готово</b>\n\n" + "\n".join(
            f"{i}. {datetime.fromisoformat(t).strftime('%d.%m.%Y %H:%M')} (Новосибирск)" for i, t in enumerate(times, 1)
        ), parse_mode="HTML",
    )
    await _show_confirmation_message(msg, ad_id, c["duration"], c["pin"], "combo", c["label"], data.get("subscription"))


async def _show_confirmation(cb, ad_id: int, duration_hours: int, pin_hours: int, ad_type: str, combo_label: str | None = None):
    await _show_confirmation_message(cb.message, ad_id, duration_hours, pin_hours, ad_type, combo_label)


async def _show_confirmation_message(target, ad_id: int, duration_hours: int, pin_hours: int, ad_type: str, combo_label: str | None = None, sub: dict | None = None):
    mode = f"🎁 Комбо: <b>{combo_label}</b>" if combo_label else "📝 Режим: <b>Пост</b>"
    data_text = [mode, f"⏱ Публикация: <b>{_hours_label(duration_hours)}</b>", f"📌 Закреп: <b>{_pin_label(pin_hours)}</b>"]
    if sub:
        data_text.append(f"🔐 Обязательная подписка: <b>{escape(str(sub.get('name') or 'ресурс'))}</b>")
        if combo_label in COMBOS:
            data_text.append(f"   Срок подписки: <b>{COMBOS[combo_label]['subscription']} ч.</b>")
    text = "👆 <b>Проверьте рекламную публикацию</b>\n\nПост выше будет опубликован без изменений.\n\n" + "\n".join(data_text) + "\n\nЕсли всё верно — нажмите «Опубликовать»."
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=advertising_confirm_keyboard(ad_id, ad_type))
    else:
        await target.edit_text(text, parse_mode="HTML", reply_markup=advertising_confirm_keyboard(ad_id, ad_type))


@router.callback_query(F.data.startswith("ad_broadcast_back:"))
async def back_from_broadcasts(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    data = await state.get_data()
    combo_key = data.get("combo_key")
    if combo_key and COMBOS[combo_key]["subscription"]:
        await state.set_state(AdvertisingState.wait_subscription)
        await cb.answer()
        await cb.message.edit_text("🔐 <b>Отправьте канал или бота для обязательной подписки.</b>\nМожно прислать @username или ссылку https://t.me/username.", parse_mode="HTML", reply_markup=advertising_subscription_back_keyboard(ad_id))
    else:
        await state.set_state(AdvertisingState.wait_combo)
        await cb.answer()
        await cb.message.edit_text("🎁 <b>Выберите комбо:</b>", parse_mode="HTML", reply_markup=advertising_combo_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_subscription_back:"))
async def back_from_subscription(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    await state.set_state(AdvertisingState.wait_combo)
    await cb.answer()
    await cb.message.edit_text("🎁 <b>Выберите комбо:</b>", parse_mode="HTML", reply_markup=advertising_combo_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_confirm_back:"))
async def back_from_confirmation(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    _, ad_id_s, kind = cb.data.split(":")
    ad_id = int(ad_id_s)
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or ad[15] != "draft":
        return await cb.answer("⚠️ Черновик уже недоступен.", show_alert=True)
    await cb.answer()
    if kind == "combo":
        key = (await state.get_data()).get("combo_key") or "24"
        if COMBOS[key]["subscription"]:
            await state.set_state(AdvertisingState.wait_subscription)
            await cb.message.edit_text("🔐 <b>Отправьте канал или бота для обязательной подписки.</b>", parse_mode="HTML", reply_markup=advertising_subscription_back_keyboard(ad_id))
        elif COMBOS[key]["broadcasts"]:
            await state.set_state(AdvertisingState.wait_broadcast_time)
            await cb.message.edit_text("📨 <b>Введите дату и время первой рассылки.</b>\nНовосибирск (UTC+7).\nФормат: <code>06.09.2026 18:30</code>", parse_mode="HTML", reply_markup=advertising_broadcast_back_keyboard(ad_id))
        else:
            await state.set_state(AdvertisingState.wait_combo)
            await cb.message.edit_text("🎁 <b>Выберите комбо:</b>", parse_mode="HTML", reply_markup=advertising_combo_keyboard(ad_id))
    else:
        duration = int(ad[9])
        await state.set_state(AdvertisingState.wait_pin)
        await cb.message.edit_text("📌 <b>На какое время нужен закреп?</b>", parse_mode="HTML", reply_markup=advertising_pin_keyboard(ad_id, duration))


@router.callback_query(F.data.startswith("ad_confirm:"))
async def confirm_advertising_publish(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    if await state.get_state() != AdvertisingState.wait_confirm.state:
        return await cb.answer("⚠️ Этот шаг уже завершён.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or ad[15] != "draft":
        return await cb.answer("⚠️ Черновик уже обработан.", show_alert=True)
    duration_hours = int(ad[9])
    pin_hours = int(ad[10] or 0)
    data = await state.get_data()
    combo_key = data.get("combo_key") if ad[17] == "combo" else None
    broadcast_times = list(data.get("broadcast_times", []))
    if ad[17] == "combo":
        if combo_key not in COMBOS:
            return await cb.answer("⚠️ Не выбран корректный пакет.", show_alert=True)
        expected = COMBOS[combo_key]["broadcasts"]
        if len(broadcast_times) != expected:
            return await cb.answer("⚠️ Не все рассылки настроены.", show_alert=True)
        if COMBOS[combo_key]["subscription"] and not ad[18]:
            return await cb.answer("⚠️ Не настроена обязательная подписка.", show_alert=True)
    await cb.answer("⏳ Публикую...")
    await cb.message.edit_text("⏳ <b>Публикую рекламный пост в канал...</b>", parse_mode="HTML")
    try:
        # Расписание рассылок сохраняем ДО публикации. Если Telegram временно
        # недоступен и публикация придётся повторить, расписание не потеряется.
        for idx, iso in enumerate(broadcast_times, 1):
            scheduled = datetime.fromisoformat(iso)
            if scheduled <= datetime.now(TIMEZONE):
                return await cb.message.edit_text("⚠️ Время одной из рассылок уже прошло. Вернитесь назад и укажите новое время.")
            if scheduled > datetime.now(TIMEZONE) + timedelta(hours=duration_hours):
                return await cb.message.edit_text("⚠️ Время одной из рассылок выходит за оплаченный срок.")
            if not await add_advertising_broadcast(ad_id, idx, scheduled):
                # UNIQUE(ad_id, sequence_no) делает повтор безопасным, поэтому
                # существующую запись считаем уже подготовленной.
                logger.info("Реклама #%s: расписание рассылки #%s уже существует", ad_id, idx)

        # Публикуем из сохранённого предпросмотра. Исходное сообщение админ
        # может удалить после подготовки рекламы, предпросмотр остаётся копией.
        source_chat_id = ad[4] or ad[2]
        source_message_id = ad[5] or ad[3]
        copied = await bot.copy_message(
            chat_id=get_setting("MAIN_CHANNEL_ID"), from_chat_id=source_chat_id, message_id=source_message_id
        )
        channel_message_id = copied.message_id
        ok = await publish_advertising_post(ad_id, channel_message_id, pin_hours)
        if not ok:
            try: await bot.delete_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id)
            except Exception: pass
            return await cb.message.edit_text("⚠️ Рекламный пост уже обработан другим действием.")

        warnings = []
        if pin_hours:
            try:
                await bot.pin_chat_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id, disable_notification=True)
            except Exception as e:
                warnings.append("не удалось установить закреп")
                logger.warning("Реклама #%s: не удалось закрепить: %s", ad_id, e)

        # Активация временной обязательной подписки начинается именно с момента публикации.
        if combo_key and COMBOS[combo_key]["subscription"]:
            try:
                if not await activate_advertising_subscription(ad_id, datetime.now(TIMEZONE)):
                    warnings.append("обязательная подписка не была активирована")
            except Exception:
                warnings.append("обязательная подписка не была активирована")
                logger.exception("Реклама #%s: ошибка активации подписки", ad_id)

        mode = f"Комбо {combo_key}" if combo_key else "Пост"
        await log("advertising_publish", f"admin {cb.from_user.id}: ad #{ad_id}, {mode}, {duration_hours}h, pin={pin_hours}h, broadcasts={len(broadcast_times)}")
        result = (
            "✅ <b>Рекламный пост опубликован.</b>\n\n"
            f"Режим: <b>{mode}</b>\n"
            f"Публикация: <b>{_hours_label(duration_hours)}</b>\n"
            f"Закреп: <b>{_pin_label(pin_hours)}</b>\n"
        )
        if combo_key and COMBOS[combo_key]["subscription"]:
            sub = await get_advertising_post(ad_id)
            if sub and sub[21]:
                result += f"🔐 Обязательная подписка: <b>{escape(str(sub[21]))}</b> на {COMBOS[combo_key]['subscription']} ч.\n"
        if broadcast_times:
            result += f"📨 Рассылок запланировано: <b>{len(broadcast_times)}</b>\n"
        result += f"🆔 Реклама #{ad_id}\n\nПост будет автоматически удалён после окончания срока."
        if warnings:
            result += "\n\n⚠️ " + "; ".join(warnings) + "."
        await cb.message.edit_text(result, parse_mode="HTML", reply_markup=advertising_done_keyboard(ad_id))
        await state.clear()
    except Exception as e:
        current = await get_advertising_post(ad_id)
        logger.exception("Ошибка публикации/настройки рекламного поста #%s", ad_id)
        if current and current[15] == "draft":
            await set_advertising_error(ad_id, str(e))
            await cb.message.edit_text(
                "❌ <b>Не удалось опубликовать рекламный пост.</b>\n\n"
                "Исходное сообщение и настройки сохранены — можно безопасно повторить.",
                parse_mode="HTML", reply_markup=advertising_retry_keyboard(ad_id),
            )
        else:
            # Если публикация уже зафиксирована в БД, нельзя переводить её
            # обратно в error: это создало бы риск повторной публикации.
            await cb.message.edit_text(
                "⚠️ <b>Реклама уже опубликована, но часть дополнительных действий "
                "не удалось завершить.</b>\n\n"
                "Основной срок удаления сохранён. Проверьте состояние в канале и "
                "при необходимости выполните действие вручную.",
                parse_mode="HTML", reply_markup=advertising_done_keyboard(ad_id),
            )


@router.callback_query(F.data.startswith("ad_retry:"))
async def retry_advertising(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id:
        return await cb.answer("⚠️ Черновик не найден.", show_alert=True)
    from app.database import reopen_advertising_post
    await reopen_advertising_post(ad_id)
    await state.update_data(ad_id=ad_id, ad_type=ad[17] or "post")
    await cb.answer()
    if (ad[17] or "post") == "combo":
        await state.set_state(AdvertisingState.wait_combo)
        await cb.message.edit_text("🎁 <b>Выберите комбо:</b>", parse_mode="HTML", reply_markup=advertising_combo_keyboard(ad_id))
    else:
        await state.set_state(AdvertisingState.wait_duration)
        await cb.message.edit_text("⏱ <b>На сколько публиковать пост?</b>", parse_mode="HTML", reply_markup=advertising_duration_keyboard(ad_id))


@router.callback_query(F.data.startswith("ad_cancel:"))
async def cancel_advertising_callback(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_advertising_post(ad_id)
    if ad and ad[1] == cb.from_user.id and ad[15] == "draft":
        await cancel_advertising_post(ad_id)
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.edit_text("❌ <b>Рекламная публикация отменена.</b>", parse_mode="HTML")


@router.callback_query(F.data.startswith("ad_delete_now:"))
async def delete_ad_now(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or not ad[8]:
        return await cb.answer("⚠️ Рекламный пост уже удалён.", show_alert=True)
    try:
        try: await bot.unpin_chat_message(get_setting("MAIN_CHANNEL_ID"), ad[8])
        except Exception: pass
        await bot.delete_message(get_setting("MAIN_CHANNEL_ID"), ad[8])
        await finish_advertising_expiry(ad_id, deleted=True)
        await cb.answer("Удалено")
        await cb.message.edit_text("🗑 <b>Рекламный пост удалён досрочно.</b>", parse_mode="HTML")
    except Exception as e:
        await cb.answer("Не удалось удалить пост.", show_alert=True)
        logger.warning("Не удалось досрочно удалить рекламу #%s: %s", ad_id, e)


@router.callback_query(F.data.startswith("ad_unpin_now:"))
async def unpin_ad_now(cb: CallbackQuery):
    if not _is_admin(cb):
        return await cb.answer("🚫 Нет доступа.", show_alert=True)
    ad_id = int(cb.data.split(":")[1])
    ad = await get_advertising_post(ad_id)
    if not ad or ad[1] != cb.from_user.id or not ad[8]:
        return await cb.answer("⚠️ Рекламный пост не найден.", show_alert=True)
    try:
        await bot.unpin_chat_message(get_setting("MAIN_CHANNEL_ID"), ad[8])
        await cb.answer("Закреп снят")
        await cb.message.edit_text("✅ <b>Закреп снят.</b>\n\nРекламный пост продолжит действовать до окончания оплаченного срока.", parse_mode="HTML")
    except Exception:
        await cb.answer("Не удалось снять закреп.", show_alert=True)

