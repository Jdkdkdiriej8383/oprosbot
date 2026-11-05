"""Voting handlers for choice polls and open text answer submissions."""

import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from database.db_manager import (
    get_poll,
    record_choice_vote,
    save_text_answer,
    track_poll_post,
    upsert_user,
)
from states.poll_states import TextResponseState

logger = logging.getLogger(__name__)

router = Router(name="voting_router")


# ==============================================================================
# Choice Poll Inline Voting
# ==============================================================================

@router.callback_query(F.data.startswith("vote:"))
async def process_choice_vote(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    Handles inline button clicks in choice polls.
    Results are completely hidden during voting; only personalized alerts are displayed.
    Automatically captures forum topic ID (message_thread_id) and chat ID.
    """
    user = callback.from_user
    if not user:
        await callback.answer("Ошибка пользователя", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неверный формат данных", show_alert=True)
        return

    poll_id = parts[1]
    try:
        option_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный ID варианта", show_alert=True)
        return

    chat_id = callback.message.chat.id if callback.message else None
    message_id = callback.message.message_id if callback.message else None
    thread_id = callback.message.message_thread_id if callback.message else None
    inline_message_id = callback.inline_message_id

    async with session_factory() as session:
        await upsert_user(
            session=session,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )

        # Track message location (including forum topic thread ID)
        await track_poll_post(
            session=session,
            poll_id=poll_id,
            chat_id=chat_id,
            message_thread_id=thread_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )

        success, is_changed, alert_message = await record_choice_vote(
            session=session,
            poll_id=poll_id,
            user_id=user.id,
            option_id=option_id,
            user_fullname=user.full_name,
            username=user.username,
        )

    await callback.answer(alert_message, show_alert=True)


@router.callback_query(F.data.startswith("poll_closed_info:"))
async def process_closed_poll_click(callback: CallbackQuery) -> None:
    """Feedback when clicking on a closed poll."""
    await callback.answer("🔒 Этот опрос уже завершён. Новые ответы не принимаются.", show_alert=True)


# ==============================================================================
# Open Text Poll Submissions
# ==============================================================================

@router.callback_query(F.data.startswith("text_reanswer:"))
async def process_reanswer_request(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """User clicked to edit/replace their previous text answer."""
    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll or poll.is_closed:
        await callback.answer("🔒 Этот опрос уже завершён.", show_alert=True)
        return

    await state.clear()
    await state.set_state(TextResponseState.waiting_for_text_answer)
    await state.update_data(poll_id=poll_id, question=poll.question)

    prompt = (
        f"✏️ <b>Обновление ответа на опрос:</b>\n"
        f"❓ <b>Вопрос:</b> «{poll.question}»\n\n"
        f"✍️ Напишите ваш новый развёрнутый ответ ответным сообщением ниже:"
    )
    if callback.message:
        await callback.message.edit_text(prompt, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "text_cancel")
async def process_text_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """User cancelled editing text response."""
    await state.clear()
    if callback.message:
        await callback.message.edit_text("👌 Ваш предыдущий ответ остаётся в силе.")
    await callback.answer()


@router.message(TextResponseState.waiting_for_text_answer, F.text)
async def process_submitted_text_answer(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Receives, validates, and stores open text answer in database."""
    user = message.from_user
    if not user:
        return

    data = await state.get_data()
    poll_id = data.get("poll_id")
    if not poll_id:
        await state.clear()
        await message.answer("⚠️ Сессия истекла. Пожалуйста, откройте опрос заново.")
        return

    answer_text = message.text.strip()
    if len(answer_text) < 1:
        await message.answer("⚠️ Ответ не может быть пустым.")
        return

    if len(answer_text) > 3000:
        await message.answer("⚠️ Текст слишком длинный (максимум 3000 символов). Сократите ответ:")
        return

    async with session_factory() as session:
        await upsert_user(
            session=session,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )

        success, is_updated, msg = await save_text_answer(
            session=session,
            poll_id=poll_id,
            user_id=user.id,
            text=answer_text,
            user_fullname=user.full_name,
            username=user.username,
        )

    await state.clear()

    if not success:
        await message.answer(f"❌ Ошибка: {msg}")
        return

    action_word = "обновлён" if is_updated else "принят и сохранён"
    await message.answer(
        f"✅ <b>Спасибо! Ваш развёрнутый ответ успешно {action_word}.</b>\n\n"
        f"Автор опроса получит его при подведении итогов.",
        parse_mode="HTML",
    )
