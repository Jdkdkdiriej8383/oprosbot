"""Handlers for /start, /help, /my_polls, and deep-link routing."""

import html
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from database.db_manager import (
    get_poll,
    get_user_polls,
    get_user_text_answer,
    upsert_user,
)
from keyboards.inline import (
    get_creator_manage_keyboard,
    get_text_response_action_keyboard,
)
from states.poll_states import TextResponseState

router = Router(name="start_router")


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep_link(
    message: Message,
    command: CommandStart,
    session_factory: async_sessionmaker[AsyncSession],
    state: FSMContext,
) -> None:
    """Handles deep-linking like /start poll_<id>."""
    if not message.from_user:
        return

    async with session_factory() as session:
        await upsert_user(
            session=session,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )

    arg = command.args or ""
    if not arg.startswith("poll_"):
        await cmd_start_default(message, session_factory, state)
        return

    poll_id = arg[len("poll_"):]

    async with session_factory() as session:
        poll = await get_poll(session, poll_id)

    if not poll:
        await message.answer("❌ Запрашиваемый опрос не найден или был удалён.")
        return

    if poll.is_closed:
        await message.answer(
            f"🔒 <b>Опрос завершён</b>\n\n"
            f"❓ Вопрос: «{poll.question}»\n\n"
            f"Приём ответов и голосов по данному опросу закрыт.",
            parse_mode="HTML",
        )
        return

    if poll.poll_type == "text":
        async with session_factory() as session:
            existing = await get_user_text_answer(
                session=session,
                poll_id=poll_id,
                user_id=message.from_user.id,
            )

        if existing:
            dt_str = existing.submitted_at.strftime("%d.%m.%Y %H:%M") if existing.submitted_at else ""
            text = (
                f"📝 <b>Опрос с открытым ответом</b>\n\n"
                f"❓ <b>Вопрос:</b>\n«{poll.question}»\n\n"
                f"ℹ️ <b>Вы уже давали ответ ({dt_str}):</b>\n"
                f"«<i>{existing.answer_text}</i>»\n\n"
                f"Вы можете отправить новый ответ (он заменит предыдущий) или оставить всё как есть:"
            )
            await message.answer(
                text=text,
                reply_markup=get_text_response_action_keyboard(poll_id),
                parse_mode="HTML",
            )
            return

        await state.clear()
        await state.set_state(TextResponseState.waiting_for_text_answer)
        await state.update_data(poll_id=poll_id, question=poll.question)

        prompt = (
            f"📝 <b>Опрос с открытым ответом</b>\n\n"
            f"❓ <b>Вопрос:</b>\n«{poll.question}»\n\n"
            f"✍️ Напишите ваш развёрнутый ответ ответным сообщением ниже.\n"
            f"<i>Вы можете отправить текст до 3000 символов.</i>"
        )
        await message.answer(prompt, parse_mode="HTML")
    else:
        bot_user = await message.bot.get_me()
        await message.answer(
            f"📊 <b>Опрос:</b>\n«{poll.question}»\n\n"
            f"Голосование по этому опросу проводится прямо в чатах/темах через интерактивные кнопки под сообщением.\n\n"
            f"Если вы создатель этого опроса, вы можете открыть панель управления ниже:",
            reply_markup=get_creator_manage_keyboard(
                poll_id=poll.id,
                bot_username=bot_user.username or "",
                poll_type=poll.poll_type,
                is_closed=poll.is_closed,
            ) if poll.creator_id == message.from_user.id else None,
            parse_mode="HTML",
        )


