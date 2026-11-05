"""Comprehensive automated test suite for oprosbot with Topic and permissions support."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.models import Base
from database.db_manager import (
    close_poll,
    create_poll,
    get_all_text_answers,
    get_choice_poll_stats,
    get_poll,
    get_poll_posts,
    get_user_polls,
    get_user_text_answer,
    get_user_vote,
    record_choice_vote,
    save_text_answer,
    track_poll_post,
    upsert_user,
)
from keyboards.inline import (
    get_choice_voting_keyboard,
    get_close_confirmation_keyboard,
    get_creator_manage_keyboard,
    get_deadline_keyboard,
    get_options_input_keyboard,
    get_poll_type_keyboard,
    get_target_destination_keyboard,
    get_text_poll_chat_keyboard,
    get_text_response_action_keyboard,
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


@pytest.fixture
async def async_session() -> AsyncSession:
    """Creates an in-memory SQLite database session."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_user_upsert(async_session: AsyncSession):
    """Verifies user registration and profile updates."""
    user = await upsert_user(
        async_session,
        user_id=12345,
        username="alice",
        full_name="Alice Smith",
    )
    assert user.id == 12345
    assert user.username == "alice"
    assert user.full_name == "Alice Smith"

    updated = await upsert_user(
        async_session,
        user_id=12345,
        username="alice_new",
        full_name="Alice Wonderland",
    )
    assert updated.id == 12345
    assert updated.username == "alice_new"
    assert updated.full_name == "Alice Wonderland"


@pytest.mark.asyncio
async def test_choice_poll_with_topic_tracking(async_session: AsyncSession):
    """Verifies choice poll creation with target topic and vote mechanics."""
    creator = await upsert_user(async_session, user_id=100, full_name="Creator")

    # Poll targeting supergroup -1001234567890 and forum topic 42
    poll = await create_poll(
        session=async_session,
        poll_id="poll_topic_1",
        creator_id=creator.id,
        creator_name="Creator",
        poll_type="choice",
        question="Which programming language is fastest for services?",
        options=["Rust", "C++", "Go"],
        target_chat_id=-1001234567890,
        target_thread_id=42,
    )

    assert poll.id == "poll_topic_1"
    assert poll.target_chat_id == -1001234567890
    assert poll.target_thread_id == 42
    assert len(poll.options) == 3

    opt_rust = poll.options[0]
    opt_cpp = poll.options[1]
    opt_go = poll.options[2]

    # Track message post in forum topic
    await track_poll_post(
        session=async_session,
        poll_id="poll_topic_1",
        chat_id=-1001234567890,
        message_thread_id=42,
        message_id=999,
    )

    posts = await get_poll_posts(async_session, "poll_topic_1")
    assert len(posts) == 1
    assert posts[0].message_thread_id == 42
    assert posts[0].chat_id == -1001234567890

    # User 201 votes for Rust
    success, is_changed, msg = await record_choice_vote(
        session=async_session,
        poll_id="poll_topic_1",
        user_id=201,
        option_id=opt_rust.id,
        user_fullname="Alice",
        username="alice",
    )
    assert success is True
    assert is_changed is False
    assert "учтён" in msg

    # Same vote clicked again
    success, is_changed, msg = await record_choice_vote(
        session=async_session,
        poll_id="poll_topic_1",
        user_id=201,
        option_id=opt_rust.id,
    )
    assert success is True
    assert is_changed is False
    assert "уже отдали свой голос" in msg

    # Change vote to Go
    success, is_changed, msg = await record_choice_vote(
        session=async_session,
        poll_id="poll_topic_1",
        user_id=201,
        option_id=opt_go.id,
    )
    assert success is True
    assert is_changed is True
    assert "изменён" in msg

    # User 202 votes for Rust
    await record_choice_vote(
        session=async_session,
        poll_id="poll_topic_1",
        user_id=202,
        option_id=opt_rust.id,
    )

    stats = await get_choice_poll_stats(async_session, "poll_topic_1")
    assert stats["total_votes"] == 2
    opt_stats = {o["text"]: o for o in stats["options"]}
    assert opt_stats["Rust"]["count"] == 1
    assert opt_stats["Go"]["count"] == 1
    assert opt_stats["C++"]["count"] == 0

    formatted = format_choice_results(poll.question, stats, is_closed=False)
    assert "50.0%" in formatted
    assert "Всего голосов:</b> 2" in formatted


