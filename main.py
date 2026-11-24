"""Точка входа бота: регистрирует роутеры и middleware, запускает polling
и фоновые задачи (автопубликация по расписанию, ребалансировка дневной
квоты, резервное копирование БД, автоотклонение просроченных заявок)."""
import asyncio
import os

from app.loader import bot, dp, logger
from app.database import init_db
from app.middlewares import setup_middlewares
from app.runtime_settings import load_settings
from app.local_ai import preload as preload_local_ai
from app.config import LOCAL_AI_PRELOAD
from app.services import (
    publish_post, recover_pending_posts, deletion_expiry_loop, moderation_expiry_loop,
)
from app.scheduler import publisher_loop, backup_loop, rebalance_loop, advertising_expiry_loop, maintenance_loop
from app.ai_learning import learning_loop

from app.handlers import (
    start,
    subscriptions,
    posting,
    moderation,
    misc,
    bans,
    admin_panel,
    pub_blacklist,
    admin_stats,
    admin_posts,
    broadcast,
    comments,
    post_deletion,
    author_lookup,
    priority_boost,
    payments,
    auto_approve_phrases,
    settings_admin,
    unlock,
    advertising,
    donation,
    post_pin,
    my_posts,
    suggested_posts,
    common,
)

ROUTERS = (
    common.commands_router,  # /cancel и /help работают из любого сценария
    start.router,
    subscriptions.router,
    posting.router,
    moderation.router,
    misc.router,
    bans.router,
    admin_panel.router,
    pub_blacklist.router,
    admin_stats.router,
    admin_posts.router,
    broadcast.router,
    comments.router,
    post_deletion.router,
    author_lookup.router,
    priority_boost.router,
    payments.router,
    auto_approve_phrases.router,
    settings_admin.router,
    unlock.router,
    advertising.router,
    donation.router,
    post_pin.router,
    my_posts.router,
    suggested_posts.router,
    common.router,  # всегда последним: «страховочные» обработчики
)


DROP_PENDING_UPDATES = os.getenv("DROP_PENDING_UPDATES", "false").lower() in ("1", "true", "yes", "on")


async def _set_commands() -> None:
    """Меню команд в Telegram (кнопка «/» рядом с полем ввода)."""
    from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
    from app.config import ADMINS
    user_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Как пользоваться ботом"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
        admin_commands = user_commands + [
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="post", description="Рекламный пост"),
        ]
        for admin_id in ADMINS:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                pass  # админ ещё не писал боту
    except Exception:
        logger.warning("Не удалось установить меню команд", exc_info=True)


async def main() -> None:
    logger.info("Запуск бота...")
    from app.config import ADMINS
    logger.info("Администраторов настроено: %s", len(ADMINS))
    await init_db()

    # Динамические настройки (цены, лимиты, ID чатов и т.д.) — читаются из
    # БД, при первом запуске заполняются из .env. Редактируются в
    # админ-панели без перезапуска (см. app/runtime_settings.py).
    await load_settings()

    # Лёгкий локальный текстовый анализатор не требует загрузки ML-модели.
    # preload оставлен для обратной совместимости со старой конфигурацией.
    if LOCAL_AI_PRELOAD:
        ai_ready = await preload_local_ai()
        logger.info("Локальный анализатор: %s", "готов" if ai_ready else "недоступен")
    else:
        logger.info("Локальный анализатор: лёгкий режим без ML-модели")

    # Если бот упал между созданием поста и постановкой его в очередь
    # публикации — доигрываем обработку. Данные не теряются: пост уже
    # был сохранён в БД (SQLite + WAL), просто нужно завершить конвейер.
    await recover_pending_posts()

    setup_middlewares(dp)
    for router in ROUTERS:
        dp.include_router(router)

    # Фоновые задачи:
    # - publisher_loop: публикует посты из очереди, когда подошло их время;
    # - rebalance_loop: если сегодня недобор публикаций (недостаточно постов
    #   с ключевыми фразами, удаление поста и т.п.) — подтягивает посты с
    #   будущих дней, чтобы дневная квота выполнялась;
    # - backup_loop: периодическое резервное копирование БД;
    # - deletion_expiry_loop: автоотклонение заявок на удаление без ответа;
    # - moderation_expiry_loop: автоотклонение постов на ручной модерации
    #   без решения модератора в течение MODERATION_TIMEOUT_HOURS.
    from app import alerts
    alerts.install()
    # Каждая фоновая задача работает под супервизором: если упадёт —
    # перезапустится сама, а админы получат одно сообщение.
    loops = {
        "публикация по расписанию": lambda: publisher_loop(publish_post),
        "ребалансировка очереди": rebalance_loop,
        "резервное копирование": backup_loop,
        "заявки на удаление": deletion_expiry_loop,
        "сроки модерации": moderation_expiry_loop,
        "реклама": advertising_expiry_loop,
        "обслуживание": maintenance_loop,
        "обучение ИИ": learning_loop,
        "мониторинг": alerts.health_loop,
    }
    background = [
        asyncio.create_task(alerts.supervise(name, factory), name=name)
        for name, factory in loops.items()
    ]

    await _set_commands()
    logger.info("Бот запущен и готов к работе")
    await alerts.notify_admins("✅ Бот запущен и работает.", key="startup", cooldown=600)
    # Накопившиеся за время простоя обновления больше НЕ выбрасываются по
    # умолчанию: среди них могут быть successful_payment — раньше оплата,
    # пришедшая во время перезапуска, просто терялась (звёзды списаны, а
    # услуга не выдана). Старое поведение: DROP_PENDING_UPDATES=true.
    await bot.delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        # Хранилище диалогов держит открытое соединение с SQLite — без
        # явного закрытия процесс не завершится.
        await dp.storage.close()
        await bot.session.close()
        logger.info("Фоновые задачи остановлены, сессия закрыта")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
