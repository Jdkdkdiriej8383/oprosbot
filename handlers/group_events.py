"""Handlers for group join events, topic creation, and notifying the user who added the bot."""

import logging
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters.chat_member_updated import (
    ADMINISTRATOR,
    IS_NOT_MEMBER,
    JOIN_TRANSITION,
    KICKED,
    LEAVE_TRANSITION,
    LEFT,
    MEMBER,
    RESTRICTED,
    ChatMemberUpdatedFilter,
)
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from database.db_manager import (
    deactivate_group_chat,
    upsert_forum_topic,
    upsert_group_chat,
    upsert_user,
)

logger = logging.getLogger(__name__)

router = Router(name="group_events_router")


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_bot_added_to_chat(
    event: ChatMemberUpdated,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    Triggered when the bot is added to a group/supergroup or promoted to administrator.
    Registers the group and immediately sends a direct PM notification to the user who added the bot!
    """
    chat = event.chat
    adder = event.from_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    is_forum = getattr(chat, "is_forum", False) or False
    logger.info(
        "Bot added to chat '%s' (ID: %s, is_forum: %s) by user %s (@%s)",
        chat.title,
        chat.id,
        is_forum,
        adder.id,
        adder.username,
    )

    async with session_factory() as session:
        # Register user
        await upsert_user(
            session=session,
            user_id=adder.id,
            username=adder.username,
            full_name=adder.full_name,
        )

        # Register group
        await upsert_group_chat(
            session=session,
            chat_id=chat.id,
            title=chat.title or "Группа",
            username=chat.username,
            chat_type=chat.type,
            is_forum=is_forum,
            added_by_user_id=adder.id,
        )

    # Send personal notification to the user who added the bot
    forum_status = "Включены темы форума (Topics) ✅" if is_forum else "Обычная супергруппа"

    pm_text = (
        f"🎉 <b>Вы успешно добавили меня в группу «{chat.title}»!</b>\n\n"
        f"🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
        f"💬 <b>Тип:</b> {forum_status}\n\n"
        f"Теперь при создании опросов через команду /create_poll вы можете <b>в один клик выбрать "
        f"эту группу и её темы форума</b> из списка кнопок!\n\n"
        f"💡 <i>Убедитесь, что у меня есть право на публикацию сообщений в группе.</i>"
    )

    try:
        await event.bot.send_message(
            chat_id=adder.id,
            text=pm_text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug("Could not send PM to adder %s: %s", adder.id, e)
        # If user hasn't opened bot PM yet, send friendly greeting in the group
        try:
            await event.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"👋 <b>Спасибо за добавление бота в «{chat.title}»!</b>\n\n"
                    f"Чтобы создавать и публиковать опросы в эту группу и её темы форума, "
                    f"откройте диалог с ботом и нажмите /start."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_bot_removed_from_chat(
    event: ChatMemberUpdated,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Marks group chat as inactive when bot is removed."""
    logger.info("Bot removed from chat '%s' (ID: %s)", event.chat.title, event.chat.id)
    async with session_factory() as session:
        await deactivate_group_chat(session=session, chat_id=event.chat.id)


@router.message(F.forum_topic_created)
async def on_forum_topic_created(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Automatically registers newly created forum topics in the chat."""
    if not message.forum_topic_created or not message.message_thread_id:
        return

    topic_name = message.forum_topic_created.name
    icon_emoji = message.forum_topic_created.icon_custom_emoji_id

    async with session_factory() as session:
        await upsert_forum_topic(
            session=session,
            chat_id=message.chat.id,
            thread_id=message.message_thread_id,
            title=topic_name,
            icon_emoji=icon_emoji,
        )
    logger.info("Registered new forum topic: '%s' (thread_id: %s) in chat %s", topic_name, message.message_thread_id, message.chat.id)


@router.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.message_thread_id)
async def auto_discover_topic(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Auto-discovers active topics when messages are sent in group threads."""
    if not message.message_thread_id:
        return

    thread_id = message.message_thread_id
    chat = message.chat

    async with session_factory() as session:
        # Ensure chat is recorded
        await upsert_group_chat(
            session=session,
            chat_id=chat.id,
            title=chat.title or "Группа",
            username=chat.username,
            chat_type=chat.type,
            is_forum=getattr(chat, "is_forum", True),
        )

        # Register topic if not exists
        await upsert_forum_topic(
            session=session,
            chat_id=chat.id,
            thread_id=thread_id,
            title=f"Тема #{thread_id}",
        )
