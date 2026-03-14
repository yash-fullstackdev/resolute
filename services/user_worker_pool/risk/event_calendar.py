"""
EventCalendar -- load and query upcoming market events.

Events include: RBI MPC, Union Budget, OPEC meetings, corporate earnings,
NSE expiry days, US Fed decisions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time, datetime, timedelta
from typing import Optional

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="event_calendar")


@dataclass
class Event:
    """A single market event."""
    name: str                       # "RBI_MPC" | "UNION_BUDGET" | "OPEC_MEETING" | "EARNINGS"
    date: date
    time_ist: time                  # Announcement time in IST
    underlying: list[str]           # Which underlyings this affects
    direction_bias: str             # "BULLISH" | "BEARISH" | "NEUTRAL"
    expected_move_pct: float        # Historical average move on this event
    category: str = "MACRO"        # "MACRO" | "EARNINGS" | "EXPIRY" | "GLOBAL"


# Default recurring events (approximate dates -- updated via config)
_DEFAULT_EVENTS: list[dict] = [
    {
        "name": "NSE_WEEKLY_EXPIRY",
        "day_of_week": 3,  # Thursday
        "time_ist": "15:30",
        "underlying": ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        "direction_bias": "NEUTRAL",
        "expected_move_pct": 1.0,
        "category": "EXPIRY",
    },
]


class EventCalendar:
    """Load events from config and provide query APIs."""

    def __init__(self, events: list[Event] | None = None) -> None:
        self._events: list[Event] = events or []
        self._loaded = False

    async def load_events(self, db=None, config_path: str | None = None) -> None:
        """Load events from database or config file.

        If *db* is provided, loads from the ``market_events`` table.
        Falls back to default recurring events.
        """
        if db is not None:
            try:
                async with db.connection() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT name, event_date, event_time_ist, underlyings,
                               direction_bias, expected_move_pct, category
                        FROM market_events
                        WHERE event_date >= CURRENT_DATE
                        ORDER BY event_date
                        """
                    )
                    for row in rows:
                        underlyings = row["underlyings"]
                        if isinstance(underlyings, str):
                            underlyings = [u.strip() for u in underlyings.split(",")]
                        self._events.append(Event(
                            name=row["name"],
                            date=row["event_date"],
                            time_ist=row["event_time_ist"],
                            underlying=underlyings,
                            direction_bias=row["direction_bias"],
                            expected_move_pct=float(row["expected_move_pct"]),
                            category=row.get("category", "MACRO"),
                        ))
                    logger.info("events_loaded_from_db", count=len(rows))
            except Exception as exc:
                logger.warning("events_db_load_failed", error=str(exc))

        # Also generate recurring weekly expiry events for next 4 weeks
        self._generate_weekly_expiries()

        self._loaded = True
        logger.info("event_calendar_ready", total_events=len(self._events))

    def _generate_weekly_expiries(self) -> None:
        """Generate weekly NSE expiry events for the next 4 weeks."""
        today = date.today()
        for week_offset in range(4):
            # Find next Thursday
            days_until_thursday = (3 - today.weekday()) % 7
            if days_until_thursday == 0 and week_offset == 0:
                expiry_date = today
            else:
                expiry_date = today + timedelta(
                    days=days_until_thursday + (7 * week_offset)
                )

            # Check if this event already exists
            exists = any(
                e.name == "NSE_WEEKLY_EXPIRY" and e.date == expiry_date
                for e in self._events
            )
            if not exists:
                self._events.append(Event(
                    name="NSE_WEEKLY_EXPIRY",
                    date=expiry_date,
                    time_ist=time(15, 30),
                    underlying=["NIFTY", "BANKNIFTY", "FINNIFTY"],
                    direction_bias="NEUTRAL",
                    expected_move_pct=1.0,
                    category="EXPIRY",
                ))

    def get_upcoming_events(
        self,
        days_ahead: int = 5,
        underlying: str | None = None,
    ) -> list[Event]:
        """Return events within *days_ahead* trading days.

        Optionally filter by *underlying*.
        """
        today = date.today()
        cutoff = today + timedelta(days=days_ahead)

        results = []
        for event in self._events:
            if today <= event.date <= cutoff:
                if underlying is None or underlying in event.underlying:
                    results.append(event)

        results.sort(key=lambda e: e.date)
        return results

    def is_event_day(self, underlying: str, target_date: date | None = None) -> bool:
        """Return True if *target_date* (default today) has an event for *underlying*."""
        target_date = target_date or date.today()
        return any(
            e.date == target_date and underlying in e.underlying
            for e in self._events
        )

    def get_event_direction(self, event: Event) -> str:
        """Return the direction bias of an event."""
        return event.direction_bias

    def get_nearest_event(
        self, underlying: str, today: date | None = None
    ) -> tuple[int, Event | None]:
        """Return (days_until, event) for the nearest future event affecting *underlying*.

        Returns (999, None) if no upcoming events.
        """
        today = today or date.today()
        nearest_days = 999
        nearest_event = None

        for event in self._events:
            delta = (event.date - today).days
            if 0 <= delta < nearest_days and underlying in event.underlying:
                nearest_days = delta
                nearest_event = event

        return nearest_days, nearest_event

    def add_event(self, event: Event) -> None:
        """Manually add an event to the calendar."""
        self._events.append(event)
