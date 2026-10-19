# Bundle

A purchasing agent for university students. A student says what they need, their all-in budget
and their deadline. The agent pools them with other students who want the same item to unlock a
supplier's group price, and charges them only when the order completes, within their limits.

Built for the 2026 FinTechathon (International Track, theme "AI as a Financial Participant").
**No real money moves:** payments are simulated ledger rows.

## Quick start

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- **The app:** http://127.0.0.1:8000
- **The API (interactive):** http://127.0.0.1:8000/docs

The database (`bundle.db`) is created and seeded with demo data on first start. Click **Reset demo**
in the app (or `POST /api/lab/reset`) to go back to the starting state; delete `bundle.db` after
pulling changes to the demo data.

Run the tests:

```bash
pytest
```

## Using the app

- **Ask Bundle (chat):** type what you need. Bundle asks one question if something is missing
  (type, budget, date), remembers the conversation ("actually make it $20"), then shows an
  approval card. Approving creates the request with its limits and runs the agent; its reply and
  every step it took appear in the chat.
- **Items Bundle doesn't list yet:** ask for anything ("a mini fridge under $120 by next Friday").
  If it isn't listed, the request becomes an *open request*: your agent watches every new listing,
  the AI judges whether it's what you asked for, and code checks your cap and date. Then it follows
  your permission (join, ask first, or suggest). If it isn't sure a listing matches, it always asks.
- **Acting as:** switch between demo students to see the app from their side.
- **My requests:** status, group progress, pickup codes, and cancel (before checkout, nobody is charged).
- **Groups:** every open group, with buttons to add a matching student, trigger the deadline, make a
  buyer drop out or raise the price.
- **Savings:** order history and total saved, in dollars and as a percentage.
- **Lab:** move simulated time, run the test scenarios, or post a supplier deal (for a listed
  product or a brand-new one) and watch waiting requests react.
- **Agent activity:** every agent run with its tool calls and refusals, plus the operation-log export.

### Demo data

| Item | Starting state |
|---|---|
| Laundry Detergent (liquid) | EcoClean group at 2/4 ($12.75); FreshBulk backup needs 3 ($13.91). The walkthrough. |
| Instant Ramen | NoodleHouse group at 2/4 ($11.00) |
| Coffee Beans | Island Roasters group at 3/4 ($23.00): one more buyer completes it |
| Winter Jacket (small/medium/large) | NorthPeak group at 2/5 ($89.00); Summit backup needs 3 ($95.00) |
| Toilet Paper | PantryPal group at 2/3 ($19.00) |
| Jasmine Rice | GrainHouse group at 1/3 ($23.00) |
| Laundry Detergent (pods), Paper Towels, Printer Paper | Offers but no open group: the agent starts one |
| Dish Soap | $9.00 offer; Mia is waiting with an $8.50 cap (post a cheaper deal in the Lab) |
| LED Desk Lamp | Only a $24.00 offer (used by the over-budget case) |

Alex Chen (student 1) is the default user and has two earlier orders in his savings tab. Sam
Rivera hasn't verified his email. All people, suppliers and prices are fictional.

## Agent modes

| Mode | When | Label in the audit trail |
|---|---|---|
| Deterministic fallback | No API key set, or the LLM call fails | `DETERMINISTIC_FALLBACK` |
| LLM tool calling | `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` set | `REAL_LLM (provider/model)` |

Copy `.env.example` to `.env` (never commit `.env`) and set:

```
OPENAI_API_KEY=sk-...
BUNDLE_LLM_MODEL=gpt-4.1-mini
```

Any OpenAI-compatible service also works (DeepSeek, Qwen, or a local Ollama model): set
`BUNDLE_LLM_BASE_URL`, its key in `OPENAI_API_KEY` and its model in `BUNDLE_LLM_MODEL`. See
`.env.example`.

Both modes use the same tools, and every limit is enforced in code inside those tools. The LLM can
understand, plan and explain, but it cannot move money outside what the student approved.

