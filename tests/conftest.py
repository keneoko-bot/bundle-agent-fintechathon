from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import clear_llm_override, set_llm_override
from app.config import Settings
from app.main import create_app

TEST_SETTINGS = Settings(
    database_url="sqlite://",
    llm_provider="none",
    llm_model=None,
    openai_api_key=None,
    anthropic_api_key=None,
    campus_domains=("cornell.edu",),
)


class ScriptedLLM:
    """A fake LLM for tests: `script(context, execute)` plays the model's part."""

    provider = "fake"
    model = "scripted"

    def __init__(self, script=None, extraction: dict | None = None, fail: bool = False):
        self.script, self.extraction, self.fail = script, extraction, fail

    def run_tools(self, system, user, tools, execute, max_steps=8):
        if self.fail:
            raise RuntimeError("simulated provider outage")
        return self.script(json.loads(user), execute)

    def extract_json(self, system, user):
        self.last_prompt = user
        if self.fail or self.extraction is None:
            raise RuntimeError("no extraction scripted")
        return self.extraction


@pytest.fixture
def client():
    set_llm_override(None)  # deterministic fallback unless a test plugs in a fake LLM
    app = create_app(TEST_SETTINGS)
    with TestClient(app) as c:
        c.ids = c.post("/api/lab/reset").json()["ids"]
        yield c
    clear_llm_override()


@pytest.fixture
def use_llm():
    def _use(llm):
        set_llm_override(llm)
    return _use


def approve(client, student_id: int, **overrides) -> dict:
    body = {"student_id": student_id, "product_key": "laundry-detergent", "variant": None, "max_total": 15,
            "needed_by": "2026-10-16", "allow_switch": True}
    body.update(overrides)
    resp = client.post("/api/intents", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def tools_of(run: dict) -> list[str]:
    return [c["tool"] for c in run["tool_calls"]]
