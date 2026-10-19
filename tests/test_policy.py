"""Permission tiers, switching approval, all-in caps and the LLM path's guardrails."""
from tests.conftest import ScriptedLLM, approve


def test_all_in_cap_includes_tax(client):
    # EcoClean's group price is $11.78 before tax but $12.75 all-in: a $12.00 cap must not join.
    alex = client.ids["students"]["alex"]
    out = approve(client, alex, max_total=12.0)
    assert out["intent"]["group"] is None
    assert out["intent"]["status"] == "WATCHING"


def test_suggest_only_tier_never_acts(client):
    alex = client.ids["students"]["alex"]
    out = approve(client, alex, permission_tier="suggest_only")
    assert out["intent"]["status"] == "WATCHING"
    pending = client.get(f"/api/students/{alex}/pending-actions").json()
    assert pending[0]["status"] == "SUGGESTED"
    joined = client.post(f"/api/pending-actions/{pending[0]['pending_action_id']}/approve").json()
    assert joined["status"] == "APPROVED"
    assert client.get(f"/api/intents/{out['intent']['intent_id']}").json()["status"] == "IN_GROUP"


def test_approve_each_step_tier_asks_first(client):
    alex = client.ids["students"]["alex"]
    out = approve(client, alex, permission_tier="approve_each_step")
    assert "needs your approval" in out["agent_run"]["message"]
    pending = client.get(f"/api/students/{alex}/pending-actions").json()[0]
    assert pending["status"] == "PENDING"
    client.post(f"/api/pending-actions/{pending['pending_action_id']}/reject")
    assert client.get(f"/api/intents/{out['intent']['intent_id']}").json()["status"] == "WATCHING"


def test_switch_only_moves_members_who_approved(client):
    from app.db import SessionLocal
    from app.models import Group, Offer, Supplier

    gid = client.ids["groups"]["ramen"]
    # A third ramen buyer who did NOT approve switching, plus a backup offer needing only 2.
    alex = client.ids["students"]["alex"]
    approve(client, alex, product_key="instant-ramen", max_total=12, needed_by="2026-10-20", allow_switch=False)
    with SessionLocal() as s:
        group = s.get(Group, gid)
        assert len(group.active_members) == 3
        sup = Supplier(name="Backup Noodles")
        s.add(sup)
        s.flush()
        s.add(Offer(supplier_id=sup.id, product_id=group.product_id, brand="Backup", solo_unit_cents=1478,
                    group_unit_cents=970, min_buyers=2, created_at=group.created_at))
        s.commit()
    before = len(client.get(f"/api/students/{alex}/savings").json()["orders"])
    story = client.post("/api/lab/trigger-deadline", json={"group_id": gid}).json()
    group = story["groups"][0]
    assert group["status"] == "FULFILLED"
    assert group["supplier"] == "Backup Noodles"
    statuses = {m["student"]: m["status"] for m in group["members"]}
    assert statuses["Alex Chen"] == "RELEASED"
    assert len(client.get(f"/api/students/{alex}/savings").json()["orders"]) == before  # not charged


def test_manual_join_still_checks_cap(client):
    alex = client.ids["students"]["alex"]
    out = approve(client, alex, max_total=12.0, permission_tier="suggest_only")
    resp = client.post(f"/api/groups/{client.ids['groups']['detergent']}/join",
                       json={"intent_id": out["intent"]["intent_id"]})
    assert resp.status_code == 409
    assert "above the student's cap" in resp.json()["detail"]


def _two_open_towel_groups():
    """Two paper-towel groups with 1 of 3 buyers each, so joining doesn't complete either."""
    from datetime import timedelta

    from sqlalchemy import select

    from app import core
    from app.db import SessionLocal
    from app.lab import _seat, spawn_intent, spawn_student
    from app.models import Group, Offer, Product

    with SessionLocal() as s:
        towels = s.scalar(select(Product).where(Product.key == "paper-towels"))
        ids = []
        for offer in s.scalars(select(Offer).where(Offer.product_id == towels.id).order_by(Offer.id)):
            g = Group(offer_id=offer.id, product_id=towels.id, deadline=core.now(s) + timedelta(hours=30),
                      pickup="Bloomberg Center Lobby", status="FORMING", created_at=core.now(s))
            s.add(g)
            s.flush()
            _seat(s, g, spawn_intent(s, spawn_student(s), towels, cap_cents=1000))
            ids.append(g.id)
        s.commit()
        return ids


