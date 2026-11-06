"""Service providing poll calculations, bar chart formatting, exports, and permissions."""

import csv
from datetime import datetime
import io
import logging
from typing import Any, Dict, List, Optional

from aiogram import Bot
from database.models import Poll, TextAnswer, Vote

logger = logging.getLogger(__name__)


def render_progress_bar(percentage: float, length: int = 8) -> str:
    """Renders visual emoji progress bar: e.g. 60% -> 🟩🟩🟩🟩🟩⬜⬜⬜"""
    if percentage <= 0:
        return "⬜" * length
    if percentage >= 100:
        return "🟩" * length

    filled = round((percentage / 100) * length)
    if filled == 0 and percentage > 0:
        filled = 1
    if filled == length and percentage < 100:
        filled = length - 1

    empty = length - filled
    return ("🟩" * filled) + ("⬜" * empty)


def pluralize_votes(n: int) -> str:
    """Russian plural forms for votes."""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 10 < n_abs < 20:
        return "голосов"
    if 1 < n1 < 5:
        return "голоса"
    if n1 == 1:
        return "голос"
    return "голосов"


def format_choice_results(
    question: str,
    stats: Dict[str, Any],
    is_closed: bool = True,
    closed_at: datetime | None = None,
    deadline_at: datetime | None = None,
    is_anonymous: bool = True,
    is_multiple_choice: bool = False,
) -> str:
    """Formats final or intermediate results for choice polls."""
    status_emoji = "🏁" if is_closed else "📊"
    status_header = "Результаты опроса (Опрос завершён)" if is_closed else "Текущие результаты опроса"

    badges = []
    if is_multiple_choice:
        badges.append("☑️ Несколько ответов")
    if not is_anonymous:
        badges.append("👤 Открытый")
    else:
        badges.append("🔒 Анонимный")

    badges_str = f" <i>({', '.join(badges)})</i>" if badges else ""

    lines = [
        f"{status_emoji} <b>{status_header}</b>{badges_str}\n",
        f"❓ <b>Вопрос:</b>\n«{question}»\n",
        "────────────────────",
    ]

    total_votes = stats.get("total_votes", 0)
    options: List[Dict[str, Any]] = stats.get("options", [])

    if not options:
        lines.append("<i>Варианты ответа отсутствуют</i>")
    else:
        for opt in options:
            pct = opt.get("percentage", 0.0)
            count = opt.get("count", 0)
            text = opt.get("text", "")
            bar = render_progress_bar(pct, length=8)
            vote_word = pluralize_votes(count)

            lines.append(
                f"<b>{text}</b>\n"
                f"{bar} <b>{pct:.1f}%</b> ({count} {vote_word})"
            )
            voters = opt.get("voters", [])
            if not is_anonymous and voters:
                voters_str = ", ".join(voters[:15])
                if len(voters) > 15:
                    voters_str += f" и ещё {len(voters) - 15}"
                lines.append(f"   👥 <i>{voters_str}</i>")
            lines.append("")

    lines.append("────────────────────")
    lines.append(f"👥 <b>Всего голосов:</b> {total_votes}")

    if is_closed and closed_at:
        dt_str = closed_at.strftime("%d.%m.%Y %H:%M UTC")
        lines.append(f"🔒 <b>Завершён:</b> {dt_str}")
    elif not is_closed and deadline_at:
        dt_str = deadline_at.strftime("%d.%m.%Y %H:%M UTC")
        lines.append(f"⏱ <b>Дедлайн:</b> {dt_str}")

    return "\n".join(lines)


