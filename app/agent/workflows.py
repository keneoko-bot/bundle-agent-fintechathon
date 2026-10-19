"""Deterministic fallback workflows: the same tools in a fixed, sensible order.

Used when no LLM key is set or an LLM call fails. Each workflow returns the
message shown to the student; every step is logged through ToolContext.call.
"""
from __future__ import annotations

from sqlalchemy import select

from app import core, engine
from app.agent.tools import ToolContext
from app.core import money
from app.models import Group, GroupStatus, Intent, IntentStatus, Offer, Tier


def _timing(group: Group) -> str:
    return (f"It closes {core.fmt_dt(group.deadline)} and should be ready for pickup around "
            f"{core.fmt_dt(engine.ready_by(group))}.")


def _skipped_note(intent: Intent, skipped: list[dict]) -> str:
    if not skipped:
        return ""
    first = skipped[0]
    lead = (f"There's an open {intent.product.name} group ({first['supplier']}, {first['buyers']}/"
            f"{first['min_buyers']} buyers), but {first['why_not']}.")
    if len(skipped) > 1:
        lead += f" {len(skipped) - 1} other open group(s) don't fit either."
    return lead + " "


def intent_confirmed(ctx: ToolContext, intent_id: int) -> str:
    intent = ctx.session.get(Intent, intent_id)
    if intent is None:
        return "Request not found."
    if intent.status != IntentStatus.WATCHING:
        return f"Your request is {intent.status.lower().replace('_', ' ')}."
    if intent.is_open_request:
        return open_request_confirmed(ctx, intent)

    found = ctx.call("find_existing_groups", intent_id=intent.id)
    if found.get("refused"):
        return f"Bundle can't act on this request yet: {found['reason']}"
    groups = found.get("groups") or []
    skipped_note = _skipped_note(intent, found.get("skipped") or [])
    if groups:
        best = groups[0]
        joined = ctx.call("join_purchase_group", intent_id=intent.id, group_id=best["group_id"])
        status = joined.get("status")
        if status == "joined":
            group = ctx.session.get(Group, best["group_id"])
            ctx.call("get_group_status", group_id=best["group_id"])
            if joined.get("group_full"):
                return (f"Bundle joined this group for you. You're buyer {joined['buyers']} of "
                        f"{joined['min_buyers']}.")
            ctx.call("find_more_buyers", group_id=best["group_id"])
            return (f"Bundle joined this group for you. You're buyer {joined['buyers']} of "
                    f"{joined['min_buyers']}. {_timing(group)} Your agent is finding more buyers if needed.")
        if status == "needs_approval":
            return f"Bundle found a group and needs your approval: {joined['summary']}"
        if status == "suggested":
            return f"Suggestion: {joined['summary']} Join it if you'd like."

    offers = (ctx.call("find_offers", intent_id=intent.id).get("offers") or [])
    if offers:
        best = offers[0]
        created = ctx.call("create_purchase_group", intent_id=intent.id, offer_id=best["offer_id"])
        status = created.get("status")
        if status == "created_and_joined":
            group = ctx.session.get(Group, created["group_id"])
            ctx.call("find_more_buyers", group_id=created["group_id"])
            opener = "So Bundle" if skipped_note else "No open group fit, so Bundle"
            return (f"{skipped_note}{opener} started a new group with {created['supplier']} at "
                    f"{created['price_per_buyer']} all-in. It needs {created['min_buyers']} buyers. "
                    f"{_timing(group)}")
        if status == "needs_approval":
            return f"{skipped_note}Bundle wants to start a group and needs your approval: {created['summary']}"
        if status == "suggested":
            return f"{skipped_note}Suggestion: {created['summary']}"

    ctx.call("keep_watching", intent_id=intent.id)
    return (f"{skipped_note}Nothing else under {money(intent.max_total_cents)} fits yet. Bundle will keep "
            f"watching until {core.fmt_date(intent.needed_by)}." if skipped_note else
            f"Nothing under {money(intent.max_total_cents)} yet. Bundle will keep watching until "
            f"{core.fmt_date(intent.needed_by)}.")


TIER_PROMISE = {
    Tier.AUTO_WITHIN_LIMITS: "join or start a group for you right away",
    Tier.APPROVE_EACH_STEP: "ask you before joining",
    Tier.SUGGEST_ONLY: "suggest it to you",
}