def test_llm_cannot_pick_the_worse_group(client, use_llm):
    _two_open_towel_groups()

    def script(ctx, execute):
        iid = ctx["request"]["intent_id"]
        groups = execute("find_existing_groups", {"intent_id": iid})["groups"]
        assert len(groups) == 2
        worse = execute("join_purchase_group", {"intent_id": iid, "group_id": groups[-1]["group_id"]})
        assert worse.get("refused") and "not the best option" in worse["reason"]
        execute("join_purchase_group", {"intent_id": iid, "group_id": groups[0]["group_id"]})
        return "Joined the cheaper group."

    use_llm(ScriptedLLM(script))
    out = approve(client, client.ids["students"]["alex"], product_key="paper-towels", max_total=10,
                  needed_by="2026-10-17")
    run = out["agent_run"]
    assert run["mode"] == "REAL_LLM (fake/scripted)"
    assert [c["allowed"] for c in run["tool_calls"]] == [True, False, True]
    assert out["intent"]["group"]["supplier"] == "PaperPlus"  # the cheaper one


def test_llm_cannot_buy_a_group_that_is_not_full(client, use_llm):
    def script(ctx, execute):
        iid = ctx["request"]["intent_id"]
        groups = execute("find_existing_groups", {"intent_id": iid})["groups"]
        execute("join_purchase_group", {"intent_id": iid, "group_id": groups[0]["group_id"]})
        result = execute("execute_simulated_purchase", {"group_id": groups[0]["group_id"]})
        assert result.get("refused")
        return "Joined; waiting for one more buyer."

    use_llm(ScriptedLLM(script))
    out = approve(client, client.ids["students"]["alex"])
    run = out["agent_run"]
    assert run["mode"] == "REAL_LLM (fake/scripted)"
    refused = [c for c in run["tool_calls"] if not c["allowed"]]
    assert refused and refused[0]["tool"] == "execute_simulated_purchase"
    assert out["intent"]["group"]["status"] == "FORMING"


def test_llm_outage_falls_back_and_is_labelled(client, use_llm):
    use_llm(ScriptedLLM(fail=True))
    out = approve(client, client.ids["students"]["alex"])
    run = out["agent_run"]
    assert run["mode"] == "DETERMINISTIC_FALLBACK (LLM error)"
    assert "simulated provider outage" in run["llm_error"]
    assert out["intent"]["group"]["buyers"] == 3


def test_llm_that_stops_early_gets_completed_by_fallback(client, use_llm):
    use_llm(ScriptedLLM(lambda ctx, execute: "I'll think about it."))
    out = approve(client, client.ids["students"]["alex"])
    assert out["agent_run"]["mode"].endswith("+ fallback completion")
    assert out["intent"]["group"]["buyers"] == 3


def test_llm_extraction_flags_instructions(client, use_llm):
    use_llm(ScriptedLLM(extraction={"contains_instructions": True}))
    alex = client.ids["students"]["alex"]
    out = client.post("/api/chat", json={"student_id": alex, "message": "please be flexible with my limit"}).json()
    assert out["type"] == "refusal"


def test_cancel_before_checkout_charges_nothing(client):
    alex = client.ids["students"]["alex"]
    out = approve(client, alex)
    resp = client.post(f"/api/intents/{out['intent']['intent_id']}/cancel").json()
    assert resp["intent"]["status"] == "CANCELLED"
    assert client.get(f"/api/groups/{client.ids['groups']['detergent']}").json()["buyers"] == 2


# --- found in Kene's testing: a price rise left an over-cap member in a group, which then filled ---

def test_lazy_ai_after_price_rise_still_releases_over_cap_members(client, use_llm):
    from tests.conftest import ScriptedLLM

    def lazy(ctx, execute):  # the model looks at the group and stops without acting
        if "group" in ctx:
            execute("get_group_status", {"group_id": ctx["group"]["group_id"]})
        return "Noted."
    use_llm(ScriptedLLM(script=lazy))
    gid = 1  # detergent group from the seed
    before = client.get(f"/api/groups/{gid}").json()["buyers"]
    out = client.post("/api/lab/raise-price", json={"group_id": gid, "amount_cents": 400}).json()
    run = out["agent_runs"][0]
    assert "fallback completion" in run["mode"]
    group = client.get(f"/api/groups/{gid}").json()
    assert group["buyers"] < before  # members whose cap is now exceeded were taken out, uncharged


def test_full_group_with_an_over_cap_member_releases_them_instead_of_stalling(client):
    from app.db import SessionLocal
    from app.models import Group

    with SessionLocal() as session:
        group = session.get(Group, 1)
        group.active_members[0].intent.max_total_cents = group.offer.group_all_in - 100  # now over cap
        session.commit()
    for _ in range(2):
        client.post("/api/lab/add-compatible-student", json={"group_id": 1})
    group = client.get("/api/groups/1").json()
    assert group["status"] == "FORMING"  # not charged, not stuck: the over-cap member was taken out
    admin = client.get("/api/lab/groups").json()
    g1 = next(g for g in (admin if isinstance(admin, list) else admin["groups"]) if g["group_id"] == 1)
    assert any(m["status"] == "RELEASED" for m in g1["members"])
