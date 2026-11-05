"""Inline keyboard builders for polls, wizard, and management."""

from typing import List
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database.models import Option


def get_cancel_keyboard(callback_data: str = "cancel_wizard") -> InlineKeyboardMarkup:
    """Standard cancel button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]
        ]
    )


def get_poll_type_keyboard() -> InlineKeyboardMarkup:
    """Step 1: Choose poll type."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 С выбором вариантов (тестовый)",
                    callback_data="poll_type:choice",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ С открытым текстовым ответом",
                    callback_data="poll_type:text",
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")],
        ]
    )


def get_options_input_keyboard(count: int) -> InlineKeyboardMarkup:
    """Step 3: Options input control keyboard."""
    buttons = []
    if count >= 2:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Готово! Сохранить варианты ({count})",
                    callback_data="options_done",
                )
            ]
        )
    if count > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🗑 Очистить добавленные варианты",
                    callback_data="options_clear",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="❌ Отмена создания", callback_data="cancel_wizard")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_visibility_keyboard() -> InlineKeyboardMarkup:
    """Visibility settings keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Скрыты до закрытия (По умолчанию)",
                    callback_data="visibility:hidden_until_close",
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")],
        ]
    )


def get_deadline_keyboard() -> InlineKeyboardMarkup:
    """Step 4: Deadline selection keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ 15 минут", callback_data="deadline:15m"),
                InlineKeyboardButton(text="⏱ 1 час", callback_data="deadline:1h"),
            ],
            [
                InlineKeyboardButton(text="⏱ 6 часов", callback_data="deadline:6h"),
                InlineKeyboardButton(text="⏱ 24 часа", callback_data="deadline:24h"),
            ],
            [
                InlineKeyboardButton(text="⏱ 3 дня", callback_data="deadline:3d"),
                InlineKeyboardButton(text="♾ Без таймера (вручную)", callback_data="deadline:none"),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Указать своё время (в минутах/часах)",
                    callback_data="deadline:custom",
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")],
        ]
    )


def get_target_destination_keyboard() -> InlineKeyboardMarkup:
    """Step 5: Select publishing destination (current PM or group/topic)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Опубликовать здесь (текущий диалог)",
                    callback_data="target:current",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 В группу / тему форума (Topics)",
                    callback_data="target:topic",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📤 Только через Inline-режим (@bot)",
                    callback_data="target:inline_only",
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")],
        ]
    )


def get_choice_voting_keyboard(
    poll_id: str,
    options: List[Option],
) -> InlineKeyboardMarkup:
    """
    Builds voting keyboard for choice polls.
    Results are completely hidden during active voting!
    """
    rows = []
    for opt in options:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"▫️ {opt.text}",
                    callback_data=f"vote:{poll_id}:{opt.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_text_poll_chat_keyboard(
    poll_id: str,
    bot_username: str,
) -> InlineKeyboardMarkup:
    """Builds deep-link button for text polls to open bot PM."""
    deep_link = f"https://t.me/{bot_username}?start=poll_{poll_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Ответить на опрос",
                    url=deep_link,
                )
            ]
        ]
    )


def get_creator_manage_keyboard(
    poll_id: str,
    bot_username: str,
    poll_type: str,
    is_closed: bool = False,
) -> InlineKeyboardMarkup:
    """Creator's management panel."""
    rows = [
        [
            InlineKeyboardButton(
                text="📤 Опубликовать в чат/канал",
                switch_inline_query=f"poll_{poll_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Финальные результаты" if is_closed else "📊 Текущая статистика",
                callback_data=f"stats:{poll_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📁 Экспорт ответов (CSV / TXT)" if poll_type == "text" else "📁 Экспорт голосов (CSV)",
                callback_data=f"export:{poll_id}",
            )
        ],
    ]

    if not is_closed:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔒 Завершить опрос",
                    callback_data=f"close_confirm:{poll_id}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_close_confirmation_keyboard(poll_id: str) -> InlineKeyboardMarkup:
    """Confirmation before early closing."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Да, завершить опрос сейчас",
                    callback_data=f"close_do:{poll_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Назад к управлению",
                    callback_data=f"manage:{poll_id}",
                )
            ],
        ]
    )


def get_text_response_action_keyboard(poll_id: str) -> InlineKeyboardMarkup:
    """Keyboard for re-submitting or keeping previous text answer."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Написать новый ответ (заменить)",
                    callback_data=f"text_reanswer:{poll_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Оставить как есть",
                    callback_data="text_cancel",
                )
            ],
        ]
    )
