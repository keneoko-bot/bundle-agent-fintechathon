"""Group-buying engine: matching, joining, recovery and checkout.

Every function here that changes group membership or moves (simulated) money runs
its own code checks first and raises PolicyRefusal when the action falls outside
what the students approved. The agent, whether an LLM or the deterministic
fallback, can only act through these functions, so the rules hold either way.
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import core
from app.core import PolicyRefusal, money
from app.models import (
    Group,
    GroupMember,
    GroupStatus,
    Intent,
    IntentStatus,
    MemberStatus,
    Offer,
    Order,
    OrderStatus,
    SavingsEntry,
    SimulatedPayment,
    Tier,
)

NEW_GROUP_WINDOW = timedelta(hours=24)  # formation window for a new group


# --------------------------------------------------------------------------
# Small predicates
# --------------------------------------------------------------------------

def variant_ok(intent_variant: str | None, offer_variant: str | None) -> bool:
    return intent_variant is None or offer_variant is None or intent_variant == offer_variant


def offer_usable(offer: Offer) -> bool:
    return bool(offer.active and offer.supplier.active)


def delivery(offer: Offer) -> timedelta:
    return timedelta(hours=offer.delivery_hours or 24)


def ready_by(group: Group) -> datetime:
    """When the order would be ready for pickup if the group completes at its deadline."""
    return group.deadline + delivery(group.offer)


def arrives_in_time(intent: Intent, completion: datetime, offer: Offer) -> bool:
    return completion + delivery(offer) <= intent.needed_by


def group_snapshot(group: Group) -> dict:
    """What a student may see about a group. Never other students' names or budgets."""
    offer = group.offer
    return {
        "group_id": group.id,
        "product": group.product.name,
        "product_image": group.product.image,
        "variant": offer.variant,
        "supplier": offer.supplier.name,
        "supplier_location": offer.supplier.location,
        "brand": offer.brand,
        "price_per_buyer": money(offer.group_all_in),
        "price_cents": offer.group_all_in,
        "solo_price": money(offer.solo_all_in),
        # Transparent per-buyer breakdown: item + tax (+ any flat fee) = all-in.
        "breakdown": {
            "item": money(offer.group_unit_cents),
            "tax": money(round(offer.group_unit_cents * offer.tax_bp / 10_000)),
            "tax_pct": round(offer.tax_bp / 100, 2),
            "fee": money(offer.fee_cents),
            "fee_cents": offer.fee_cents,
            "all_in": money(offer.group_all_in),
        },
        "buyers": len(group.active_members),
        "min_buyers": offer.min_buyers,
        "deadline": group.deadline.isoformat(),
        "ready_by": ready_by(group).isoformat(),
        "delivery_hours": offer.delivery_hours,
        "pickup": group.pickup,
        "status": group.status,
    }


def _pickup_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


# --------------------------------------------------------------------------
# Finding options (read-only)
# --------------------------------------------------------------------------

def join_problems(session: Session, intent: Intent, group: Group) -> list[str]:
    """Every reason this intent may not join this group (empty list = allowed)."""
    t = core.now(session)
    offer = group.offer
    problems: list[str] = []
    if not intent.student.email_verified:
        problems.append("Only students with a verified campus email can join groups.")
    if intent.status != IntentStatus.WATCHING:
        problems.append(f"This request is not looking for a group (status {intent.status}).")
    if group.status != GroupStatus.FORMING:
        problems.append(f"Group {group.id} is not forming (status {group.status}).")
    if group.deadline <= t:
        problems.append(f"Group {group.id}'s deadline has passed.")
    if not offer_usable(offer):
        problems.append("The supplier's offer is not available.")
    if group.product_id != intent.product_id:
        problems.append("The group is for a different item.")
    if not variant_ok(intent.variant, offer.variant):
        problems.append("The group is for a different type of this item.")
    if offer.group_all_in > intent.max_total_cents:
        problems.append(
            f"The all-in price {money(offer.group_all_in)} is above the student's cap "
            f"{money(intent.max_total_cents)}."
        )
    if not arrives_in_time(intent, group.deadline, offer):
        problems.append("The group would not be ready before the student's deadline.")
    if group.pickup != intent.pickup:
        problems.append("The group uses a different pickup point.")
    if any(m.student_id == intent.student_id for m in group.active_members):
        problems.append("The student is already in this group.")
    if len(group.active_members) >= offer.min_buyers:
        problems.append("The group is already full.")
    return problems


