"""
Заявка на удаление поста.

Флоу: пользователь присылает ссылку на пост (или пересылает его из канала) ->
пишет причину -> если он автор поста или упомянут в его тексте (@username) —
пост удаляется сразу; иначе заявка уходит на рассмотрение двум рецензентам
(app.config.DELETION_REVIEWERS), и если никто не ответит в течение
DELETION_REQUEST_TIMEOUT_HOURS — отклоняется автоматически (см.
app.services.deletion_expiry_loop).
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from app.config import DELETION_REVIEWERS
from app.database import is_banned, get_post_by_channel_message_id
from app.keyboards import cancel_to_menu_keyboard, menu_btn
from app.states import DeletePostState
from app.services import resolve_channel_post_id, submit_deletion_request, resolve_deletion_request

router = Router()


@router.callback_query(F.data == "delete_post_request")
async def delete_post_request_start(cb: CallbackQuery, state: FSMContext):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    await state.set_state(DeletePostState.wait_post_link)
    await cb.message.edit_text(
        "🗑 <b>Удаление поста</b>\n\n"
        "Пришлите ссылку на пост в канале (вида <code>https://t.me/канал/123</code>) "
        "или просто перешлите этот пост сюда из канала.",
        parse_mode='HTML',
        reply_markup=cancel_to_menu_keyboard()
    )


@router.message(DeletePostState.wait_post_link)
async def delete_post_receive_link(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")

    channel_message_id = await resolve_channel_post_id(msg)
    if not channel_message_id:
        return await msg.answer(
            "❌ Не удалось распознать пост. Пришлите ссылку вида "
            "<code>https://t.me/канал/123</code> или перешлите сообщение прямо из канала.",
            parse_mode='HTML',
            reply_markup=cancel_to_menu_keyboard()
        )

    post = await get_post_by_channel_message_id(channel_message_id)
    if not post:
        return await msg.answer(
            "❌ Пост не найден среди опубликованных (возможно, он уже удалён "
            "или был опубликован до обновления бота). Попробуйте другой пост.",
            reply_markup=cancel_to_menu_keyboard()
        )

    await state.update_data(post_id=post[0])
    await state.set_state(DeletePostState.wait_reason)
    await msg.answer(
        "📝 <b>Опишите причину для удаления поста:</b>",
        parse_mode='HTML',
        reply_markup=cancel_to_menu_keyboard()
    )


@router.message(DeletePostState.wait_reason)
async def delete_post_receive_reason(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")

    reason = (msg.text or "").strip()
    if not reason:
        return await msg.answer("❌ Причина не может быть пустой. Опишите её текстом.")

    data = await state.get_data()
    post_id = data.get("post_id")
    await state.clear()

    if not post_id:
        return await msg.answer("❌ Ошибка: пост не найден, начните заново.", reply_markup=menu_btn())

    result, _request_id = await submit_deletion_request(post_id, msg.from_user.id, reason)

    if result == "auto_deleted":
        await msg.answer(
            "✅ Пост удалён — заявка одобрена автоматически, так как вы автор "
            "этого поста (или упомянуты в нём).",
            reply_markup=menu_btn()
        )
    elif result == "pending":
        await msg.answer(
            "📨 Заявка на удаление отправлена администрации. "
            "Обычно отвечают в течение 24 часов — если ответа не будет, "
            "заявка отклонится автоматически, мы вам сообщим.",
            reply_markup=menu_btn()
        )
    elif result in ("not_found", "not_published"):
        await msg.answer(
            "❌ Этот пост не найден среди опубликованных.",
            reply_markup=menu_btn()
        )
    else:
        await msg.answer(
            "⚠️ Произошла ошибка при обработке заявки. Попробуйте позже.",
            reply_markup=menu_btn()
        )


# ================== РЕШЕНИЕ РЕЦЕНЗЕНТА ==================
@router.callback_query(F.data.startswith("delreq_approve_"))
async def delreq_approve(cb: CallbackQuery):
    if cb.from_user.id not in DELETION_REVIEWERS:
        return await cb.answer("🚫 У вас нет доступа к решению по этой заявке.", show_alert=True)

    request_id = int(cb.data.split("_")[2])
    ok, status = await resolve_deletion_request(request_id, cb.from_user.id, approve=True)

    if not ok:
        return await cb.answer("⚠️ Заявка уже была обработана кем-то ещё.", show_alert=True)
    await cb.answer("✅ Пост удалён.", show_alert=True)


@router.callback_query(F.data.startswith("delreq_reject_"))
async def delreq_reject(cb: CallbackQuery):
    if cb.from_user.id not in DELETION_REVIEWERS:
        return await cb.answer("🚫 У вас нет доступа к решению по этой заявке.", show_alert=True)

    request_id = int(cb.data.split("_")[2])
    ok, status = await resolve_deletion_request(request_id, cb.from_user.id, approve=False)

    if not ok:
        return await cb.answer("⚠️ Заявка уже была обработана кем-то ещё.", show_alert=True)
    await cb.answer("❌ Заявка отклонена.", show_alert=True)