def format_text_poll_summary(
    question: str,
    answers_count: int,
    is_closed: bool = True,
    closed_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> str:
    """Formats summary for an open text poll."""
    status_emoji = "🏁" if is_closed else "📝"
    status_header = "Опрос с открытым ответом (Завершён)" if is_closed else "Опрос с открытым ответом"

    lines = [
        f"{status_emoji} <b>{status_header}</b>\n",
        f"❓ <b>Вопрос:</b>\n«{question}»\n",
        "────────────────────",
        f"📬 <b>Получено ответов:</b> {answers_count}",
    ]

    if is_closed:
        if closed_at:
            dt_str = closed_at.strftime("%d.%m.%Y %H:%M UTC")
            lines.append(f"🔒 <b>Завершён:</b> {dt_str}")
        lines.append("\n📁 Полный список ответов прикреплён файлами ниже.")
    else:
        if deadline_at:
            dt_str = deadline_at.strftime("%d.%m.%Y %H:%M UTC")
            lines.append(f"⏱ <b>Дедлайн:</b> {dt_str}")
        lines.append("\n💡 <i>Голосование открыто. Ответы принимаются в личных сообщениях с ботом.</i>")

    return "\n".join(lines)


def generate_text_answers_csv(
    question: str,
    answers: List[TextAnswer],
    is_anonymous: bool = False,
) -> bytes:
    """Generates Excel CSV (UTF-8 with BOM) for text answers."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    writer.writerow(["Опрос с открытыми ответами"])
    writer.writerow(["Вопрос:", question])
    writer.writerow(["Всего ответов:", len(answers)])
    writer.writerow(["Тип:", "Анонимный" if is_anonymous else "Открытый"])
    writer.writerow([])

    writer.writerow([
        "№",
        "ID пользователя",
        "Имя / Fullname",
        "Username",
        "Дата и время (UTC)",
        "Текст ответа"
    ])

    for idx, ans in enumerate(answers, start=1):
        dt_str = ans.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if ans.submitted_at else ""
        if is_anonymous:
            u_id = "—"
            fullname_str = f"Участник #{idx}"
            username_str = "—"
        else:
            u_id = ans.user_id
            fullname_str = ans.user_fullname or "—"
            username_str = f"@{ans.username}" if ans.username else "—"

        writer.writerow([
            idx,
            u_id,
            fullname_str,
            username_str,
            dt_str,
            ans.answer_text
        ])

    return output.getvalue().encode("utf-8-sig")


def generate_text_answers_txt(
    question: str,
    answers: List[TextAnswer],
    is_anonymous: bool = False,
) -> bytes:
    """Generates clean readable TXT document with all text answers."""
    lines = [
        "==================================================",
        "          ОТЧЕТ ПО ОТКРЫТЫМ ОТВЕТАМ ОПРОСА        ",
        "==================================================",
        f"Вопрос: {question}",
        f"Всего ответов: {len(answers)}",
        f"Тип: {'Анонимный' if is_anonymous else 'Открытый'}",
        "==================================================\n",
    ]

    if not answers:
        lines.append("Ответов пока нет.\n")
    else:
        for idx, ans in enumerate(answers, start=1):
            dt_str = ans.submitted_at.strftime("%d.%m.%Y %H:%M:%S UTC") if ans.submitted_at else ""
            if is_anonymous:
                user_info = f"Участник #{idx}"
            else:
                user_info = ans.user_fullname or ""
                if ans.username:
                    user_info += f" (@{ans.username})"
                user_info += f" [ID: {ans.user_id}]"

            lines.append(f"Ответ #{idx} от {user_info} ({dt_str}):")
            lines.append("-" * 40)
            lines.append(ans.answer_text)
            lines.append("\n" + "=" * 50 + "\n")

    return "\n".join(lines).encode("utf-8")


def generate_choice_votes_csv(
    question: str,
    votes: List[Vote],
    options_map: dict[int, str],
    is_anonymous: bool = False,
) -> bytes:
    """Generates CSV audit of choice votes."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    writer.writerow(["Отчет по голосованию (Опрос с вариантами)"])
    writer.writerow(["Вопрос:", question])
    writer.writerow(["Всего проголосовало:", len(votes)])
    writer.writerow(["Тип голосования:", "Анонимное" if is_anonymous else "Открытое"])
    writer.writerow([])

    writer.writerow([
        "№",
        "ID пользователя",
        "Имя",
        "Username",
        "Выбранный вариант",
        "Дата и время (UTC)"
    ])

    for idx, v in enumerate(votes, start=1):
        dt_str = v.voted_at.strftime("%Y-%m-%d %H:%M:%S") if v.voted_at else ""
        opt_text = options_map.get(v.option_id, "Неизвестный вариант")
        if is_anonymous:
            u_id = "—"
            u_name = f"Участник #{idx}"
            u_user = "—"
        else:
            u_id = v.user_id
            u_name = v.user_fullname or "—"
            u_user = f"@{v.username}" if v.username else "—"

        writer.writerow([
            idx,
            u_id,
            u_name,
            u_user,
            opt_text,
            dt_str
        ])

    return output.getvalue().encode("utf-8-sig")


async def is_user_creator_or_admin(
    bot: Bot,
    poll: Poll,
    user_id: int,
    chat_id: Optional[int] = None,
) -> bool:
    """
    Checks if a user is either the creator of the poll
    OR an administrator of the group/forum where it was published.
    """
    if poll.creator_id == user_id:
        return True

    target_chat = chat_id or poll.target_chat_id
    if target_chat and target_chat < 0:
        try:
            member = await bot.get_chat_member(chat_id=target_chat, user_id=user_id)
            if member.status in ("creator", "administrator"):
                return True
        except Exception as e:
            logger.debug("Failed to check chat admin status: %s", e)

    return False
