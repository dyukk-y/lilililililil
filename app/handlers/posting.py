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

    checking_msg = await msg.answer("🔎 Проверяю фото...")
    try:
        is_explicit, nsfw_class, nsfw_score = await screen_photo(data["photo"])
    finally:
        try:
            await checking_msg.delete()
        except Exception:
            pass

    if is_explicit:
        await msg.answer(
            "❌ <b>Публикация отклонена</b>\n\n"
            "На фото обнаружен запрещённый контент.",
            parse_mode='HTML',
            reply_markup=menu_btn()
        )
        await log("auto_reject_nsfw_photo", f"user {msg.from_user.id}: class={nsfw_class} score={nsfw_score:.2f}")
        await state.clear()
        return

    await state.clear()

    post_id = await create_post(msg.from_user.id, text, data["photo"])
    logger.info(f"Создан пост #{post_id} с фото от пользователя {msg.from_user.id}")

    await _notify_submitted(msg, post_id)
    await _route_safely(post_id, photo_checked=True)
    await log("new_post", f"photo post #{post_id} from user {msg.from_user.id}")

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

    await state.clear()

    post_id = await create_post(msg.from_user.id, msg.text, None)
    logger.info(f"Создан текстовый пост #{post_id} от пользователя {msg.from_user.id}")

    await _notify_submitted(msg, post_id)
    await _route_safely(post_id)
    await log("new_post", f"text post #{post_id} from user {msg.from_user.id}")


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
        "✅ Ваш пост отправлен на модерацию.\n"
        f"📊 Постов в очереди: {position}.\n"
        "Мы сообщим вам о итогах, как только посты будут рассмотрены.",
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