def compatible_groups(session: Session, intent: Intent) -> list[Group]:
    """Open groups this intent could join, best first: cheapest, then soonest deadline."""
    if intent.product_id is None:  # open request: nothing to join until a listing matches
        return []
    stmt = select(Group).where(
        Group.status == GroupStatus.FORMING, Group.product_id == intent.product_id
    )
    groups = [g for g in session.scalars(stmt) if not join_problems(session, intent, g)]
    groups.sort(key=lambda g: (g.offer.group_all_in, g.deadline, g.id))
    return groups


def affordable_offers(session: Session, intent: Intent) -> list[Offer]:
    """Offers that could start a new group within the cap. Over-cap offers are never returned."""
    if intent.product_id is None:
        return []
    stmt = select(Offer).where(Offer.product_id == intent.product_id, Offer.active.is_(True))
    offers = [
        o
        for o in session.scalars(stmt)
        if o.supplier.active
        and variant_ok(intent.variant, o.variant)
        and o.group_all_in <= intent.max_total_cents
    ]
    offers.sort(key=lambda o: (o.group_all_in, o.min_buyers, o.id))
    return offers


def new_group_deadline(session: Session, intent: Intent, offer: Offer) -> datetime:
    return min(core.now(session) + NEW_GROUP_WINDOW, intent.needed_by - delivery(offer))


def skip_reason(session: Session, intent: Intent, group: Group) -> str | None:
    """A plain reason an open group doesn't fit, for the agent to explain to the student.

    Returns None when the group fits, or when it shouldn't be mentioned at all: never surface a
    group whose price is above the student's cap (no nudging past the budget).
    """
    offer = group.offer
    if not join_problems(session, intent, group):
        return None
    if (offer.group_all_in > intent.max_total_cents or group.status != GroupStatus.FORMING
            or group.deadline <= core.now(session) or not offer_usable(offer)
            or len(group.active_members) >= offer.min_buyers
            or any(m.student_id == intent.student_id for m in group.active_members)
            or not intent.student.email_verified):
        return None
    reasons = []
    if not variant_ok(intent.variant, offer.variant):
        reasons.append(f"it's for {offer.variant}, and you asked for {intent.variant}")
    if group.pickup != intent.pickup:
        reasons.append(f"its pickup is at {group.pickup}")
    if not arrives_in_time(intent, group.deadline, offer):
        reasons.append(f"it closes {core.fmt_dt(group.deadline)} and would be ready around "
                       f"{core.fmt_dt(ready_by(group))}, after your date ({core.fmt_date(intent.needed_by)})")
    return "; ".join(reasons) or None


def skipped_groups(session: Session, intent: Intent) -> list[dict]:
    if intent.product_id is None:
        return []
    stmt = select(Group).where(Group.status == GroupStatus.FORMING, Group.product_id == intent.product_id)
    out = []
    for g in session.scalars(stmt):
        reason = skip_reason(session, intent, g)
        if reason:
            snap = group_snapshot(g)
            out.append({"group_id": g.id, "supplier": snap["supplier"], "price_per_buyer": snap["price_per_buyer"],
                        "buyers": snap["buyers"], "min_buyers": snap["min_buyers"], "why_not": reason})
    return out


# --------------------------------------------------------------------------
# Open requests: items that weren't listed when the student asked
# --------------------------------------------------------------------------

def listing_routes(session: Session, intent: Intent, offer: Offer) -> tuple[list[Group], list[Offer]]:
    """If this open request were for the listed product: the groups it could join and the offers
    it could start a group with (within the cap, ready by the student's date)."""
    saved = intent.product_id
    with session.no_autoflush:
        intent.product_id = offer.product_id
        try:
            groups = compatible_groups(session, intent)
            t = core.now(session)
            offers = [o for o in affordable_offers(session, intent)
                      if new_group_deadline(session, intent, o) > t + timedelta(hours=1)]
        finally:
            intent.product_id = saved
    return groups, offers