@router.message(CommandStart())
async def cmd_start_default(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    state: FSMContext,
) -> None:
    """Standard /start greeting."""
    if not message.from_user:
        return

    await state.clear()

    async with session_factory() as session:
        await upsert_user(
            session=session,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )

    first_name = html.escape(message.from_user.first_name or "друг")
    greeting = (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        f"Я — бот для проведения интерактивных опросов со <b>скрытыми результатами</b>, "
        f"поддержкой <b>форумных тем (Topics / Ветки)</b> и <b>открытых развёрнутых ответов</b>.\n\n"
        f"✨ <b>Возможности:</b>\n"
        f"• 📊 <b>Опросы с вариантами</b> — скрытые результаты во время голосования, "
        f"участник может сменить голос, в конце публикуется диаграмма (прогресс-бар)!\n"
        f"• 💬 <b>Поддержка тем форума</b> — бот публикует и завершает опрос в нужной ветке "
        f"супергруппы по её <code>message_thread_id</code>.\n"
        f"• ✍️ <b>Опросы со свободным текстом</b> — сбор мнений в ЛС бота с экспортом в Excel (CSV) / TXT.\n"
        f"• ⏱ <b>Таймер дедлайна</b> — автоматическое завершение опроса через APScheduler.\n"
        f"• 🔒 <b>Защита</b> — 1 человек = 1 голос, управление только у создателя или админа чата.\n\n"
        f"Выберите действие ниже:"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Создать новый опрос",
                    callback_data="btn_create_poll",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Мои опросы",
                    callback_data="btn_my_polls",
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Помощь и справка",
                    callback_data="btn_help",
                )
            ],
        ]
    )

    await message.answer(greeting, reply_markup=kb, parse_mode="HTML")


@router.message(Command("help"))
@router.callback_query(F.data == "btn_help")
async def cmd_help(event: Message | None = None, **kwargs) -> None:
    """Displays user manual."""
    help_text = (
        "📖 <b>Инструкция по работе с ботом:</b>\n\n"
        "1️⃣ <b>Создание опроса:</b>\n"
        "• Отправьте команду /create_poll (в ЛС или прямо в ветке форума).\n"
        "• Если команда отправлена внутри темы форума, бот автоматически привяжет её ID!\n"
        "• Выберите тип (с вариантами или открытый текст), задайте вопрос и варианты.\n"
        "• Настройте дедлайн завершения.\n"
        "• Выберите место публикации (текущий чат, группа/ветка форума по ID, или Inline).\n\n"
        "2️⃣ <b>Голосование:</b>\n"
        "• Результаты скрыты до завершения опроса. При клике участник получает личный alert.\n"
        "• До закрытия опроса выбор можно изменить.\n\n"
        "3️⃣ <b>Подведение итогов:</b>\n"
        "• Итоги подводятся автоматически по дедлайну или досрочно создателем / админом.\n"
        "• Исходный пост отключает кнопки, а в ту же ветку форума публикуется итоговая диаграмма!\n"
        "• По текстовым опросам автор получает файлы CSV (Excel) и TXT.\n\n"
        "<b>Команды:</b>\n"
        "/create_poll — создать новый опрос\n"
        "/my_polls — список созданных опросов\n"
        "/help — данная справка"
    )

    if isinstance(event, Message):
        await event.answer(help_text, parse_mode="HTML")
    elif hasattr(event, "message") and event.message:
        await event.message.answer(help_text, parse_mode="HTML")
        await event.answer()


@router.message(Command("my_polls"))
@router.callback_query(F.data == "btn_my_polls")
async def cmd_my_polls(
    event: Message | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    **kwargs,
) -> None:
    """Lists creator's polls."""
    from_user = event.from_user
    if not from_user or not session_factory:
        return

    async with session_factory() as session:
        polls = await get_user_polls(session, creator_id=from_user.id, limit=10)

    if not polls:
        msg = "ℹ️ У вас пока нет созданных опросов.\nНажмите /create_poll чтобы создать первый!"
        if isinstance(event, Message):
            await event.answer(msg)
        elif hasattr(event, "message") and event.message:
            await event.message.answer(msg)
            await event.answer()
        return

    lines = ["📋 <b>Ваши опросы:</b>\n"]
    buttons = []

    for p in polls:
        status_icon = "🔒" if p.is_closed else "🟢"
        type_icon = "📊" if p.poll_type == "choice" else "✍️"
        short_q = p.question[:35] + ("..." if len(p.question) > 35 else "")
        lines.append(f"{status_icon} {type_icon} <b>«{short_q}»</b> (ID: <code>{p.id}</code>)")

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{type_icon} «{short_q}»",
                    callback_data=f"manage:{p.id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Создать ещё опрос",
                callback_data="btn_create_poll",
            )
        ]
    )

    text = "\n".join(lines) + "\n\n<i>Нажмите на опрос ниже для управления:</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif hasattr(event, "message") and event.message:
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
