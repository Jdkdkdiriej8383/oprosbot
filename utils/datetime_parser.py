"""Utility for parsing human-entered exact dates and times in various formats with timezone support."""

from datetime import datetime, timedelta, timezone
import re
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone_obj(tz_name: str) -> ZoneInfo:
    """Returns ZoneInfo object, fallback to UTC if invalid."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        return ZoneInfo("UTC")


def parse_exact_datetime(
    text: str,
    tz_name: str = "UTC",
) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Parses a date and time string into a timezone-aware UTC datetime.
    Supports formats:
    - 'ДД.ММ.ГГГГ ЧЧ:ММ' (e.g. '07.11.2026 18:00')
    - 'ГГГГ-ММ-ДД ЧЧ:ММ' (e.g. '2026-11-07 18:00')
    - 'ДД.ММ ЧЧ:ММ' (e.g. '07.11 18:00' -> current or next year)
    - 'ЧЧ:ММ' (e.g. '18:00' -> today or tomorrow)
    - 'завтра [в] ЧЧ:ММ' (e.g. 'завтра 18:00', 'завтра в 18:00')

    Returns:
        (utc_datetime, formatted_local_str) on success, or (None, error_message) on failure.
    """
    cleaned = text.strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)

    tz = get_timezone_obj(tz_name)
    now_local = datetime.now(tz)

    target_local: Optional[datetime] = None

    # 1. "завтра [в] ЧЧ:ММ"
    m_tomorrow = re.match(r"^завтра\s+(?:в\s+)?(\d{1,2}):(\d{2})$", cleaned)
    if m_tomorrow:
        hour, minute = int(m_tomorrow.group(1)), int(m_tomorrow.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            tomorrow_date = (now_local + timedelta(days=1)).date()
            target_local = datetime(
                tomorrow_date.year, tomorrow_date.month, tomorrow_date.day,
                hour, minute, tzinfo=tz
            )

    # 2. "ЧЧ:ММ"
    if not target_local:
        m_time = re.match(r"^(\d{1,2}):(\d{2})$", cleaned)
        if m_time:
            hour, minute = int(m_time.group(1)), int(m_time.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                candidate = datetime(
                    now_local.year, now_local.month, now_local.day,
                    hour, minute, tzinfo=tz
                )
                # If time has already passed today, assume tomorrow
                if candidate <= now_local + timedelta(minutes=1):
                    candidate += timedelta(days=1)
                target_local = candidate

    # 3. "ДД.ММ.ГГГГ ЧЧ:ММ" or "ДД/ММ/ГГГГ ЧЧ:ММ"
    if not target_local:
        m_full = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})\s+(\d{1,2}):(\d{2})$", cleaned)
        if m_full:
            day, month, year = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
            hour, minute = int(m_full.group(4)), int(m_full.group(5))
            try:
                target_local = datetime(year, month, day, hour, minute, tzinfo=tz)
            except ValueError:
                return None, "Некорректная дата или время."

    # 4. "ГГГГ-ММ-ДД ЧЧ:ММ"
    if not target_local:
        m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$", cleaned)
        if m_iso:
            year, month, day = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
            hour, minute = int(m_iso.group(4)), int(m_iso.group(5))
            try:
                target_local = datetime(year, month, day, hour, minute, tzinfo=tz)
            except ValueError:
                return None, "Некорректная дата или время."

    # 5. "ДД.ММ ЧЧ:ММ" (current year, or next year if month passed)
    if not target_local:
        m_short = re.match(r"^(\d{1,2})[./](\d{1,2})\s+(\d{1,2}):(\d{2})$", cleaned)
        if m_short:
            day, month = int(m_short.group(1)), int(m_short.group(2))
            hour, minute = int(m_short.group(3)), int(m_short.group(4))
            year = now_local.year
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
                if candidate < now_local - timedelta(days=1):
                    # Next year
                    candidate = datetime(year + 1, month, day, hour, minute, tzinfo=tz)
                target_local = candidate
            except ValueError:
                return None, "Некорректная дата или время."

    if not target_local:
        return None, (
            "⚠️ Не удалось распознать дату и время.\n"
            "Пожалуйста, используйте один из форматов:\n"
            "• <code>ДД.ММ ЧЧ:ММ</code> (например: <code>08.11 18:30</code>)\n"
            "• <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (например: <code>08.11.2026 18:30</code>)\n"
            "• <code>ЧЧ:ММ</code> (например: <code>19:00</code>)\n"
            "• <code>завтра 14:00</code>"
        )

    # Convert to UTC
    target_utc = target_local.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    if target_utc <= now_utc + timedelta(minutes=1):
        return None, "⚠️ Указанное время уже прошло или наступит менее чем через 1 минуту. Укажите время в будущем."

    formatted_str = target_local.strftime("%d.%m.%Y в %H:%M")
    return target_utc, formatted_str
