"""Database tables. Money is stored in integer cents; times are naive campus-local datetimes."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# --- Enums kept as plain strings so they read well in the database and logs ---

class Tier:
    """Permission tiers: how much the agent may do without asking."""

    SUGGEST_ONLY = "suggest_only"          # agent finds options, never acts
    APPROVE_EACH_STEP = "approve_each_step"  # agent proposes, student approves each action
    AUTO_WITHIN_LIMITS = "auto_within_limits"  # agent acts alone inside the approved limits

    ALL = (SUGGEST_ONLY, APPROVE_EACH_STEP, AUTO_WITHIN_LIMITS)


class IntentStatus:
    WATCHING = "WATCHING"
    IN_GROUP = "IN_GROUP"
    FULFILLED = "FULFILLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class GroupStatus:
    FORMING = "FORMING"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


class MemberStatus:
    ACTIVE = "ACTIVE"
    DROPPED = "DROPPED"            # left or removed (e.g. dropout)
    RELEASED = "RELEASED"          # moved out by the agent (e.g. didn't allow a switch)
    PAYMENT_FAILED = "PAYMENT_FAILED"


class OrderStatus:
    ORDERED = "ORDERED"
    SHIPPED = "SHIPPED"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    COLLECTED = "COLLECTED"

    FLOW = (ORDERED, SHIPPED, READY_FOR_PICKUP, COLLECTED)


# --- Tables ---

class AppMeta(Base):
    """Key/value settings, e.g. the demo database's schema version."""

    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class SimClock(Base):
    """Simulated clock so the Lab can move time forward."""

    __tablename__ = "sim_clock"
    id: Mapped[int] = mapped_column(primary_key=True)
    now: Mapped[datetime] = mapped_column(DateTime)


class Student(Base):
    __tablename__ = "students"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    pickup_default: Mapped[str] = mapped_column(String(200), default="Bloomberg Center Lobby")
    card_fails: Mapped[bool] = mapped_column(Boolean, default=False)  # simulated card problem
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    variants_csv: Mapped[str] = mapped_column(String(200), default="")  # e.g. "liquid,pods"
    keywords_csv: Mapped[str] = mapped_column(String(300), default="")

    @property
    def variants(self) -> list[str]:
        return [v for v in self.variants_csv.split(",") if v]

    @property
    def keywords(self) -> list[str]:
        return [k for k in self.keywords_csv.split(",") if k]


class Offer(Base):
    """A supplier's price for one product: solo price and group price at a minimum group size.

    Prices are stored before tax; all-in totals add tax and fees (the student's cap is all-in).
    """

    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    variant: Mapped[str | None] = mapped_column(String(60), nullable=True)
    brand: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")  # untrusted supplier text
    solo_unit_cents: Mapped[int] = mapped_column(Integer)
    group_unit_cents: Mapped[int] = mapped_column(Integer)
    tax_bp: Mapped[int] = mapped_column(Integer, default=825)  # basis points: 825 = 8.25%
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    min_buyers: Mapped[int] = mapped_column(Integer)
    delivery_hours: Mapped[int] = mapped_column(Integer, default=24)  # group closes -> ready for pickup
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    supplier: Mapped[Supplier] = relationship()
    product: Mapped[Product] = relationship()

    def _all_in(self, unit_cents: int) -> int:
        return unit_cents + round(unit_cents * self.tax_bp / 10_000) + self.fee_cents

    @property
    def solo_all_in(self) -> int:
        return self._all_in(self.solo_unit_cents)

    @property
    def group_all_in(self) -> int:
        return self._all_in(self.group_unit_cents)


