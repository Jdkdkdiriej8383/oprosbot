"""APScheduler service for automated poll completion with Telegram Topic support."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from database.db_manager import (
    close_poll,
    get_active_polls_with_deadlines,
    get_all_text_answers,
    get_choice_poll_stats,
    get_poll,
    get_poll_posts,
)
from services.poll_service import (
    format_choice_results,
    format_text_poll_summary,
    generate_text_answers_csv,
    generate_text_answers_txt,
)

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    """Returns global AsyncIOScheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def schedule_poll_closing(
    poll_id: str,
    deadline_at: datetime,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    reminder_minutes: Optional[int] = None,
) -> None:
    """Schedules automatic poll closure at deadline and pre-close reminder if requested."""
    scheduler = get_scheduler()
    job_id = f"close_poll_{poll_id}"

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        close_poll_and_publish,
        trigger="date",
        run_date=deadline_at,
        args=[poll_id, bot, session_factory, True],
        id=job_id,
        replace_existing=True,
    )
    logger.info("Scheduled auto-closing for poll %s at %s UTC", poll_id, deadline_at)

    # Schedule pre-closing reminder
    reminder_job_id = f"reminder_{poll_id}"
    if scheduler.get_job(reminder_job_id):
        scheduler.remove_job(reminder_job_id)

    if reminder_minutes and reminder_minutes > 0:
        reminder_at = deadline_at - timedelta(minutes=reminder_minutes)
        now_utc = datetime.now(timezone.utc)
        if reminder_at.tzinfo is None:
            reminder_at = reminder_at.replace(tzinfo=timezone.utc)

        if reminder_at > now_utc:
            scheduler.add_job(
                send_poll_reminder,
                trigger="date",
                run_date=reminder_at,
                args=[poll_id, bot, session_factory],
                id=reminder_job_id,
                replace_existing=True,
            )
            logger.info("Scheduled reminder for poll %s at %s UTC", poll_id, reminder_at)


def cancel_poll_closing(poll_id: str) -> None:
    """Cancels scheduled closing and reminder jobs."""
    scheduler = get_scheduler()
    job_id = f"close_poll_{poll_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Cancelled schedule for poll %s", poll_id)

    reminder_job_id = f"reminder_{poll_id}"
    if scheduler.get_job(reminder_job_id):
        scheduler.remove_job(reminder_job_id)


