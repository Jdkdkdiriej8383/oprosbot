"""Poll creation wizard handler supporting group and topic selection, and publication confirmations."""

from datetime import datetime, timedelta, timezone
import logging
import re
import uuid
from typing import List, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from sqlalchemy.orm import selectinload

from database.db_manager import (
    create_poll,
    get_chat_topics,
    get_group_chat,
    get_user_group_chats,
    track_poll_post,
    upsert_forum_topic,
    upsert_group_chat,
    upsert_user,
)
from database.models import ForumTopic, GroupChat
from keyboards.inline import (
    get_cancel_keyboard,
    get_choice_voting_keyboard,
    get_creator_manage_keyboard,
    get_deadline_keyboard,
    get_groups_selection_keyboard,
    get_options_input_keyboard,
    get_poll_settings_keyboard,
    get_poll_type_keyboard,
    get_target_destination_keyboard,
    get_text_poll_chat_keyboard,
    get_topics_selection_keyboard,
)
from services.scheduler import schedule_poll_closing
from states.poll_states import PollCreation

logger = logging.getLogger(__name__)

router = Router(name="create_poll_router")


@router.message(Command("create_poll"))
@router.callback_query(F.data == "btn_create_poll")
async def start_create_poll(
    event: Message | CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    **kwargs,
) -> None:
    """Initiates poll creation wizard and detects current group/topic context."""
    user = event.from_user
    if not user:
        return

    async with session_factory() as session:
        await upsert_user(
            session=session,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )

    await state.clear()
    await state.set_state(PollCreation.waiting_for_type)
    await state.update_data(options=[])

    orig_chat_id = None
    orig_thread_id = None

    if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
        orig_chat_id = event.chat.id
        orig_thread_id = event.message_thread_id
        await state.update_data(
            target_chat_id=orig_chat_id,
            target_thread_id=orig_thread_id,
            auto_detected=True,
        )

    topic_info = ""
    if orig_chat_id:
        topic_info = f"\n\n📍 <i>Опрос будет автоматически опубликован в текущую группу/тему (ID темы: {orig_thread_id or 'Основная'})</i>"

    text = (
        f"🚀 <b>Мастер создания опроса</b>{topic_info}\n\n"
        f"<b>Шаг 1 из 5:</b> Выберите тип опроса:\n\n"
        f"• <b>📊 С выбором вариантов</b> — голосование с кнопками в чате/теме. "
        f"Результаты скрыты до закрытия, на финише строится диаграмма.\n"
        f"• <b>✍️ С открытым текстовым ответом</b> — карточка с кнопкой «Ответить», "
        f"участники дают развернутые ответы в ЛС, а бот формирует отчёт (CSV/TXT)."
    )

    if isinstance(event, Message):
        await event.answer(
            text,
            reply_markup=get_poll_type_keyboard(),
            parse_mode="HTML",
            message_thread_id=event.message_thread_id if event.chat.type in ("group", "supergroup") else None,
        )
    elif isinstance(event, CallbackQuery) and event.message:
        await event.message.answer(text, reply_markup=get_poll_type_keyboard(), parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "cancel_wizard")
