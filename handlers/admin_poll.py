"""Poll administration and management handlers (Creator and Chat Admin permissions)."""

from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from config import get_settings
from database.db_manager import (
    add_poll_option,
    create_poll_template,
    delete_closed_polls_for_user,
    delete_poll,
    delete_template,
    get_all_text_answers,
    get_chat_topics,
    get_choice_poll_stats,
    get_poll,
    get_poll_posts,
    get_template,
    get_user_group_chats,
    get_user_polls,
    get_user_templates,
    update_poll_deadline,
    update_poll_question,
    update_poll_result_destination,
)
from database.models import Vote
from keyboards.inline import (
    get_choice_voting_keyboard,
    get_close_confirmation_keyboard,
    get_creator_manage_keyboard,
    get_deadline_keyboard,
    get_delete_all_closed_confirm_keyboard,
    get_delete_poll_confirm_keyboard,
    get_poll_edit_keyboard,
    get_result_dest_selection_keyboard,
    get_template_action_keyboard,
    get_templates_list_keyboard,
)
from services.poll_service import (
    format_choice_results,
    format_text_poll_summary,
    generate_choice_votes_csv,
    generate_text_answers_csv,
    generate_text_answers_txt,
    is_user_creator_or_admin,
)
from services.scheduler import (
    cancel_poll_closing,
    close_poll_and_publish,
    schedule_poll_closing,
)
from states.poll_states import PollCreation, PollEditing
from utils.datetime_parser import parse_exact_datetime

logger = logging.getLogger(__name__)

router = Router(name="admin_poll_router")


# ==============================================================================
# Management Dashboard & Basic Actions
# ==============================================================================

@router.message(Command("close_poll"))
async def cmd_close_poll(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Closes a poll by command: /close_poll <poll_id>."""
    if not message.from_user:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("ℹ️ Укажите ID опроса: <code>/close_poll &lt;ID&gt;</code>", parse_mode="HTML")
        return

    poll_id = args[1].strip()

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll:
        await message.answer("❌ Опрос с таким ID не найден.")
        return

    has_perm = await is_user_creator_or_admin(
        bot=message.bot,
        poll=poll,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
    )
    if not has_perm:
        await message.answer("⛔ Только создатель опроса или администратор чата может закрыть его!")
        return

    if poll.is_closed:
        await message.answer("ℹ️ Этот опрос уже был закрыт ранее.")
        return

    success = await close_poll_and_publish(
        poll_id=poll_id,
        bot=message.bot,
        session_factory=session_factory,
        is_automated=False,
        closed_by_user_id=message.from_user.id,
        is_admin=True,
    )

    if success:
        await message.answer(f"✅ Опрос «{poll.question}» успешно закрыт! Результаты опубликованы.")
    else:
        await message.answer("❌ Произошла ошибка при закрытии опроса.")


@router.callback_query(F.data.startswith("manage:"))
async def manage_poll(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Displays management dashboard for poll creator / admin."""
    user = callback.from_user
    if not user:
        return

    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll:
        await callback.answer("❌ Опрос не найден", show_alert=True)
        return

    chat_id = callback.message.chat.id if callback.message else None
    has_perm = await is_user_creator_or_admin(callback.bot, poll, user.id, chat_id)
    if not has_perm:
        await callback.answer("⛔ У вас нет прав для управления этим опросом!", show_alert=True)
        return

    bot_user = await callback.bot.get_me()
    bot_username = bot_user.username or ""

    status_str = "🔒 Завершён" if poll.is_closed else "🟢 Активен"
    type_str = "📊 С вариантами" if poll.poll_type == "choice" else "✍️ С открытыми ответами"
    mode_info = "☑️ Множественный выбор" if poll.is_multiple_choice else "🔘 Один ответ"
    anon_info = "🔒 Анонимный" if poll.is_anonymous else "👤 Открытый"

    text = (
        f"⚙️ <b>Панель управления опросом</b>\n\n"
        f"❓ <b>Вопрос:</b> «{poll.question}»\n"
        f"<b>Тип:</b> {type_str}\n"
        f"<b>Режим:</b> {mode_info} | {anon_info}\n"
        f"<b>Статус:</b> {status_str}\n"
        f"<b>ID:</b> <code>{poll.id}</code>\n\n"
        f"Выберите действие:"
    )

    kb = get_creator_manage_keyboard(
        poll_id=poll.id,
        bot_username=bot_username,
        poll_type=poll.poll_type,
        is_closed=poll.is_closed,
    )

    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("stats:"))
