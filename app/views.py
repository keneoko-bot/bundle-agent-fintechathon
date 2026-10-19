"""JSON views returned by the API."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine
from app.core import money, pct
from app.models import AgentRun, Group, Intent, Order, SavingsEntry, Student


def student_names(session: Session) -> dict[int, str]:
    return {s.id: s.name for s in session.scalars(select(Student))}


def run_view(run: AgentRun, include_results: bool = True, names: dict[int, str] | None = None) -> dict:
    calls = []
    for tc in sorted(run.tool_calls, key=lambda c: c.seq):
        item = {
            "seq": tc.seq,
            "tool": tc.name,
            "args": json.loads(tc.args_json or "{}"),
            "allowed": tc.allowed,
        }
        if tc.refusal_reason:
            item["refusal_reason"] = tc.refusal_reason
        if include_results:
            item["result"] = json.loads(tc.result_json or "{}")
        calls.append(item)
    return {
        "run_id": run.id,
        "kind": run.kind,
        "mode": run.mode,
        "started_at": run.started_at.isoformat(),
        "intent_id": run.intent_id,
        "group_id": run.group_id,
        "student_id": run.student_id,
        "student_name": (names or {}).get(run.student_id) if run.student_id else None,
        "message": run.message,
        "llm_error": run.llm_error,
        "tool_sequence": " → ".join(c["tool"] for c in calls),
        "tool_calls": calls,
    }


def intent_view(session: Session, intent: Intent) -> dict:
    view = {
        "intent_id": intent.id,
        "student_id": intent.student_id,
        "item": intent.item_name,
        "product_key": intent.product.key if intent.product else None,
        "open_request": intent.is_open_request,  # waiting for an item Bundle doesn't list yet
        "wanted": intent.wanted_text,
        "type": intent.variant or "any",
        "max_total": money(intent.max_total_cents),
        "max_total_cents": intent.max_total_cents,
        "needed_by": intent.needed_by.isoformat(),
        "pickup": intent.pickup,
        "allow_switch": intent.allow_switch,
        "permission_tier": intent.permission_tier,
        "status": intent.status,
        "group": None,
    }
    if intent.group_id:
        view["group"] = engine.group_snapshot(session.get(Group, intent.group_id))
        order = session.scalar(select(Order).where(Order.group_id == intent.group_id))
        if order is not None and intent.status == "FULFILLED":
            view["order"] = {"order_id": order.id, "status": order.status, "pickup_code": order.pickup_code}
    return view


def savings_view(session: Session, student_id: int) -> dict:
    rows = list(session.scalars(
        select(SavingsEntry).where(SavingsEntry.student_id == student_id).order_by(SavingsEntry.id)))
    paid = sum(r.paid_cents for r in rows)
    solo = sum(r.solo_cents for r in rows)
    saved = sum(r.saved_cents for r in rows)
    orders = []
    for r in rows:
        order = session.get(Order, r.order_id)
        orders.append({
            "order_id": r.order_id,
            "item": r.item,
            "date": r.created_at.date().isoformat(),
            "paid": money(r.paid_cents),
            "solo_price": money(r.solo_cents),
            "saved": money(r.saved_cents),
            "saved_pct": pct(r.saved_cents, r.solo_cents),
            "order_status": order.status if order else None,
        })
    return {
        "student_id": student_id,
        "orders": orders,
        "total_paid": money(paid),
        "total_saved": money(saved),
        "total_saved_cents": saved,
        "total_saved_pct": pct(saved, solo),
        "note": "Savings are recorded only at checkout, against each supplier's individual price.",
    }


def group_admin_view(group: Group) -> dict:
    """Lab/admin view: includes member names, which students never see about each other."""
    snap = engine.group_snapshot(group)
    snap["members"] = [
        {"student": m.student.name, "status": m.status, "intent_id": m.intent_id}
        for m in group.members
    ]
    return snap
