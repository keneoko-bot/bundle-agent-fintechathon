"""Student-facing API routes."""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import core, engine
from app.agent import extract, runner
from app.agent.llm import get_llm
from app.agent.safety import sanitize
from app.agent.tools import ToolContext
from app.config import Settings
from app.db import get_session
from app.models import (
    AgentRun,
    AuditEvent,
    ChatSession,
    Group,
    Intent,
    IntentStatus,
    MemberStatus,
    Offer,
    Order,
    OrderStatus,
    PendingAction,
    Product,
    Student,
    Tier,
)
from app.views import intent_view, run_view, savings_view, student_names

router = APIRouter(prefix="/api")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _student(session: Session, student_id: int) -> Student:
    student = session.get(Student, student_id)
    if student is None:
        raise HTTPException(404, "Student not found")
    return student


def _require_verified(student: Student) -> None:
    if not student.email_verified:
        raise HTTPException(403, "Verify your campus email before using Bundle.")


# --------------------------------------------------------------------------
# Status and catalogue
# --------------------------------------------------------------------------

@router.get("/health")
def health(session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    llm = get_llm(settings)
    return {
        "ok": True,
        "agent_mode": f"REAL_LLM ({llm.provider}/{llm.model})" if llm else "DETERMINISTIC_FALLBACK",
        "simulated_now": core.now(session).isoformat(),
    }


@router.get("/catalog")
def catalog(session: Session = Depends(get_session)):
    out = []
    for p in session.scalars(select(Product).order_by(Product.id)):
        offers = session.scalars(select(Offer).where(Offer.product_id == p.id).order_by(Offer.id))
        out.append({
            "product_key": p.key, "name": p.name, "variants": p.variants,
            "offers": [{
                "offer_id": o.id, "supplier": o.supplier.name, "brand": o.brand, "variant": o.variant,
                "solo_price": core.money(o.solo_all_in), "group_price": core.money(o.group_all_in),
                "min_buyers": o.min_buyers, "available": engine.offer_usable(o),
            } for o in offers],
        })
    return out


# --------------------------------------------------------------------------
# Students and verified campus emails
# --------------------------------------------------------------------------

class SignUp(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)


class VerifyCode(BaseModel):
    code: str


@router.get("/students")
def list_students(session: Session = Depends(get_session)):
    return [{"student_id": s.id, "name": s.name, "email": s.email, "email_verified": s.email_verified}
            for s in session.scalars(select(Student).order_by(Student.id))]


@router.post("/students")
def sign_up(body: SignUp, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    email = body.email.strip().lower()
    domain = email.rsplit("@", 1)[-1]
    if "@" not in email or not any(domain == d or domain.endswith("." + d) for d in settings.campus_domains):
        raise HTTPException(400, f"Use your campus email ({', '.join(settings.campus_domains)}).")
    if session.scalar(select(Student).where(Student.email == email)):
        raise HTTPException(409, "That email is already registered.")
    code = f"{secrets.randbelow(10**6):06d}"
    student = Student(name=body.name, email=email, email_verified=False, verification_code=code,
                      is_simulated=False)
    session.add(student)
    session.flush()
    core.audit(session, "student", "signed_up", student_id=student.id, email_domain=domain)
    return {"student_id": student.id, "email_verified": False,
            "demo_verification_code": code,
            "note": "In production the code is emailed; the demo returns it here."}


@router.post("/students/{student_id}/verify")
def verify(student_id: int, body: VerifyCode, session: Session = Depends(get_session)):
    student = _student(session, student_id)
    if not student.verification_code or body.code.strip() != student.verification_code:
        core.audit(session, "student", "verification_failed", student_id=student.id)
        raise HTTPException(400, "Wrong code.")
    student.email_verified = True
    student.verification_code = None
    core.audit(session, "student", "email_verified", student_id=student.id)
    return {"student_id": student.id, "email_verified": True}


# --------------------------------------------------------------------------
# Chat -> draft -> approved request
# --------------------------------------------------------------------------

class ChatIn(BaseModel):
    student_id: int
    message: str = Field(min_length=1, max_length=2000)
    session_id: int | None = Field(None, description="Continue a conversation (returned by the previous reply)")
    partial: dict | None = Field(None, description="Optional: draft so far, if not using session_id")


@router.post("/chat")
def chat(body: ChatIn, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    """Chat with Bundle. Send back the returned session_id so follow-ups keep their context."""
    student = _student(session, body.student_id)
    _require_verified(student)
    chat_session = None
    if body.session_id is not None:
        chat_session = session.get(ChatSession, body.session_id)
        if chat_session is None or chat_session.student_id != student.id or chat_session.status != "OPEN":
            chat_session = None
    if chat_session is None:
        chat_session = ChatSession(student_id=student.id, created_at=core.now(session))
        session.add(chat_session)
        session.flush()
    partial = body.partial or chat_session.draft or None
    history = chat_session.history
    result = extract.handle_chat(session, get_llm(settings), student, body.message, partial, history)
    reply = result.get("question") or result.get("message") or ""
    history = (history + [{"role": "user", "text": body.message}, {"role": "bundle", "text": reply}])[-12:]
    chat_session.history_json = json.dumps(history)
    if result["type"] == "clarification":
        chat_session.draft_json = json.dumps(result.get("partial") or {})
    elif result["type"] == "draft":
        d = result["draft"]
        chat_session.draft_json = json.dumps({
            "product_key": d["product_key"], "wanted_item": d.get("wanted_item"),
            "variant": d["variant"], "any_variant": d["variant"] is None,
            "max_total": d["max_total"], "needed_by": d["needed_by"], "pickup": d["pickup"],
            "allow_switch": d["allow_switch"]})
    return {**result, "session_id": chat_session.id}


class IntentIn(BaseModel):
    student_id: int
    product_key: str | None = Field(None, description="A listed product; or leave empty and use wanted_item")
    wanted_item: str | None = Field(None, max_length=200,
                                    description="Something Bundle doesn't list yet; the agent watches for it")
    variant: str | None = None
    max_total: float = Field(gt=0, le=10_000, description="All-in cap in dollars (tax and fees included)")
    needed_by: date
    pickup: str | None = None
    allow_switch: bool = False
    permission_tier: str = Tier.AUTO_WITHIN_LIMITS
    raw_text: str = ""
    session_id: int | None = None


@router.post("/intents")
def approve_intent(body: IntentIn, session: Session = Depends(get_session),
                   settings: Settings = Depends(get_settings)):
    """The approval click: creates the request and its limits, then runs the agent."""
    student = _student(session, body.student_id)
    _require_verified(student)
    product, wanted = None, None
    if body.product_key:
        product = session.scalar(select(Product).where(Product.key == body.product_key))
        if product is None:
            raise HTTPException(404, "Unknown product")
        if body.variant is not None and body.variant not in product.variants:
            raise HTTPException(400, f"Type must be one of {product.variants} or null for any")
    else:
        wanted, hit = sanitize((body.wanted_item or "").strip())
        if not wanted:
            raise HTTPException(400, "Give a product_key, or wanted_item for something Bundle doesn't list yet")
        if hit:
            raise HTTPException(400, "The item description can't contain instructions")
    if body.permission_tier not in Tier.ALL:
        raise HTTPException(400, f"permission_tier must be one of {list(Tier.ALL)}")
    t = core.now(session)
    needed_by = datetime.combine(body.needed_by, time(23, 59))
    if needed_by <= t:
        raise HTTPException(400, "needed_by must be in the future")
    intent = Intent(
        student_id=student.id, product_id=product.id if product else None, wanted_text=wanted,
        variant=body.variant if product else None,
        max_total_cents=round(body.max_total * 100), needed_by=needed_by,
        pickup=body.pickup or student.pickup_default, allow_switch=body.allow_switch,
        permission_tier=body.permission_tier, status=IntentStatus.WATCHING,
        raw_text=body.raw_text, created_at=t,
    )
    session.add(intent)
    session.flush()
    core.audit(session, "student", "request_approved", student_id=student.id, intent_id=intent.id,
               item=intent.item_name, open_request=intent.is_open_request,
               cap=core.money(intent.max_total_cents), needed_by=needed_by.isoformat(),
               allow_switch=intent.allow_switch, permission_tier=intent.permission_tier)
    if body.session_id is not None:
        chat_session = session.get(ChatSession, body.session_id)
        if chat_session is not None and chat_session.student_id == student.id:
            chat_session.status = "CLOSED"  # the next message starts a fresh request
    run = runner.run_agent(session, settings, "INTENT_CONFIRMED", intent.id)
    return {"intent": intent_view(session, intent), "agent_run": run_view(run, names=student_names(session))}


@router.get("/intents/{intent_id}")
def get_intent(intent_id: int, session: Session = Depends(get_session)):
    intent = session.get(Intent, intent_id)
    if intent is None:
        raise HTTPException(404, "Request not found")
    return intent_view(session, intent)


@router.get("/students/{student_id}/intents")
def student_intents(student_id: int, session: Session = Depends(get_session)):
    _student(session, student_id)
    rows = session.scalars(select(Intent).where(Intent.student_id == student_id).order_by(Intent.id.desc()))
    return [intent_view(session, i) for i in rows]


@router.post("/intents/{intent_id}/cancel")
def cancel_intent(intent_id: int, session: Session = Depends(get_session),
                  settings: Settings = Depends(get_settings)):
    """The student can cancel any time before checkout. Nobody is charged."""
    intent = session.get(Intent, intent_id)
    if intent is None:
        raise HTTPException(404, "Request not found")
    if intent.status in (IntentStatus.FULFILLED, IntentStatus.CANCELLED, IntentStatus.EXPIRED):
        raise HTTPException(409, f"Request is already {intent.status}")
    run = None
    group_id = intent.group_id
    if group_id:
        group = session.get(Group, group_id)
        member = next(m for m in group.active_members if m.intent_id == intent.id)
        engine.remove_member(session, member, MemberStatus.DROPPED, "student cancelled",
                             actor="student", intent_status=IntentStatus.CANCELLED)
        run = runner.run_agent(session, settings, "BUYER_DROPOUT", group_id)
    else:
        intent.status = IntentStatus.CANCELLED
    core.audit(session, "student", "request_cancelled", student_id=intent.student_id, intent_id=intent.id)
    return {"intent": intent_view(session, intent),
            "agent_run": run_view(run, names=student_names(session)) if run else None}


# --------------------------------------------------------------------------
# Groups, manual joins and approvals (lower permission tiers)
# --------------------------------------------------------------------------

@router.get("/groups")
def list_groups(status: str | None = None, student_id: int | None = None,
                session: Session = Depends(get_session)):
    """Groups as a student sees them. With student_id, `mine` marks groups they are in."""
    stmt = select(Group).order_by(Group.id)
    if status:
        stmt = stmt.where(Group.status == status.upper())
    out = []
    for g in session.scalars(stmt):
        snap = engine.group_snapshot(g)
        if student_id is not None:
            snap["mine"] = any(m.student_id == student_id for m in g.active_members)
        out.append(snap)
    return out


@router.get("/groups/{group_id}")
def get_group(group_id: int, session: Session = Depends(get_session)):
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(404, "Group not found")
    return engine.group_snapshot(group)


def _student_action(session: Session, intent: Intent, tool: str, args: dict) -> dict:
    """A student's own action goes through the same tools and checks, logged as actor=student."""
    run = AgentRun(kind="STUDENT_ACTION", mode="STUDENT", started_at=core.now(session),
                   student_id=intent.student_id, intent_id=intent.id)
    session.add(run)
    session.flush()
    ctx = ToolContext(session, run, actor="student")
    result = ctx.call(tool, **args)
    extra = runner._settle(ctx)
    if result.get("refused"):
        done = result.get("reason", "")
    elif result.get("status") in ("joined", "created_and_joined"):
        verb = "joined the" if result["status"] == "joined" else "started a"
        done = (f"Bundle {verb} {result.get('product', '')} group at {result.get('price_per_buyer')} all-in. "
                f"You're buyer {result.get('buyers')} of {result.get('min_buyers')}.")
    else:
        done = "Done."
    run.message = " ".join(p for p in [done, extra] if p)
    run.group_id = min(ctx.touched_groups) if ctx.touched_groups else None
    session.flush()
    return {"result": result, "agent_run": run_view(run, names=student_names(session))}


class JoinIn(BaseModel):
    intent_id: int


@router.post("/groups/{group_id}/join")
def join_group(group_id: int, body: JoinIn, session: Session = Depends(get_session)):
    intent = session.get(Intent, body.intent_id)
    if intent is None:
        raise HTTPException(404, "Request not found")
    _require_verified(intent.student)
    out = _student_action(session, intent, "join_purchase_group", {"intent_id": intent.id, "group_id": group_id})
    if out["result"].get("refused"):
        raise HTTPException(409, out["result"]["reason"])
    return out


@router.get("/students/{student_id}/pending-actions")
def pending_actions(student_id: int, session: Session = Depends(get_session)):
    rows = session.scalars(select(PendingAction).where(PendingAction.student_id == student_id)
                           .order_by(PendingAction.id.desc()))
    return [{"pending_action_id": p.id, "intent_id": p.intent_id, "action": p.action, "summary": p.summary,
             "status": p.status, "created_at": p.created_at.isoformat()} for p in rows]


@router.post("/pending-actions/{action_id}/approve")
def approve_action(action_id: int, session: Session = Depends(get_session)):
    pending = session.get(PendingAction, action_id)
    if pending is None:
        raise HTTPException(404, "Not found")
    if pending.status not in ("PENDING", "SUGGESTED"):
        raise HTTPException(409, f"Already {pending.status}")
    intent = session.get(Intent, pending.intent_id)
    out = _student_action(session, intent, pending.action, pending.args)
    pending.status = "FAILED" if out["result"].get("refused") else "APPROVED"
    core.audit(session, "student", "pending_action_" + pending.status.lower(), student_id=pending.student_id,
               intent_id=pending.intent_id, pending_action_id=pending.id)
    return {"status": pending.status, **out}


@router.post("/pending-actions/{action_id}/reject")
def reject_action(action_id: int, session: Session = Depends(get_session)):
    pending = session.get(PendingAction, action_id)
    if pending is None:
        raise HTTPException(404, "Not found")
    pending.status = "REJECTED"
    core.audit(session, "student", "pending_action_rejected", student_id=pending.student_id,
               intent_id=pending.intent_id, pending_action_id=pending.id)
    return {"status": "REJECTED"}


# --------------------------------------------------------------------------
# Notifications, savings, orders
# --------------------------------------------------------------------------

@router.get("/students/{student_id}/notifications")
def notifications(student_id: int, session: Session = Depends(get_session)):
    _student(session, student_id)
    return [{"at": n.created_at.isoformat(), "message": n.message}
            for n in core.latest_notifications(session, student_id)]


@router.get("/students/{student_id}/savings")
def savings(student_id: int, session: Session = Depends(get_session)):
    _student(session, student_id)
    return savings_view(session, student_id)


@router.get("/orders/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return {"order_id": order.id, "group_id": order.group_id, "status": order.status,
            "quantity": order.quantity, "pickup_code": order.pickup_code,
            "supplier": order.offer.supplier.name}


@router.post("/orders/{order_id}/advance")
def advance_order(order_id: int, session: Session = Depends(get_session)):
    """Simulated fulfilment: ORDERED → SHIPPED → READY_FOR_PICKUP → COLLECTED."""
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    flow = OrderStatus.FLOW
    idx = flow.index(order.status)
    if idx == len(flow) - 1:
        raise HTTPException(409, "Order already collected")
    order.status = flow[idx + 1]
    group = session.get(Group, order.group_id)
    if order.status == OrderStatus.READY_FOR_PICKUP:
        for m in group.active_members:
            core.notify(session, m.student_id,
                        f"Your {group.product.name} is ready at {group.pickup}. Pickup code {order.pickup_code}.")
    core.audit(session, "system", "order_status", group_id=group.id, order_id=order.id, status=order.status)
    return {"order_id": order.id, "status": order.status}


# --------------------------------------------------------------------------
# Audit trail (operation logs for the execution-evidence deliverable)
# --------------------------------------------------------------------------

@router.get("/agent-runs")
def agent_runs(group_id: int | None = None, intent_id: int | None = None, limit: int = 50,
               session: Session = Depends(get_session)):
    stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)
    if group_id is not None:
        stmt = stmt.where(AgentRun.group_id == group_id)
    if intent_id is not None:
        stmt = stmt.where(AgentRun.intent_id == intent_id)
    names = student_names(session)
    return [run_view(r, names=names) for r in session.scalars(stmt)]


@router.get("/agent-runs/{run_id}")
def agent_run(run_id: int, session: Session = Depends(get_session)):
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run_view(run, names=student_names(session))


def _audit_rows(session: Session, event_type: str | None, limit: int) -> list[dict]:
    stmt = select(AuditEvent).order_by(AuditEvent.id).limit(limit)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    return [{"id": e.id, "ts": e.ts.isoformat(), "actor": e.actor, "event_type": e.event_type,
             "student_id": e.student_id, "intent_id": e.intent_id, "group_id": e.group_id,
             "run_id": e.run_id, "details": json.loads(e.details_json or "{}")}
            for e in session.scalars(stmt)]


@router.get("/audit")
def audit(event_type: str | None = None, limit: int = 500, session: Session = Depends(get_session)):
    return _audit_rows(session, event_type, limit)


@router.get("/audit/export")
def audit_export(format: str = "json", session: Session = Depends(get_session)):
    rows = _audit_rows(session, None, 100_000)
    names = student_names(session)
    runs = [run_view(r, names=names) for r in session.scalars(select(AgentRun).order_by(AgentRun.id))]
    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "ts", "actor", "event_type", "student_id", "intent_id", "group_id", "run_id", "details"])
        for r in rows:
            writer.writerow([r["id"], r["ts"], r["actor"], r["event_type"], r["student_id"], r["intent_id"],
                             r["group_id"], r["run_id"], json.dumps(r["details"])])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=bundle_audit_log.csv"})
    body = json.dumps({"exported_at": core.now(session).isoformat(), "audit_events": rows, "agent_runs": runs},
                      indent=2, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=bundle_operation_log.json"})


@router.post("/system/tick")
def system_tick(session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    """What a scheduler would call: process due deadlines and expire old requests."""
    runs = runner.tick(session, settings)
    names = student_names(session)
    return {"simulated_now": core.now(session).isoformat(), "agent_runs": [run_view(r, names=names) for r in runs]}
