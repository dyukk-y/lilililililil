from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext

from app.loader import logger
from app.auth import is_admin as _is_admin
from app.config import ADMINS
from app.runtime_settings import get as get_setting
from app.text_quality import detect_gibberish
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *
from app.utils import h

router = Router()


def _daily_limit() -> int:
    return get_setting("USER_DAILY_POST_LIMIT")


async def _limit_reached(user_id: int) -> bool:
    """Дневной лимит постов (настройка USER_DAILY_POST_LIMIT, раньше было
    жёстко зашито число 5 и настройка из админ-панели ни на что не влияла)."""
    return await posts_today(user_id) >= _daily_limit()


def _limit_text() -> str:
    return f"🔒 Лимит: {_daily_limit()} постов в день."

# ================== OFFER ==================
@router.callback_query(F.data == "offer")
async def offer(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    if await _limit_reached(cb.from_user.id):
        return await cb.answer(_limit_text(), show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 С фото", callback_data="with_photo")],
        [InlineKeyboardButton(text="📝 Без фото", callback_data="no_photo")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu")]
    ])
    await cb.message.edit_text(
        "Выберите тип поста:\n\n"
        "⚠️ <b>Важно:</b>\n"
        "Помните о правилах публикации. Пост проходит модерацию, "
        "срок рассмотрения — до 24 часов.",
        parse_mode='HTML',
        reply_markup=kb
    )

# ================== WITH PHOTO ==================
@router.callback_query(F.data == "with_photo")
async def with_photo(cb: CallbackQuery, state: FSMContext):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    await state.set_state(PostState.wait_photo)
    await cb.message.edit_text(
        "Пришлите фото для публикации. 📷\n\n"
        "<i>Можно сразу с подписью — если укажете текст с эмодзи 🧑/👩 "
        "прямо в подписи к фото, отдельно присылать его не нужно.</i>",
        parse_mode='HTML',
        reply_markup=back_to_previous()
    )

@router.message(PostState.wait_photo)
async def get_photo(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете отправлять посты.")
    
    if not msg.photo:
        return await msg.answer("❗ Нужно отправить именно фото.", reply_markup=back_to_previous())
    
    await state.update_data(photo=msg.photo[-1].file_id)
    await state.set_state(PostState.wait_text_after_photo)

    # Фото пришло сразу с подписью (обычное поведение на телефоне — фото и
    # текст отправляются одним действием) — раньше бот эту подпись
    # полностью игнорировал и просил прислать текст ЕЩЁ РАЗ отдельным
    # сообщением, из-за чего не видел эмодзи 🧑/👩, если пользователь указал
    # его именно в подписи. Теперь подпись обрабатывается сразу как текст
    # поста. Состояние выставлено ДО этой проверки: если подпись не пройдёт
    # валидацию, следующее сообщение пользователя (уже просто текст, без
    # фото) всё равно попадёт в правильный хендлер ниже.
    if msg.caption:
        await _finalize_photo_post(msg, state, msg.caption)
        return

    await msg.answer(
        "✅ Фото принято.\n\n"
        "📝 <b>Теперь пришли текст к фото:</b>\n\n"
        "⚠️ Не забудьте добавить 🧑 или 👩 в текст!",
        parse_mode='HTML',
        reply_markup=back_to_post_type()
    )

@router.message(PostState.wait_text_after_photo)
async def get_text_after_photo(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете отправлять посты.")

    if msg.photo:
        # Пользователь прислал другое фото вместо текста — заменяем фото
        # (с подписью — сразу обрабатываем как готовый пост).
        await state.update_data(photo=msg.photo[-1].file_id)
        if msg.caption:
            return await _finalize_photo_post(msg, state, msg.caption)
        return await msg.answer("✅ Фото заменено. Теперь пришлите текст к фото.", reply_markup=back_to_post_type())

    if not msg.text:
        return await msg.answer("❗ Пришлите текст поста обычным сообщением.", reply_markup=back_to_post_type())

    await _finalize_photo_post(msg, state, msg.text)


async def _finalize_photo_post(msg: Message, state: FSMContext, text: str) -> None:
    """Общая логика для фото-поста: применяется и когда текст пришёл сразу
    подписью к фото, и когда пришёл отдельным следующим сообщением."""
    if not await _passes_auto_checks(msg, state, text, has_photo=True):
        return

    data = await state.get_data()
    if not data.get("photo"):
        await state.clear()
        return await msg.answer("⚠️ Фото потерялось, начните заново.", reply_markup=menu_btn())

    await _show_preview(msg, state, text, data["photo"])


# ================== ПРЕДПРОСМОТР ПЕРЕД ОТПРАВКОЙ ==================
async def _show_preview(msg: Message, state: FSMContext, text: str, photo: str | None) -> None:
    """Показывает пост ровно так, как он будет выглядеть в канале, с кнопками
    «Отправить / Изменить / Отмена». Опечатку можно исправить до модерации."""
    await state.update_data(draft_text=text, photo=photo)
    await state.set_state(PostState.confirm)
    await msg.answer("👀 <b>Так пост будет выглядеть в канале:</b>", parse_mode='HTML')
    if photo:
        await msg.answer_photo(photo=photo, caption=h(text), parse_mode='HTML', reply_markup=post_preview_keyboard())
    else:
        await msg.answer(h(text), parse_mode='HTML', reply_markup=post_preview_keyboard())


@router.message(PostState.confirm)
async def preview_new_text(msg: Message, state: FSMContext):
    """Вместо нажатия кнопки пользователь прислал исправленный текст (или
    новое фото с подписью) — обновляем черновик и показываем заново."""
    data = await state.get_data()
    photo = data.get("photo")
    if msg.photo:
        photo = msg.photo[-1].file_id
        await state.update_data(photo=photo)
    text = msg.text or msg.caption
    if not text:
        return await msg.answer("✏️ Пришлите исправленный текст или нажмите «✅ Отправить» выше.")
    if not await _passes_auto_checks(msg, state, text, has_photo=bool(photo)):
        return
    await _show_preview(msg, state, text, photo)


@router.callback_query(F.data == "post_edit")
async def preview_edit(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await state.get_state() != PostState.confirm.state:
        return await cb.answer("⚠️ Черновик уже отправлен или устарел.", show_alert=True)
    await state.set_state(PostState.wait_text_after_photo if data.get("photo") else PostState.wait_text_only)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        "✏️ Пришлите исправленный текст поста"
        + (" (фото сохранится)." if data.get("photo") else "."),
        reply_markup=back_to_post_type(),
    )


@router.callback_query(F.data == "post_cancel")
async def preview_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Черновик удалён")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer("🗑 Черновик удалён.", reply_markup=main_menu())


@router.callback_query(F.data == "post_send")
async def preview_send(cb: CallbackQuery, state: FSMContext):
    if await state.get_state() != PostState.confirm.state:
        return await cb.answer("⚠️ Этот пост уже отправлен или устарел.", show_alert=True)
    data = await state.get_data()
    text, photo = data.get("draft_text"), data.get("photo")
    # Сразу очищаем состояние: двойное нажатие не создаст два поста.
    await state.clear()
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not text:
        return await cb.message.answer("⚠️ Черновик потерялся, начните заново.", reply_markup=main_menu())

    user_id = cb.from_user.id
    if await _limit_reached(user_id):
        return await cb.message.answer(_limit_text(), reply_markup=menu_btn())

    if photo:
        checking_msg = await cb.message.answer("🔎 Проверяю фото...")
        try:
            is_explicit, nsfw_class, nsfw_score = await screen_photo(photo)
        finally:
            try:
                await checking_msg.delete()
            except Exception:
                pass
        if is_explicit:
            await log("auto_reject_nsfw_photo", f"user {user_id}: class={nsfw_class} score={nsfw_score:.2f}")
            return await cb.message.answer(
                "❌ <b>Публикация отклонена</b>\n\nНа фото обнаружен запрещённый контент.",
                parse_mode='HTML', reply_markup=menu_btn(),
            )

    post_id = await create_post(user_id, text, photo)
    logger.info("Создан пост #%s (%s) от пользователя %s", post_id, "фото" if photo else "текст", user_id)
    await _notify_submitted(cb.message, post_id)
    await _route_safely(post_id, photo_checked=bool(photo))
    await log("new_post", f"{'photo' if photo else 'text'} post #{post_id} from user {user_id}")


# ================== NO PHOTO ==================
@router.callback_query(F.data == "no_photo")
async def no_photo(cb: CallbackQuery, state: FSMContext):
    if await is_banned(cb.from_user.id):
        return await cb.answer("Вы заблокированы.", show_alert=True)
    
    await state.set_state(PostState.wait_text_only)
    await cb.message.edit_text(
        "Пришлите текст для публикации.\n\n"
        "⚠️ Не забудь добавить 🧑 или 👩 в текст!",
        reply_markup=back_to_previous()
    )

@router.message(PostState.wait_text_only)
async def get_text_only(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете отправлять посты.")

    if not msg.text:
        return await msg.answer("❗ Пришлите текст поста обычным сообщением.", reply_markup=back_to_previous())

    if not await _passes_auto_checks(msg, state, msg.text):
        return

    await _show_preview(msg, state, msg.text, None)


async def _route_safely(post_id: int, photo_checked: bool = False) -> None:
    """Маршрутизация уже сохранённого поста. Пользователю к этому моменту
    уже сказано «пост отправлен на модерацию», поэтому сбой здесь не должен
    превращаться в сообщение об ошибке: пост лежит в БД и будет
    дообработан recover_pending_posts при следующем запуске."""
    try:
        await route_new_post(post_id, photo_checked=photo_checked)
    except Exception:
        logger.exception("Не удалось маршрутизировать пост #%s — будет восстановлен при перезапуске", post_id)


# ================== ЕДИНОЕ УВЕДОМЛЕНИЕ О ПРИЁМЕ ПОСТА ==================
async def _notify_submitted(msg: Message, post_id: int) -> None:
    """Единственное сообщение пользователю сразу после отправки поста.
    Намеренно НЕ раскрывает, будет ли пост опубликован автоматически или
    уйдёт на ручную модерацию, и не называет точное время публикации —
    только общий размер очереди. Под сообщением — кнопка платного
    ускорения проверки."""
    position = await get_total_pending_count()
    await msg.answer(
        f"✅ <b>Пост #{post_id} отправлен на модерацию</b>\n\n"
        f"📊 Постов в очереди: <b>{position}</b>\n"
        + ("🔔 Мы сообщим о результате.\n\n" if get_setting("NOTIFY_AUTHOR_ON_PUBLISH")
           else "🔔 Мы сообщим, если пост будет отклонён.\n\n") +
        "<i>Хотите быстрее? Нажмите «⚡ Ускорить проверку».</i>",
        parse_mode='HTML',
        reply_markup=priority_boost_keyboard(post_id)
    )


# ================== ОБЩИЕ АВТОМАТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕД СОЗДАНИЕМ ПОСТА ==================
async def _passes_auto_checks(msg: Message, state: FSMContext, text: str, has_photo: bool = False) -> bool:
    """Стоп-слова (маты/оскорбления/спам) + валидация текста (в т.ч. минимум
    3 слова, обязательный эмодзи 🧑/👩) + проверка на осмысленность +
    проверка на повторяющийся (нечётко похожий) текст.

    ВАЖНО: текст передаётся явным параметром, а не читается из msg.text —
    для поста с фото и подписью реальный текст лежит в msg.caption, а
    msg.text для такого сообщения всегда пустой (это и было причиной бага,
    когда эмодзи 🧑/👩 в подписи к фото не засчитывался).

    Возвращает False, если пост нужно отклонить ещё до создания записи в
    БД (пользователю уже отправлен ответ)."""
    text = text or ""

    # Лимит проверяется и здесь, а не только при нажатии «Предложить пост»:
    # иначе его можно было обойти, открыв несколько диалогов заранее.
    if await _limit_reached(msg.from_user.id):
        await state.clear()
        await msg.answer(_limit_text(), reply_markup=menu_btn())
        return False

    is_blacklisted, keyword = await is_in_publication_blacklist(text)
    if is_blacklisted:
        await msg.answer(
            f"❌ <b>Публикация отклонена</b>\n\n"
            f"Текст содержит запрещённое слово/фразу (маты, оскорбления или спам): "
            f"<code>{h(keyword.strip())}</code>",
            parse_mode='HTML',
            reply_markup=menu_btn()
        )
        await log("auto_reject_blacklist", f"user {msg.from_user.id}: keyword '{keyword}'")
        await state.clear()
        return False

    is_valid, error_message = validate_post_text(text, has_photo=has_photo)
    if not is_valid:
        await msg.answer(
            error_message,
            parse_mode='HTML',
            reply_markup=back_to_post_type()
        )
        return False

    # Более строгая проверка на осмысленность текста — только для обычных
    # пользователей. Админам можно писать что угодно (например, тестовые
    # посты).
    if not _is_admin(msg.from_user.id):
        is_gibberish, gibberish_reason = detect_gibberish(text)
        if is_gibberish:
            await msg.answer(
                "❌ <b>Публикация отклонена</b>\n\n"
                "Текст не похож на осмысленное сообщение. Опишите словами, "
                "что вы хотите рассказать.",
                parse_mode='HTML',
                reply_markup=back_to_post_type()
            )
            await log("auto_reject_gibberish", f"user {msg.from_user.id}: {gibberish_reason}")
            return False

    duplicate_count = await count_similar_posts(text)
    if duplicate_count >= get_setting("DUPLICATE_REPEAT_LIMIT"):
        await msg.answer(
            "❌ <b>Публикация отклонена</b>\n\n"
            "Такой (или очень похожий) текст уже присылали слишком много раз. "
            "Пришлите, пожалуйста, что-то новое.",
            parse_mode='HTML',
            reply_markup=menu_btn()
        )
        await log("auto_reject_duplicate", f"user {msg.from_user.id}: {duplicate_count} похожих постов")
        await state.clear()
        return False

    return True


@router.callback_query(F.data == "back_to_previous_step")
async def back_to_previous_step(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    if await _limit_reached(cb.from_user.id):
        return await cb.answer(_limit_text(), show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 С фото", callback_data="with_photo")],
        [InlineKeyboardButton(text="📝 Без фото", callback_data="no_photo")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu")]
    ])
    
    await cb.message.edit_text(
        "Выберите тип поста:\n\n"
        "⚠️ <b>Важно:</b>\n"
        "Помните о правилах публикации",
        parse_mode='HTML',
        reply_markup=kb
    )
    await cb.answer()