@pytest.mark.asyncio
async def test_text_poll_workflow(async_session: AsyncSession):
    """Verifies open text poll with response submission and updates."""
    creator = await upsert_user(async_session, user_id=300, full_name="Surveyor")

    poll = await create_poll(
        session=async_session,
        poll_id="poll_text_1",
        creator_id=creator.id,
        creator_name="Surveyor",
        poll_type="text",
        question="What features would you like to see?",
    )
    assert poll.poll_type == "text"

    # User submits answer
    success, is_upd, msg = await save_text_answer(
        session=async_session,
        poll_id="poll_text_1",
        user_id=401,
        text="Dark mode and graph visualizations.",
        user_fullname="Charlie",
        username="charlie401",
    )
    assert success is True
    assert is_upd is False

    ans = await get_user_text_answer(async_session, "poll_text_1", 401)
    assert ans is not None
    assert ans.answer_text == "Dark mode and graph visualizations."

    # User updates answer
    success, is_upd, msg = await save_text_answer(
        session=async_session,
        poll_id="poll_text_1",
        user_id=401,
        text="Updated: Dark mode, graphs, and CSV exports.",
    )
    assert success is True
    assert is_upd is True

    all_answers = await get_all_text_answers(async_session, "poll_text_1")
    assert len(all_answers) == 1
    assert "Updated" in all_answers[0].answer_text
    assert all_answers[0].user_fullname == "Charlie"  # Preserved!

    # Test CSV export
    csv_bytes = generate_text_answers_csv(poll.question, all_answers)
    assert csv_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    csv_str = csv_bytes.decode("utf-8-sig")
    assert "charlie401" in csv_str
    assert "Updated" in csv_str

    # Test TXT export
    txt_bytes = generate_text_answers_txt(poll.question, all_answers)
    assert "Charlie" in txt_bytes.decode("utf-8")


@pytest.mark.asyncio
async def test_admin_and_creator_permissions(async_session: AsyncSession):
    """Verifies that creator or admin can close poll, stranger cannot."""
    creator = await upsert_user(async_session, user_id=500, full_name="Author")
    poll = await create_poll(
        session=async_session,
        poll_id="poll_perm_test",
        creator_id=creator.id,
        creator_name="Author",
        poll_type="choice",
        question="Is architecture clean?",
        options=["Yes", "Absolutely"],
    )
    opt_id = poll.options[0].id

    # Stranger (id=999) cannot close
    success, p, msg = await close_poll(async_session, "poll_perm_test", user_id=999, is_admin=False)
    assert success is False
    assert "нет прав" in msg

    # Chat admin (id=888 with is_admin=True) can close
    success, p, msg = await close_poll(async_session, "poll_perm_test", user_id=888, is_admin=True)
    assert success is True
    assert p.is_closed is True

    # Vote after closure rejected
    success, chg, msg = await record_choice_vote(
        session=async_session,
        poll_id="poll_perm_test",
        user_id=777,
        option_id=opt_id,
    )
    assert success is False
    assert "закрыт" in msg or "завершён" in msg


def test_ui_and_keyboards():
    """Verifies keyboard builders including destination keyboard."""
    assert render_progress_bar(50, length=8) == "🟩🟩🟩🟩⬜⬜⬜⬜"
    assert pluralize_votes(1) == "голос"
    assert pluralize_votes(3) == "голоса"
    assert pluralize_votes(5) == "голосов"

    kb_dest = get_target_destination_keyboard()
    assert any("тему форума" in btn.text for row in kb_dest.inline_keyboard for btn in row)
    kb_type = get_poll_type_keyboard()
    assert len(kb_type.inline_keyboard) == 3
