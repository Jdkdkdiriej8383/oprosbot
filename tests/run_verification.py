"""Verification script testing database, CRUD, groups, topics, and services."""

import asyncio
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.models import Base
from database.db_manager import (
    close_poll,
    create_poll,
    get_all_text_answers,
    get_chat_topics,
    get_choice_poll_stats,
    get_group_chat,
    get_poll,
    get_poll_posts,
    get_user_group_chats,
    get_user_text_answer,
    get_user_vote,
    record_choice_vote,
    save_text_answer,
    track_poll_post,
    upsert_forum_topic,
    upsert_group_chat,
    upsert_user,
)
from keyboards.inline import (
    get_choice_voting_keyboard,
    get_creator_manage_keyboard,
    get_deadline_keyboard,
    get_groups_selection_keyboard,
    get_options_input_keyboard,
    get_poll_type_keyboard,
    get_target_destination_keyboard,
    get_text_poll_chat_keyboard,
    get_topics_selection_keyboard,
)
from services.poll_service import (
    format_choice_results,
    format_text_poll_summary,
    generate_choice_votes_csv,
    generate_text_answers_csv,
    generate_text_answers_txt,
    pluralize_votes,
    render_progress_bar,
)


async def main():
    print("=== Starting OprosBot Topic & Core Verification ===")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 1. User
        print("[1/7] Testing User upsert...")
        u = await upsert_user(session, user_id=1, username="author", full_name="Author User")
        assert u.id == 1
        print("  -> User OK.")

        # 2. Group registration & Topics
        print("[2/7] Testing Group registration and Topic auto-discovery...")
        grp = await upsert_group_chat(
            session=session,
            chat_id=-100999888,
            title="Dev Forum",
            is_forum=True,
            added_by_user_id=1,
        )
        assert grp.is_forum is True
        await upsert_forum_topic(session, chat_id=-100999888, thread_id=15, title="General Discussions")
        topics = await get_chat_topics(session, chat_id=-100999888)
        assert len(topics) == 1
        assert topics[0].title == "General Discussions"

        user_grps = await get_user_group_chats(session, user_id=1)
        assert len(user_grps) == 1
        print("  -> Group and Topic Registration OK.")

        # 3. Poll with Forum Topic
        print("[3/7] Testing Poll creation with Forum Topic (message_thread_id)...")
        poll = await create_poll(
            session=session,
            poll_id="topic_poll_1",
            creator_id=1,
            creator_name="Author User",
            poll_type="choice",
            question="Favorite framework?",
            options=["aiogram", "FastAPI", "Django"],
            target_chat_id=-100999888,
            target_thread_id=15,
        )
        assert poll.target_thread_id == 15
        assert poll.target_chat_id == -100999888

        await track_poll_post(
            session=session,
            poll_id="topic_poll_1",
            chat_id=-100999888,
            message_thread_id=15,
            message_id=777,
        )
        posts = await get_poll_posts(session, "topic_poll_1")
        assert len(posts) == 1
        assert posts[0].message_thread_id == 15
        print("  -> Forum Topic routing OK.")

        # 4. Voting & Vote switching
        print("[4/7] Testing Voting & Vote Changing...")
        ok, chg, msg = await record_choice_vote(
            session, poll_id="topic_poll_1", user_id=10, option_id=poll.options[0].id, user_fullname="Voter"
        )
        assert ok and not chg
        ok, chg, msg = await record_choice_vote(
            session, poll_id="topic_poll_1", user_id=10, option_id=poll.options[1].id
        )
        assert ok and chg
        print("  -> Voting & Changing OK.")

        # 5. Text poll & answers
        print("[5/7] Testing Text Poll & Answers...")
        text_poll = await create_poll(
            session=session,
            poll_id="text_1",
            creator_id=1,
            creator_name="Author User",
            poll_type="text",
            question="Suggestions?",
        )
        ok, upd, msg = await save_text_answer(
            session, poll_id="text_1", user_id=20, text="First idea", user_fullname="Ideator"
        )
        assert ok and not upd
        ok, upd, msg = await save_text_answer(
            session, poll_id="text_1", user_id=20, text="Updated better idea"
        )
        assert ok and upd
        answers = await get_all_text_answers(session, "text_1")
        assert len(answers) == 1
        assert answers[0].user_fullname == "Ideator"
        print("  -> Text Poll OK.")

        # 6. Admin & Creator rights
        print("[6/7] Testing Admin & Creator Permissions...")
        ok, p, msg = await close_poll(session, "topic_poll_1", user_id=999, is_admin=False)
        assert not ok
        ok, p, msg = await close_poll(session, "topic_poll_1", user_id=888, is_admin=True)
        assert ok and p.is_closed
        print("  -> Permissions OK.")

        # 7. Exporters
        print("[7/7] Testing Exporters...")
        csv_bytes = generate_text_answers_csv(text_poll.question, answers)
        assert csv_bytes.startswith(b"\xef\xbb\xbf")
        txt_bytes = generate_text_answers_txt(text_poll.question, answers)
        assert "Updated better idea" in txt_bytes.decode("utf-8")
        print("  -> Exporters OK.")

    await engine.dispose()
    print("=== ALL VERIFICATION CHECKS PASSED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(main())
