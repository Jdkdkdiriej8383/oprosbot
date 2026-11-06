"""Database manager and CRUD operations with topic and async SQLite/PostgreSQL support."""

from datetime import datetime, timezone
import json
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

from database.models import (
    Base,
    ForumTopic,
    GroupChat,
    Option,
    Poll,
    PollPost,
    PollTemplate,
    TextAnswer,
    User,
    Vote,
)

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
    """Creates database tables and applies lightweight schema migrations."""
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        logger.info("Initializing database schema...")
        await conn.run_sync(Base.metadata.create_all)

        # SQLite automatic migration check for new columns
        if "sqlite" in database_url:
            from sqlalchemy import text
            try:
                res = await conn.execute(text("PRAGMA table_info(polls)"))
                cols = {row[1] for row in res.fetchall()}
                if "result_chat_id" not in cols:
                    await conn.execute(text("ALTER TABLE polls ADD COLUMN result_chat_id BIGINT"))
                    logger.info("Schema migration: added polls.result_chat_id")
                if "result_thread_id" not in cols:
                    await conn.execute(text("ALTER TABLE polls ADD COLUMN result_thread_id INTEGER"))
                    logger.info("Schema migration: added polls.result_thread_id")
                if "is_multiple_choice" not in cols:
                    await conn.execute(text("ALTER TABLE polls ADD COLUMN is_multiple_choice BOOLEAN DEFAULT 0"))
                    logger.info("Schema migration: added polls.is_multiple_choice")
                if "is_anonymous" not in cols:
                    await conn.execute(text("ALTER TABLE polls ADD COLUMN is_anonymous BOOLEAN DEFAULT 1"))
                    logger.info("Schema migration: added polls.is_anonymous")
                if "reminder_minutes" not in cols:
                    await conn.execute(text("ALTER TABLE polls ADD COLUMN reminder_minutes INTEGER"))
                    logger.info("Schema migration: added polls.reminder_minutes")
                if "allowed_voters" not in cols:
                    await conn.execute(text("ALTER TABLE polls ADD COLUMN allowed_voters VARCHAR(50) DEFAULT 'all'"))
                    logger.info("Schema migration: added polls.allowed_voters")
            except Exception as e:
                logger.warning("Could not verify schema columns: %s", e)

        logger.info("Database schema initialized.")


