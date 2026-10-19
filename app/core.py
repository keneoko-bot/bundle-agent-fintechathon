"""Small shared helpers: simulated clock, audit log, notifications, money formatting."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppMeta, AuditEvent, Notification, SimClock

DEFAULT_START = datetime(2026, 10, 12, 10, 0)

# The shared delivery destination for the whole house. Admin-configurable; every
# group order is delivered here (no per-student pickup). Defaults seeded on first run.
DEFAULT_HOUSE_NAME = "The House at Cornell Tech"
DEFAULT_HOUSE_ADDRESS = "1 E Loop Rd, Roosevelt Island, New York, NY 10044"


def get_house(session: Session) -> dict:
    rows = {m.key: m.value for m in session.scalars(
        select(AppMeta).where(AppMeta.key.in_(["house_name", "house_address"])))}
    return {
        "name": rows.get("house_name", DEFAULT_HOUSE_NAME),
        "address": rows.get("house_address", DEFAULT_HOUSE_ADDRESS),
    }


def set_house(session: Session, name: str, address: str) -> dict:
    for key, value in (("house_name", name.strip()), ("house_address", address.strip())):
        row = session.get(AppMeta, key)
        if row is None:
            session.add(AppMeta(key=key, value=value))
        else:
            row.value = value
    session.flush()
    return get_house(session)


class PolicyRefusal(Exception):
    """Raised by the code checks when an action falls outside what the student approved."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- Clock ---

def now(session: Session) -> datetime:
    clock = session.get(SimClock, 1)
    if clock is None:
        clock = SimClock(id=1, now=DEFAULT_START)
        session.add(clock)
        session.flush()
    return clock.now


def set_now(session: Session, value: datetime) -> datetime:
    clock = session.get(SimClock, 1)
    if clock is None:
        clock = SimClock(id=1, now=value)
        session.add(clock)
    else:
        clock.now = value
    session.flush()
    return value


def advance(session: Session, hours: float) -> datetime:
    return set_now(session, now(session) + timedelta(hours=hours))


# --- Audit and notifications ---

def audit(
    session: Session,
    actor: str,
    event_type: str,
    *,
    student_id: int | None = None,
    intent_id: int | None = None,
    group_id: int | None = None,
    run_id: int | None = None,
    **details: Any,
) -> AuditEvent:
    event = AuditEvent(
        ts=now(session),
        actor=actor,
        event_type=event_type,
        student_id=student_id,
        intent_id=intent_id,
        group_id=group_id,
        run_id=run_id,
        details_json=json.dumps(details, default=str),
    )
    session.add(event)
    session.flush()
    return event


def notify(session: Session, student_id: int, message: str) -> Notification:
    note = Notification(student_id=student_id, created_at=now(session), message=message)
    session.add(note)
    session.flush()
    return note


def latest_notifications(session: Session, student_id: int, limit: int = 20) -> list[Notification]:
    stmt = (
        select(Notification)
        .where(Notification.student_id == student_id)
        .order_by(Notification.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


# --- Formatting ---

def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def fmt_date(value: datetime) -> str:
    return value.strftime("%b %d, %Y").replace(" 0", " ")


def fmt_dt(value: datetime) -> str:
    return value.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
