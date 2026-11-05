"""Database manager and CRUD operations with topic and async SQLite/PostgreSQL support."""

from datetime import datetime, timezone
import logging
from typing import List, Optional, Tuple

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from database.models import Base, Option, Poll, PollPost, TextAnswer, User, Vote

logger = logging.getLogger(__name__)

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(database_url: str = "sqlite+aiosqlite:///oprosbot.db") -> AsyncEngine:
    """Returns or creates the global AsyncEngine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            database_url,
            echo=False,
            future=True,
        )

        if "sqlite" in database_url:
            @event.listens_for(_engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_session_factory(database_url: str = "sqlite+aiosqlite:///oprosbot.db") -> async_sessionmaker[AsyncSession]:
    """Returns or creates the async session factory."""
    global _session_factory
    if _session_factory is None:
        engine = get_engine(database_url)
        _session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def init_db(database_url: str = "sqlite+aiosqlite:///oprosbot.db") -> None:
    """Creates database tables."""
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        logger.info("Initializing database schema...")
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema initialized.")


async def close_db() -> None:
    """Disposes engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


# ==============================================================================
# CRUD Operations
# ==============================================================================

async def upsert_user(
    session: AsyncSession,
    user_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
) -> User:
    """Creates or updates a Telegram user record."""
    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    if user is None:
        user = User(id=user_id, username=username, full_name=full_name)
        session.add(user)
    else:
        user.username = username
        user.full_name = full_name

    await session.commit()
    await session.refresh(user)
    return user


async def create_poll(
    session: AsyncSession,
    poll_id: str,
    creator_id: int,
    creator_name: Optional[str],
    poll_type: str,
    question: str,
    options: Optional[List[str]] = None,
    deadline_at: Optional[datetime] = None,
    results_visibility: str = "hidden_until_close",
    target_chat_id: Optional[int] = None,
    target_thread_id: Optional[int] = None,
) -> Poll:
    """Creates a new poll with topic support."""
    poll = Poll(
        id=poll_id,
        creator_id=creator_id,
        creator_name=creator_name,
        poll_type=poll_type,
        question=question,
        deadline_at=deadline_at,
        results_visibility=results_visibility,
        target_chat_id=target_chat_id,
        target_thread_id=target_thread_id,
        is_closed=False,
    )
    session.add(poll)
    await session.flush()

    if poll_type == "choice" and options:
        for idx, opt_text in enumerate(options):
            opt = Option(
                poll_id=poll.id,
                text=opt_text.strip(),
                order_index=idx,
            )
            session.add(opt)

    await session.commit()
    return await get_poll(session, poll_id)  # type: ignore


