"""FSM state definitions for poll creation and responses."""

from aiogram.fsm.state import State, StatesGroup


class PollCreation(StatesGroup):
    """States for the step-by-step poll creation wizard."""
    waiting_for_type = State()
    waiting_for_question = State()
    waiting_for_options = State()
    waiting_for_deadline = State()
    waiting_for_custom_deadline = State()
    waiting_for_destination = State()
    waiting_for_topic_forward_or_id = State()


class TextResponseState(StatesGroup):
    """States for submitting an open text answer to a poll."""
    waiting_for_text_answer = State()
