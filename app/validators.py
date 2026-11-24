"""Проверки прав/контекста (чат, тема) и валидация пользовательского ввода."""
from aiogram.types import Message, CallbackQuery

from app.runtime_settings import get as get_setting
from app.emoji_rules import missing_emoji_message, starts_with_person_emoji


def is_valid_moderators_chat(message: Message) -> bool:
    """Проверяет, что сообщение из правильной темы группы модераторов."""
    if message.chat.id != get_setting("MODERATORS_CHAT_ID"):
        return False
    topic_id = get_setting("MODERATORS_TOPIC_ID")
    if message.message_thread_id is not None and message.message_thread_id != topic_id:
        return False
    return True


def is_valid_admins_chat(message: Message) -> bool:
    """Проверяет, что сообщение из правильной темы группы администраторов."""
    if message.chat.id != get_setting("ADMINS_CHAT_ID"):
        return False
    topic_id = get_setting("ADMINS_TOPIC_ID")
    if message.message_thread_id is not None and message.message_thread_id != topic_id:
        return False
    return True


async def validate_chat_for_moderation(callback: CallbackQuery) -> bool:
    """Проверяет, что колбэк из правильной темы для модерации."""
    if callback.message is None or callback.message.chat.id != get_setting("MODERATORS_CHAT_ID"):
        return False
    if callback.message.message_thread_id is not None:
        return callback.message.message_thread_id == get_setting("MODERATORS_TOPIC_ID")
    return True


async def validate_chat_for_admin_actions(callback: CallbackQuery) -> bool:
    """Проверяет, что колбэк из правильной темы для админ-действий."""
    if callback.message is None or callback.message.chat.id != get_setting("ADMINS_CHAT_ID"):
        return False
    if callback.message.message_thread_id is not None:
        return callback.message.message_thread_id == get_setting("ADMINS_TOPIC_ID")
    return True


async def validate_chat_for_moderation_or_admin(callback: CallbackQuery) -> bool:
    """Разрешает действие (публикация/отклонение поста), если колбэк пришёл
    ИЗ ЛЮБОГО из двух чатов — модераторов или администраторов. Нужно для
    автоматически одобренных постов: они больше не попадают в чат
    модераторов вообще, только к администраторам, но там всё равно должна
    работать кнопка "Опубликовать сейчас"/"Отказать"."""
    return await validate_chat_for_moderation(callback) or await validate_chat_for_admin_actions(callback)


def validate_post_text(text: str, has_photo: bool = False) -> tuple[bool, str]:
    if not text or text.strip() == "":
        return False, "❌ Текст поста не может быть пустым."

    if len(text.strip()) < 5:
        return False, "❌ Текст поста слишком короткий"

    # Раньше максимум был жёстко зашит (100), и настройка MAX_POST_LENGTH из
    # админ-панели ни на что не влияла. Для поста с фото дополнительно
    # учитываем лимит Telegram на подпись к фото (1024 символа) — иначе
    # такой пост невозможно опубликовать в канал.
    max_length = int(get_setting("MAX_POST_LENGTH"))
    if has_photo:
        max_length = min(max_length, 1024)
    if len(text) > max_length:
        return False, f"❌ Текст поста слишком длинный (максимум {max_length} символов)."

    # Минимум 3 слова — одиночное слово/междометие постом считаться не может.
    # Считаем словами только токены, содержащие буквы — обязательный эмодзи
    # 🧑/👩 не должен засчитываться как "слово".
    word_count = len([w for w in text.split() if any(ch.isalpha() for ch in w)])
    if word_count < 3:
        return False, "❌ Пост должен содержать минимум 3 слова."

    if not starts_with_person_emoji(text):
        return False, missing_emoji_message()

    return True, "✅ Текст прошел проверку."
