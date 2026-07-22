"""Проверки прав/контекста (чат, тема) и валидация пользовательского ввода."""
from aiogram.types import Message, CallbackQuery

from app.runtime_settings import get as get_setting


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
    if callback.message.chat.id != get_setting("MODERATORS_CHAT_ID"):
        return False
    if callback.message.message_thread_id is not None:
        return callback.message.message_thread_id == get_setting("MODERATORS_TOPIC_ID")
    return True


async def validate_chat_for_admin_actions(callback: CallbackQuery) -> bool:
    """Проверяет, что колбэк из правильной темы для админ-действий."""
    if callback.message.chat.id != get_setting("ADMINS_CHAT_ID"):
        return False
    if callback.message.message_thread_id is not None:
        return callback.message.message_thread_id == get_setting("ADMINS_TOPIC_ID")
    return True


def validate_post_text(text: str) -> tuple[bool, str]:
    if not text or text.strip() == "":
        return False, "❌ Текст поста не может быть пустым."

    if len(text.strip()) < 5:
        return False, "❌ Текст поста слишком короткий"

    if len(text) > 100:
        return False, "❌ Текст поста слишком длинный (максимум 100 символов)."

    # Минимум 3 слова — одиночное слово/междометие постом считаться не может.
    # Считаем словами только токены, содержащие буквы — обязательный эмодзи
    # 🧑/👩 не должен засчитываться как "слово".
    word_count = len([w for w in text.split() if any(ch.isalpha() for ch in w)])
    if word_count < 3:
        return False, "❌ Пост должен содержать минимум 3 слова."

    if "🧑" not in text and "👩" not in text:
        return False, (
            "❌ <b>Обязательно добавьте один из этих эмодзи:</b>\n"
            "• 🧑 (мужчина)\n"
            "• 👩 (женщина)\n\n"
            "Примеры:\n"
            "• 🧑 Понравилась ...\n"
            "• 👩 Расскажите о ..."
        )

    return True, "✅ Текст прошел проверку."