async def view_intermediate_stats(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Displays intermediate statistics to creator or chat admin."""
    user = callback.from_user
    if not user:
        return

    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)
        if not poll:
            await callback.answer("❌ Опрос не найден", show_alert=True)
            return

        chat_id = callback.message.chat.id if callback.message else None
        has_perm = await is_user_creator_or_admin(callback.bot, poll, user.id, chat_id)
        if not has_perm:
            await callback.answer("⛔ Только автор опроса или администратор чата может смотреть статистику!", show_alert=True)
            return

        if poll.poll_type == "choice":
            stats = await get_choice_poll_stats(session, poll_id, include_voters=not poll.is_anonymous)
            stats_text = (
                f"👁 <b>Промежуточная статистика (видна автору и админам):</b>\n\n"
                f"{format_choice_results(poll.question, stats, is_closed=poll.is_closed, closed_at=poll.closed_at, deadline_at=poll.deadline_at, is_anonymous=poll.is_anonymous, is_multiple_choice=poll.is_multiple_choice)}"
            )
        else:
            answers = await get_all_text_answers(session, poll_id)
            stats_text = (
                f"👁 <b>Статистика ответов (видна автору и админам):</b>\n\n"
                f"{format_text_poll_summary(poll.question, len(answers), is_closed=poll.is_closed, closed_at=poll.closed_at, deadline_at=poll.deadline_at)}"
            )

    bot_user = await callback.bot.get_me()
    kb = get_creator_manage_keyboard(
        poll_id=poll.id,
        bot_username=bot_user.username or "",
        poll_type=poll.poll_type,
        is_closed=poll.is_closed,
    )

    if callback.message:
        await callback.message.edit_text(stats_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("close_confirm:"))
async def confirm_close_poll(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Asks confirmation before early closure."""
    user = callback.from_user
    if not user:
        return

    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll:
        await callback.answer("❌ Опрос не найден", show_alert=True)
        return

    chat_id = callback.message.chat.id if callback.message else None
    has_perm = await is_user_creator_or_admin(callback.bot, poll, user.id, chat_id)
    if not has_perm:
        await callback.answer("⛔ Только автор или администратор чата может закрыть опрос!", show_alert=True)
        return

    if poll.is_closed:
        await callback.answer("ℹ️ Опрос уже закрыт.", show_alert=True)
        return

    warn_text = (
        f"⚠️ <b>Вы уверены, что хотите досрочно завершить опрос?</b>\n\n"
        f"«{poll.question}»\n\n"
        f"• Приём новых голосов и ответов будет остановлен.\n"
        f"• В чатах и ветках форума сообщение обновится с финальными результатами.\n"
        f"• Кнопки голосования будут отключены."
    )

    if callback.message:
        await callback.message.edit_text(
            warn_text,
            reply_markup=get_close_confirmation_keyboard(poll_id),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("close_do:"))
async def do_close_poll(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Executes early poll closure."""
    user = callback.from_user
    if not user:
        return

    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll:
        await callback.answer("❌ Опрос не найден", show_alert=True)
        return

    chat_id = callback.message.chat.id if callback.message else None
    has_perm = await is_user_creator_or_admin(callback.bot, poll, user.id, chat_id)
    if not has_perm:
        await callback.answer("⛔ У вас нет прав для закрытия опроса!", show_alert=True)
        return

    if poll.is_closed:
        await callback.answer("ℹ️ Опрос уже был закрыт ранее.", show_alert=True)
        return

    success = await close_poll_and_publish(
        poll_id=poll_id,
        bot=callback.bot,
        session_factory=session_factory,
        is_automated=False,
        closed_by_user_id=user.id,
        is_admin=True,
    )

    if success:
        await callback.answer("✅ Опрос успешно завершён!", show_alert=True)
        bot_user = await callback.bot.get_me()
        kb = get_creator_manage_keyboard(
            poll_id=poll.id,
            bot_username=bot_user.username or "",
            poll_type=poll.poll_type,
            is_closed=True,
        )
        if callback.message:
            await callback.message.edit_text(
                f"🏁 <b>Опрос успешно завершён!</b>\n\n"
                f"❓ «{poll.question}»\n\n"
                f"Итоговые результаты зафиксированы и опубликованы в теме.",
                reply_markup=kb,
                parse_mode="HTML",
            )
    else:
        await callback.answer("❌ Ошибка при закрытии опроса", show_alert=True)


@router.callback_query(F.data.startswith("export:"))
async def export_poll_data(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exports poll results to CSV/TXT."""
    user = callback.from_user
    if not user:
        return

    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

        if not poll:
            await callback.answer("❌ Опрос не найден", show_alert=True)
            return

        chat_id = callback.message.chat.id if callback.message else None
        has_perm = await is_user_creator_or_admin(callback.bot, poll, user.id, chat_id)
        if not has_perm:
            await callback.answer("⛔ Только автор или администратор чата может экспортировать данные!", show_alert=True)
            return

        if poll.poll_type == "text":
            answers = await get_all_text_answers(session, poll_id)
            if not answers:
                await callback.answer("ℹ️ В данном опросе пока нет ответов для экспорта.", show_alert=True)
                return

            csv_data = generate_text_answers_csv(poll.question, answers, is_anonymous=poll.is_anonymous)
            txt_data = generate_text_answers_txt(poll.question, answers, is_anonymous=poll.is_anonymous)

            csv_file = BufferedInputFile(csv_data, filename=f"poll_{poll_id[:8]}_answers.csv")
            txt_file = BufferedInputFile(txt_data, filename=f"poll_{poll_id[:8]}_answers.txt")

            await callback.bot.send_document(
                chat_id=user.id,
                document=csv_file,
                caption=f"📊 Экспорт CSV: «{poll.question[:30]}...» ({len(answers)} ответов)",
            )
            await callback.bot.send_document(
                chat_id=user.id,
                document=txt_file,
                caption=f"📝 Экспорт TXT: «{poll.question[:30]}...» ({len(answers)} ответов)",
            )
            await callback.answer("📁 Файлы экспорта отправлены вам в ЛС!", show_alert=True)

        else:
            stmt = select(Vote).where(Vote.poll_id == poll_id)
            res = await session.execute(stmt)
            votes = list(res.scalars().all())

            if not votes:
                await callback.answer("ℹ️ В данном опросе пока нет голосов для экспорта.", show_alert=True)
                return

            options_map = {opt.id: opt.text for opt in poll.options}
            csv_data = generate_choice_votes_csv(poll.question, votes, options_map, is_anonymous=poll.is_anonymous)

            csv_file = BufferedInputFile(csv_data, filename=f"poll_{poll_id[:8]}_votes.csv")

            await callback.bot.send_document(
                chat_id=user.id,
                document=csv_file,
                caption=f"📊 Экспорт голосов: «{poll.question[:30]}...» ({len(votes)} голосов)",
            )
            await callback.answer("📁 Файл экспорта отправлен вам в ЛС!", show_alert=True)


# ==============================================================================
# Edit Poll Handlers
# ==============================================================================

@router.callback_query(F.data.startswith("edit_poll:"))
async def on_edit_poll_menu(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Shows options for editing an ongoing poll."""
    user = callback.from_user
    if not user:
        return
    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll or poll.is_closed:
        await callback.answer("❌ Опрос не найден или уже завершён.", show_alert=True)
        return

    chat_id = callback.message.chat.id if callback.message else None
    if not await is_user_creator_or_admin(callback.bot, poll, user.id, chat_id):
        await callback.answer("⛔ Недостаточно прав для редактирования.", show_alert=True)
        return

    text = (
        f"✏️ <b>Редактирование активного опроса</b>\n\n"
        f"❓ <b>Вопрос:</b> «{poll.question}»\n\n"
        f"Выберите, что вы хотите изменить:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=get_poll_edit_keyboard(poll.id, poll.poll_type), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("edit_q:"))
async def on_edit_question(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiates question text edit."""
    poll_id = callback.data.split(":")[1]
    await state.set_state(PollEditing.waiting_for_new_question)
    await state.update_data(editing_poll_id=poll_id)
    if callback.message:
        await callback.message.edit_text(
            "✏️ <b>Введите новый текст вопроса:</b>\n<i>(От 3 до 500 символов)</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"edit_poll:{poll_id}")]]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(PollEditing.waiting_for_new_question, F.text)
async def process_new_question(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saves new question text and updates published messages."""
    data = await state.get_data()
    poll_id = data.get("editing_poll_id")
    await state.clear()
    if not poll_id:
        return

    new_q = (message.text or "").strip()
    if len(new_q) < 3:
        await message.answer("⚠️ Вопрос слишком короткий.")
        return

    async with session_factory() as session:
        poll = await update_poll_question(session, poll_id, new_q)
        if not poll:
            await message.answer("❌ Опрос не найден.")
            return
        posts = await get_poll_posts(session, poll_id)

    # Sync published messages in chats
    for post in posts:
        try:
            if post.chat_id and post.message_id:
                if poll.poll_type == "choice":
                    post_text = (
                        f"📊 <b>Опрос:</b>\n«{poll.question}»\n\n"
                        f"<i>🔒 Результаты скрыты до завершения голосования.</i>"
                    )
                    kb = get_choice_voting_keyboard(poll.id, poll.options)
                    await message.bot.edit_message_text(
                        chat_id=post.chat_id,
                        message_id=post.message_id,
                        text=post_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
        except Exception as e:
            logger.debug("Failed to update post after question edit: %s", e)

    bot_user = await message.bot.get_me()
    await message.answer(
        f"✅ <b>Вопрос опроса успешно обновлён!</b>\n\nНовый вопрос: «{new_q}»",
        reply_markup=get_creator_manage_keyboard(
            poll_id=poll.id,
            bot_username=bot_user.username or "",
            poll_type=poll.poll_type,
            is_closed=poll.is_closed,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("edit_add_opt:"))
async def on_edit_add_option(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiates adding a new option to active poll."""
    poll_id = callback.data.split(":")[1]
    await state.set_state(PollEditing.waiting_for_new_option)
    await state.update_data(editing_poll_id=poll_id)
    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Введите текст нового варианта ответа:</b>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"edit_poll:{poll_id}")]]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(PollEditing.waiting_for_new_option, F.text)
async def process_new_option(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Adds option to active poll and refreshes keyboards in published posts."""
    data = await state.get_data()
    poll_id = data.get("editing_poll_id")
    await state.clear()
    if not poll_id:
        return

    new_opt_text = (message.text or "").strip()
    if not new_opt_text:
        return

    async with session_factory() as session:
        success, poll, msg = await add_poll_option(session, poll_id, new_opt_text)
        if not success or not poll:
            await message.answer(f"❌ {msg}")
            return
        posts = await get_poll_posts(session, poll_id)

    # Update voting buttons in published posts
    new_kb = get_choice_voting_keyboard(poll.id, poll.options)
    for post in posts:
        try:
            if post.chat_id and post.message_id:
                await message.bot.edit_message_reply_markup(
                    chat_id=post.chat_id,
                    message_id=post.message_id,
                    reply_markup=new_kb,
                )
        except Exception as e:
            logger.debug("Failed to update post reply markup: %s", e)

    bot_user = await message.bot.get_me()
    await message.answer(
        f"✅ <b>Вариант «{new_opt_text}» успешно добавлен!</b>\nКнопки в чате обновлены.",
        reply_markup=get_creator_manage_keyboard(
            poll_id=poll.id,
            bot_username=bot_user.username or "",
            poll_type=poll.poll_type,
            is_closed=poll.is_closed,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("edit_deadline:"))
async def on_edit_deadline_menu(callback: CallbackQuery) -> None:
    """Shows deadline adjustment menu."""
    poll_id = callback.data.split(":")[1]
    if callback.message:
        await callback.message.edit_text(
            "⏱ <b>Изменение времени завершения опроса:</b>\n\nВыберите новый дедлайн:",
            reply_markup=get_deadline_keyboard(is_editing=True, poll_id=poll_id),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("edl:"))
async def on_edit_deadline_choice(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Handles deadline change."""
    parts = callback.data.split(":")
    poll_id = parts[1]
    choice = parts[2]

    if choice == "custom":
        await state.set_state(PollEditing.waiting_for_new_deadline_custom)
        await state.update_data(editing_poll_id=poll_id)
        if callback.message:
            await callback.message.edit_text(
                "✏️ Введите новую длительность (например: <code>2h</code> или <code>45m</code>):",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"edit_deadline:{poll_id}")]]
                ),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    if choice == "exact":
        await state.set_state(PollEditing.waiting_for_new_deadline_exact)
        await state.update_data(editing_poll_id=poll_id)
        settings = get_settings()
        if callback.message:
            await callback.message.edit_text(
                f"📅 <b>Укажите точную дату и время завершения:</b>\n"
                f"Примеры: <code>15.11 18:00</code>, <code>21:30</code>, <code>завтра 14:00</code>\n\n"
                f"<i>Часовой пояс: {settings.TIMEZONE}</i>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"edit_deadline:{poll_id}")]]
                ),
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

    now_utc = datetime.now(timezone.utc)
    new_deadline = now_utc + timedelta(minutes=duration_minutes) if duration_minutes else None

    async with session_factory() as session:
        poll = await update_poll_deadline(session, poll_id, new_deadline)

    if new_deadline:
        schedule_poll_closing(
            poll_id=poll_id,
            deadline_at=new_deadline,
            bot=callback.bot,
            session_factory=session_factory,
            reminder_minutes=poll.reminder_minutes if poll else None,
        )
    else:
        cancel_poll_closing(poll_id)

    bot_user = await callback.bot.get_me()
    dead_str = new_deadline.strftime("%d.%m.%Y %H:%M UTC") if new_deadline else "без таймера (ручное)"
    if callback.message:
        await callback.message.edit_text(
            f"✅ <b>Дедлайн успешно изменён!</b>\n\nНовое время: <b>{dead_str}</b>",
            reply_markup=get_creator_manage_keyboard(
                poll_id=poll_id,
                bot_username=bot_user.username or "",
                poll_type=poll.poll_type if poll else "choice",
                is_closed=poll.is_closed if poll else False,
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(PollEditing.waiting_for_new_deadline_custom, F.text)
async def process_edit_custom_deadline(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Parses custom duration for deadline edit."""
    data = await state.get_data()
    poll_id = data.get("editing_poll_id")
    await state.clear()
    if not poll_id:
        return

    raw = (message.text or "").strip().lower()
    duration_minutes: Optional[int] = None
    match_hours = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:h|hour|hours|ч|час|часа|часов)$", raw)
    match_mins = re.match(r"^([0-9]+)\s*(?:m|min|mins|м|мин|минут|минуты)?$", raw)
    if match_hours:
        duration_minutes = int(float(match_hours.group(1)) * 60)
    elif match_mins:
        duration_minutes = int(match_mins.group(1))

    if not duration_minutes:
        await message.answer("⚠️ Не удалось распознать время.")
        return

    new_deadline = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
    async with session_factory() as session:
        poll = await update_poll_deadline(session, poll_id, new_deadline)

    schedule_poll_closing(
        poll_id=poll_id,
        deadline_at=new_deadline,
        bot=message.bot,
        session_factory=session_factory,
        reminder_minutes=poll.reminder_minutes if poll else None,
    )

    bot_user = await message.bot.get_me()
    await message.answer(
        f"✅ <b>Дедлайн успешно обновлён!</b>\nНовое время: через {duration_minutes} мин.",
        reply_markup=get_creator_manage_keyboard(
            poll_id=poll_id,
            bot_username=bot_user.username or "",
            poll_type=poll.poll_type if poll else "choice",
            is_closed=poll.is_closed if poll else False,
        ),
        parse_mode="HTML",
    )


@router.message(PollEditing.waiting_for_new_deadline_exact, F.text)
async def process_edit_exact_datetime(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Parses exact datetime for deadline edit."""
    data = await state.get_data()
    poll_id = data.get("editing_poll_id")
    await state.clear()
    if not poll_id:
        return

    settings = get_settings()
    target_utc, formatted_str = parse_exact_datetime(message.text or "", tz_name=settings.TIMEZONE)
    if not target_utc:
        await message.answer(formatted_str or "Некорректная дата.")
        return

    async with session_factory() as session:
        poll = await update_poll_deadline(session, poll_id, target_utc)

    schedule_poll_closing(
        poll_id=poll_id,
        deadline_at=target_utc,
        bot=message.bot,
        session_factory=session_factory,
        reminder_minutes=poll.reminder_minutes if poll else None,
    )

    bot_user = await message.bot.get_me()
    await message.answer(
        f"✅ <b>Дедлайн успешно изменён!</b>\n\n📅 Завершение: <b>{formatted_str}</b>",
        reply_markup=get_creator_manage_keyboard(
            poll_id=poll_id,
            bot_username=bot_user.username or "",
            poll_type=poll.poll_type if poll else "choice",
            is_closed=poll.is_closed if poll else False,
        ),
        parse_mode="HTML",
    )


# ==============================================================================
# Result Destination Handlers
# ==============================================================================

@router.callback_query(F.data.startswith("res_dest:"))
async def on_result_destination_menu(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Shows menu to configure result destination topic."""
    user = callback.from_user
    if not user:
        return
    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        groups = await get_user_group_chats(session, user.id)

    if callback.message:
        await callback.message.edit_text(
            "🎯 <b>Настройка темы для вывода итогов</b>\n\n"
            "По умолчанию итоги опроса публикуются в ту же ветку форума / чат, где проводился опрос.\n"
            "Вы можете назначить другую группу или тему форума:",
            reply_markup=get_result_dest_selection_keyboard(poll_id, groups),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("set_res_orig:"))
async def on_set_result_dest_orig(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resets result destination to original topic."""
    poll_id = callback.data.split(":")[1]
    async with session_factory() as session:
        poll = await update_poll_result_destination(session, poll_id, None, None)

    await callback.answer("✅ Итоги будут публиковаться в исходную тему.", show_alert=True)
    bot_user = await callback.bot.get_me()
    if callback.message and poll:
        await callback.message.edit_text(
            f"⚙️ <b>Панель управления опросом</b>\n\n«{poll.question}»\n\n"
            f"✅ Итоги будут опубликованы в ту же тему, где шло голосование.",
            reply_markup=get_creator_manage_keyboard(
                poll_id=poll.id,
                bot_username=bot_user.username or "",
                poll_type=poll.poll_type,
                is_closed=poll.is_closed,
            ),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("set_res_grp:"))
async def on_set_result_dest_grp(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Shows topics for chosen group."""
    parts = callback.data.split(":")
    poll_id = parts[1]
    group_id = int(parts[2])

    async with session_factory() as session:
        topics = await get_chat_topics(session, group_id)

    buttons = [
        [
            InlineKeyboardButton(
                text="💬 Основная ветка (General)",
                callback_data=f"set_res_tpc:{poll_id}:{group_id}:0",
            )
        ]
    ]
    for t in topics:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📌 {t.title}",
                    callback_data=f"set_res_tpc:{poll_id}:{group_id}:{t.thread_id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"res_dest:{poll_id}")])

    if callback.message:
        await callback.message.edit_text(
            "📌 <b>Выберите тему форума для вывода итогов:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("set_res_tpc:"))
async def on_set_result_dest_topic(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saves specific chat and thread ID for results."""
    parts = callback.data.split(":")
    poll_id = parts[1]
    group_id = int(parts[2])
    thread_id = int(parts[3])
    actual_thread = thread_id if thread_id != 0 else None

    async with session_factory() as session:
        poll = await update_poll_result_destination(session, poll_id, group_id, actual_thread)

    await callback.answer("✅ Тема для публикации итогов сохранена!", show_alert=True)
    bot_user = await callback.bot.get_me()
    if callback.message and poll:
        await callback.message.edit_text(
            f"⚙️ <b>Панель управления опросом</b>\n\n«{poll.question}»\n\n"
            f"🎯 <b>Итоги будут опубликованы в:</b> группа <code>{group_id}</code>, тема #{thread_id or 'General'}",
            reply_markup=get_creator_manage_keyboard(
                poll_id=poll.id,
                bot_username=bot_user.username or "",
                poll_type=poll.poll_type,
                is_closed=poll.is_closed,
            ),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("set_res_manual:"))
async def on_set_res_manual(callback: CallbackQuery, state: FSMContext) -> None:
    """Manual entry of result chat and thread ID."""
    poll_id = callback.data.split(":")[1]
    await state.set_state(PollEditing.waiting_for_new_result_dest)
    await state.update_data(editing_poll_id=poll_id)
    if callback.message:
        await callback.message.edit_text(
            "✏️ Отправьте ID чата и ID темы через двоеточие (например: <code>-1001234567890:42</code>):",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"res_dest:{poll_id}")]]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(PollEditing.waiting_for_new_result_dest, F.text)
async def process_manual_result_dest(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saves manual result chat and thread."""
    data = await state.get_data()
    poll_id = data.get("editing_poll_id")
    await state.clear()
    if not poll_id:
        return

    text = (message.text or "").strip()
    match = re.match(r"^(-100\d+|\d+)(?::(\d+))?$", text)
    if not match:
        await message.answer("⚠️ Неверный формат. Ожидалось: <code>-1001234567890:42</code> или <code>-1001234567890</code>")
        return

    chat_id = int(match.group(1))
    thread_id = int(match.group(2)) if match.group(2) else None

    async with session_factory() as session:
        poll = await update_poll_result_destination(session, poll_id, chat_id, thread_id)

    bot_user = await message.bot.get_me()
    await message.answer(
        f"✅ <b>Тема для вывода итогов успешно настроена!</b>\n\n"
        f"Чат: <code>{chat_id}</code>, тема: <code>{thread_id or 'General'}</code>",
        reply_markup=get_creator_manage_keyboard(
            poll_id=poll_id,
            bot_username=bot_user.username or "",
            poll_type=poll.poll_type if poll else "choice",
            is_closed=poll.is_closed if poll else False,
        ),
        parse_mode="HTML",
    )


# ==============================================================================
# Delete Poll Handlers
# ==============================================================================

@router.callback_query(F.data.startswith("del_poll:"))
async def on_del_poll_request(callback: CallbackQuery) -> None:
    """Asks confirmation before deleting poll."""
    poll_id = callback.data.split(":")[1]
    if callback.message:
        await callback.message.edit_text(
            "🗑 <b>Удаление опроса</b>\n\n"
            "⚠️ Вы действительно хотите удалить этот опрос?\n"
            "Все собранные ответы, голоса и статистика будут удалены безвозвратно.",
            reply_markup=get_delete_poll_confirm_keyboard(poll_id),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("del_confirm:"))
async def on_del_poll_confirm(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deletes poll and cleans up posts and scheduler."""
    user = callback.from_user
    if not user:
        return
    poll_id = callback.data.split(":")[1]

    cancel_poll_closing(poll_id)

    async with session_factory() as session:
        posts = await get_poll_posts(session, poll_id)
        deleted = await delete_poll(session, poll_id)

    # Disable buttons on published posts
    for post in posts:
        try:
            if post.chat_id and post.message_id:
                await callback.bot.edit_message_reply_markup(
                    chat_id=post.chat_id,
                    message_id=post.message_id,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="❌ Опрос удалён автором", callback_data="none")]]
                    ),
                )
        except Exception:
            pass

    if deleted:
        await callback.answer("🗑 Опрос успешно удалён!", show_alert=True)
    else:
        await callback.answer("❌ Опрос не найден или уже удалён.", show_alert=True)

    # Return to /my_polls
    async with session_factory() as session:
        polls = await get_user_polls(session, creator_id=user.id, limit=25)

    if not polls:
        text = "ℹ️ У вас больше нет созданных опросов."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➕ Создать новый опрос", callback_data="btn_create_poll")]]
        )
    else:
        lines = ["📋 <b>Ваши опросы:</b>\n"]
        buttons = []
        for p in polls:
            status_icon = "🔒" if p.is_closed else "🟢"
            type_icon = "📊" if p.poll_type == "choice" else "✍️"
            short_q = p.question[:35] + ("..." if len(p.question) > 35 else "")
            lines.append(f"{status_icon} {type_icon} <b>«{short_q}»</b> (ID: <code>{p.id}</code>)")
            buttons.append([InlineKeyboardButton(text=f"{type_icon} «{short_q}»", callback_data=f"manage:{p.id}")])
        buttons.append([InlineKeyboardButton(text="➕ Создать ещё опрос", callback_data="btn_create_poll")])
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "del_all_closed_ask")
async def on_del_all_closed_ask(callback: CallbackQuery) -> None:
    """Asks confirmation to delete all closed polls."""
    if callback.message:
        await callback.message.edit_text(
            "🗑 <b>Удаление всех завершённых опросов</b>\n\n"
            "⚠️ Вы уверены? Будут удалены все опросы со статусом 🔒 (Завершён).\n"
            "Активные опросы останутся без изменений.",
            reply_markup=get_delete_all_closed_confirm_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "del_all_closed_confirm")
