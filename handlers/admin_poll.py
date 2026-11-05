"""Poll administration and management handlers (Creator and Chat Admin permissions)."""

import logging
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from database.db_manager import (
    get_all_text_answers,
    get_choice_poll_stats,
    get_poll,
)
from database.models import Vote
from keyboards.inline import (
    get_close_confirmation_keyboard,
    get_creator_manage_keyboard,
)
from services.poll_service import (
    format_choice_results,
    format_text_poll_summary,
    generate_choice_votes_csv,
    generate_text_answers_csv,
    generate_text_answers_txt,
    is_user_creator_or_admin,
)
from services.scheduler import close_poll_and_publish

logger = logging.getLogger(__name__)

router = Router(name="admin_poll_router")


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

    # Check permission (creator or chat admin)
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

    text = (
        f"⚙️ <b>Панель управления опросом</b>\n\n"
        f"❓ <b>Вопрос:</b> «{poll.question}»\n"
        f"<b>Тип:</b> {type_str}\n"
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
            stats = await get_choice_poll_stats(session, poll_id)
            stats_text = (
                f"👁 <b>Промежуточная статистика (видна автору и админам):</b>\n\n"
                f"{format_choice_results(poll.question, stats, is_closed=poll.is_closed, closed_at=poll.closed_at, deadline_at=poll.deadline_at)}"
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

            csv_data = generate_text_answers_csv(poll.question, answers)
            txt_data = generate_text_answers_txt(poll.question, answers)

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
            csv_data = generate_choice_votes_csv(poll.question, votes, options_map)

            csv_file = BufferedInputFile(csv_data, filename=f"poll_{poll_id[:8]}_votes.csv")

            await callback.bot.send_document(
                chat_id=user.id,
                document=csv_file,
                caption=f"📊 Экспорт голосов: «{poll.question[:30]}...» ({len(votes)} голосов)",
            )
            await callback.answer("📁 Файл экспорта отправлен вам в ЛС!", show_alert=True)
