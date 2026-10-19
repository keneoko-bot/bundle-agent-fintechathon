"""Replays the captured walkthrough run end to end (deterministic fallback mode)."""
from tests.conftest import approve, tools_of


def test_chat_extracts_alex_request(client):
    alex = client.ids["students"]["alex"]
    out = client.post("/api/chat", json={
        "student_id": alex,
        "message": "I need detergent by Friday. Any brand is fine, but keep my total under $15."}).json()
    assert out["type"] == "draft"
    d = out["draft"]
    assert d["product_key"] == "laundry-detergent"
    assert d["max_total"] == 15
    assert d["needed_by"] == "2026-10-16"
    assert d["pickup"] == "Bloomberg Center Lobby"
    assert d["allow_switch"] is True
    assert "any brand or supplier" in out["approval_card"]["text"]
    assert "all-in" in out["approval_card"]["text"]


def test_full_walkthrough(client):
    alex = client.ids["students"]["alex"]
    detergent_group = client.ids["groups"]["detergent"]

    # Step 3: one approval, the agent joins the existing 2/4 group.
    out = approve(client, alex)
    run = out["agent_run"]
    assert run["mode"] == "DETERMINISTIC_FALLBACK"
    assert tools_of(run)[:3] == ["find_existing_groups", "join_purchase_group", "get_group_status"]
    assert "You're buyer 3 of 4" in run["message"]
    assert out["intent"]["group"]["group_id"] == detergent_group
    assert out["intent"]["group"]["buyers"] == 3

    # Steps 4-5: nobody else comes; at the deadline the agent switches supplier and completes.
    story = client.post("/api/lab/trigger-deadline", json={"group_id": detergent_group}).json()
    run = story["agent_runs"][0]
    assert tools_of(run) == ["get_group_status", "evaluate_recovery_options", "switch_group_supplier",
                             "execute_simulated_purchase"]
    assert "$13.91/buyer, saving $4.09" in run["message"]
    group = story["groups"][0]
    assert group["status"] == "FULFILLED"
    assert group["supplier"] == "FreshBulk Co"
    assert group["price_per_buyer"] == "$13.91"

    # Step 7: savings land at checkout, for all three buyers.
    savings = client.get(f"/api/students/{alex}/savings").json()
    newest = savings["orders"][-1]  # Alex also has two earlier orders in the demo data
    assert newest["paid"] == "$13.91"
    assert newest["solo_price"] == "$18.00"
    assert newest["saved"] == "$4.09"
    assert newest["saved_pct"] == 22.7
    assert savings["total_saved"] == "$14.65"  # $5.56 + $5.00 earlier, plus $4.09
    for key in ("jordan", "priya"):
        assert client.get(f"/api/students/{client.ids['students'][key]}/savings").json()["total_saved"] == "$4.09"

    # Step 6: fulfilment states and a pickup code.
    order_id = newest["order_id"]
    statuses = [client.post(f"/api/orders/{order_id}/advance").json()["status"] for _ in range(3)]
    assert statuses == ["SHIPPED", "READY_FOR_PICKUP", "COLLECTED"]
    assert len(client.get(f"/api/orders/{order_id}").json()["pickup_code"]) == 6


def test_savings_only_after_checkout(client):
    alex = client.ids["students"]["alex"]
    before = len(client.get(f"/api/students/{alex}/savings").json()["orders"])
    approve(client, alex)
    assert len(client.get(f"/api/students/{alex}/savings").json()["orders"]) == before


def test_audit_export(client):
    approve(client, client.ids["students"]["alex"])
    data = client.get("/api/audit/export").json()
    assert any(e["event_type"] == "request_approved" for e in data["audit_events"])
    assert data["agent_runs"][0]["tool_calls"]
    csv_text = client.get("/api/audit/export?format=csv").text
    assert csv_text.startswith("id,ts,actor,event_type")
