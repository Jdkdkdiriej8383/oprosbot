"""Inline mode handler for sending polls directly to groups, channels, and forum topics."""

import logging
from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Poll
from keyboards.inline import (
    get_choice_voting_keyboard,
    get_text_poll_chat_keyboard,
)

logger = logging.getLogger(__name__)

router = Router(name="inline_router")


@router.inline_query()
async def process_inline_query(
    inline_query: InlineQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    Handles inline queries:
    @bot poll_id
    or search recent polls created by the user.
    """
    user_id = inline_query.from_user.id
    raw_query = inline_query.query.strip()
    bot_user = await inline_query.bot.get_me()
    bot_username = bot_user.username or ""

    async with session_factory() as session:
        if raw_query.startswith("poll_"):
            poll_id = raw_query[len("poll_"):]
            stmt = (
                select(Poll)
                .options(selectinload(Poll.options))
                .where(Poll.id == poll_id)
            )
        elif raw_query:
            stmt = (
                select(Poll)
                .options(selectinload(Poll.options))
                .where(
                    (Poll.id == raw_query) | (Poll.question.ilike(f"%{raw_query}%")),
                    Poll.creator_id == user_id,
                )
                .order_by(Poll.created_at.desc())
                .limit(10)
            )
        else:
            stmt = (
                select(Poll)
                .options(selectinload(Poll.options))
                .where(Poll.creator_id == user_id)
                .order_by(Poll.created_at.desc())
                .limit(10)
            )

        res = await session.execute(stmt)
        polls = list(res.scalars().all())

    results = []
    for p in polls:
        is_closed = p.is_closed

        if p.poll_type == "choice":
            type_label = "📊 Опрос с вариантами"
            status_text = "🔒 Завершён" if is_closed else "🟢 Идёт голосование"

            message_text = (
                f"📊 <b>Опрос:</b>\n"
                f"«{p.question}»\n\n"
                f"<i>🔒 Результаты скрыты до завершения голосования.</i>"
            )
            reply_markup = get_choice_voting_keyboard(p.id, p.options) if not is_closed else None
        else:
            type_label = "✍️ Опрос с открытым ответом"
            status_text = "🔒 Завершён" if is_closed else "🟢 Приём ответов"

            message_text = (
                f"📝 <b>Опрос с открытым ответом:</b>\n"
                f"«{p.question}»\n\n"
                f"<i>Нажмите кнопку ниже, чтобы дать развёрнутый ответ боту в ЛС.</i>"
            )
            reply_markup = get_text_poll_chat_keyboard(p.id, bot_username) if not is_closed else None

        results.append(
            InlineQueryResultArticle(
                id=f"poll_{p.id}",
                title=f"{p.question[:50]}...",
                description=f"{type_label} | {status_text}",
                input_message_content=InputTextMessageContent(
                    message_text=message_text,
                    parse_mode="HTML",
                ),
                reply_markup=reply_markup,
            )
        )

    await inline_query.answer(
        results=results,
        cache_time=1,
        is_personal=True,
    )