class Intent(Base):
    """A student's request plus the limits they approved (the mandate)."""

    __tablename__ = "intents"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    # None = an "open request" for something Bundle doesn't list yet (see wanted_text).
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    wanted_text: Mapped[str | None] = mapped_column(String(200), nullable=True)  # what they asked for
    variant: Mapped[str | None] = mapped_column(String(60), nullable=True)  # None = any
    max_total_cents: Mapped[int] = mapped_column(Integer)  # all-in cap
    needed_by: Mapped[datetime] = mapped_column(DateTime)
    pickup: Mapped[str] = mapped_column(String(200))
    allow_switch: Mapped[bool] = mapped_column(Boolean, default=False)  # any brand/supplier
    permission_tier: Mapped[str] = mapped_column(String(30), default=Tier.AUTO_WITHIN_LIMITS)
    status: Mapped[str] = mapped_column(String(30), default=IntentStatus.WATCHING)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    student: Mapped[Student] = relationship()
    product: Mapped[Product | None] = relationship()

    @property
    def is_open_request(self) -> bool:
        """Waiting for an item that isn't listed yet; becomes a normal request once matched."""
        return self.product_id is None

    @property
    def item_name(self) -> str:
        if self.product is not None:
            return self.product.name
        return self.wanted_text or "your item"


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    variant: Mapped[str | None] = mapped_column(String(60), nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime)  # formation deadline
    pickup: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=GroupStatus.FORMING)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    offer: Mapped[Offer] = relationship()
    product: Mapped[Product] = relationship()
    members: Mapped[list["GroupMember"]] = relationship(back_populates="group")

    @property
    def active_members(self) -> list["GroupMember"]:
        return [m for m in self.members if m.status == MemberStatus.ACTIVE]


class GroupMember(Base):
    __tablename__ = "group_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    intent_id: Mapped[int] = mapped_column(ForeignKey("intents.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    status: Mapped[str] = mapped_column(String(20), default=MemberStatus.ACTIVE)
    joined_at: Mapped[datetime] = mapped_column(DateTime)

    group: Mapped[Group] = relationship(back_populates="members")
    intent: Mapped[Intent] = relationship()
    student: Mapped[Student] = relationship()


class SimulatedPayment(Base):
    """A pretend ledger row. No real money moves anywhere."""

    __tablename__ = "simulated_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    intent_id: Mapped[int] = mapped_column(ForeignKey("intents.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # SUCCEEDED | FAILED | REFUNDED
    created_at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(String(300), default="")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.ORDERED)
    pickup_code: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    offer: Mapped[Offer] = relationship()


class SavingsEntry(Base):
    """Written only at checkout, once money has (simulated-ly) moved."""

    __tablename__ = "savings"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    intent_id: Mapped[int] = mapped_column(ForeignKey("intents.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    item: Mapped[str] = mapped_column(String(200))
    paid_cents: Mapped[int] = mapped_column(Integer)
    solo_cents: Mapped[int] = mapped_column(Integer)
    saved_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    message: Mapped[str] = mapped_column(Text)


class PendingAction(Base):
    """An action the agent wants to take but the student's tier says to ask first."""

    __tablename__ = "pending_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("intents.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    action: Mapped[str] = mapped_column(String(60))  # e.g. join_purchase_group
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime)

    @property
    def args(self) -> dict[str, Any]:
        return json.loads(self.args_json or "{}")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))  # INTENT_CONFIRMED, FORMATION_DEADLINE, ...
    mode: Mapped[str] = mapped_column(String(40), default="DETERMINISTIC_FALLBACK")
    student_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    message: Mapped[str] = mapped_column(Text, default="")
    llm_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="run", order_by="ToolCall.seq")


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"))
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60))
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(40))  # agent | student | lab | system
    event_type: Mapped[str] = mapped_column(String(60))
    student_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")


class ChatSession(Base):
    """Short-term chat memory: the draft so far and the last few messages."""

    __tablename__ = "chat_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | CLOSED
    draft_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")

    @property
    def draft(self) -> dict[str, Any]:
        return json.loads(self.draft_json or "{}")

    @property
    def history(self) -> list[dict[str, str]]:
        return json.loads(self.history_json or "[]")