async def on_del_all_closed_confirm(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deletes all closed polls for user."""
    user = callback.from_user
    if not user:
        return

    async with session_factory() as session:
        count = await delete_closed_polls_for_user(session, user.id)
        polls = await get_user_polls(session, creator_id=user.id, limit=25)

    await callback.answer(f"🗑 Удалено завершённых опросов: {count} шт.", show_alert=True)

    if not polls:
        text = "ℹ️ У вас пока нет созданных опросов."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➕ Создать новый опрос", callback_data="btn_create_poll")]]
        )
    else:
        lines = ["📋 <b>Ваши опросы:</b>\n"]
        buttons = []
        for p in polls:
            status_icon = "🔒" if p.is_closed else "🟢"
            type_icon = "📊" if p.poll_type == "choice" else "✍️"
            short_q = p.question[:35] + ("..." if len(p.question) > 35 else "")
            lines.append(f"{status_icon} {type_icon} <b>«{short_q}»</b> (ID: <code>{p.id}</code>)")
            buttons.append([InlineKeyboardButton(text=f"{type_icon} «{short_q}»", callback_data=f"manage:{p.id}")])
        buttons.append([InlineKeyboardButton(text="➕ Создать ещё опрос", callback_data="btn_create_poll")])
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ==============================================================================
# Template Handlers
# ==============================================================================

@router.callback_query(F.data.startswith("save_tpl:"))
async def on_save_template(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saves existing poll as a reusable template."""
    user = callback.from_user
    if not user:
        return
    poll_id = callback.data.split(":")[1]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)
        if not poll:
            await callback.answer("❌ Опрос не найден.", show_alert=True)
            return

        opt_texts = [opt.text for opt in poll.options] if poll.options else []
        title = poll.question[:35] + ("..." if len(poll.question) > 35 else "")

        await create_poll_template(
            session=session,
            user_id=user.id,
            title=title,
            poll_type=poll.poll_type,
            question=poll.question,
            options=opt_texts,
            is_multiple_choice=poll.is_multiple_choice,
            is_anonymous=poll.is_anonymous,
            allowed_voters=poll.allowed_voters,
        )

    await callback.answer("💾 Опрос успешно сохранён в ваши шаблоны!", show_alert=True)


