"""Tests for new features:
1. Exact datetime parsing
2. Poll editing & deletion
3. Multiple-choice voting (toggle on/off)
4. Voter restrictions (with_username, admins_only)
5. Privacy (Anonymous vs Public voting)
6. Pre-closing reminder scheduling
7. Reusable poll templates
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.models import Base, Poll, Option, User, Vote
from database.db_manager import (
    add_poll_option,
    close_poll,
    create_poll,
    create_poll_template,
    delete_closed_polls_for_user,
    delete_poll,
    delete_template,
    get_choice_poll_stats,
    get_poll,
    get_template,
    get_user_polls,
    get_user_templates,
    record_choice_vote,
    update_poll_deadline,
    update_poll_question,
    update_poll_result_destination,
    upsert_user,
)
from services.poll_service import (
    format_choice_results,
    generate_choice_votes_csv,
    generate_text_answers_csv,
)
from services.scheduler import (
    cancel_poll_closing,
    get_scheduler,
    schedule_poll_closing,
)
from utils.datetime_parser import parse_exact_datetime


def test_parse_exact_datetime():
    future_year = datetime.now().year + 1
    dt_str = f"15.08.{future_year} 14:30"
    target, formatted = parse_exact_datetime(dt_str, tz_name="UTC")
    assert target is not None
    assert target.year == future_year
    assert target.month == 8
    assert target.day == 15
    assert target.hour == 14
    assert target.minute == 30
    assert "15.08." in formatted

    target_tom, _ = parse_exact_datetime("завтра 18:00", tz_name="UTC")
    assert target_tom is not None
    assert target_tom.hour == 18

    target_past, err = parse_exact_datetime("01.01.2020 12:00", tz_name="UTC")
    assert target_past is None
    assert "прошло" in err.lower()


@pytest.mark.asyncio
async def test_poll_editing_and_deletion():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        await upsert_user(session, user_id=1001, username="author", full_name="Author")
        poll = await create_poll(
            session=session,
            poll_id="poll_edit_test",
            creator_id=1001,
            creator_name="Author",
            poll_type="choice",
            question="Initial Question?",
            options=["Option 1", "Option 2"],
            target_chat_id=-100111,
            target_thread_id=5,
        )
        assert poll.question == "Initial Question?"
        assert len(poll.options) == 2
        assert poll.result_chat_id is None

        # 1. Update Question
        updated = await update_poll_question(session, "poll_edit_test", "Modified Question?")
        assert updated is not None
        assert updated.question == "Modified Question?"

        # 2. Add Option
        new_opt = await add_poll_option(session, "poll_edit_test", "Option 3")
        assert new_opt is not None
        assert new_opt.text == "Option 3"
        poll_reloaded = await get_poll(session, "poll_edit_test")
        assert len(poll_reloaded.options) == 3

        # 3. Update Result Destination
        await update_poll_result_destination(session, "poll_edit_test", result_chat_id=-100999, result_thread_id=42)
        poll_dest = await get_poll(session, "poll_edit_test")
        assert poll_dest.result_chat_id == -100999
        assert poll_dest.result_thread_id == 42

        # 4. Update Deadline
        new_dl = datetime.now(timezone.utc) + timedelta(hours=2)
        await update_poll_deadline(session, "poll_edit_test", new_dl)
        poll_dl = await get_poll(session, "poll_edit_test")
        assert poll_dl.deadline_at is not None

        # 5. Delete closed polls
        poll2 = await create_poll(
            session=session,
            poll_id="poll_closed_test",
            creator_id=1001,
            creator_name="Author",
            poll_type="text",
            question="Closed Poll Question?",
        )
        await close_poll(session, "poll_closed_test", user_id=1001, is_admin=True)

        user_polls = await get_user_polls(session, 1001)
        assert len(user_polls) == 2

        deleted_closed_count = await delete_closed_polls_for_user(session, 1001)
        assert deleted_closed_count == 1

        user_polls_after = await get_user_polls(session, 1001)
        assert len(user_polls_after) == 1
        assert user_polls_after[0].id == "poll_edit_test"

        # 6. Delete single poll
        del_res = await delete_poll(session, "poll_edit_test")
        assert del_res is True

        user_polls_empty = await get_user_polls(session, 1001)
        assert len(user_polls_empty) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_multiple_choice_voting():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        await upsert_user(session, user_id=2001, username="voter1", full_name="Voter One")
        poll = await create_poll(
            session=session,
            poll_id="poll_mult_test",
            creator_id=2001,
            creator_name="Voter One",
            poll_type="choice",
            question="Multiple Choice Test?",
            options=["A", "B", "C"],
            is_multiple_choice=True,
        )
        opt_a = poll.options[0].id
        opt_b = poll.options[1].id

        # Vote for A
        ok, changed, msg = await record_choice_vote(session, "poll_mult_test", 2001, opt_a, "Voter One", "voter1")
        assert ok is True
        assert "выбрали «A»" in msg

        # Vote for B as well (multiple choice)
        ok, changed, msg = await record_choice_vote(session, "poll_mult_test", 2001, opt_b, "Voter One", "voter1")
        assert ok is True
        assert "выбрали «B»" in msg

        stats = await get_choice_poll_stats(session, "poll_mult_test")
        assert stats["total_votes"] == 2

        # Vote for A again -> toggles off A
        ok, changed, msg = await record_choice_vote(session, "poll_mult_test", 2001, opt_a, "Voter One", "voter1")
        assert ok is True
        assert "сняли выбор с варианта «A»" in msg

        stats_after = await get_choice_poll_stats(session, "poll_mult_test")
        assert stats_after["total_votes"] == 1
        assert stats_after["options"][0]["count"] == 0
        assert stats_after["options"][1]["count"] == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_voter_restrictions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        # 1. with_username restriction
        poll_u = await create_poll(
            session=session,
            poll_id="poll_user_req",
            creator_id=3001,
            creator_name="Creator",
            poll_type="choice",
            question="Username Req?",
            options=["Yes", "No"],
            allowed_voters="with_username",
        )
        opt_id = poll_u.options[0].id

        # User without username
        ok, _, msg = await record_choice_vote(session, "poll_user_req", 3002, opt_id, "NoUser", username=None)
        assert ok is False
        assert "@username" in msg

        # User with username
        ok, _, msg = await record_choice_vote(session, "poll_user_req", 3003, opt_id, "WithUser", username="alice")
        assert ok is True

        # 2. admins_only restriction
        poll_adm = await create_poll(
            session=session,
            poll_id="poll_adm_req",
            creator_id=3001,
            creator_name="Creator",
            poll_type="choice",
            question="Admins Only?",
            options=["Yes", "No"],
            allowed_voters="admins_only",
        )
        opt_id2 = poll_adm.options[0].id

        # Regular user (not admin, not creator)
        ok, _, msg = await record_choice_vote(session, "poll_adm_req", 3004, opt_id2, "User", username="bob", is_admin=False)
        assert ok is False
        assert "администраторы" in msg

        # Admin user
        ok, _, msg = await record_choice_vote(session, "poll_adm_req", 3005, opt_id2, "Admin", username="admin_user", is_admin=True)
        assert ok is True

        # Creator is always allowed
        ok, _, msg = await record_choice_vote(session, "poll_adm_req", 3001, opt_id2, "Creator", username="creator", is_admin=False)
        assert ok is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_anonymous_vs_public_results():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        # 1. Public Poll
        poll_pub = await create_poll(
            session=session,
            poll_id="poll_public",
            creator_id=4001,
            creator_name="Creator",
            poll_type="choice",
            question="Public Question?",
            options=["Option A", "Option B"],
            is_anonymous=False,
        )
        opt_a = poll_pub.options[0].id
        await record_choice_vote(session, "poll_public", 4002, opt_a, "Alice Smith", "alice")

        stats_pub = await get_choice_poll_stats(session, "poll_public", include_voters=True)
        text_pub = format_choice_results("Public Question?", stats_pub, is_anonymous=False)
        assert "@alice" in text_pub
        assert "Открытый" in text_pub

        # CSV for public poll includes names
        votes = [Vote(id=1, poll_id="poll_public", user_id=4002, option_id=opt_a, username="alice", user_fullname="Alice Smith")]
        csv_pub = generate_choice_votes_csv("Public Question?", votes, {opt_a: "Option A"}, is_anonymous=False)
        assert b"@alice" in csv_pub
        assert b"Alice Smith" in csv_pub

        # CSV for anonymous poll masks names
        csv_anon = generate_choice_votes_csv("Public Question?", votes, {opt_a: "Option A"}, is_anonymous=True)
        assert b"@alice" not in csv_anon
        assert b"\xd0\xa3\xd1\x87\xd0\xb0\xd1\x81\xd1\x82\xd0\xbd\xd0\xb8\xd0\xba #1" in csv_anon  # "Участник #1" in UTF-8

    await engine.dispose()


@pytest.mark.asyncio
async def test_poll_templates():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        # Create template
        tpl = await create_poll_template(
            session=session,
            user_id=5001,
            title="Weekly Standup Poll",
            poll_type="choice",
            question="What is your progress today?",
            options=["Done", "In progress", "Blocked"],
            is_multiple_choice=True,
            is_anonymous=False,
            allowed_voters="all",
        )
        assert tpl.id is not None
        assert tpl.title == "Weekly Standup Poll"
        assert len(tpl.options) == 3

        # List user templates
        templates = await get_user_templates(session, 5001)
        assert len(templates) == 1
        assert templates[0].question == "What is your progress today?"

        # Get single template
        t_get = await get_template(session, tpl.id)
        assert t_get is not None
        assert t_get.is_multiple_choice is True

        # Delete template
        del_ok = await delete_template(session, tpl.id, user_id=5001)
        assert del_ok is True

        templates_after = await get_user_templates(session, 5001)
        assert len(templates_after) == 0

    await engine.dispose()


def test_scheduler_pre_closing_reminder():
    scheduler = get_scheduler()
    bot_mock = MagicMock()
    session_factory_mock = MagicMock()

    future_dl = datetime.now(timezone.utc) + timedelta(hours=2)
    poll_id = "test_rem_poll"

    schedule_poll_closing(
        poll_id=poll_id,
        deadline_at=future_dl,
        bot=bot_mock,
        session_factory=session_factory_mock,
        reminder_minutes=30,
    )

    close_job = scheduler.get_job(f"close_poll_{poll_id}")
    rem_job = scheduler.get_job(f"reminder_{poll_id}")
    assert close_job is not None
    assert rem_job is not None

    # Cancel schedule removes both jobs
    cancel_poll_closing(poll_id)
    assert scheduler.get_job(f"close_poll_{poll_id}") is None
    assert scheduler.get_job(f"reminder_{poll_id}") is None