async def close_db() -> None:
    """Disposes engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


# ==============================================================================
# User CRUD
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


# ==============================================================================
# GroupChat and ForumTopic CRUD
# ==============================================================================

async def upsert_group_chat(
    session: AsyncSession,
    chat_id: int,
    title: str,
    username: Optional[str] = None,
    chat_type: str = "supergroup",
    is_forum: bool = False,
    added_by_user_id: Optional[int] = None,
) -> GroupChat:
    """Creates or updates a registered group chat."""
    stmt = select(GroupChat).where(GroupChat.id == chat_id)
    res = await session.execute(stmt)
    chat = res.scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)

    if chat is None:
        chat = GroupChat(
            id=chat_id,
            title=title,
            username=username,
            chat_type=chat_type,
            is_forum=is_forum,
            added_by_user_id=added_by_user_id,
            is_active=True,
            created_at=now_utc,
        )
        session.add(chat)
    else:
        chat.title = title
        chat.username = username
        chat.chat_type = chat_type
        chat.is_forum = is_forum
        chat.is_active = True
        chat.updated_at = now_utc
        if added_by_user_id is not None:
            chat.added_by_user_id = added_by_user_id

    await session.commit()
    await session.refresh(chat)
    return chat


async def deactivate_group_chat(session: AsyncSession, chat_id: int) -> None:
    """Marks group chat as inactive when bot is removed."""
    stmt = select(GroupChat).where(GroupChat.id == chat_id)
    res = await session.execute(stmt)
    chat = res.scalar_one_or_none()
    if chat:
        chat.is_active = False
        chat.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def get_group_chat(session: AsyncSession, chat_id: int) -> Optional[GroupChat]:
    """Retrieves group chat with topics."""
    stmt = (
        select(GroupChat)
        .options(selectinload(GroupChat.topics))
        .where(GroupChat.id == chat_id)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def get_user_group_chats(
    session: AsyncSession,
    user_id: int,
) -> List[GroupChat]:
    """Retrieves active groups that were added by this user."""
    stmt = (
        select(GroupChat)
        .options(selectinload(GroupChat.topics))
        .where(GroupChat.added_by_user_id == user_id, GroupChat.is_active == True)  # noqa: E712
        .order_by(GroupChat.title.asc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def upsert_forum_topic(
    session: AsyncSession,
    chat_id: int,
    thread_id: int,
    title: str,
    icon_emoji: Optional[str] = None,
) -> ForumTopic:
    """Registers or updates a forum topic."""
    stmt = select(ForumTopic).where(
        ForumTopic.chat_id == chat_id,
        ForumTopic.thread_id == thread_id,
    )
    res = await session.execute(stmt)
    topic = res.scalar_one_or_none()

    if topic is None:
        topic = ForumTopic(
            chat_id=chat_id,
            thread_id=thread_id,
            title=title,
            icon_emoji=icon_emoji,
        )
        session.add(topic)
    else:
        topic.title = title
        if icon_emoji:
            topic.icon_emoji = icon_emoji

    await session.commit()
    group = await session.get(GroupChat, chat_id)
    if group:
        session.expire(group, ["topics"])
    await session.refresh(topic)
    return topic


async def get_chat_topics(session: AsyncSession, chat_id: int) -> List[ForumTopic]:
    """Gets all known topics for a chat."""
    stmt = (
        select(ForumTopic)
        .where(ForumTopic.chat_id == chat_id)
        .order_by(ForumTopic.title.asc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


# ==============================================================================
# Poll CRUD Operations
# ==============================================================================

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
    result_chat_id: Optional[int] = None,
    result_thread_id: Optional[int] = None,
    is_multiple_choice: bool = False,
    is_anonymous: bool = True,
    reminder_minutes: Optional[int] = None,
    allowed_voters: str = "all",
) -> Poll:
    """Creates a new poll with topic, custom result destination, and advanced options."""
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
        result_chat_id=result_chat_id,
        result_thread_id=result_thread_id,
        is_multiple_choice=is_multiple_choice,
        is_anonymous=is_anonymous,
        reminder_minutes=reminder_minutes,
        allowed_voters=allowed_voters,
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
    limit: int = 25,
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


async def update_poll_question(
    session: AsyncSession,
    poll_id: str,
    question: str,
) -> Optional[Poll]:
    """Updates question text of an active poll."""
    stmt = update(Poll).where(Poll.id == poll_id).values(question=question)
    await session.execute(stmt)
    await session.commit()
    return await get_poll(session, poll_id)


async def add_poll_option(
    session: AsyncSession,
    poll_id: str,
    option_text: str,
) -> Optional[Option]:
    """Adds a new option to a choice poll while it is running."""
    poll = await get_poll(session, poll_id)
    if not poll or poll.is_closed or poll.poll_type != "choice":
        return None
    next_idx = len(poll.options)
    opt = Option(
        poll_id=poll_id,
        text=option_text.strip(),
        order_index=next_idx,
    )
    session.add(opt)
    await session.commit()
    session.expire(poll, ["options"])
    await session.refresh(opt)
    return opt


async def update_poll_deadline(
    session: AsyncSession,
    poll_id: str,
    deadline_at: Optional[datetime],
) -> Optional[Poll]:
    """Updates deadline of an active poll."""
    stmt = update(Poll).where(Poll.id == poll_id).values(deadline_at=deadline_at)
    await session.execute(stmt)
    await session.commit()
    return await get_poll(session, poll_id)


async def update_poll_result_destination(
    session: AsyncSession,
    poll_id: str,
    result_chat_id: Optional[int],
    result_thread_id: Optional[int],
) -> Optional[Poll]:
    """Updates where the results will be published."""
    stmt = update(Poll).where(Poll.id == poll_id).values(
        result_chat_id=result_chat_id,
        result_thread_id=result_thread_id,
    )
    await session.execute(stmt)
    await session.commit()
    return await get_poll(session, poll_id)


async def delete_poll(session: AsyncSession, poll_id: str) -> bool:
    """Deletes a poll and cascades all associated votes, answers, and posts."""
    poll = await get_poll(session, poll_id)
    if not poll:
        return False
    await session.delete(poll)
    await session.commit()
    return True


async def delete_closed_polls_for_user(session: AsyncSession, creator_id: int) -> int:
    """Deletes all finished/closed polls for a given creator."""
    stmt = select(Poll.id).where(Poll.creator_id == creator_id, Poll.is_closed == True)  # noqa: E712
    res = await session.execute(stmt)
    poll_ids = list(res.scalars().all())
    if not poll_ids:
        return 0
    del_stmt = delete(Poll).where(Poll.id.in_(poll_ids))
    await session.execute(del_stmt)
    await session.commit()
    return len(poll_ids)


async def record_choice_vote(
    session: AsyncSession,
    poll_id: str,
    user_id: int,
    option_id: int,
    user_fullname: Optional[str] = None,
    username: Optional[str] = None,
    is_admin: bool = False,
) -> Tuple[bool, bool, str]:
    """
    Records or updates a choice vote with multiple-choice and voter restriction support.
    Returns: (success, is_changed, notification_message)
    """
    poll = await get_poll(session, poll_id)
    if not poll:
        return False, False, "❌ Опрос не найден."

    if poll.is_closed:
        return False, False, "🔒 Опрос уже завершён. Приём голосов закрыт."

    if poll.allowed_voters == "with_username" and not username:
        return False, False, "⛔ В этом опросе могут голосовать только участники с установленным @username в профиле Telegram."

    if poll.allowed_voters == "admins_only" and not is_admin and user_id != poll.creator_id:
        return False, False, "⛔ В этом опросе могут голосовать только администраторы."

    opt_stmt = select(Option).where(
        Option.id == option_id,
        Option.poll_id == poll_id,
    )
    opt_res = await session.execute(opt_stmt)
    option = opt_res.scalar_one_or_none()
    if not option:
        return False, False, "❌ Выбранный вариант не найден."

    now_utc = datetime.now(timezone.utc)

    if poll.is_multiple_choice:
        # Multiple choice toggle: check if user already voted for this option
        specific_vote_stmt = select(Vote).where(
            Vote.poll_id == poll_id,
            Vote.user_id == user_id,
            Vote.option_id == option_id,
        )
        res = await session.execute(specific_vote_stmt)
        this_vote = res.scalar_one_or_none()
        if this_vote:
            await session.delete(this_vote)
            await session.commit()
            return True, True, f"Вы сняли выбор с варианта «{option.text}». 👌"
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
            return True, True, f"Вы выбрали «{option.text}»! ✅\n(Множественный выбор: можете выбрать ещё)"

    # Single choice
    vote_stmt = select(Vote).where(
        Vote.poll_id == poll_id,
        Vote.user_id == user_id,
    )
    vote_res = await session.execute(vote_stmt)
    existing_vote = vote_res.scalar_one_or_none()

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


async def get_choice_poll_stats(
    session: AsyncSession,
    poll_id: str,
    include_voters: bool = False,
) -> dict:
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

    opt_voters = {}
    if include_voters:
        v_stmt = select(Vote).where(Vote.poll_id == poll_id).order_by(Vote.voted_at.asc())
        v_res = await session.execute(v_stmt)
        for v in v_res.scalars().all():
            v_name = f"@{v.username}" if v.username else (v.user_fullname or f"ID:{v.user_id}")
            opt_voters.setdefault(v.option_id, []).append(v_name)

    results = []
    for opt in options:
        count = counts_map.get(opt.id, 0)
        percentage = (count / total_votes * 100) if total_votes > 0 else 0.0
        opt_data = {
            "option_id": opt.id,
            "text": opt.text,
            "count": count,
            "percentage": round(percentage, 1),
        }
        if include_voters:
            opt_data["voters"] = opt_voters.get(opt.id, [])
        results.append(opt_data)

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
    is_admin: bool = False,
) -> Tuple[bool, bool, str]:
    """Saves or updates an open text answer."""
    poll = await get_poll(session, poll_id)
    if not poll:
        return False, False, "❌ Опрос не найден."

    if poll.is_closed:
        return False, False, "🔒 Опрос уже завершён. Приём ответов закрыт."

    if poll.allowed_voters == "with_username" and not username:
        return False, False, "⛔ В этом опросе могут участвовать только пользователи с установленным @username в профиле Telegram."

    if poll.allowed_voters == "admins_only" and not is_admin and user_id != poll.creator_id:
        return False, False, "⛔ В этом опросе могут участвовать только администраторы."

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


# ==============================================================================
# Poll Templates CRUD
# ==============================================================================

async def create_poll_template(
    session: AsyncSession,
    user_id: int,
    title: str,
    poll_type: str,
    question: str,
    options: Optional[List[str]] = None,
    is_multiple_choice: bool = False,
    is_anonymous: bool = True,
    allowed_voters: str = "all",
) -> PollTemplate:
    """Creates a reusable poll template."""
    opts_json = json.dumps(options, ensure_ascii=False) if options else None
    tpl = PollTemplate(
        user_id=user_id,
        title=title.strip(),
        poll_type=poll_type,
        question=question.strip(),
        options_json=opts_json,
        is_multiple_choice=is_multiple_choice,
        is_anonymous=is_anonymous,
        allowed_voters=allowed_voters,
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return tpl


async def get_user_templates(
    session: AsyncSession,
    user_id: int,
) -> List[PollTemplate]:
    """Retrieves all templates created by a user."""
    stmt = (
        select(PollTemplate)
        .where(PollTemplate.user_id == user_id)
        .order_by(PollTemplate.created_at.desc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_template(
    session: AsyncSession,
    template_id: int,
) -> Optional[PollTemplate]:
    """Retrieves a single template by ID."""
    return await session.get(PollTemplate, template_id)


async def delete_template(
    session: AsyncSession,
    template_id: int,
    user_id: int,
) -> bool:
    """Deletes a template if owned by user."""
    stmt = select(PollTemplate).where(
        PollTemplate.id == template_id,
        PollTemplate.user_id == user_id,
    )
    res = await session.execute(stmt)
    tpl = res.scalar_one_or_none()
    if not tpl:
        return False
    await session.delete(tpl)
    await session.commit()
    return True
