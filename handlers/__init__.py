"""Handlers package for oprosbot."""

from handlers.admin_poll import router as admin_poll_router
from handlers.create_poll import router as create_poll_router
from handlers.group_events import router as group_events_router
from handlers.inline_mode import router as inline_router
from handlers.start import router as start_router
from handlers.voting import router as voting_router

__all__ = [
    "start_router",
    "create_poll_router",
    "voting_router",
    "admin_poll_router",
    "group_events_router",
    "inline_router",
]
