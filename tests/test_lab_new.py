"""The six new Lab cases (walkthrough pages 6-8) plus verified emails."""
from tests.conftest import approve, tools_of


def test_two_groups_one_short_joins_cheaper(client):
    story = client.post("/api/lab/scenario/two-groups", json={"equal_prices": False}).json()
    by_supplier = {g["supplier"]: g for g in story["groups"]}
    assert story["joined_group_id"] == by_supplier["Staples"]["group_id"]  # $8.40 vs $9.09
    assert by_supplier["Staples"]["status"] == "FULFILLED"
    assert by_supplier["Target"]["status"] == "FORMING"  # left for other students' agents


def test_two_groups_equal_price_joins_sooner_deadline(client):
    story = client.post("/api/lab/scenario/two-groups", json={"equal_prices": True}).json()
    by_supplier = {g["supplier"]: g for g in story["groups"]}
    assert by_supplier["Target"]["price_per_buyer"] == by_supplier["Staples"]["price_per_buyer"]
    assert story["joined_group_id"] == by_supplier["Target"]["group_id"]  # 30h vs 40h


def test_better_deal_moves_whole_group_when_everyone_approved(client):
    story = client.post("/api/lab/scenario/better-deal", json={"everyone_allows_switch": True}).json()
    run = story["agent_runs"][0]
    assert tools_of(run)[:2] == ["check_better_deal", "move_whole_group"]
    group = story["groups"][0]
    assert group["supplier"] == "Ramen Direct"
    assert group["price_per_buyer"] == "$9.50"
    assert all(m["status"] == "ACTIVE" for m in group["members"])


def test_better_deal_stays_when_someone_did_not_approve_switching(client):
    story = client.post("/api/lab/scenario/better-deal", json={"everyone_allows_switch": False}).json()
    run = story["agent_runs"][0]
    assert "move_whole_group" not in tools_of(run)
    assert story["groups"][0]["supplier"] == "Jetro Cash & Carry"
    assert "staying in the current group" in run["message"]


def test_payment_fails_group_recovers_and_price_unchanged(client):
    story = client.post("/api/lab/scenario/payment-fails", json={}).json()
    group = story["groups"][0]
    assert group["status"] == "FORMING"
    assert group["buyers"] == 3 and group["min_buyers"] == 4
    assert story["price_before"] == story["price_after"] == "$12.75"
    failed = [m for m in group["members"] if m["status"] == "PAYMENT_FAILED"]
    assert len(failed) == 1
    assert "nobody was charged" in story["agent_runs"][0]["message"]
    events = client.get("/api/audit").json()
    assert not any(e["event_type"] == "purchase_completed" for e in events)


def test_over_budget_offer_is_never_mentioned(client):
    story = client.post("/api/lab/scenario/over-budget").json()
    run = story["agent_runs"][0]
    assert tools_of(run) == ["find_existing_groups", "find_offers", "keep_watching"]
    assert run["tool_calls"][1]["result"]["offers"] == []
    assert "$24" not in run["message"] and "Wayfair" not in run["message"]
    assert "Nothing under $20.00 yet" in run["message"]
    assert story["intent"]["status"] == "WATCHING"


def test_vague_request_asks_one_clarifying_question(client):
    out = client.post("/api/lab/scenario/vague-request").json()
    chat = out["chat"]
    assert chat["type"] == "clarification"
    assert "liquid or pods" in chat["question"]
    assert "total" in chat["question"] and "when you need it" in chat["question"]


def test_clarification_follow_up_completes_the_draft(client):
    alex = client.ids["students"]["alex"]
    first = client.post("/api/chat", json={"student_id": alex, "message": "I need detergent"}).json()
    second = client.post("/api/chat", json={"student_id": alex, "message": "pods, $20 max, by Thursday",
                                            "partial": first["partial"]}).json()
    assert second["type"] == "draft"
    assert second["draft"]["variant"] == "pods"
    assert second["draft"]["max_total"] == 20


def test_prompt_injection_in_chat_is_ignored_and_logged(client):
    alex = client.ids["students"]["alex"]
    approve(client, alex)
    out = client.post("/api/lab/scenario/prompt-injection", json={"channel": "chat"}).json()
    assert out["chat"]["type"] == "refusal"
    intent = client.get(f"/api/students/{alex}/intents").json()[0]
    assert intent["max_total"] == "$15.00"
    events = client.get("/api/audit?event_type=injection_ignored").json()
    assert events and events[-1]["details"]["source"] == "chat message"


def test_prompt_injection_in_supplier_text_is_ignored_and_logged(client):
    story = client.post("/api/lab/scenario/prompt-injection", json={"channel": "supplier"}).json()
    assert story["intent"]["group"]["supplier"] == "Costco Business Center"
    assert story["intent"]["group"]["price_per_buyer"] == "$12.75"
    events = client.get("/api/audit?event_type=injection_ignored").json()
    assert any("supplier offer" in e["details"]["source"] for e in events)
    for run in story["agent_runs"]:
        for call in run["tool_calls"]:
            assert "ShadyBulk" not in str(call["result"].get("supplier", ""))


def test_unverified_student_cannot_join(client):
    story = client.post("/api/lab/scenario/unverified-student").json()
    run = story["agent_runs"][0]
    assert run["tool_calls"][0]["allowed"] is False
    assert "verified campus email" in run["message"]
    assert story["groups"][0]["buyers"] == 2
    sam = client.ids["students"]["sam"]
    assert client.post("/api/chat", json={"student_id": sam, "message": "detergent"}).status_code == 403


def test_sign_up_requires_campus_email_and_verification(client):
    assert client.post("/api/students", json={"name": "X", "email": "x@gmail.com"}).status_code == 400
    out = client.post("/api/students", json={"name": "Nia Obi", "email": "nia.obi@cornell.edu"}).json()
    sid = out["student_id"]
    assert client.post(f"/api/students/{sid}/verify", json={"code": "000000x"}).status_code == 400
    assert client.post(f"/api/students/{sid}/verify",
                       json={"code": out["demo_verification_code"]}).json()["email_verified"] is True