async def send_poll_reminder(
    poll_id: str,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sends pre-close notification to poll topic/chat and creator."""
    logger.info("Executing pre-close reminder for poll %s", poll_id)
    async with session_factory() as session:
        poll = await get_poll(session, poll_id)
        if not poll or poll.is_closed:
            return

        posts = await get_poll_posts(session, poll_id)

    rem_min = poll.reminder_minutes or 60
    rem_text = (
        f"⏰ <b>Внимание: скоро завершение опроса!</b>\n\n"
        f"❓ <b>Вопрос:</b> «{poll.question}»\n"
        f"⏳ До окончания голосования осталось <b>~{rem_min} мин.</b>\n"
        f"Не забудьте оставить свой голос выше! 👆"
    )

    sent_topics = set()
    for post in posts:
        if post.chat_id and (post.chat_id, post.message_thread_id) not in sent_topics:
            try:
                await bot.send_message(
                    chat_id=post.chat_id,
                    message_thread_id=post.message_thread_id,
                    text=rem_text,
                    parse_mode="HTML",
                )
                sent_topics.add((post.chat_id, post.message_thread_id))
            except Exception as e:
                logger.debug("Failed to send reminder to chat %s: %s", post.chat_id, e)

    try:
        await bot.send_message(
            chat_id=poll.creator_id,
            text=f"⏰ <b>Напоминание:</b> до завершения вашего опроса «{poll.question}» осталось ~{rem_min} мин.",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def close_poll_and_publish(
    poll_id: str,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    is_automated: bool = False,
    closed_by_user_id: Optional[int] = None,
    is_admin: bool = False,
) -> bool:
    """
    Closes poll and publishes results:
    1. Updates original poll message in topic/chat (removes/disables voting buttons).
    2. Sends final results post to the EXACT SAME forum topic (message_thread_id).
    3. Sends reports and files to creator in PM.
    """
    logger.info("Executing closing for poll %s (automated=%s)", poll_id, is_automated)
    cancel_poll_closing(poll_id)

    async with session_factory() as session:
        success, poll, msg = await close_poll(
            session=session,
            poll_id=poll_id,
            user_id=closed_by_user_id,
            is_admin=is_admin,
            force=is_automated,
        )
        if not success or not poll:
            logger.warning("Could not close poll %s: %s", poll_id, msg)
            return False

        if poll.poll_type == "choice":
            stats = await get_choice_poll_stats(session, poll_id, include_voters=not poll.is_anonymous)
            final_text = format_choice_results(
                question=poll.question,
                stats=stats,
                is_closed=True,
                closed_at=poll.closed_at,
                is_anonymous=poll.is_anonymous,
                is_multiple_choice=poll.is_multiple_choice,
            )
            closed_button_text = "🔒 Голосование завершено"
        else:
            answers = await get_all_text_answers(session, poll_id)
            final_text = format_text_poll_summary(
                question=poll.question,
                answers_count=len(answers),
                is_closed=True,
                closed_at=poll.closed_at,
            )
            closed_button_text = "🔒 Приём ответов закрыт"

        closed_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=closed_button_text,
                        callback_data=f"poll_closed_info:{poll_id}",
                    )
                ]
            ]
        )

        posts = await get_poll_posts(session, poll_id)

    # 1. Update existing messages and publish final post to topic/chat
    has_custom_result_dest = bool(poll.result_chat_id)
    results_sent_chats = set()

    for post in posts:
        try:
            if post.inline_message_id:
                # Update inline query message
                await bot.edit_message_text(
                    text=final_text,
                    inline_message_id=post.inline_message_id,
                    reply_markup=closed_markup,
                    parse_mode="HTML",
                )
            elif post.chat_id and post.message_id:
                # Disable buttons on original message
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=post.chat_id,
                        message_id=post.message_id,
                        reply_markup=closed_markup,
                    )
                except TelegramAPIError as e:
                    logger.debug("Could not update reply markup: %s", e)

                # If no custom result destination, post results into the same topic
                if not has_custom_result_dest:
                    await bot.send_message(
                        chat_id=post.chat_id,
                        message_thread_id=post.message_thread_id,
                        text=final_text,
                        parse_mode="HTML",
                    )
                    results_sent_chats.add((post.chat_id, post.message_thread_id))
                else:
                    try:
                        await bot.send_message(
                            chat_id=post.chat_id,
                            message_thread_id=post.message_thread_id,
                            text="🔒 <b>Опрос завершён!</b>\nИтоговые результаты опубликованы в назначенной теме и отправлены автору.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
        except TelegramAPIError as e:
            logger.debug("Error updating post in chat %s, thread %s: %s", post.chat_id, post.message_thread_id, e)
        except Exception as e:
            logger.error("Unexpected error publishing to post: %s", e)

    # Publish to custom destination if configured
    if has_custom_result_dest and (poll.result_chat_id, poll.result_thread_id) not in results_sent_chats:
        try:
            await bot.send_message(
                chat_id=poll.result_chat_id,
                message_thread_id=poll.result_thread_id,
                text=final_text,
                parse_mode="HTML",
            )
            logger.info("Published poll %s results to custom destination chat %s thread %s", poll_id, poll.result_chat_id, poll.result_thread_id)
        except Exception as e:
            logger.error("Failed to publish results to custom destination chat %s thread %s: %s", poll.result_chat_id, poll.result_thread_id, e)

    # 2. Notify creator in PM
    try:
        closer_info = "⏰ Время вышло (дедлайн)" if is_automated else "👤 Опрос завершён вручную"

        if poll.poll_type == "choice":
            creator_msg = (
                f"🔔 <b>Опрос завершён!</b>\n"
                f"{closer_info}\n\n"
                f"{final_text}"
            )
            await bot.send_message(
                chat_id=poll.creator_id,
                text=creator_msg,
                parse_mode="HTML",
            )
        else:
            async with session_factory() as session:
                answers = await get_all_text_answers(session, poll_id)

            csv_data = generate_text_answers_csv(poll.question, answers, is_anonymous=poll.is_anonymous)
            txt_data = generate_text_answers_txt(poll.question, answers, is_anonymous=poll.is_anonymous)

            csv_file = BufferedInputFile(csv_data, filename=f"poll_{poll_id[:8]}_answers.csv")
            txt_file = BufferedInputFile(txt_data, filename=f"poll_{poll_id[:8]}_answers.txt")

            creator_msg = (
                f"🔔 <b>Опрос с открытыми ответами завершён!</b>\n"
                f"{closer_info}\n\n"
                f"❓ <b>Вопрос:</b> «{poll.question}»\n"
                f"📬 <b>Всего ответов:</b> {len(answers)}\n\n"
                f"📎 Ниже прикреплены отчёты со всеми ответами."
            )
            await bot.send_message(
                chat_id=poll.creator_id,
                text=creator_msg,
                parse_mode="HTML",
            )

            if answers:
                await bot.send_document(
                    chat_id=poll.creator_id,
                    document=csv_file,
                    caption=f"📊 Excel CSV отчёт ({len(answers)} ответов)",
                )
                await bot.send_document(
                    chat_id=poll.creator_id,
                    document=txt_file,
                    caption=f"📝 Текстовый TXT отчёт ({len(answers)} ответов)",
                )
            else:
                await bot.send_message(
                    chat_id=poll.creator_id,
                    text="ℹ️ За время опроса ответов не поступило.",
                )
    except TelegramAPIError as e:
        logger.warning("Could not send closing notice to creator %s: %s", poll.creator_id, e)
    except Exception as e:
        logger.error("Error sending closing notice: %s", e)

    return True


async def resume_scheduled_polls(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Recovers active poll deadlines on bot startup."""
    logger.info("Checking active polls with deadlines...")
    now_utc = datetime.now(timezone.utc)

    async with session_factory() as session:
        active_polls = await get_active_polls_with_deadlines(session)

    logger.info("Found %d active scheduled polls.", len(active_polls))

    for poll in active_polls:
        if not poll.deadline_at:
            continue

        deadline = poll.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)

        if deadline <= now_utc:
            logger.info("Poll %s deadline passed while bot was offline. Closing now...", poll.id)
            asyncio.create_task(
                close_poll_and_publish(
                    poll_id=poll.id,
                    bot=bot,
                    session_factory=session_factory,
                    is_automated=True,
                )
            )
        else:
            schedule_poll_closing(
                poll_id=poll.id,
                deadline_at=deadline,
                bot=bot,
                session_factory=session_factory,
                reminder_minutes=poll.reminder_minutes,
            )
