"""Клавиатуры (inline keyboards) бота."""
from typing import Any, Dict, List

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.tg_ui import DANGER, PRIMARY, SUCCESS, btn


def get_subscription_keyboard(subscriptions_to_show: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура со ссылками на обязательные каналы/группы для подписки."""
    keyboard = []

    for sub in subscriptions_to_show:
        emoji = "📢" if sub["type"] == "channel" else ("🤖" if sub["type"] == "bot" else "👥")
        keyboard.append([InlineKeyboardButton(text=f"{emoji} {sub['name']}", url=sub["url"])])

    if subscriptions_to_show:
        keyboard.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ================== KEYBOARDS ==================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✍️ Предложить пост", "offer", style=PRIMARY)],
        [
            InlineKeyboardButton(text="📋 Мои посты", callback_data="my_posts"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="❓ Помощь", callback_data="faq"),
            InlineKeyboardButton(text="📜 Правила", callback_data="rules"),
        ],
        [InlineKeyboardButton(text="📢 Реклама", url="https://t.me/smotrmaslyanino_price")],
        [
            InlineKeyboardButton(text="⭐ Stars", url="https://t.me/theyasha_bot?start=ref_6702947726"),
            InlineKeyboardButton(text="🔒 VPN", url="https://t.me/YashaVPN_robot?start=anhVIOjJ"),
        ],
    ])

def profile_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои посты", callback_data="my_posts"),
         InlineKeyboardButton(text="❤️ Пожертвовать", callback_data="donate")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def menu_btn():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def menu_navigation_keyboard(back_callback: str = "menu"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data=back_callback)]
    ])

def cancel_to_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
    ])

def rules_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Юридическое уведомление", url="https://teletype.in/@smotrmaslyanino/responsibility")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])

def back_to_post_type():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ К выбору типа поста", callback_data="offer")]
    ])

def faq_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑️ Удалить запись", callback_data="delete_post_request"),
            InlineKeyboardButton(text="🔎 Узнать автора", callback_data="author_lookup_start"),
        ],
        [InlineKeyboardButton(text="👥 Администрация", callback_data="admins")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def admins_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="faq")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def admin_menu(is_super_admin: bool = False):
    """Компактная админ-панель: сетка 2×N вместо 13 строк по одной кнопке."""
    B = InlineKeyboardButton
    keyboard = [
        [B(text="📅 Очередь", callback_data="pending_posts"),
         B(text="📊 Статистика", callback_data="admin_stats")],
        [B(text="🤖 ИИ", callback_data="admin_ai_stats"),
         B(text="🔑 Фразы", callback_data="auto_phrase_list")],
        [B(text="🚫 Баны и стоп-слова", callback_data="blacklist"),
         B(text="💳 Подписки", callback_data="manage_subscriptions")],
        [B(text="✉️ Рассылка", callback_data="broadcast"),
         B(text="📢 Реклама", callback_data="admin_ad_post")],
        [B(text="📋 Логи", callback_data="admin_logs"),
         B(text="🗄 Бэкап", callback_data="admin_backup_now")],
    ]
    if is_super_admin:
        keyboard.append([
            B(text="⚙️ Настройки", callback_data="settings_list"),
            B(text="💬 Комментарий", callback_data="settings_intro_comment"),
        ])
        keyboard.append([B(text="📥 Экспорт пользователей", callback_data="export_users")])
    keyboard.append([B(text="🔄 Обновить", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def admin_back_keyboard(extra: list | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def blacklist_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Баны", callback_data="banned_users"),
         InlineKeyboardButton(text="📝 Стоп-слова", callback_data="pub_blacklist")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

def pub_blacklist_menu(current_page: int = 1, total_pages: int = 1):
    """Клавиатура для черного списка публикаций с кнопками управления"""
    keyboard = []
    
    nav_buttons = []
    if current_page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"pubblack_page_{current_page - 1}"))
    if current_page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"pubblack_page_{current_page + 1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([
        InlineKeyboardButton(text="➕ Добавить слово", callback_data="add_pub_blacklist"),
        InlineKeyboardButton(text="🗑️ Удалить слово", callback_data="remove_pub_blacklist")
    ])
    
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="blacklist")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def blacklist_cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="pub_blacklist")]
    ])


# ================== ФРАЗЫ ДЛЯ АВТОПУБЛИКАЦИИ ==================
def auto_phrase_menu(current_page: int = 1, total_pages: int = 1):
    keyboard = []

    nav_buttons = []
    if current_page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"auto_phrase_page_{current_page - 1}"))
    if current_page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"auto_phrase_page_{current_page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([
        InlineKeyboardButton(text="➕ Добавить фразу", callback_data="add_auto_phrase"),
        InlineKeyboardButton(text="🗑️ Удалить фразу", callback_data="remove_auto_phrase"),
    ])
    keyboard.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def auto_phrase_cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="auto_phrase_list")]
    ])


# ================== ДИНАМИЧЕСКИЕ НАСТРОЙКИ (только супер-админы) ==================
def settings_list_keyboard(keys_with_labels: List[tuple], back_callback: str = "settings_list") -> InlineKeyboardMarkup:
    """keys_with_labels: список (key, отображаемая_строка_кнопки)."""
    keyboard = [
        [InlineKeyboardButton(text=label, callback_data=f"settings_edit_{key}")]
        for key, label in keys_with_labels
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def settings_categories_keyboard(categories: List[str]) -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    buttons = [B(text=title, callback_data=f"settings_cat_{i}") for i, title in enumerate(categories)]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([B(text="⬅️ В админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def settings_edit_cancel_keyboard(back_callback: str = "settings_list") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=back_callback)]
    ])

def intro_comment_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текст комментария", callback_data="settings_edit_INTRO_COMMENT_TEXT")],
        [InlineKeyboardButton(text="✏️ Кнопка 1: текст", callback_data="settings_edit_INTRO_COMMENT_BTN1_LABEL")],
        [InlineKeyboardButton(text="🔗 Кнопка 1: ссылка", callback_data="settings_edit_INTRO_COMMENT_BTN1_URL")],
        [InlineKeyboardButton(text="✏️ Кнопка 2: текст", callback_data="settings_edit_INTRO_COMMENT_BTN2_LABEL")],
        [InlineKeyboardButton(text="🔗 Кнопка 2: ссылка", callback_data="settings_edit_INTRO_COMMENT_BTN2_URL")],
        [InlineKeyboardButton(text="✏️ Кнопка 3: текст", callback_data="settings_edit_INTRO_COMMENT_BTN3_LABEL")],
        [InlineKeyboardButton(text="🔗 Кнопка 3: ссылка", callback_data="settings_edit_INTRO_COMMENT_BTN3_URL")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")],
    ])

def broadcast_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текстовая рассылка", callback_data="broadcast_text")],
        [InlineKeyboardButton(text="📷 Рассылка с фото", callback_data="broadcast_photo")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

def broadcast_confirm_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            btn("✅ Начать рассылку", "broadcast_start", style=SUCCESS),
            btn("❌ Отмена", "broadcast_cancel", style=DANGER)
        ]
    ])

def broadcast_cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="broadcast_cancel")]
    ])

def subscriptions_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список", callback_data="list_subscriptions"),
         InlineKeyboardButton(text="🗑️ Удалить", callback_data="remove_subscription")],
        [InlineKeyboardButton(text="➕ Канал", callback_data="add_channel_subscription"),
         InlineKeyboardButton(text="➕ Группа", callback_data="add_group_subscription")],
        [InlineKeyboardButton(text="🔄 Перечитать из БД", callback_data="refresh_subscriptions")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

def subscription_cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="manage_subscriptions")]
    ])

def ads_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Прайс-лист", url="https://t.me/smotrmaslyanino_price")],
        [InlineKeyboardButton(text="🛒 Купить", url="https://t.me/theaugustine")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])

def moderation_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Основная клавиатура карточки модерации: действие + диагностика."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Опубликовать", f"pub_{post_id}", style=SUCCESS),
         btn("❌ Отказать", f"rej_{post_id}", style=DANGER)],
        [InlineKeyboardButton(text="🤖 Почему ИИ так решил?", callback_data=f"why_ai_{post_id}")],
    ])

def disabled_moderation_keyboard(post_id: int, action: str = "published") -> InlineKeyboardMarkup:
    """Финальный результат модерации вместо кнопок принятия решения.

    Кнопка остаётся кликабельной: она показывает, кто принял решение,
    поэтому модератор видит результат и может открыть его детали.
    """
    if action == "published":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Опубликовано", callback_data=f"who_pub_{post_id}")]
        ])
    if action == "cancelled":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отменён автором", callback_data="disabled")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отказано", callback_data=f"who_rej_{post_id}")]
    ])

def back_to_previous():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_previous_step")]
    ])

def pagination_keyboard(current_page: int, total_pages: int, list_type: str, back_callback: str = "blacklist"):
    keyboard = []
    
    nav_buttons = []
    if current_page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"{list_type}_page_{current_page - 1}"))
    if current_page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"{list_type}_page_{current_page + 1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton(text="⬅️ В меню", callback_data=back_callback)])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def pending_posts_keyboard(current_page: int, total_pages: int):
    keyboard = []
    
    nav_buttons = []
    if current_page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"pending_page_{current_page - 1}"))
    if current_page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"pending_page_{current_page + 1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([
        InlineKeyboardButton(text="⏩ Опубликовать пост", callback_data="admin_publish_post"),
        InlineKeyboardButton(text="❌ Отклонить пост", callback_data="admin_reject_post")
    ])

    keyboard.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="pending_posts")])
    keyboard.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def admin_post_confirm_keyboard(post_id: int, action: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            btn("✅ Да", f"admin_{action}_confirm_{post_id}", style=SUCCESS if action == "publish" else DANGER),
            btn("↩️ Нет", f"admin_{action}_cancel")
        ]
    ])

def admin_reject_reason_confirm_keyboard(post_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            btn("✅ Отправить", f"admin_reject_send_{post_id}", style=DANGER),
            btn("↩️ Отмена", "admin_reject_cancel")
        ]
    ])


# ================== УДАЛЕНИЕ ПОСТА ПО ЗАЯВКЕ ==================
def deletion_reviewer_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            btn("🗑 Удалить", f"delreq_approve_{request_id}", style=DANGER),
            btn("❌ Отклонить", f"delreq_reject_{request_id}")
        ]
    ])

def deletion_resolved_keyboard(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="disabled")]
    ])


# ================== ПОИСК АВТОРА ПОСТА (платная расшифровка) ==================
def author_reveal_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔓 Расшифровать", f"reveal_author_{post_id}", style=PRIMARY)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def author_revealed_keyboard(author_id: int | None = None, username: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    copy_row = []
    if author_id:
        copy_row.append(btn("📋 ID", copy=author_id))
    if username:
        copy_row.append(btn("📋 @username", copy=f"@{username}"))
    if copy_row:
        rows.append(copy_row)
    return InlineKeyboardMarkup(inline_keyboard=rows + [
        [InlineKeyboardButton(text="✅ Расшифровано", callback_data="disabled")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])


# ================== ПЕРВЫЙ КОММЕНТАРИЙ ПОД ПОСТОМ В КАНАЛЕ ==================
def _button_label(value: str, fallback: str, max_len: int = 32) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    return value if len(value) <= max_len else value[:max_len - 1] + "…"


def intro_comment_keyboard(post_id: int = 0, bot_username: str = "") -> InlineKeyboardMarkup:
    """Компактная и читабельная клавиатура первого комментария."""
    from app.runtime_settings import get as get_setting
    b1 = InlineKeyboardButton(
        text=_button_label(get_setting("INTRO_COMMENT_BTN1_LABEL"), "✍️ Предложить"),
        url=get_setting("INTRO_COMMENT_BTN1_URL"),
    )
    b2 = InlineKeyboardButton(
        text=_button_label(get_setting("INTRO_COMMENT_BTN2_LABEL"), "⭐ Звёзды"),
        url=get_setting("INTRO_COMMENT_BTN2_URL"),
    )
    b3 = InlineKeyboardButton(
        text=_button_label(get_setting("INTRO_COMMENT_BTN3_LABEL"), "🛡 VPN"),
        url=get_setting("INTRO_COMMENT_BTN3_URL"),
    )
    keyboard = [[b2, b3], [b1]]
    if bot_username:
        keyboard.append([
            InlineKeyboardButton(text="🔎 Автор", url=f"https://t.me/{bot_username}?start=author_{post_id}"),
            InlineKeyboardButton(text="🗑 Удалить", url=f"https://t.me/{bot_username}?start=delete_{post_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def priority_admin_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Опубликовать", f"priority_pub_{post_id}", style=SUCCESS),
         btn("❌ Отклонить", f"priority_rej_{post_id}", style=DANGER)],
    ])


# ================== УСКОРЕНИЕ ПРОВЕРКИ ПОСТА ==================
def priority_boost_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("⚡ Ускорить проверку", f"priority_boost_{post_id}", style=PRIMARY)]
    ])


# ================== РЕКЛАМНЫЕ ПОСТЫ ==================
def advertising_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Пост", callback_data="ad_type:post")],
        [InlineKeyboardButton(text="🎁 Комбо", callback_data="ad_type:combo")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ad_abort")],
    ])


def advertising_type_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ К выбору типа", callback_data="ad_type_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ad_abort")],
    ])


def advertising_subscription_back_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ К выбору комбо", callback_data=f"ad_subscription_back:{ad_id}")],
        [InlineKeyboardButton(text="✏️ Заменить пост", callback_data=f"ad_replace:{ad_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ad_cancel:{ad_id}")],
    ])

def advertising_duration_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="24 часа", callback_data=f"ad_duration:{ad_id}:24"),
         InlineKeyboardButton(text="48 часов", callback_data=f"ad_duration:{ad_id}:48")],
        [InlineKeyboardButton(text="72 часа", callback_data=f"ad_duration:{ad_id}:72"),
         InlineKeyboardButton(text="Неделя", callback_data=f"ad_duration:{ad_id}:168")],
        [InlineKeyboardButton(text="✏️ Заменить пост", callback_data=f"ad_replace:{ad_id}")],
        [InlineKeyboardButton(text="↩️ К выбору типа", callback_data="ad_type_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ad_cancel:{ad_id}")],
    ])


def advertising_pin_keyboard(ad_id: int, duration_hours: int) -> InlineKeyboardMarkup:
    options = []
    for hours, label in ((24, "24 часа"), (48, "48 часов"), (72, "72 часа"), (168, "На неделю")):
        if hours <= duration_hours:
            options.append(InlineKeyboardButton(text=label, callback_data=f"ad_pin:{ad_id}:{hours}"))
    keyboard = [options[i:i+2] for i in range(0, len(options), 2)]
    keyboard.append([InlineKeyboardButton(text="🚫 Не нужен", callback_data=f"ad_pin:{ad_id}:0")])
    keyboard.append([InlineKeyboardButton(text="↩️ Вернуться к сроку публикации", callback_data=f"ad_pin_back:{ad_id}")])
    keyboard.append([InlineKeyboardButton(text="✏️ Заменить пост", callback_data=f"ad_replace:{ad_id}")])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"ad_cancel:{ad_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def advertising_combo_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="24 часа", callback_data=f"ad_combo:{ad_id}:24"),
         InlineKeyboardButton(text="48 часов", callback_data=f"ad_combo:{ad_id}:48")],
        [InlineKeyboardButton(text="72 часа", callback_data=f"ad_combo:{ad_id}:72"),
         InlineKeyboardButton(text="Неделя", callback_data=f"ad_combo:{ad_id}:week")],
        [InlineKeyboardButton(text="Неделя+", callback_data=f"ad_combo:{ad_id}:week_plus")],
        [InlineKeyboardButton(text="✏️ Заменить пост", callback_data=f"ad_replace:{ad_id}")],
        [InlineKeyboardButton(text="↩️ К выбору типа", callback_data="ad_type_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ad_cancel:{ad_id}")],
    ])


def advertising_retry_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"ad_retry:{ad_id}")],
        [InlineKeyboardButton(text="✏️ Заменить пост", callback_data=f"ad_replace:{ad_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ad_cancel:{ad_id}")],
    ])


def published_author_keyboard(post_id: int, link: str | None) -> InlineKeyboardMarkup:
    rows = []
    if link:
        rows.append([InlineKeyboardButton(text="🔗 Открыть пост", url=link)])
    rows.append([btn("📌 Закрепить в канале", f"pin_offer_{post_id}", style=PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pin_hours_keyboard(post_id: int, hours_options, price_per_hour: int) -> InlineKeyboardMarkup:
    buttons = [
        btn(f"{h} ч · {h * price_per_hour} ⭐", f"pin_buy_{post_id}_{h}", style=PRIMARY)
        for h in hours_options
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="❌ Не нужно", callback_data="pin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def post_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Отправить", "post_send", style=SUCCESS)],
        [btn("✏️ Изменить", "post_edit"),
         btn("❌ Отмена", "post_cancel", style=DANGER)],
    ])


# ================== ГОТОВЫЕ ПРИЧИНЫ ОТКАЗА ==================
REJECT_REASONS = [
    "Нет эмодзи 🧑/👩",
    "Реклама или продажа",
    "Нарушение правил канала",
    "Оскорбления или грубость",
    "Фото не подходит",
    "Такой пост уже был",
    "Непонятный текст",
]


def reject_reasons_keyboard(post_id: int, prefix: str, cancel_callback: str) -> InlineKeyboardMarkup:
    """prefix: rejr (карточка модерации) или arejr (админ-панель)."""
    buttons = [
        btn(reason, f"{prefix}_{post_id}_{i}", style=DANGER)
        for i, reason in enumerate(REJECT_REASONS)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("↩️ Отмена", cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_emoji_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🧑 Это парень", "addemoji_m", style=PRIMARY), btn("👩 Это девушка", "addemoji_f", style=PRIMARY)],
        [InlineKeyboardButton(text="↩️ К выбору типа поста", callback_data="offer")],
    ])
