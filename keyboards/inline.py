"""Inline keyboard builders for polls, wizard, groups, topics, and management."""

from typing import List, Optional
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database.models import ForumTopic, GroupChat, Option


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


def get_deadline_keyboard(is_editing: bool = False, poll_id: str = "") -> InlineKeyboardMarkup:
    """Deadline selection keyboard."""
    prefix = f"edl:{poll_id}:" if is_editing else "deadline:"
    cancel_cb = f"edit_poll:{poll_id}" if is_editing else "cancel_wizard"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ 15 минут", callback_data=f"{prefix}15m"),
                InlineKeyboardButton(text="⏱ 1 час", callback_data=f"{prefix}1h"),
            ],
            [
                InlineKeyboardButton(text="⏱ 6 часов", callback_data=f"{prefix}6h"),
                InlineKeyboardButton(text="⏱ 24 часа", callback_data=f"{prefix}24h"),
            ],
            [
                InlineKeyboardButton(text="⏱ 3 дня", callback_data=f"{prefix}3d"),
                InlineKeyboardButton(text="♾ Без таймера", callback_data=f"{prefix}none"),
            ],
            [
                InlineKeyboardButton(
                    text="📅 Точная дата и время",
                    callback_data=f"{prefix}exact",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Указать длительность вручную",
                    callback_data=f"{prefix}custom",
                )
            ],
            [InlineKeyboardButton(text="🔙 Назад" if is_editing else "❌ Отмена", callback_data=cancel_cb)],
        ]
    )


def get_target_destination_keyboard() -> InlineKeyboardMarkup:
    """Step 5: Select publishing destination fallback (current PM or group/topic)."""
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


def get_groups_selection_keyboard(groups: List[GroupChat]) -> InlineKeyboardMarkup:
    """Step 5: Interactive list of registered groups + options."""
    rows = []

    # Button for each connected group
    for g in groups:
        forum_badge = " [Форум/Темы]" if g.is_forum else ""
        short_title = g.title[:30] + ("..." if len(g.title) > 30 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👥 {short_title}{forum_badge}",
                    callback_data=f"sel_grp:{g.id}",
                )
            ]
        )

    # Current chat (PM) option
    rows.append(
        [
            InlineKeyboardButton(
                text="💬 Опубликовать здесь (в этот диалог)",
                callback_data="target:current",
            )
        ]
    )

    # Manual input or forwarding
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Указать другой чат / тему вручную",
                callback_data="target:topic_manual",
            )
        ]
    )

    # Inline mode only
    rows.append(
        [
            InlineKeyboardButton(
                text="📤 Только через Inline-режим (@bot)",
                callback_data="target:inline_only",
            )
        ]
    )

    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_topics_selection_keyboard(
    chat_id: int,
    topics: List[ForumTopic],
    is_forum: bool = True,
) -> InlineKeyboardMarkup:
    """Step 5b: List of known topics in the selected group."""
    rows = []

    # General / Main topic option
    rows.append(
        [
            InlineKeyboardButton(
                text="💬 Основная ветка (General)",
                callback_data=f"sel_tpc:{chat_id}:0",
            )
        ]
    )

    # Each registered topic
    for t in topics:
        if t.thread_id != 0:
            short_t = t.title[:30] + ("..." if len(t.title) > 30 else "")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📌 {short_t}",
                        callback_data=f"sel_tpc:{chat_id}:{t.thread_id}",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Ввести ID темы вручную",
                callback_data=f"sel_tpc_manual:{chat_id}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Назад к списку групп",
                callback_data="back_to_groups",
            )
        ]
    )

    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    message_link: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Creator's management panel."""
    rows = []

    # If poll was published into a chat and link is available
    if message_link:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔗 Открыть опрос в чате",
                    url=message_link,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="📤 Опубликовать в чат/канал",
                switch_inline_query=f"poll_{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📊 Финальные результаты" if is_closed else "📊 Текущая статистика",
                callback_data=f"stats:{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📁 Экспорт ответов (CSV / TXT)" if poll_type == "text" else "📁 Экспорт голосов (CSV)",
                callback_data=f"export:{poll_id}",
            )
        ]
    )

    if not is_closed:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Изменить опрос",
                    callback_data=f"edit_poll:{poll_id}",
                ),
                InlineKeyboardButton(
                    text="🎯 Тема для итогов",
                    callback_data=f"res_dest:{poll_id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔒 Завершить опрос",
                    callback_data=f"close_confirm:{poll_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="💾 В шаблон",
                callback_data=f"save_tpl:{poll_id}",
            ),
            InlineKeyboardButton(
                text="🗑 Удалить опрос",
                callback_data=f"del_poll:{poll_id}",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="📋 Мои опросы",
                callback_data="btn_my_polls",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_poll_edit_keyboard(poll_id: str, poll_type: str) -> InlineKeyboardMarkup:
    """Editing actions menu for an active poll."""
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Изменить текст вопроса",
                callback_data=f"edit_q:{poll_id}",
            )
        ],
    ]
    if poll_type == "choice":
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Добавить вариант ответа",
                    callback_data=f"edit_add_opt:{poll_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⏱ Изменить дедлайн / дату",
                callback_data=f"edit_deadline:{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🎯 Изменить тему для итогов",
                callback_data=f"res_dest:{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Назад в управление",
                callback_data=f"manage:{poll_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_delete_poll_confirm_keyboard(poll_id: str) -> InlineKeyboardMarkup:
    """Confirmation before deleting a poll."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Да, удалить опрос навсегда",
                    callback_data=f"del_confirm:{poll_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Отмена",
                    callback_data=f"manage:{poll_id}",
                )
            ],
        ]
    )


def get_delete_all_closed_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmation before deleting all closed polls."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Да, удалить все завершённые",
                    callback_data="del_all_closed_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Отмена",
                    callback_data="btn_my_polls",
                )
            ],
        ]
    )


def get_result_dest_selection_keyboard(
    poll_id: str,
    groups: List[GroupChat],
) -> InlineKeyboardMarkup:
    """Choose destination for final poll results."""
    rows = [
        [
            InlineKeyboardButton(
                text="💬 В ту же тему/чат (по умолчанию)",
                callback_data=f"set_res_orig:{poll_id}",
            )
        ]
    ]

    for g in groups:
        short_title = g.title[:25] + ("..." if len(g.title) > 25 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👥 {short_title}",
                    callback_data=f"set_res_grp:{poll_id}:{g.id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Указать ID чата/темы вручную",
                callback_data=f"set_res_manual:{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Назад",
                callback_data=f"manage:{poll_id}",
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


def get_templates_list_keyboard(templates: List) -> InlineKeyboardMarkup:
    """List of saved templates."""
    rows = []
    for t in templates:
        icon = "📊" if t.poll_type == "choice" else "✍️"
        short_title = t.title[:30] + ("..." if len(t.title) > 30 else "")
        rows.append(
            [InlineKeyboardButton(text=f"{icon} {short_title}", callback_data=f"tpl_view:{t.id}")]
        )
    rows.append([InlineKeyboardButton(text="➕ Создать новый опрос", callback_data="btn_create_poll")])
    rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="btn_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_template_action_keyboard(template_id: int) -> InlineKeyboardMarkup:
    """Action menu for a single template."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Запустить опрос по шаблону",
                    callback_data=f"tpl_launch:{template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить шаблон",
                    callback_data=f"tpl_del:{template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 К списку шаблонов",
                    callback_data="btn_templates",
                )
            ],
        ]
    )