def listing_problems(session: Session, intent: Intent, offer: Offer) -> list[str]:
    """Code checks before an open request may take a new listing. The AI only judges whether the
    listing is the item; everything about money, timing and eligibility is decided here."""
    problems: list[str] = []
    if not intent.student.email_verified:
        problems.append("Only students with a verified campus email can use group buying.")
    if intent.status != IntentStatus.WATCHING:
        problems.append(f"This request is not looking for a group (status {intent.status}).")
    if intent.product_id is not None:
        problems.append("This request is already for a listed item.")
    if not offer_usable(offer):
        problems.append("The supplier's offer is not available.")
    if offer.group_all_in > intent.max_total_cents:
        problems.append("The listing is above the student's cap.")
    if not problems:
        groups, offers = listing_routes(session, intent, offer)
        if not groups and not offers:
            problems.append("No group for this listing could be ready by the student's date.")
    return problems


def adopt_listing(session: Session, intent: Intent, offer: Offer, actor: str = "agent") -> None:
    """Turn an open request into a normal request for the listed product (limits unchanged)."""
    problems = listing_problems(session, intent, offer)
    if problems:
        raise PolicyRefusal(" ".join(problems))
    intent.product_id = offer.product_id
    intent.product = offer.product
    intent.variant = None
    session.flush()
    core.audit(session, actor, "open_request_matched", student_id=intent.student_id, intent_id=intent.id,
               wanted=intent.wanted_text, product=offer.product.name, offer_id=offer.id)


def unadopt_listing(session: Session, intent: Intent) -> None:
    """Undo adopt_listing if the follow-up join/start was refused, so the request keeps watching."""
    intent.product_id = None
    intent.product = None
    session.flush()


# --------------------------------------------------------------------------
# Membership changes (checked)
# --------------------------------------------------------------------------

def join_group(session: Session, intent: Intent, group: Group, actor: str = "agent") -> dict:
    problems = join_problems(session, intent, group)
    if problems:
        raise PolicyRefusal(" ".join(problems))
    member = GroupMember(
        group_id=group.id,
        intent_id=intent.id,
        student_id=intent.student_id,
        status=MemberStatus.ACTIVE,
        joined_at=core.now(session),
    )
    group.members.append(member)
    intent.status = IntentStatus.IN_GROUP
    intent.group_id = group.id
    session.flush()
    core.audit(
        session, actor, "joined_group",
        student_id=intent.student_id, intent_id=intent.id, group_id=group.id,
        price=money(group.offer.group_all_in),
    )
    snap = group_snapshot(group)
    snap["group_full"] = snap["buyers"] >= snap["min_buyers"]
    return snap


def create_group(session: Session, intent: Intent, offer: Offer, actor: str = "agent") -> tuple[Group, dict]:
    t = core.now(session)
    problems: list[str] = []
    if not intent.student.email_verified:
        problems.append("Only students with a verified campus email can start groups.")
    if intent.status != IntentStatus.WATCHING:
        problems.append(f"This request is not looking for a group (status {intent.status}).")
    if not offer_usable(offer):
        problems.append("The supplier's offer is not available.")
    if offer.product_id != intent.product_id or not variant_ok(intent.variant, offer.variant):
        problems.append("The offer is for a different item.")
    if offer.group_all_in > intent.max_total_cents:
        problems.append(
            f"The all-in price {money(offer.group_all_in)} is above the student's cap "
            f"{money(intent.max_total_cents)}."
        )
    deadline = new_group_deadline(session, intent, offer)
    if deadline <= t + timedelta(hours=1):
        problems.append("Too little time left before the student's deadline to form a group.")
    if problems:
        raise PolicyRefusal(" ".join(problems))
    group = Group(
        offer_id=offer.id,
        product_id=offer.product_id,
        variant=offer.variant,
        deadline=deadline,
        pickup=intent.pickup,
        status=GroupStatus.FORMING,
        created_at=t,
    )
    session.add(group)
    session.flush()
    core.audit(session, actor, "created_group", student_id=intent.student_id,
               intent_id=intent.id, group_id=group.id, offer_id=offer.id)
    return group, join_group(session, intent, group, actor)