## The walkthrough via the API

```bash
B=http://127.0.0.1:8000/api
curl -X POST $B/lab/reset
# 1. Alex describes what he needs (keep the returned session_id for follow-ups)
curl -X POST $B/chat -H 'content-type: application/json' \
  -d '{"student_id":1,"message":"I need detergent by Friday. Any brand is fine, but keep my total under $15."}'
# 2. Alex approves the request card once: the agent joins the open 2/4 group
curl -X POST $B/intents -H 'content-type: application/json' \
  -d '{"student_id":1,"product_key":"laundry-detergent","max_total":15,"needed_by":"2026-10-16","allow_switch":true}'
# 3. Nobody else comes: at the deadline the agent switches supplier and completes
curl -X POST $B/lab/trigger-deadline -H 'content-type: application/json' -d '{"group_id":1}'
# 4. Savings tab: order history, total saved in $ and %
curl $B/students/1/savings
# 5. Operation log for the execution-evidence deliverable
curl -o bundle_operation_log.json "$B/audit/export?format=json"
```

## Simulation Lab

All under `/api/lab` (also on the app's Lab tab). Each returns the scenario, the agent run(s) with
every tool call, and a line to say when demoing.

| Endpoint | What it shows |
|---|---|
| `POST /reset` | Back to the starting scenario |
| `POST /add-compatible-student` | A matching student appears; a full group checks out immediately |
| `POST /trigger-deadline` | Deadline arrives; the agent switches supplier or cancels and charges nobody |
| `POST /disable-supplier` | Removes a supplier (set-up only; see `scenario/backup-supplier-gone`) |
| `POST /buyer-dropout` | A committed buyer leaves; the agent re-runs and keeps recruiting |
| `POST /raise-price` | The supplier raises its price; every cap is re-checked |
| `POST /advance-clock` | Moves simulated time forward and processes due deadlines |
| `POST /add-offer` | A supplier posts a deal, for a listed product or a new one (`new_product_name`); waiting requests, open groups and open requests react |
| `POST /scenario/two-groups` | Two groups each one buyer short: joins the cheaper one; equal prices → sooner deadline |
| `POST /scenario/better-deal` | A cheaper offer appears after joining: stays, or moves the whole group if it works for everyone |
| `POST /scenario/payment-fails` | One payment fails at checkout: nobody is charged, the group recovers, nobody's price rises |
| `POST /scenario/over-budget` | Only an over-cap offer exists: the agent never mentions it and keeps watching |
| `POST /scenario/vague-request` | "I need detergent": the agent asks one clarifying question |
| `POST /scenario/prompt-injection` | Instructions in a chat message or supplier text are ignored and logged |
| `POST /scenario/unverified-student` | A student without a verified campus email can't join |
| `POST /scenario/backup-supplier-gone` | A 3-buyer group hits its deadline just as its backup supplier stops selling: safe cancel |
| `POST /scenario/open-request` | `{"case": "auto"}` an unlisted item is listed later and the agents act on their own; `"ask"` the student's permission says ask first; `"unsure"` a near-match (50" vs 55" TV) always asks |

## How it works

```
chat ──► extract (LLM or rule-based, with conversation memory) ──► approval card ──► request + limits
                                                                                          │
                       ┌──────────────────────────────────────────────────────────────────┘
                       ▼
   agent runner (LLM tool calling, or deterministic fallback)
        │  tools: find_existing_groups, join_purchase_group, find_offers, create_purchase_group,
        │         evaluate_recovery_options, switch_group_supplier, check_better_deal,
        │         move_whole_group, execute_simulated_purchase, cancel_group, find_more_buyers,
        │         check_listing_match (AI: is this new listing the item?), take_listing, ...
        ▼
   engine (code checks on every action) ──► simulated payments, orders, savings, audit log
```

- `app/engine.py`: matching, joining, recovery and checkout. Every function that changes a group
  or moves simulated money checks the student's limits first and refuses otherwise.
- `app/agent/tools.py`: the agent's tools; every call is logged with its arguments, result and
  any refusal.
- `app/agent/runner.py`: runs the LLM or the fallback for an event (request approved, deadline,
  dropout, price change, new offer), then applies system rules (a full group checks out at once).
- `app/agent/workflows.py`: the deterministic fallback.
- `app/agent/extract.py`: chat to request draft, clarifying questions, refusing instructions.
- `app/agent/match.py`: the AI judgement for open requests (is a new listing what the student
  asked for: yes, unsure or no), with a keyword fallback. The AI never sees prices or caps.
- `app/agent/safety.py`: detects instruction-like text in untrusted input.
- `app/api.py`, `app/lab.py`: HTTP routes. `app/seed.py`: demo data. `app/static/`: the front end
  (plain HTML, CSS and JavaScript, no build step).

### Rules the agent follows (enforced in code)

1. The cap is the **all-in total** (tax and fees included). Nothing above it is joined, suggested or mentioned.
2. Join the **cheapest** compatible group; if prices are equal, the one with the **sooner deadline**.
3. If no group fits, start one with the cheapest offer within the cap; otherwise keep watching.
4. A full group checks out immediately; nobody is charged before that.
5. At the deadline: switch to the cheapest viable offer, moving **only members who approved any
   brand or supplier**; if nothing is viable, cancel and charge nobody.
6. A better deal after joining: **stay**, unless it is the same item, cheaper for every member,
   everyone approved switching and it is no harder to fill, in which case **move the whole group**.
7. A failed payment means nobody is charged; the group recovers like a dropout and **nobody's price rises**.
8. Only students with a **verified campus email** can request or join.
9. Chat and supplier text can never change limits; attempts are refused and logged.
10. For an item that wasn't listed: the agent acts on a new listing only if the AI judges it a match,
    it fits the cap and date, and the student's permission allows it. An unsure match always asks;
    an over-cap listing is never judged or mentioned.

### Permission tiers

Chosen per request on the approval card:

| Tier | The agent may |
|---|---|
| `suggest_only` | Find options and suggest them; the student joins themselves |
| `approve_each_step` | Prepare joins and new groups; each needs the student's approval |
| `auto_within_limits` (default) | Join, start groups, switch (if approved) and complete purchases within the cap and deadline |

## API overview

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/students` · `/api/students/{id}/verify` | Sign up with a campus email · verify the code |
| POST | `/api/chat` | Message → clarifying question, refusal, or a draft request card (send back `session_id`) |
| POST | `/api/intents` | Approve a request (creates the limits) and run the agent. Use `product_key`, or `wanted_item` for something not listed yet |
| POST | `/api/intents/{id}/cancel` | Cancel before checkout |
| GET | `/api/students/{id}/intents` · `/notifications` · `/savings` · `/pending-actions` | Student views |
| POST | `/api/pending-actions/{id}/approve` · `/reject` | Approve or reject an action (lower tiers) |
| GET | `/api/groups` | Groups (`?status=FORMING`, `?student_id=` marks the student's own) |
| POST | `/api/groups/{id}/join` | A student joins a group themselves |
| POST | `/api/orders/{id}/advance` | Simulated fulfilment: ordered → shipped → ready → collected |
| GET | `/api/agent-runs` · `/api/audit` · `/api/audit/export` | Audit trail and operation-log export (JSON or CSV) |
| POST | `/api/system/tick` | What a scheduler calls: process due deadlines |

## Tests

`pytest` runs the walkthrough end to end, every Lab case, the permission tiers, the all-in cap,
switch approval, chat memory, supplier deals, the front end being served, and the LLM path's
guardrails, using a scripted fake model so no key is needed.

## Deployment

The app is a single FastAPI service with SQLite, serving both the API and the front end. Run it
locally as above, or in a container:

```bash
docker build -t bundle .
docker run -p 8000:8000 bundle
```

## License

MIT. See `LICENSE`. All students, suppliers and prices in the demo data are fictional.