async def cancel_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancels poll creation."""
    await state.clear()
    if callback.message:
        await callback.message.edit_text("❌ Создание опроса отменено.")
    await callback.answer("Отменено")


# --- Step 1: Type Selection ---
@router.callback_query(PollCreation.waiting_for_type, F.data.startswith("poll_type:"))
async def process_poll_type(callback: CallbackQuery, state: FSMContext) -> None:
    """Processes choice between choice poll and text poll."""
    poll_type = callback.data.split(":")[1]
    await state.update_data(poll_type=poll_type)

    type_name = "с вариантами ответа" if poll_type == "choice" else "с открытым текстовым ответом"

    await state.set_state(PollCreation.waiting_for_question)
    await callback.message.edit_text(  # type: ignore
        f"✅ Выбран тип: <b>{type_name}</b>\n\n"
        f"<b>Шаг 2:</b> Введите <b>текст вопроса</b> для опроса:\n"
        f"<i>(Длина от 3 до 500 символов)</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# --- Step 2: Question Input ---
@router.message(PollCreation.waiting_for_question, F.text)
async def process_question(message: Message, state: FSMContext) -> None:
    """Validates and stores question text."""
    question = message.text.strip()
    if len(question) < 3:
        await message.answer("⚠️ Вопрос слишком короткий. Введите минимум 3 символа:")
        return
    if len(question) > 500:
        await message.answer("⚠️ Вопрос слишком длинный (максимум 500 символов).")
        return

    await state.update_data(question=question)
    data = await state.get_data()
    poll_type = data.get("poll_type")

    if poll_type == "choice":
        await state.set_state(PollCreation.waiting_for_options)
        text = (
            f"❓ <b>Вопрос:</b> «{question}»\n\n"
            f"<b>Шаг 3:</b> Добавьте <b>варианты ответа</b>:\n"
            f"Отправляйте варианты по одному или пришлите список, где каждый вариант с новой строки.\n\n"
            f"<i>Минимум: 2 варианта, максимум: 10.</i>\n\n"
            f"Текущие варианты: <i>(пока пусто)</i>"
        )
        await message.answer(text, reply_markup=get_options_input_keyboard(0), parse_mode="HTML")
    else:
        # For text poll, skip options step and go directly to deadline
        await state.set_state(PollCreation.waiting_for_deadline)
        text = (
            f"❓ <b>Вопрос:</b> «{question}»\n\n"
            f"<b>Шаг 3:</b> Настройка завершения опроса (Дедлайн):\n"
            f"Выберите длительность опроса или настройте ручное закрытие:"
        )
        await message.answer(text, reply_markup=get_deadline_keyboard(), parse_mode="HTML")


# --- Step 3: Options Input for Choice Poll ---
@router.message(PollCreation.waiting_for_options, F.text)
async def process_options_text(message: Message, state: FSMContext) -> None:
    """Receives and stores choice options."""
    data = await state.get_data()
    current_options: List[str] = data.get("options", [])

    new_lines = [line.strip() for line in message.text.split("\n") if line.strip()]

    for line in new_lines:
        if len(current_options) >= 10:
            break
        if len(line) > 100:
            line = line[:100]
        if line not in current_options:
            current_options.append(line)

    await state.update_data(options=current_options)

    options_list_str = "\n".join(
        [f"{idx}. {opt}" for idx, opt in enumerate(current_options, start=1)]
    )

    msg = (
        f"📝 <b>Текущие варианты ответа ({len(current_options)}/10):</b>\n"
        f"{options_list_str}\n\n"
    )

    if len(current_options) < 2:
        msg += "⚠️ Добавьте ещё как минимум один вариант (минимум 2):"
    elif len(current_options) >= 10:
        msg += "✅ Достигнут максимум вариантов (10). Нажмите «Готово» ниже:"
    else:
        msg += "Вы можете отправить ещё варианты или нажать «Готово»:"

    await message.answer(
        msg,
        reply_markup=get_options_input_keyboard(len(current_options)),
        parse_mode="HTML",
    )


@router.callback_query(PollCreation.waiting_for_options, F.data == "options_clear")
async def clear_options(callback: CallbackQuery, state: FSMContext) -> None:
    """Clears all options."""
    await state.update_data(options=[])
    await callback.message.edit_text(  # type: ignore
        "🗑 Все добавленные варианты очищены.\n\n"
        "Отправляйте новые варианты ответа по одному или списком с новой строки:",
        reply_markup=get_options_input_keyboard(0),
    )
    await callback.answer("Варианты очищены")


@router.callback_query(PollCreation.waiting_for_options, F.data == "options_done")
async def done_options(callback: CallbackQuery, state: FSMContext) -> None:
    """Completes options input and moves to deadline step."""
    data = await state.get_data()
    options: List[str] = data.get("options", [])

    if len(options) < 2:
        await callback.answer("⚠️ Необходимо минимум 2 варианта ответа!", show_alert=True)
        return

    await state.set_state(PollCreation.waiting_for_deadline)
    text = (
        f"✅ Варианты сохранены ({len(options)} шт.)\n\n"
        f"<b>Шаг 4:</b> Настройка завершения опроса (Дедлайн):\n"
        f"Выберите время активности опроса:"
    )
    await callback.message.edit_text(  # type: ignore
        text,
        reply_markup=get_deadline_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# --- Step 4: Deadline Selection ---
@router.callback_query(PollCreation.waiting_for_deadline, F.data.startswith("deadline:"))
async def process_deadline_choice(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Processes deadline choice."""
    choice = callback.data.split(":")[1]

    if choice == "custom":
        await state.set_state(PollCreation.waiting_for_custom_deadline)
        await callback.message.edit_text(  # type: ignore
            "✏️ <b>Введите желаемую длительность опроса:</b>\n\n"
            "Примеры:\n"
            "• <code>45</code> или <code>45m</code> — 45 минут\n"
            "• <code>2h</code> или <code>2 часа</code> — 2 часа\n"
            "• <code>1.5h</code> — 90 минут\n\n"
            "<i>Отправьте сообщение с длительностью:</i>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if choice == "exact":
        await state.set_state(PollCreation.waiting_for_exact_datetime)
        from config import get_settings
        settings = get_settings()
        await callback.message.edit_text(  # type: ignore
            f"📅 <b>Укажите точную дату и время завершения:</b>\n\n"
            f"Примеры форматов:\n"
            f"• <code>08.11 18:00</code> (день.месяц часы:минуты)\n"
            f"• <code>08.11.2026 18:00</code> (полная дата)\n"
            f"• <code>21:30</code> (сегодня или завтра)\n"
            f"• <code>завтра 15:00</code>\n\n"
            f"<i>Часовой пояс: {settings.TIMEZONE}</i>\n\n"
            f"<i>Отправьте сообщение с датой и временем:</i>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    duration_minutes: Optional[int] = None
    if choice == "15m":
        duration_minutes = 15
    elif choice == "1h":
        duration_minutes = 60
    elif choice == "6h":
        duration_minutes = 360
    elif choice == "24h":
        duration_minutes = 1440
    elif choice == "3d":
        duration_minutes = 4320
    elif choice == "none":
        duration_minutes = None

    await state.update_data(duration_minutes=duration_minutes)
    await show_poll_settings_step(callback, state)
    await callback.answer()


@router.message(PollCreation.waiting_for_custom_deadline, F.text)
async def process_custom_deadline(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Parses custom duration string."""
    raw = message.text.strip().lower()

    duration_minutes: Optional[int] = None
    match_hours = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:h|hour|hours|ч|час|часа|часов)$", raw)
    match_mins = re.match(r"^([0-9]+)\s*(?:m|min|mins|м|мин|минут|минуты)?$", raw)

    if match_hours:
        hours = float(match_hours.group(1))
        duration_minutes = int(hours * 60)
    elif match_mins:
        duration_minutes = int(match_mins.group(1))

    if not duration_minutes or duration_minutes < 1:
        await message.answer(
            "⚠️ Не удалось распознать длительность. Укажите число минут (например, <code>45</code>) "
            "или часов (например, <code>3h</code>):",
            parse_mode="HTML",
        )
        return

    if duration_minutes > 43200:
        await message.answer("⚠️ Максимальная длительность — 30 дней.")
        return

    await state.update_data(duration_minutes=duration_minutes)
    await show_poll_settings_step(message, state)


@router.message(PollCreation.waiting_for_exact_datetime, F.text)
async def process_exact_datetime_step(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Parses exact deadline date and time."""
    from config import get_settings
    from utils.datetime_parser import parse_exact_datetime

    settings = get_settings()
    target_utc, formatted_str = parse_exact_datetime(
        text=message.text or "",
        tz_name=settings.TIMEZONE,
    )
    if not target_utc:
        await message.answer(formatted_str or "Некорректная дата/время.", parse_mode="HTML")
        return

    await state.update_data(
        exact_deadline_iso=target_utc.isoformat(),
        exact_deadline_str=formatted_str,
        duration_minutes=None,
    )
    await show_poll_settings_step(message, state)


# --- Step 5: Advanced Poll Settings ---
async def show_poll_settings_step(
    event: Message | CallbackQuery,
    state: FSMContext,
) -> None:
    """Renders step with interactive toggles for advanced settings."""
    data = await state.get_data()
    poll_type = data.get("poll_type", "choice")
    is_choice = (poll_type == "choice")
    is_multiple = data.get("is_multiple_choice", False)
    is_anon = data.get("is_anonymous", True)
    voters = data.get("allowed_voters", "all")
    has_deadline = bool(data.get("duration_minutes") or data.get("exact_deadline_iso"))
    reminder_min = data.get("reminder_minutes")

    if has_deadline and "reminder_minutes" not in data:
        reminder_min = 60 if (data.get("duration_minutes") or 120) >= 120 else None
        await state.update_data(reminder_minutes=reminder_min)

    await state.set_state(PollCreation.waiting_for_settings)

    mult_desc = "несколько вариантов" if is_multiple else "один вариант"
    anon_desc = "анонимно" if is_anon else "открыто (имена видны)"
    voter_desc = {
        "all": "все участники",
        "with_username": "только с @username",
        "admins_only": "только админы",
    }.get(voters, "все")

    lines = [
        "⚙️ <b>Шаг 5: Дополнительные настройки опроса</b>\n",
        f"• <b>Выбор ответов:</b> {mult_desc}",
        f"• <b>Приватность:</b> {anon_desc}",
        f"• <b>Участники:</b> {voter_desc}",
    ]
    if has_deadline:
        rem_str = f"за {reminder_min} мин. до дедлайна" if reminder_min else "отключено"
        lines.append(f"• <b>Напоминание в чат:</b> {rem_str}")

    lines.append("\n<i>Нажимайте кнопки ниже для переключения параметров:</i>")
    text = "\n".join(lines)

    kb = get_poll_settings_keyboard(
        is_choice=is_choice,
        is_multiple=is_multiple,
        is_anon=is_anon,
        voters=voters,
        reminder_min=reminder_min,
        has_deadline=has_deadline,
    )

    if isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        target = event if isinstance(event, Message) else getattr(event, "message", None)
        if target:
            await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(PollCreation.waiting_for_settings, F.data == "cfg:multichoice")
async def on_toggle_multichoice(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    curr = data.get("is_multiple_choice", False)
    await state.update_data(is_multiple_choice=not curr)
    await show_poll_settings_step(callback, state)
    await callback.answer()


@router.callback_query(PollCreation.waiting_for_settings, F.data == "cfg:anon")
async def on_toggle_anon(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    curr = data.get("is_anonymous", True)
    await state.update_data(is_anonymous=not curr)
    await show_poll_settings_step(callback, state)
    await callback.answer()


@router.callback_query(PollCreation.waiting_for_settings, F.data == "cfg:voters")
async def on_toggle_voters(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    curr = data.get("allowed_voters", "all")
    cycle = ["all", "with_username", "admins_only"]
    idx = cycle.index(curr) if curr in cycle else 0
    next_val = cycle[(idx + 1) % len(cycle)]
    await state.update_data(allowed_voters=next_val)
    await show_poll_settings_step(callback, state)
    await callback.answer()


@router.callback_query(PollCreation.waiting_for_settings, F.data == "cfg:reminder")
async def on_toggle_reminder(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    curr = data.get("reminder_minutes")
    cycle = [60, 30, 15, None]
    idx = cycle.index(curr) if curr in cycle else len(cycle) - 1
    next_val = cycle[(idx + 1) % len(cycle)]
    await state.update_data(reminder_minutes=next_val)
    await show_poll_settings_step(callback, state)
    await callback.answer()


@router.callback_query(PollCreation.waiting_for_settings, F.data == "cfg:done")
async def on_settings_done(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await proceed_to_destination_step(callback, state, session_factory)
    await callback.answer()


# --- Step 5: Destination (Groups & Topics) Selection ---
async def proceed_to_destination_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Shows connected groups or options for publishing."""
    data = await state.get_data()
    if data.get("auto_detected"):
        await finalize_and_publish_poll(event, state, session_factory)
        return

    user = event.from_user
    if not user:
        return

    # Fetch groups added by user or registered in bot
    async with session_factory() as session:
        groups = await get_user_group_chats(session, user.id)

        # If user has no added groups specifically, also check all active groups where bot was added
        if not groups:
            stmt = (
                select(GroupChat)
                .options(selectinload(GroupChat.topics))
                .where(GroupChat.is_active == True)  # noqa: E712
                .order_by(GroupChat.title.asc())
                .limit(10)
            )
            res = await session.execute(stmt)
            groups = list(res.scalars().all())

    await state.set_state(PollCreation.waiting_for_destination)

    if groups:
        text = (
            "📍 <b>Шаг 5: Выберите место для публикации:</b>\n\n"
            "Ниже показаны группы, в которые вы добавили бота. Нажмите на нужную группу, "
            "чтобы выбрать её или одну из её тем форума (Topics):\n"
        )
        kb = get_groups_selection_keyboard(groups)
    else:
        text = (
            "📍 <b>Шаг 5: Выберите место для публикации:</b>\n\n"
            "• <b>💬 В этот диалог (ЛС)</b> — опрос останется в этом чате с ботом.\n"
            "• <b>👥 В группу / тему форума (Topics)</b> — публикация по ID или пересылке сообщения.\n"
            "• <b>📤 Только через Inline (@bot)</b> — отправка через кнопку в любой чат."
        )
        kb = get_target_destination_keyboard()

    if isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        target = event if isinstance(event, Message) else event.message
        if target:
            await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "back_to_groups")
async def back_to_groups_list(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Returns to the groups list from topic selector."""
    await proceed_to_destination_step(callback, state, session_factory)
    await callback.answer()


@router.callback_query(PollCreation.waiting_for_destination, F.data.startswith("sel_grp:"))
async def on_group_selected(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """User clicked one of the connected groups."""
    chat_id = int(callback.data.split(":")[1])

    async with session_factory() as session:
        group = await get_group_chat(session, chat_id)

    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    # Check if forum / has topics
    topics = group.topics if group else []

    if group.is_forum or topics:
        # Show topic selector for this group
        text = (
            f"💬 <b>Группа «{group.title}» поддерживает темы форума (Topics).</b>\n\n"
            f"Выберите тему, в которую нужно опубликовать опрос:"
        )
        kb = get_topics_selection_keyboard(chat_id, topics, is_forum=group.is_forum)
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
    else:
        # Standard group without topics -> immediately publish!
        await state.update_data(target_chat_id=chat_id, target_thread_id=None)
        await finalize_and_publish_poll(callback, state, session_factory)
        await callback.answer()


@router.callback_query(PollCreation.waiting_for_destination, F.data.startswith("sel_tpc:"))
async def on_topic_selected(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """User selected a specific topic in the group."""
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    thread_id = int(parts[2])

    target_thread = None if thread_id == 0 else thread_id
    await state.update_data(target_chat_id=chat_id, target_thread_id=target_thread)
    await finalize_and_publish_poll(callback, state, session_factory)
    await callback.answer()


@router.callback_query(PollCreation.waiting_for_destination, F.data.startswith("sel_tpc_manual:"))
async def on_topic_manual_selected(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """User chose to manually enter a topic ID for the selected group."""
    chat_id = int(callback.data.split(":")[1])
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(PollCreation.waiting_for_custom_topic_id)

    prompt = (
        "✏️ <b>Введите ID темы форума:</b>\n\n"
        "Отправьте целое число — номер темы (например: <code>42</code> или <code>15</code>):\n"
        "<i>(Чтобы узнать ID темы, скопируйте ссылку на любое сообщение из неё: https://t.me/c/.../<b>42</b>/...)</i>"
    )
    if callback.message:
        await callback.message.edit_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.message(PollCreation.waiting_for_custom_topic_id, F.text)
async def process_custom_topic_id(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Parses manually entered topic thread ID."""
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("⚠️ Введите числовой ID темы (например: <code>42</code>):", parse_mode="HTML")
        return

    thread_id = int(raw)
    await state.update_data(target_thread_id=thread_id)

    # Also register this topic for future quick selections
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    if chat_id:
        async with session_factory() as session:
            await upsert_forum_topic(
                session=session,
                chat_id=chat_id,
                thread_id=thread_id,
                title=f"Тема #{thread_id}",
            )

    await finalize_and_publish_poll(message, state, session_factory)


@router.callback_query(PollCreation.waiting_for_destination, F.data.startswith("target:"))
async def process_target_choice(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Processes target destination fallback buttons."""
    choice = callback.data.split(":")[1]

    if choice == "current":
        await state.update_data(
            target_chat_id=callback.message.chat.id if callback.message else None,
            target_thread_id=None,
        )
        await finalize_and_publish_poll(callback, state, session_factory)
    elif choice == "inline_only":
        await state.update_data(target_chat_id=None, target_thread_id=None)
        await finalize_and_publish_poll(callback, state, session_factory)
    elif choice in ("topic", "topic_manual"):
        await state.set_state(PollCreation.waiting_for_topic_forward_or_id)
        prompt = (
            "👥 <b>Укажите целевой чат и тему форума (Topic):</b>\n\n"
            "Вы можете:\n"
            "1. <b>Переслать любое сообщение</b> из нужной темы форума.\n"
            "2. Либо отправить текстом:\n"
            "   • <code>-1001234567890:42</code> (где 42 — ID темы)\n"
            "   • <code>-1001234567890</code> (если опрос в общую ветку группы)\n"
            "   • Ссылку на тему: <code>https://t.me/c/1234567890/42</code>\n\n"
            "<i>Бот должен быть добавлен в группу с правами на отправку сообщений!</i>"
        )
        if callback.message:
            await callback.message.edit_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.message(PollCreation.waiting_for_topic_forward_or_id)
async def process_topic_input(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Parses forwarded message or text format for Chat ID and message_thread_id."""
    chat_id: Optional[int] = None
    thread_id: Optional[int] = None

    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        thread_id = message.message_thread_id
    elif message.forward_origin and hasattr(message.forward_origin, "chat"):
        chat_id = message.forward_origin.chat.id
        thread_id = message.message_thread_id

    if not chat_id and message.text:
        raw = message.text.strip()
        link_match = re.search(r"t\.me/c/(\d+)/(\d+)", raw)
        if link_match:
            chat_id = int(f"-100{link_match.group(1)}")
            thread_id = int(link_match.group(2))
        else:
            parts = re.split(r"[:\s]+", raw)
            try:
                chat_id = int(parts[0])
                if len(parts) > 1 and parts[1].isdigit():
                    thread_id = int(parts[1])
            except ValueError:
                pass

    if not chat_id:
        await message.answer(
            "⚠️ Не удалось распознать чат и тему. Пожалуйста, отправьте ID в формате "
            "<code>-1001234567890:42</code> или перешлите сообщение из ветки форума:",
            parse_mode="HTML",
        )
        return

    await state.update_data(target_chat_id=chat_id, target_thread_id=thread_id)
    await finalize_and_publish_poll(message, state, session_factory)


# --- Step 6: Finalize & Publish with direct confirmation ---
async def finalize_and_publish_poll(
    event: Message | CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saves poll in database, schedules timer, publishes to target, and confirms to creator in PM."""
    user = event.from_user
    if not user:
        return

    bot = event.bot
    data = await state.get_data()
    await state.clear()

    poll_id = uuid.uuid4().hex[:12]
    poll_type = data.get("poll_type", "choice")
    question = data.get("question", "")
    options = data.get("options", [])
    duration_minutes = data.get("duration_minutes")
    exact_deadline_iso = data.get("exact_deadline_iso")
    exact_deadline_str = data.get("exact_deadline_str")
    target_chat_id = data.get("target_chat_id")
    target_thread_id = data.get("target_thread_id")
    result_chat_id = data.get("result_chat_id")
    result_thread_id = data.get("result_thread_id")
    is_multiple_choice = data.get("is_multiple_choice", False)
    is_anonymous = data.get("is_anonymous", True)
    allowed_voters = data.get("allowed_voters", "all")
    reminder_minutes = data.get("reminder_minutes")

    deadline_at: Optional[datetime] = None
    if exact_deadline_iso:
        try:
            deadline_at = datetime.fromisoformat(exact_deadline_iso)
        except Exception:
            deadline_at = None
    elif duration_minutes:
        deadline_at = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)

    # Save to database
    async with session_factory() as session:
        poll = await create_poll(
            session=session,
            poll_id=poll_id,
            creator_id=user.id,
            creator_name=user.full_name,
            poll_type=poll_type,
            question=question,
            options=options if poll_type == "choice" else None,
            deadline_at=deadline_at,
            results_visibility="hidden_until_close",
            target_chat_id=target_chat_id,
            target_thread_id=target_thread_id,
            result_chat_id=result_chat_id,
            result_thread_id=result_thread_id,
            is_multiple_choice=is_multiple_choice,
            is_anonymous=is_anonymous,
            allowed_voters=allowed_voters,
            reminder_minutes=reminder_minutes,
        )

    # Schedule APScheduler job
    if deadline_at:
        schedule_poll_closing(
            poll_id=poll_id,
            deadline_at=deadline_at,
            bot=bot,
            session_factory=session_factory,
            reminder_minutes=reminder_minutes,
        )

    bot_user = await bot.get_me()
    bot_username = bot_user.username or ""

    if exact_deadline_str:
        deadline_text = f"📅 <b>Дедлайн:</b> {exact_deadline_str}"
    elif deadline_at and duration_minutes:
        deadline_text = f"⏱ <b>Дедлайн:</b> через {duration_minutes} мин. ({deadline_at.strftime('%d.%m.%Y %H:%M UTC')})"
    else:
        deadline_text = "♾ <b>Дедлайн:</b> только ручное завершение"

    badges = []
    if poll_type == "choice":
        badges.append("☑️ Несколько ответов" if is_multiple_choice else "🔘 Один ответ")
    badges.append("🔒 Анонимный" if is_anonymous else "👤 Открытый")
    if allowed_voters == "with_username":
        badges.append("🛡 Только с @username")
    elif allowed_voters == "admins_only":
        badges.append("👑 Только админы")

    mode_badge = f"<i>ℹ️ {', '.join(badges)}</i>\n"

    published_in_target = False
    message_link: Optional[str] = None
    target_title = "Чат"

    # Auto-publish to target chat & topic if specified
    if target_chat_id:
        try:
            if poll_type == "choice":
                post_text = (
                    f"📊 <b>Опрос:</b>\n"
                    f"«{question}»\n\n"
                    f"{mode_badge}"
                    f"<i>🔒 Результаты скрыты до завершения голосования.</i>\n"
                    f"{deadline_text}"
                )
                markup = get_choice_voting_keyboard(poll_id, poll.options)
            else:
                post_text = (
                    f"📝 <b>Опрос с открытым ответом:</b>\n"
                    f"«{question}»\n\n"
                    f"{mode_badge}"
                    f"<i>Нажмите кнопку ниже, чтобы дать развёрнутый ответ боту в ЛС.</i>\n"
                    f"{deadline_text}"
                )
                markup = get_text_poll_chat_keyboard(poll_id, bot_username)

            sent_msg = await bot.send_message(
                chat_id=target_chat_id,
                message_thread_id=target_thread_id,
                text=post_text,
                reply_markup=markup,
                parse_mode="HTML",
            )

            # Record post
            async with session_factory() as session:
                await track_poll_post(
                    session=session,
                    poll_id=poll_id,
                    chat_id=target_chat_id,
                    message_thread_id=target_thread_id,
                    message_id=sent_msg.message_id,
                )

            published_in_target = True
            target_title = sent_msg.chat.title or "Группа"

            # Construct direct link to the message
            if sent_msg.chat.username:
                if target_thread_id:
                    message_link = f"https://t.me/{sent_msg.chat.username}/{target_thread_id}/{sent_msg.message_id}"
                else:
                    message_link = f"https://t.me/{sent_msg.chat.username}/{sent_msg.message_id}"
            else:
                # Private supergroup
                clean_id = str(target_chat_id).replace("-100", "")
                if target_thread_id:
                    message_link = f"https://t.me/c/{clean_id}/{target_thread_id}/{sent_msg.message_id}"
                else:
                    message_link = f"https://t.me/c/{clean_id}/{sent_msg.message_id}"

        except Exception as e:
            logger.warning("Could not auto-publish to chat %s, thread %s: %s", target_chat_id, target_thread_id, e)

    # Confirmation message for creator in PM
    if published_in_target:
        pub_status_block = (
            f"✅ <b>Опрос успешно опубликован!</b>\n"
            f"📍 <b>Место:</b> «{target_title}» (ID: <code>{target_chat_id}</code>)\n"
            f"💬 <b>Тема:</b> <code>{target_thread_id or 'Основная ветка'}</code>\n"
        )
    elif target_chat_id:
        pub_status_block = (
            f"⚠️ <i>Не удалось отправить автоматически в чат {target_chat_id}. "
            f"Убедитесь, что бот имеет права отправки в эту ветку. Вы можете переслать опрос через кнопку ниже:</i>\n"
        )
    else:
        pub_status_block = "✅ <b>Опрос создан и готов к публикации!</b>\n"

    creator_msg_text = (
        f"🎉 <b>Подтверждение создания опроса</b>\n\n"
        f"{pub_status_block}\n"
        f"❓ <b>Вопрос:</b> «{question}»\n"
        f"🔒 <b>Результаты:</b> скрыты во время голосования\n"
        f"{deadline_text}\n\n"
        f"💡 Ниже доступна ваша персональная панель управления опросом:"
    )

    creator_kb = get_creator_manage_keyboard(
        poll_id=poll_id,
        bot_username=bot_username,
        poll_type=poll_type,
        is_closed=False,
        message_link=message_link,
    )

    # Always notify creator directly in PM if event happened outside PM, or edit existing message
    if isinstance(event, CallbackQuery) and event.message and event.message.chat.type == "private":
        await event.message.edit_text(creator_msg_text, reply_markup=creator_kb, parse_mode="HTML")
    else:
        # If initiated from group/topic or text message, reply or send to creator's PM
        target_msg = event if isinstance(event, Message) else event.message
        if target_msg:
            # If in group, send short alert in group and full panel in PM
            if target_msg.chat.type in ("group", "supergroup"):
                await target_msg.answer(
                    f"✅ Опрос создан! Панель управления отправлена вам в личные сообщения с ботом.",
                    message_thread_id=target_msg.message_thread_id,
                )
                try:
                    await bot.send_message(
                        chat_id=user.id,
                        text=creator_msg_text,
                        reply_markup=creator_kb,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            else:
                await target_msg.answer(creator_msg_text, reply_markup=creator_kb, parse_mode="HTML")
