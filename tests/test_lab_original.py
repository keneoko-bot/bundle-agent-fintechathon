"""The original Simulation Lab buttons from the walkthrough."""
from tests.conftest import approve, tools_of


def _group(client, group_id):
    return next(g for g in client.get("/api/lab/groups").json() if g["group_id"] == group_id)


def test_add_compatible_student_twice_completes_without_waiting(client):
    gid = client.ids["groups"]["detergent"]
    first = client.post("/api/lab/add-compatible-student", json={"group_id": gid}).json()
    assert first["groups"][0]["buyers"] == 3
    second = client.post("/api/lab/add-compatible-student", json={"group_id": gid}).json()
    run = second["agent_runs"][0]
    assert "execute_simulated_purchase" in tools_of(run)
    assert second["groups"][0]["status"] == "FULFILLED"
    assert "completed the purchase" in run["message"]


def test_disable_backup_supplier_then_deadline_cancels_and_charges_nobody(client):
    gid = client.ids["groups"]["detergent"]
    approve(client, client.ids["students"]["alex"])  # 3/4
    client.post("/api/lab/disable-supplier", json={"supplier_name": "FreshBulk Co"})
    story = client.post("/api/lab/trigger-deadline", json={"group_id": gid}).json()
    run = story["agent_runs"][0]
    assert tools_of(run)[-1] == "cancel_group"
    assert story["groups"][0]["status"] == "CANCELLED"
    assert "Nobody was charged" in run["message"]
    events = client.get("/api/audit").json()
    assert not any(e["event_type"] == "purchase_completed" for e in events)


def test_buyer_dropout_keeps_group_forming(client):
    gid = client.ids["groups"]["detergent"]
    approve(client, client.ids["students"]["alex"])  # 3/4
    story = client.post("/api/lab/buyer-dropout", json={"group_id": gid}).json()
    assert story["groups"][0]["buyers"] == 2
    assert story["groups"][0]["status"] == "FORMING"
    assert "dropped out" in story["agent_runs"][0]["message"]


def test_price_rise_switches_when_backup_fits_everyone(client):
    gid = client.ids["groups"]["detergent"]
    approve(client, client.ids["students"]["alex"])  # 3/4 at $12.75
    story = client.post("/api/lab/raise-price", json={"group_id": gid, "amount_cents": 400}).json()
    run = story["agent_runs"][0]
    assert "switch_group_supplier" in tools_of(run)
    group = story["groups"][0]
    assert group["supplier"] == "FreshBulk Co"
    assert group["status"] == "FULFILLED"  # 3 buyers meets FreshBulk's minimum


def test_price_rise_with_no_alternative_releases_everyone(client):
    gid = client.ids["groups"]["detergent"]  # 2/4, FreshBulk needs 3 so it isn't viable
    story = client.post("/api/lab/raise-price", json={"group_id": gid, "amount_cents": 400}).json()
    run = story["agent_runs"][0]
    assert "release_members_over_cap" in tools_of(run)
    assert story["groups"][0]["status"] == "CANCELLED"
    assert "Nobody was charged" in run["message"]


def test_small_price_rise_within_caps_continues(client):
    gid = client.ids["groups"]["detergent"]
    story = client.post("/api/lab/raise-price", json={"group_id": gid, "amount_cents": 100}).json()
    assert "still within every member's cap" in story["agent_runs"][0]["message"]
    assert story["groups"][0]["status"] == "FORMING"


def test_advance_clock_runs_deadlines(client):
    approve(client, client.ids["students"]["alex"])
    out = client.post("/api/lab/advance-clock", json={"hours": 25}).json()
    assert out["agent_runs"][0]["kind"] == "FORMATION_DEADLINE"
