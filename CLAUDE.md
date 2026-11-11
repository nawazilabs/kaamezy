# Kaamezy — instructions for Claude Code

Kaamezy (कामईज़ी) is a prototype for the Paytm Build for India hackathon: an AI teammate that does real work safely. Every money step goes through a **deterministic guardrail** before it runs through **mock** partners, and a human gets a decision brief when they should. The LLM is used only where there is ambiguity (understanding the message, wording a reply); code decides everything that touches money.

It has two product areas:

1. **Merchant side** (built for the 31 Oct 2026 finals): a merchant's settlement problem, loan offers, shop insurance and device upgrades, in their language, with proactive outreach.
2. **Consumer side** (current focus: **loans and insurance automation**): a person buys **health insurance** and takes a **personal loan** in a Paytm-style app, with on-device document autofill and an AI assistant. Backend in `chaukas/consumer/`, UI in `web/`.

## Who is working on it

One person now (the three-member split of the finals build day is over). You may edit any file. The old "Member N owns this" labels in docstrings are historical; the old per-person plans are in `docs/archive/`. **Shared contracts** still deserve care: `chaukas/core/models.py`, `data/schema.sql`, `CONTRACTS.md`, `config/policy.yaml`, `requirements.txt`. Change them additively (new optional field, new enum value) and update `CONTRACTS.md` in the same change.

## Document map

| File | Read it when |
|------|-------------|
| `README.md` | First. What it is, how to run it, what is real and what is mock. |
| `CONTRACTS.md` | **Before writing code.** Shared models (C1), guardrail (C2), tools (C3), privacy (C4), merchant API (C5), **consumer API (C5b)**, repo layout (C6). |
| `data/schema.sql` · `data/consumer_schema.sql` | Before touching the DB. Merchant tables and consumer tables are separate files. |
| `config/*.yaml` | Every limit, threshold and fact lives here (`policy.yaml`, `consumer.yaml`, `consumer_kb.yaml`, `lender_sim.yaml`, `models.yaml`, ...). |
| `prompts/*.v1.txt` | Every LLM prompt (`intent`, `reply`, `handoff`, `proactive_notify`, `consumer_assistant`). |
| `COMPLIANCE.md` | Before anything user-facing, consent-, loan-, insurance- or PII-related. Maps RBI FREE-AI, RBI Digital Lending, IRDAI-aligned practice and the DPDP Act to concrete controls. |
| `DEMO_SCRIPT.md` | What must work on stage: the 5 merchant scenes, plus the consumer flows (appendix). |
| `CHAUKAS_EXPLAINED.md` | The design with the *why*, in Hinglish. §14 judge Q&A, §16 consumer flows. |
| `CHAUKAS_SYSTEM_DESIGN.md` | HLD/LLD background. Where it conflicts with `CONTRACTS.md`, **CONTRACTS wins**. |
| `CHAUKAS_PROBLEM_STATEMENT.md` | The problem, scope and acceptance metrics (has a scope update at the top). |
| `web/README.md` | The static frontend: flow, files, deploy. |
| `docs/archive/` | History only (finals-day plans, change logs). Not instructions. |

## Non-negotiable rules