def remove_member(
    session: Session,
    member: GroupMember,
    status: str,
    reason: str,
    actor: str = "agent",
    intent_status: str | None = None,
) -> None:
    """Take a member out of a group and put their request back in a sensible state."""
    member.status = status
    intent = member.intent
    intent.group_id = None
    if intent_status:
        intent.status = intent_status
    elif status == MemberStatus.PAYMENT_FAILED:
        intent.status = IntentStatus.PAYMENT_FAILED
    else:
        intent.status = (
            IntentStatus.WATCHING if core.now(session) < intent.needed_by else IntentStatus.EXPIRED
        )
    session.flush()
    core.audit(session, actor, "member_removed", student_id=member.student_id,
               intent_id=intent.id, group_id=member.group_id, new_status=status, reason=reason)


# --------------------------------------------------------------------------
# Recovery: switching supplier, moving a whole group, releasing members
# --------------------------------------------------------------------------

def _switch_problems(session: Session, intent: Intent, offer: Offer) -> list[str]:
    reasons = []
    if not intent.allow_switch:
        reasons.append("did not approve switching to another brand or supplier")
    if intent.permission_tier != Tier.AUTO_WITHIN_LIMITS:
        reasons.append("their permission level requires asking first")
    if not variant_ok(intent.variant, offer.variant):
        reasons.append("the offer is a different type")
    if offer.group_all_in > intent.max_total_cents:
        reasons.append("the price is above their cap")
    if not arrives_in_time(intent, core.now(session), offer):
        reasons.append("it would arrive after their deadline")
    return reasons


def evaluate_recovery_options(session: Session, group: Group) -> list[dict]:
    """Other offers for the same item, and which current members could move to each."""
    stmt = select(Offer).where(Offer.product_id == group.product_id, Offer.active.is_(True))
    options = []
    for offer in session.scalars(stmt):
        if offer.id == group.offer_id or not offer.supplier.active:
            continue
        eligible, excluded = [], []
        for m in group.active_members:
            reasons = _switch_problems(session, m.intent, offer)
            if reasons:
                excluded.append({"member_id": m.id, "reasons": reasons})
            else:
                eligible.append(m.id)
        options.append({
            "offer_id": offer.id,
            "supplier": offer.supplier.name,
            "brand": offer.brand,
            "price_per_buyer": money(offer.group_all_in),
            "price_cents": offer.group_all_in,
            "min_buyers": offer.min_buyers,
            "eligible_members": len(eligible),
            "excluded_members": excluded,
            "viable": len(eligible) >= offer.min_buyers,
        })
    options.sort(key=lambda o: (not o["viable"], o["price_cents"], o["min_buyers"]))
    return options


def switch_supplier(session: Session, group: Group, offer: Offer, actor: str = "agent",
                    require_full: bool = True) -> dict:
    """Recovery switch: members who approved switching move; anyone else is released.

    At a deadline the switch only makes sense if the group is then full (require_full).
    """
    if group.status != GroupStatus.FORMING:
        raise PolicyRefusal(f"Group {group.id} is not forming.")
    if not offer_usable(offer) or offer.product_id != group.product_id:
        raise PolicyRefusal("That offer is not available for this item.")
    eligible, excluded = [], []
    for m in group.active_members:
        reasons = _switch_problems(session, m.intent, offer)
        (excluded if reasons else eligible).append((m, reasons))
    if require_full and len(eligible) < offer.min_buyers:
        raise PolicyRefusal(
            f"Only {len(eligible)} member(s) approved this switch; the offer needs {offer.min_buyers}."
        )
    old_offer = group.offer
    for m, reasons in excluded:
        remove_member(session, m, MemberStatus.RELEASED, "switch not approved: " + "; ".join(reasons), actor)
        core.notify(
            session, m.student_id,
            f"Your {group.product.name} group switched supplier, but you weren't moved because you "
            f"{reasons[0]}. Your request is back to watching; nobody charged you.",
        )
    group.offer_id = offer.id
    group.offer = offer
    group.variant = offer.variant
    session.flush()
    core.audit(session, actor, "switched_supplier", group_id=group.id,
               from_offer=old_offer.id, to_offer=offer.id, price=money(offer.group_all_in),
               released_members=[m.id for m, _ in excluded])
    for m, _ in eligible:
        core.notify(
            session, m.student_id,
            f"Your {group.product.name} group switched to {offer.supplier.name} at "
            f"{money(offer.group_all_in)} all-in, which is within your cap.",
        )
    snap = group_snapshot(group)
    snap["group_full"] = snap["buyers"] >= snap["min_buyers"]
    snap["released_members"] = len(excluded)
    return snap


