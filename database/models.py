"""SQLAlchemy 2.0 Async database models for polls, topics, and votes."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class for all models."""
    pass


class User(Base):
    """Telegram user model."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    polls: Mapped[List["Poll"]] = relationship(
        "Poll", back_populates="creator", cascade="all, delete-orphan"
    )


class Poll(Base):
    """Poll model with forum topics (message_thread_id) support."""
    __tablename__ = "polls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    creator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    creator_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    poll_type: Mapped[str] = mapped_column(String(20), index=True)  # 'choice' or 'text'
    question: Mapped[str] = mapped_column(Text, nullable=False)

    # Forum topic and chat routing
    target_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    target_thread_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # message_thread_id

    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    results_visibility: Mapped[str] = mapped_column(
        String(50), default="hidden_until_close"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    creator: Mapped["User"] = relationship("User", back_populates="polls")
    options: Mapped[List["Option"]] = relationship(
        "Option",
        back_populates="poll",
        cascade="all, delete-orphan",
        order_by="Option.order_index",
        lazy="selectin",
    )
    votes: Mapped[List["Vote"]] = relationship(
        "Vote", back_populates="poll", cascade="all, delete-orphan"
    )
    text_answers: Mapped[List["TextAnswer"]] = relationship(
        "TextAnswer", back_populates="poll", cascade="all, delete-orphan"
    )
    posts: Mapped[List["PollPost"]] = relationship(
        "PollPost", back_populates="poll", cascade="all, delete-orphan", lazy="selectin"
    )


class Option(Base):
    """Answer option for choice polls."""
    __tablename__ = "options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("polls.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    poll: Mapped["Poll"] = relationship("Poll", back_populates="options")
    votes: Mapped[List["Vote"]] = relationship(
        "Vote", back_populates="option", cascade="all, delete-orphan"
    )


class Vote(Base):
    """Recorded choice vote by a user."""
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", name="uq_poll_user_vote"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("polls.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_fullname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    option_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("options.id", ondelete="CASCADE"), index=True
    )
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    poll: Mapped["Poll"] = relationship("Poll", back_populates="votes")
    option: Mapped["Option"] = relationship("Option", back_populates="votes")


class TextAnswer(Base):
    """Open text answer submitted by a user."""
    __tablename__ = "text_answers"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", name="uq_poll_user_text_answer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("polls.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_fullname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    poll: Mapped["Poll"] = relationship("Poll", back_populates="text_answers")


class PollPost(Base):
    """Tracks message locations where the poll was published (with topic support)."""
    __tablename__ = "poll_posts"
    __table_args__ = (
        UniqueConstraint(
            "poll_id", "chat_id", "message_id", "inline_message_id",
            name="uq_poll_post_location"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("polls.id", ondelete="CASCADE"), index=True
    )
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    message_thread_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Forum topic ID
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    inline_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    poll: Mapped["Poll"] = relationship("Poll", back_populates="posts")


# Backwards compatibility aliases
PollOption = Option
TextResponse = TextAnswer