1. **The guardrail is deterministic Python. Never use an LLM inside it.** No code path may execute an action without an ALLOW decision. The DBs also enforce it with triggers (`executions_require_allow`, `consumer_exec_require_allow`): never remove or bypass them.
2. **Money amounts come from the ledger / rule engine, never from the user's message, the client, or an LLM.** Amounts are `int` rupees, never `float`. Consumer premiums and loan figures come only from `chaukas/consumer/engine.py`; the client never sends an amount and the server recomputes and compares (`AMOUNT_MISMATCH`).
3. **BLOCK is final.** No confirmation (OTP, human, "yes") turns a BLOCK into an ALLOW.
4. **Never cache money state** (settlements, balances, payouts, quotes, guardrail decisions, auth tokens). `tests/test_cache_safety.py` fails on any module-level `cache`/`memo` container or new `lru_cache`.
5. **No raw PII** in logs, traces, audit tables, turns, handoffs or LLM prompts. Route it through `chaukas/privacy/` (`mask`, `mask_text`, `tokenise`, `scrub`). Only `chaukas/agents/action.py` may call `detokenise()`. The **Aadhaar number is never collected, stored or logged** (KYC is a DigiLocker mock: a verified flag + last 4 digits). PAN is format-checked and stored masked only.
6. **Audit and consent tables are append-only** (`audit_log`, `consents`, `consumer_audit`, `consumer_consents`): never write UPDATE or DELETE against them.
7. **Config lives in `config/*.yaml`, prompts in `prompts/*.v1.txt`.** Never hardcode limits, thresholds, facts or prompt text.
8. **No LangChain or LangGraph.** Plain Python, FastAPI, Pydantic v2. Every external service (payments, tickets, lender, insurer, DigiLocker) is a deterministic **mock**.
9. **Shared models live only in `chaukas/core/models.py`.** Never redefine `PlannedStep`, `Decision` or the others elsewhere. (Consumer request bodies live in `chaukas/consumer/models.py`; they are new, not redefinitions.)
10. In pitch-facing text never say "RBI-compliant" or "certified". Say "aligned with".
11. **The consumer assistant cannot act.** The model picks a topic and a language and words a short reply from `config/consumer_kb.yaml`; code masks PII first, stops prompt injection before any model call, rejects a reply that adds a number, a promise or personal data, and maps topic to on-screen action. The model never chooses an action. One extension, `consumer/prefill.py`: for a loan message with figures the model may copy out the **stated** fields (amount, tenure, income, EMI, age, employment, purpose) as a *suggestion for the form*. Code requires every number to appear in the person's own message, range-checks against `config/consumer.yaml`, drops (never clamps) what fails, leaves unstated fields empty, and the reply is knowledge-base text. Nothing is stored, no quote or credit check runs, and the person still taps "Check my offer"; the amount and decision come only from `engine.lender_decide` and the guardrail. The payment actions (`open_payment_help`, `open_bill_pay`, `open_spends`, `open_settlements`) are navigation or form-prefill only: `consumer/billpay.py` may suggest a biller and a listed recharge plan, never an amount for a bill and never an account number, and a payment needs the user's tap on `/pay/confirm`, decided by `guardrail.check_bill_payment`.
12. **OTP, human approval and KYC are checked on the server.** The browser never holds a flag that unlocks money. A human approval is a staff-written row bound to the exact quote and insurer.

## Working rules

- **Build in priority order**, test as you go, and keep changes small. After a demo or freeze is declared, bug fixes only.
- **Tests are the contract.** Add a test with every behaviour change. Full run: `python -m pytest` (about 2 minutes, ~840 tests). The suite includes project guards: cache safety, PII leak, fairness, red-team.
- **Be honest about what is real.** The rule engines, guardrails, audit, OCR parsing and LLM assistant are real code; partners, KYC, payouts, policy issue and loan disbursal are mocks. Do not let UI text or docs claim otherwise.
- **Git:** short task branches (`feat/…`), merge to `main` often, never force-push `main`, never commit `.env` or `data/*.db`. `ui/node_modules`, `ui/.wrangler` and `logs/` are untracked on purpose.
- Every skeleton file has a docstring stating purpose and contract: read it before editing.

## Commands

Run everything from the repo root; imports are absolute (`from chaukas.core.models import PlannedStep`).

- Install: `python -m pip install -r requirements.txt` (Python 3.11)
- Seed the merchant DB: `python -m data.seed` (writes `data/chaukas.db`; the consumer tables are created on first use)
- Run the API: `python -m uvicorn chaukas.main:app --reload` (port 8000)
- Run the frontend: `python -m http.server 8080 --directory web`, then open `http://localhost:8080` (on localhost it talks to `http://localhost:8000`; elsewhere use `?api=https://…`)
- Tests: `python -m pytest` · parser test: `node web/docparse.test.js`
- Eval: `python -m eval.run_eval`, then `python -m eval.redteam`
- Secrets in `.env` (copy `.env.example`): `SARVAM_API_KEY`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `CHAUKAS_STAFF_KEY`. Local consumer-API testing without login: `CHAUKAS_CONSUMER_AUTH=off`.

## Known gaps (keep these honest in the pitch)

- Only `SARVAM_API_KEY` is configured; without a working model the assistant and the merchant agents fall back to deterministic text (the response says so).
- The consumer UI has no offline mode: it needs the backend (Render free tier sleeps: wake it before a demo).
- "Demo: a human approves" uses a demo staff key from the browser; a real deployment sets `CHAUKAS_STAFF_KEY` and keeps it on the agent desk.
- All rule numbers (premium factors, lender thresholds, limits) are illustrative placeholders in `config/`.