@router.message(Command("templates"))
@router.callback_query(F.data == "btn_templates")
async def show_templates_list(
    event: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lists creator's reusable poll templates."""
    user = event.from_user
    if not user:
        return

    async with session_factory() as session:
        templates = await get_user_templates(session, user.id)

    if not templates:
        text = (
            "📁 <b>Шаблоны опросов</b>\n\n"
            "У вас пока нет сохранённых шаблонов.\n\n"
            "💡 <i>Чтобы создать шаблон, откройте любой созданный опрос и нажмите «💾 В шаблон». "
            "После этого вы сможете запускать аналогичный опрос в 1 клик!</i>"
        )
        buttons = [
            [InlineKeyboardButton(text="➕ Создать новый опрос", callback_data="btn_create_poll")],
            [InlineKeyboardButton(text="📋 Мои опросы", callback_data="btn_my_polls")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="btn_main_menu")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    else:
        text = (
            "📁 <b>Ваши шаблоны опросов:</b>\n\n"
            "Выберите шаблон для просмотра, быстрого запуска или удаления:"
        )
        kb = get_templates_list_keyboard(templates)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data.startswith("tpl_view:"))
async def on_view_template(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Shows details of a single template."""
    user = callback.from_user
    if not user:
        return
    tpl_id = int(callback.data.split(":")[1])

    async with session_factory() as session:
        tpl = await get_template(session, tpl_id)

    if not tpl or tpl.user_id != user.id:
        await callback.answer("❌ Шаблон не найден.", show_alert=True)
        return

    type_name = "С выбором вариантов" if tpl.poll_type == "choice" else "С открытым ответом"
    mode_str = "Множественный выбор" if tpl.is_multiple_choice else "Один вариант"
    anon_str = "Анонимное" if tpl.is_anonymous else "Открытое"
    voter_map = {"all": "Все участники", "with_username": "Только с @username", "admins_only": "Только админы"}
    voter_str = voter_map.get(tpl.allowed_voters or "all", "Все")

    opts_str = ""
    if tpl.options:
        opts_str = "\n\n<b>Варианты ответа:</b>\n" + "\n".join([f"• {opt}" for opt in tpl.options])

    text = (
        f"📄 <b>Шаблон: «{tpl.title}»</b>\n\n"
        f"❓ <b>Вопрос:</b> «{tpl.question}»\n"
        f"🏷 <b>Тип:</b> {type_name}\n"
        f"🔘 <b>Режим:</b> {mode_str}\n"
        f"🔒 <b>Приватность:</b> {anon_str}\n"
        f"👥 <b>Участники:</b> {voter_str}"
        f"{opts_str}"
    )

    kb = get_template_action_keyboard(tpl.id)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("tpl_launch:"))
async def on_launch_template(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Launches a new poll pre-filled from template."""
    user = callback.from_user
    if not user:
        return
    tpl_id = int(callback.data.split(":")[1])

    async with session_factory() as session:
        tpl = await get_template(session, tpl_id)

    if not tpl or tpl.user_id != user.id:
        await callback.answer("❌ Шаблон не найден.", show_alert=True)
        return

    await state.clear()
    await state.set_state(PollCreation.waiting_for_deadline)
    await state.update_data(
        poll_type=tpl.poll_type,
        question=tpl.question,
        options=list(tpl.options) if tpl.options else [],
        is_multiple_choice=tpl.is_multiple_choice,
        is_anonymous=tpl.is_anonymous,
        allowed_voters=tpl.allowed_voters or "all",
    )

    if callback.message:
        await callback.message.edit_text(
            f"🚀 <b>Быстрый запуск опроса по шаблону</b>\n\n"
            f"❓ <b>Вопрос:</b> «{tpl.question}»\n\n"
            f"<b>Шаг 4:</b> Выберите дедлайн (время активности) для опроса:",
            reply_markup=get_deadline_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("tpl_del:"))
async def on_delete_template(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deletes a template."""
    user = callback.from_user
    if not user:
        return
    tpl_id = int(callback.data.split(":")[1])

    async with session_factory() as session:
        deleted = await delete_template(session, tpl_id, user.id)

    if deleted:
        await callback.answer("🗑 Шаблон удалён.", show_alert=True)
    else:
        await callback.answer("❌ Шаблон не найден.", show_alert=True)

    await show_templates_list(callback, session_factory)
