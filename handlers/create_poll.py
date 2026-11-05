"""Poll creation wizard handler supporting Forum Topics (message_thread_id)."""

from datetime import datetime, timedelta, timezone
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
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from database.db_manager import (
    create_poll,
    get_poll,
    track_poll_post,
    upsert_user,
)
from keyboards.inline import (
    get_cancel_keyboard,
    get_choice_voting_keyboard,
    get_creator_manage_keyboard,
    get_deadline_keyboard,
    get_options_input_keyboard,
    get_poll_type_keyboard,
    get_target_destination_keyboard,
    get_text_poll_chat_keyboard,
)
from services.scheduler import schedule_poll_closing
from states.poll_states import PollCreation

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

    # Context auto-detection: if command was sent inside a group or topic
    orig_chat_id = None
    orig_thread_id = None

    if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
        orig_chat_id = event.chat.id
        orig_thread_id = event.message_thread_id  # Automatically captured topic ID!
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
    await proceed_to_destination_step(callback, state, session_factory)
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
    await proceed_to_destination_step(message, state, session_factory)


async def proceed_to_destination_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Checks if target chat was already auto-detected or asks user where to publish."""
    data = await state.get_data()
    if data.get("auto_detected"):
        # Target group and topic already known from command initiation context
        await finalize_and_publish_poll(event, state, session_factory)
        return

    await state.set_state(PollCreation.waiting_for_destination)
    text = (
        "📍 <b>Шаг 5: Выберите место публикации опроса:</b>\n\n"
        "• <b>💬 В этот диалог (ЛС)</b> — опрос останется в этом чате с ботом.\n"
        "• <b>👥 В группу / тему форума (Topics)</b> — бот сразу опубликует опрос в указанную группу и конкретную ветку форума.\n"
        "• <b>📤 Только через Inline (@bot)</b> — вы сможете самостоятельно переслать опрос в любой чат через кнопку."
    )

    if isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(text, reply_markup=get_target_destination_keyboard(), parse_mode="HTML")
    else:
        target = event if isinstance(event, Message) else event.message
        if target:
            await target.answer(text, reply_markup=get_target_destination_keyboard(), parse_mode="HTML")


# --- Step 5: Destination Selection ---
@router.callback_query(PollCreation.waiting_for_destination, F.data.startswith("target:"))
async def process_target_choice(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Processes destination choice."""
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
    elif choice == "topic":
        await state.set_state(PollCreation.waiting_for_topic_forward_or_id)
        prompt = (
            "👥 <b>Укажите целевой чат и тему форума (Topic):</b>\n\n"
            "Вы можете:\n"
            "1. <b>Переслать любое сообщение</b> из нужной темы форума (бот сам определит Chat ID и Topic ID).\n"
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

    # 1. Forwarded message detection
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        thread_id = message.message_thread_id
    elif message.forward_origin and hasattr(message.forward_origin, "chat"):
        chat_id = message.forward_origin.chat.id
        thread_id = message.message_thread_id

    # 2. Text parsing if not forwarded
    if not chat_id and message.text:
        raw = message.text.strip()

        # Link format: https://t.me/c/1234567890/42 or https://t.me/c/1234567890/42/100
        link_match = re.search(r"t\.me/c/(\d+)/(\d+)", raw)
        if link_match:
            chat_id = int(f"-100{link_match.group(1)}")
            thread_id = int(link_match.group(2))
        else:
            # Format: -1001234567890:42 or -1001234567890 42 or -1001234567890
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


# --- Step 6: Finalize & Publish ---
async def finalize_and_publish_poll(
    event: Message | CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saves poll in database, schedules APScheduler timer, and publishes to target topic."""
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
    target_chat_id = data.get("target_chat_id")
    target_thread_id = data.get("target_thread_id")

    deadline_at: Optional[datetime] = None
    if duration_minutes:
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
        )

    # Schedule APScheduler job
    if deadline_at:
        schedule_poll_closing(
            poll_id=poll_id,
            deadline_at=deadline_at,
            bot=bot,
            session_factory=session_factory,
        )

    bot_user = await bot.get_me()
    bot_username = bot_user.username or ""

    deadline_text = (
        f"⏱ <b>Дедлайн:</b> через {duration_minutes} мин. ({deadline_at.strftime('%d.%m.%Y %H:%M UTC')})"
        if deadline_at
        else "♾ <b>Дедлайн:</b> только ручное завершение"
    )

    published_in_target = False

    # Publish directly to target chat & topic if specified
    if target_chat_id:
        try:
            if poll_type == "choice":
                post_text = (
                    f"📊 <b>Опрос:</b>\n"
                    f"«{question}»\n\n"
                    f"<i>🔒 Результаты скрыты до завершения голосования.</i>\n"
                    f"{deadline_text}"
                )
                markup = get_choice_voting_keyboard(poll_id, poll.options)
            else:
                post_text = (
                    f"📝 <b>Опрос с открытым ответом:</b>\n"
                    f"«{question}»\n\n"
                    f"<i>Нажмите кнопку ниже, чтобы дать развёрнутый ответ боту в ЛС.</i>\n"
                    f"{deadline_text}"
                )
                markup = get_text_poll_chat_keyboard(poll_id, bot_username)

            sent_msg = await bot.send_message(
                chat_id=target_chat_id,
                message_thread_id=target_thread_id,  # Thread/Topic parameter!
                text=post_text,
                reply_markup=markup,
                parse_mode="HTML",
            )

            # Record post for live editing when closed
            async with session_factory() as session:
                await track_poll_post(
                    session=session,
                    poll_id=poll_id,
                    chat_id=target_chat_id,
                    message_thread_id=target_thread_id,
                    message_id=sent_msg.message_id,
                )
            published_in_target = True
        except Exception as e:
            logger.warning("Could not auto-publish to chat %s, thread %s: %s", target_chat_id, target_thread_id, e)

    # Confirmation in creator's PM / current interaction
    pub_info = ""
    if published_in_target:
        pub_info = f"\n✅ <b>Опубликовано в ветку:</b> <code>{target_chat_id}</code> (тема: <code>{target_thread_id or 'Основная'}</code>)\n"
    elif target_chat_id:
        pub_info = f"\n⚠️ <i>Не удалось отправить автоматически в чат {target_chat_id}. Убедитесь, что бот добавлен и имеет права администратора. Опубликуйте через инлайн кнопку ниже.</i>\n"

    msg_text = (
        f"🎉 <b>Опрос успешно создан!</b>\n\n"
        f"❓ <b>Вопрос:</b> «{question}»\n"
        f"{pub_info}"
        f"🔒 <b>Результаты:</b> скрыты во время голосования\n"
        f"{deadline_text}\n\n"
        f"💡 <b>Управление и публикация:</b>\n"
        f"Вы можете в любой момент посмотреть промежуточную статистику или опубликовать опрос в любой чат/канал."
    )

    creator_kb = get_creator_manage_keyboard(
        poll_id=poll_id,
        bot_username=bot_username,
        poll_type=poll_type,
        is_closed=False,
    )

    if isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(msg_text, reply_markup=creator_kb, parse_mode="HTML")
    else:
        target_msg = event if isinstance(event, Message) else event.message
        if target_msg:
            await target_msg.answer(
                msg_text,
                reply_markup=creator_kb,
                parse_mode="HTML",
                message_thread_id=target_msg.message_thread_id if target_msg.chat.type in ("group", "supergroup") else None,
            )