def open_request_confirmed(ctx: ToolContext, intent: Intent) -> str:
    watched = ctx.call("keep_watching", intent_id=intent.id)
    if watched.get("refused"):
        return f"Bundle can't act on this request yet: {watched['reason']}"
    return (f"“{intent.wanted_text}” isn't listed on Bundle yet, so your agent is watching for it until "
            f"{core.fmt_date(intent.needed_by)}. When a matching deal within {money(intent.max_total_cents)} "
            f"is listed, it will {TIER_PROMISE.get(intent.permission_tier, 'ask you first')}. If it isn't sure "
            f"a listing is what you asked for, it will ask.")


def listing_match(ctx: ToolContext, intent_id: int, offer_id: int) -> str:
    """A new listing appeared: is it what this open request asked for, and what may the agent do?"""
    intent, offer = ctx.session.get(Intent, intent_id), ctx.session.get(Offer, offer_id)
    if intent is None or offer is None or not intent.is_open_request or intent.status != IntentStatus.WATCHING:
        return "Nothing to do: the request is no longer waiting for a new listing."
    check = ctx.call("check_listing_match", intent_id=intent.id, offer_id=offer.id)
    if check.get("refused") or check.get("error"):
        return f"Bundle couldn't check the new listing: {check.get('reason') or check.get('error')}"
    if check["match"] == "no":
        return (f"A new listing appeared, but it isn't what you asked for (“{intent.wanted_text}”). "
                f"Bundle keeps watching.")
    took = ctx.call("take_listing", intent_id=intent.id, offer_id=offer.id)
    status = took.get("status")
    if status in ("joined", "created_and_joined"):
        group = ctx.session.get(Group, took["group_id"])
        verb = "joined a group" if status == "joined" else "started a group"
        msg = (f"Found it: {offer.product.name} from {offer.supplier.name} was just listed. {check['reason']} "
               f"Your permission lets Bundle act within your limits, so it {verb} at {took['price_per_buyer']} "
               f"all-in. You're buyer {took['buyers']} of {took['min_buyers']}. {_timing(group)}")
        if not took.get("group_full"):
            ctx.call("find_more_buyers", group_id=group.id)
            msg += " Your agent is finding more buyers."
        return msg
    if status == "needs_approval":
        lead = ("Bundle isn't sure it's what you asked for: " if took.get("why_asking", "").startswith("not sure")
                else "")
        return (f"{offer.product.name} from {offer.supplier.name} was just listed at {check['price_per_buyer']} "
                f"all-in, within your cap. {lead}{check['reason']} It needs your approval before joining "
                f"(see Approvals).")
    if status == "suggested":
        return (f"Suggestion: {offer.product.name} from {offer.supplier.name} was just listed at "
                f"{check['price_per_buyer']} all-in, within your cap. {check['reason']}")
    return f"Bundle couldn't use the new listing: {took.get('reason', 'it was refused')}"


def formation_deadline(ctx: ToolContext, group_id: int) -> str:
    group = ctx.session.get(Group, group_id)
    if group is None or group.status != GroupStatus.FORMING:
        return "Nothing to do: the group is no longer forming."
    status = ctx.call("get_group_status", group_id=group.id)
    if status["buyers"] >= status["min_buyers"]:
        result = ctx.call("execute_simulated_purchase", group_id=group.id)
        if result.get("status") == "completed":
            return (f"The group filled, so Bundle completed the simulated purchase: "
                    f"{result['price_per_buyer']}/buyer, saving {result['saved_per_buyer']} vs. buying alone.")

    options = ctx.call("evaluate_recovery_options", group_id=group.id).get("options") or []
    viable = [o for o in options if o["viable"]]
    if viable:
        best = viable[0]
        switched = ctx.call("switch_group_supplier", group_id=group.id, offer_id=best["offer_id"])
        if not switched.get("refused") and switched.get("group_full"):
            result = ctx.call("execute_simulated_purchase", group_id=group.id)
            if result.get("status") == "completed":
                return (f"Your original group didn't fill, so Bundle switched to a viable offer from "
                        f"{best['supplier']} that still met your budget and deadline, and completed the "
                        f"simulated purchase: {result['price_per_buyer']}/buyer, saving "
                        f"{result['saved_per_buyer']} vs. buying alone.")
            return "Bundle switched supplier, but a payment failed, so nobody was charged and it is finding another buyer."

    ctx.call("cancel_group", group_id=group.id,
             reason="it didn't fill by the deadline and no other offer fit everyone's limits")
    return ("The group didn't fill by its deadline and no other offer fit everyone's limits, so Bundle "
            "cancelled it. Nobody was charged.")