async def get_poll(session: AsyncSession, poll_id: str) -> Optional[Poll]:
    """Retrieves poll with options and posts eagerly loaded."""
    stmt = (
        select(Poll)
        .options(
            selectinload(Poll.options),
            selectinload(Poll.posts),
        )
        .where(Poll.id == poll_id)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_user_polls(
    session: AsyncSession,
    creator_id: int,
    limit: int = 15,
) -> List[Poll]:
    """Retrieves polls created by a user."""
    stmt = (
        select(Poll)
        .options(selectinload(Poll.options))
        .where(Poll.creator_id == creator_id)
        .order_by(Poll.created_at.desc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def record_choice_vote(
    session: AsyncSession,
    poll_id: str,
    user_id: int,
    option_id: int,
    user_fullname: Optional[str] = None,
    username: Optional[str] = None,
) -> Tuple[bool, bool, str]:
    """
    Records or updates a choice vote.
    Returns: (success, is_changed, notification_message)
    """
    poll = await get_poll(session, poll_id)
    if not poll:
        return False, False, "❌ Опрос не найден."

    if poll.is_closed:
        return False, False, "🔒 Опрос уже завершён. Приём голосов закрыт."

    opt_stmt = select(Option).where(
        Option.id == option_id,
        Option.poll_id == poll_id,
    )
    opt_res = await session.execute(opt_stmt)
    option = opt_res.scalar_one_or_none()
    if not option:
        return False, False, "❌ Выбранный вариант не найден."

    vote_stmt = select(Vote).where(
        Vote.poll_id == poll_id,
        Vote.user_id == user_id,
    )
    vote_res = await session.execute(vote_stmt)
    existing_vote = vote_res.scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)

    if existing_vote:
        if existing_vote.option_id == option_id:
            return True, False, "Вы уже отдали свой голос за этот вариант! 👌"
        else:
            existing_vote.option_id = option_id
            existing_vote.voted_at = now_utc
            if user_fullname is not None:
                existing_vote.user_fullname = user_fullname
            if username is not None:
                existing_vote.username = username
            await session.commit()
            return True, True, f"🔄 Ваш голос изменён на: «{option.text}»"
    else:
        new_vote = Vote(
            poll_id=poll_id,
            user_id=user_id,
            option_id=option_id,
            user_fullname=user_fullname,
            username=username,
            voted_at=now_utc,
        )
        session.add(new_vote)
        await session.commit()
        return True, False, f"✅ Ваш голос учтён: «{option.text}»"


async def get_user_vote(
    session: AsyncSession,
    poll_id: str,
    user_id: int,
) -> Optional[Vote]:
    """Gets vote of a specific user."""
    stmt = (
        select(Vote)
        .options(selectinload(Vote.option))
        .where(Vote.poll_id == poll_id, Vote.user_id == user_id)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_choice_poll_stats(session: AsyncSession, poll_id: str) -> dict:
    """Calculates statistics for a choice poll."""
    options_stmt = (
        select(Option)
        .where(Option.poll_id == poll_id)
        .order_by(Option.order_index)
    )
    options_res = await session.execute(options_stmt)
    options = list(options_res.scalars().all())

    counts_stmt = (
        select(Vote.option_id, func.count(Vote.id))
        .where(Vote.poll_id == poll_id)
        .group_by(Vote.option_id)
    )
    counts_res = await session.execute(counts_stmt)
    counts_map = dict(counts_res.all())

    total_votes = sum(counts_map.values())

    results = []
    for opt in options:
        count = counts_map.get(opt.id, 0)
        percentage = (count / total_votes * 100) if total_votes > 0 else 0.0
        results.append(
            {
                "option_id": opt.id,
                "text": opt.text,
                "count": count,
                "percentage": round(percentage, 1),
            }
        )

    return {
        "poll_id": poll_id,
        "total_votes": total_votes,
        "options": results,
    }


async def save_text_answer(
    session: AsyncSession,
    poll_id: str,
    user_id: int,
    text: str,
    user_fullname: Optional[str] = None,
    username: Optional[str] = None,
) -> Tuple[bool, bool, str]:
    """Saves or updates an open text answer."""
    poll = await get_poll(session, poll_id)
    if not poll:
        return False, False, "❌ Опрос не найден."

    if poll.is_closed:
        return False, False, "🔒 Опрос уже завершён. Приём ответов закрыт."

    now_utc = datetime.now(timezone.utc)

    resp_stmt = select(TextAnswer).where(
        TextAnswer.poll_id == poll_id,
        TextAnswer.user_id == user_id,
    )
    resp_res = await session.execute(resp_stmt)
    existing = resp_res.scalar_one_or_none()

    if existing:
        existing.answer_text = text.strip()
        existing.updated_at = now_utc
        if user_fullname is not None:
            existing.user_fullname = user_fullname
        if username is not None:
            existing.username = username
        await session.commit()
        return True, True, "✏️ Ваш ответ был успешно обновлён!"
    else:
        new_resp = TextAnswer(
            poll_id=poll_id,
            user_id=user_id,
            user_fullname=user_fullname,
            username=username,
            answer_text=text.strip(),
            submitted_at=now_utc,
        )
        session.add(new_resp)
        await session.commit()
        return True, False, "✅ Ваш развёрнутый ответ принят и сохранён!"


async def get_user_text_answer(
    session: AsyncSession,
    poll_id: str,
    user_id: int,
) -> Optional[TextAnswer]:
    """Gets user's text answer."""
    stmt = select(TextAnswer).where(
        TextAnswer.poll_id == poll_id,
        TextAnswer.user_id == user_id,
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_all_text_answers(
    session: AsyncSession,
    poll_id: str,
) -> List[TextAnswer]:
    """Gets all text answers for a poll."""
    stmt = (
        select(TextAnswer)
        .where(TextAnswer.poll_id == poll_id)
        .order_by(TextAnswer.submitted_at.asc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def close_poll(
    session: AsyncSession,
    poll_id: str,
    user_id: Optional[int] = None,
    is_admin: bool = False,
    force: bool = False,
) -> Tuple[bool, Optional[Poll], str]:
    """
    Closes a poll.
    Can be closed if force=True, user is creator, or is_admin=True.
    """
    poll = await get_poll(session, poll_id)
    if not poll:
        return False, None, "❌ Опрос не найден."

    if not force and not is_admin and user_id is not None and poll.creator_id != user_id:
        return False, poll, "⛔ У вас нет прав для закрытия этого опроса!"

    if poll.is_closed:
        return False, poll, "ℹ️ Этот опрос уже был закрыт ранее."

    poll.is_closed = True
    poll.closed_at = datetime.now(timezone.utc)
    await session.commit()
    closed_poll = await get_poll(session, poll_id)
    return True, closed_poll, "✅ Опрос успешно завершён!"


async def track_poll_post(
    session: AsyncSession,
    poll_id: str,
    chat_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    message_id: Optional[int] = None,
    inline_message_id: Optional[str] = None,
) -> None:
    """Tracks message locations with topic support (message_thread_id)."""
    if not inline_message_id and not (chat_id and message_id):
        return

    stmt = select(PollPost).where(
        PollPost.poll_id == poll_id,
        PollPost.chat_id == chat_id,
        PollPost.message_id == message_id,
        PollPost.inline_message_id == inline_message_id,
    )
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is None:
        post = PollPost(
            poll_id=poll_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )
        session.add(post)
        try:
            await session.commit()
        except Exception:
            await session.rollback()


async def get_poll_posts(
    session: AsyncSession,
    poll_id: str,
) -> List[PollPost]:
    """Gets all tracked posts for a poll."""
    stmt = select(PollPost).where(PollPost.poll_id == poll_id)
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_active_polls_with_deadlines(
    session: AsyncSession,
) -> List[Poll]:
    """Gets all active polls with pending deadlines."""
    stmt = (
        select(Poll)
        .options(selectinload(Poll.options), selectinload(Poll.posts))
        .where(
            Poll.is_closed == False,  # noqa: E712
            Poll.deadline_at.is_not(None),
        )
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())