def better_deal_problems(session: Session, group: Group, offer: Offer) -> list[str]:
    """Rule for a better deal appearing after joining: move only if it works for everyone."""
    current = group.offer
    problems = []
    if group.status != GroupStatus.FORMING:
        problems.append("The group is not forming.")
    if not offer_usable(offer) or offer.product_id != group.product_id:
        problems.append("The offer is not for this item.")
    if offer.group_all_in >= current.group_all_in:
        problems.append("The new offer is not cheaper than the current one.")
    if offer.min_buyers > current.min_buyers:
        problems.append("The new offer needs more buyers, so the group would be harder to fill.")
    for m in group.active_members:
        reasons = _switch_problems(session, m.intent, offer)
        if reasons:
            problems.append(f"A member {reasons[0]}.")
    return problems


def move_whole_group(session: Session, group: Group, offer: Offer, actor: str = "agent") -> dict:
    problems = better_deal_problems(session, group, offer)
    if problems:
        raise PolicyRefusal("Staying in the current group: " + " ".join(problems))
    return switch_supplier(session, group, offer, actor, require_full=False)


def release_over_cap(session: Session, group: Group, actor: str = "agent") -> list[int]:
    released = []
    price = group.offer.group_all_in
    for m in list(group.active_members):
        if price > m.intent.max_total_cents:
            remove_member(session, m, MemberStatus.RELEASED, "price now above cap", actor)
            core.notify(
                session, m.student_id,
                f"The {group.product.name} price rose to {money(price)}, above your cap, so Bundle "
                f"took you out of the group. Nobody charged you and your request is back to watching.",
            )
            released.append(m.id)
    return released


def members_over_cap(group: Group) -> int:
    price = group.offer.group_all_in
    return sum(1 for m in group.active_members if price > m.intent.max_total_cents)


# --------------------------------------------------------------------------
# Checkout (the only place simulated money moves)
# --------------------------------------------------------------------------

def execute_purchase(session: Session, group: Group, actor: str = "agent") -> dict:
    offer = group.offer
    members = group.active_members
    problems = []
    if group.status != GroupStatus.FORMING:
        problems.append(f"Group {group.id} is not forming.")
    if not offer_usable(offer):
        problems.append("The supplier's offer is not available.")
    if len(members) < offer.min_buyers:
        problems.append(f"The group has {len(members)} of {offer.min_buyers} buyers.")
    for m in members:
        if offer.group_all_in > m.intent.max_total_cents:
            problems.append("The price is above a member's cap.")
        if not m.student.email_verified:
            problems.append("A member's email is not verified.")
        if m.intent.status != IntentStatus.IN_GROUP:
            problems.append("A member's request is not active in this group.")
    if problems:
        raise PolicyRefusal(" ".join(sorted(set(problems))))

    t = core.now(session)
    price = offer.group_all_in

    # Phase 1: check every card first, so nobody is charged unless everyone can pay.
    failing = [m for m in members if m.student.card_fails]
    if failing:
        for m in failing:
            session.add(SimulatedPayment(
                group_id=group.id, student_id=m.student_id, intent_id=m.intent_id,
                amount_cents=price, status="FAILED", created_at=t, note="card declined (simulated)",
            ))
            remove_member(session, m, MemberStatus.PAYMENT_FAILED, "payment failed at checkout")
            core.notify(
                session, m.student_id,
                f"Your payment for {group.product.name} didn't go through, so you were taken out of "
                f"the group. Update your card and send your request again.",
            )
        remaining = group.active_members
        for m in remaining:
            core.notify(
                session, m.student_id,
                f"One payment failed, so the {group.product.name} order didn't go through and nobody "
                f"was charged. Your group is now {len(remaining)}/{offer.min_buyers} and Bundle is "
                f"finding another buyer. Your price stays {money(price)}.",
            )
        core.audit(session, actor, "payment_failed", group_id=group.id,
                   failed_members=[m.id for m in failing], price_unchanged=money(price))
        snap = group_snapshot(group)
        snap["status_detail"] = "payment_failed"
        snap["failed_buyers"] = len(failing)
        return {"status": "payment_failed", "group": snap}

    # Phase 2: charge everyone the same per-buyer price from the offer.
    order = Order(group_id=group.id, offer_id=offer.id, quantity=len(members),
                  status=OrderStatus.ORDERED, pickup_code=_pickup_code(), created_at=t)
    session.add(order)
    session.flush()
    for m in members:
        session.add(SimulatedPayment(
            group_id=group.id, student_id=m.student_id, intent_id=m.intent_id,
            amount_cents=price, status="SUCCEEDED", created_at=t, note="simulated charge",
        ))
        saved = max(offer.solo_all_in - price, 0)
        session.add(SavingsEntry(
            student_id=m.student_id, intent_id=m.intent_id, order_id=order.id,
            item=f"{group.product.name} · {offer.supplier.name}",
            paid_cents=price, solo_cents=offer.solo_all_in, saved_cents=saved, created_at=t,
        ))
        m.intent.status = IntentStatus.FULFILLED
        core.notify(
            session, m.student_id,
            f"Done: {group.product.name} from {offer.supplier.name} for {money(price)} all-in, "
            f"saving {money(saved)} versus buying alone. Pickup at {group.pickup}, code {order.pickup_code}.",
        )
    group.status = GroupStatus.FULFILLED
    group.completed_at = t
    session.flush()
    core.audit(session, actor, "purchase_completed", group_id=group.id, order_id=order.id,
               buyers=len(members), price=money(price), total=money(price * len(members)))
    return {
        "status": "completed",
        "order_id": order.id,
        "pickup_code": order.pickup_code,
        "price_per_buyer": money(price),
        "saved_per_buyer": money(max(offer.solo_all_in - price, 0)),
        "buyers": len(members),
    }