def buyer_dropout(ctx: ToolContext, group_id: int) -> str:
    group = ctx.session.get(Group, group_id)
    if group is None or group.status != GroupStatus.FORMING:
        return "Nothing to do: the group is no longer forming."
    status = ctx.call("get_group_status", group_id=group.id)
    if status["deadline_passed"]:
        return formation_deadline(ctx, group_id)
    filled = ctx.call("find_more_buyers", group_id=group.id)
    if filled.get("group_full"):
        return "A buyer dropped out, but Bundle found a replacement from saved requests."
    return (f"A buyer dropped out, so the group is {filled['buyers']}/{filled['min_buyers']}. Bundle keeps "
            f"looking until the deadline; if it still can't fill, it will try other suppliers or cancel "
            f"and charge nobody.")


def price_change(ctx: ToolContext, group_id: int) -> str:
    group = ctx.session.get(Group, group_id)
    if group is None or group.status != GroupStatus.FORMING:
        return "Nothing to do: the group is no longer forming."
    status = ctx.call("get_group_status", group_id=group.id)
    if status["members_over_cap"] == 0:
        return (f"The price changed to {status['price_per_buyer']}, which is still within every member's "
                f"cap, so the group continues.")
    options = ctx.call("evaluate_recovery_options", group_id=group.id).get("options") or []
    viable = [o for o in options if o["viable"]]
    if viable:
        best = viable[0]
        switched = ctx.call("switch_group_supplier", group_id=group.id, offer_id=best["offer_id"])
        if not switched.get("refused"):
            return (f"The new price was above some members' caps, so Bundle switched to {best['supplier']} "
                    f"at {best['price_per_buyer']}, which fits everyone who approved switching.")
    released = ctx.call("release_members_over_cap", group_id=group.id)
    if released.get("buyers", 0) == 0:
        ctx.call("cancel_group", group_id=group.id, reason="the price rose above every member's cap")
        return "The price rose above every member's cap, so Bundle cancelled the group. Nobody was charged."
    ctx.call("find_more_buyers", group_id=group.id)
    return (f"The new price is above some members' caps, so they were taken out and nobody was charged. "
            f"The group continues at {released['price_per_buyer']}.")


def rescan_watching(ctx: ToolContext, product_id: int) -> int:
    """Give every watching request for this item another look. The tools act for "act within limits"
    and ask or suggest for the other permission levels."""
    stmt = (select(Intent)
            .where(Intent.status == IntentStatus.WATCHING, Intent.product_id == product_id)
            .order_by(Intent.created_at, Intent.id))
    acted = 0
    for intent in list(ctx.session.scalars(stmt)):
        if intent.status != IntentStatus.WATCHING:
            continue
        groups = ctx.call("find_existing_groups", intent_id=intent.id).get("groups") or []
        if groups:
            if ctx.call("join_purchase_group", intent_id=intent.id,
                        group_id=groups[0]["group_id"]).get("status") == "joined":
                acted += 1
            continue
        offers = ctx.call("find_offers", intent_id=intent.id).get("offers") or []
        if offers and ctx.call("create_purchase_group", intent_id=intent.id,
                               offer_id=offers[0]["offer_id"]).get("status") == "created_and_joined":
            acted += 1
    return acted


def new_offer(ctx: ToolContext, offer_id: int) -> str:
    offer = ctx.session.get(Offer, offer_id)
    if offer is None:
        return "Offer not found."
    stmt = select(Group).where(Group.status == GroupStatus.FORMING, Group.product_id == offer.product_id,
                               Group.offer_id != offer.id)
    notes = []
    for group in list(ctx.session.scalars(stmt)):
        check = ctx.call("check_better_deal", group_id=group.id, offer_id=offer.id)
        if check.get("move_whole_group"):
            moved = ctx.call("move_whole_group", group_id=group.id, offer_id=offer.id)
            if not moved.get("refused"):
                notes.append(f"A better deal appeared, so Bundle moved the whole group to "
                             f"{offer.supplier.name} at {money(offer.group_all_in)}, cheaper for everyone.")
                continue
        notes.append(f"A cheaper offer appeared, but Bundle is staying in the current group: "
                     f"{check.get('reasons', ['rule not met'])[0]}")
    acted = rescan_watching(ctx, offer.product_id)
    if acted:
        notes.append(f"It also matched {acted} waiting request(s) to groups.")
    return " ".join(notes) or "No open groups or waiting requests were affected."