def get_poll_settings_keyboard(
    is_choice: bool,
    is_multiple: bool,
    is_anon: bool,
    voters: str,
    reminder_min: Optional[int],
    has_deadline: bool,
) -> InlineKeyboardMarkup:
    """Settings toggle keyboard for poll wizard."""
    rows = []

    # 1. Multiple choice (only for choice polls)
    if is_choice:
        mult_text = "☑️ Режим: Несколько ответов" if is_multiple else "🔘 Режим: Один ответ"
        rows.append([InlineKeyboardButton(text=mult_text, callback_data="cfg:multichoice")])

    # 2. Privacy: Anonymous vs Public
    anon_text = "🔒 Голосование: Анонимное" if is_anon else "👤 Голосование: Открытое"
    rows.append([InlineKeyboardButton(text=anon_text, callback_data="cfg:anon")])

    # 3. Voter restrictions
    voter_labels = {
        "all": "👥 Кто голосует: Все",
        "with_username": "🛡 Кто: С @username",
        "admins_only": "👑 Кто: Только админы",
    }
    rows.append([InlineKeyboardButton(text=voter_labels.get(voters, "👥 Кто: Все"), callback_data="cfg:voters")])

    # 4. Reminder (only if deadline is configured)
    if has_deadline:
        if not reminder_min:
            rem_text = "⏰ Напоминание: Выкл"
        elif reminder_min == 60:
            rem_text = "⏰ Напоминание: За 1 час"
        elif reminder_min == 30:
            rem_text = "⏰ Напоминание: За 30 мин"
        elif reminder_min == 15:
            rem_text = "⏰ Напоминание: За 15 мин"
        else:
            rem_text = f"⏰ Напоминание: За {reminder_min} мин"
        rows.append([InlineKeyboardButton(text=rem_text, callback_data="cfg:reminder")])

    rows.append([InlineKeyboardButton(text="➡️ Далее (выбор места публикации)", callback_data="cfg:done")])
    rows.append([InlineKeyboardButton(text="❌ Отмена создания", callback_data="cancel_wizard")])

    return InlineKeyboardMarkup(inline_keyboard=rows)