def cancel_group(session: Session, group: Group, reason: str, actor: str = "agent") -> dict:
    if group.status != GroupStatus.FORMING:
        raise PolicyRefusal(f"Group {group.id} is not forming.")
    for m in list(group.active_members):
        remove_member(session, m, MemberStatus.RELEASED, reason, actor)
        intent = m.intent
        tail = (
            f" Bundle will keep watching until {core.fmt_date(intent.needed_by)}."
            if intent.status == IntentStatus.WATCHING else ""
        )
        core.notify(
            session, m.student_id,
            f"Your {group.product.name} group was cancelled ({reason}). Nobody was charged.{tail}",
        )
    group.status = GroupStatus.CANCELLED
    group.completed_at = core.now(session)
    session.flush()
    core.audit(session, actor, "group_cancelled", group_id=group.id, reason=reason)
    return {"status": "cancelled", "group_id": group.id, "reason": reason, "charged": 0}


# --------------------------------------------------------------------------
# Recruiting: fill a group from other students' saved requests
# --------------------------------------------------------------------------

def find_more_buyers(session: Session, group: Group, actor: str = "agent") -> list[int]:
    """Join watching requests (whose owners let the agent act) to this group, oldest first."""
    joined: list[int] = []
    stmt = (
        select(Intent)
        .where(Intent.status == IntentStatus.WATCHING, Intent.product_id == group.product_id)
        .order_by(Intent.created_at, Intent.id)
    )
    for intent in session.scalars(stmt):
        if len(group.active_members) >= group.offer.min_buyers:
            break
        if intent.permission_tier != Tier.AUTO_WITHIN_LIMITS:
            continue
        if join_problems(session, intent, group):
            continue
        join_group(session, intent, group, actor)
        core.notify(
            session, intent.student_id,
            f"Bundle found a {group.product.name} group for you at "
            f"{money(group.offer.group_all_in)} all-in and joined it.",
        )
        joined.append(intent.id)
    return joined


# --------------------------------------------------------------------------
# Time-based housekeeping
# --------------------------------------------------------------------------

def due_groups(session: Session) -> list[Group]:
    t = core.now(session)
    stmt = select(Group).where(Group.status == GroupStatus.FORMING, Group.deadline <= t)
    return list(session.scalars(stmt))


def expire_intents(session: Session) -> list[int]:
    t = core.now(session)
    stmt = select(Intent).where(Intent.status == IntentStatus.WATCHING, Intent.needed_by < t)
    expired = []
    for intent in session.scalars(stmt):
        intent.status = IntentStatus.EXPIRED
        core.notify(session, intent.student_id,
                    f"Your request for {intent.item_name} expired without a match. Nobody charged you.")
        expired.append(intent.id)
    session.flush()
    return expired
