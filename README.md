# Kaamezy (कामईज़ी)

An AI teammate for Paytm that does real work **safely**. It understands a person in their language, fixes the problem or completes the purchase, and runs every money step through a **deterministic guardrail** (plain code, no LLM) before anything executes through **mock** partners. When a human should decide, it hands them a brief.

Built for the Paytm Build for India hackathon.

> **Honest status:** the rule engines, guardrails, audit trail, OCR parsing and the LLM assistant are real code. Partners (lender, insurer, payouts, DigiLocker) are mocks, and every number in `config/` is an illustrative placeholder. Nothing here moves real money or issues a real policy.

## What it does

| Area | What a user can do | Where |
|------|--------------------|-------|
| **Consumer: health insurance** | Describe the family, autofill from a PAN/Aadhaar photo (read **on the device**), compare 3 insurers priced by a rule engine, buy through the guardrail (KYC, consent, premium check, OTP over ₹30,000, human underwriter for risky cases) | `chaukas/consumer/`, `web/` |
| **Consumer: personal loan** | Check an offer from a simulated lender (age, income, EMIs only), read the Key Facts Statement, give two consents, get a guardrail-checked mock disbursal. A policy holder can file a (simulated) claim and, once it is approved and its payout is pending, take a short advance against it (My claims); the personal loan never looks at the policy | `chaukas/consumer/`, `web/` |
| **AI assistant** | Ask about claims, waiting periods, EMI, charges, privacy in Hindi, Hinglish or English. The model picks a topic and words a reply from a knowledge base; code checks the reply and chooses any on-screen action | `chaukas/consumer/assistant.py` |
| **Merchant: support + sales** | Settlement problems, loan offers, shop insurance, device upgrades, proactive outreach on a failed settlement, agent-desk handoff | `chaukas/agents/`, `chaukas/core/`, `chaukas/policy/` |

## The idea in one picture

```
person ──► [LLM: understand + word the reply]        ambiguity only
              │
              ▼
        [code: rule engine / lender]   amounts come ONLY from here
              │
              ▼
        [code: GUARDRAIL]  ALLOW · NEEDS_CONFIRMATION (OTP / human) · BLOCK (final)
              │ ALLOW only
              ▼
        [mock partner]  +  append-only audit log  +  DB trigger: no execution without an ALLOW row
```

Why it holds up: no prompt can talk the guardrail into ALLOW, an amount can never come from the client or the model, and the database itself refuses an execution without an ALLOW decision or an edit to the audit log. Details and the judge Q&A: `CHAUKAS_EXPLAINED.md`.

## Run it

Python 3.11 and (for one test) Node.

```bash
python -m pip install -r requirements.txt
cp .env.example .env                       # add SARVAM_API_KEY; see below
python -m data.seed                        # merchant demo data -> data/chaukas.db
python -m uvicorn chaukas.main:app --reload            # API on http://localhost:8000
python -m http.server 8080 --directory web             # frontend on http://localhost:8080
```

Open `http://localhost:8080`. On localhost the frontend talks to `http://localhost:8000`; deployed, pass `?api=https://your-backend`.

For local work without Supabase login, start the API with `CHAUKAS_CONSUMER_AUTH=off` (everyone is one demo user). The sign-in page itself uses the Supabase project in `web/index.html`.

```bash
python -m pytest                           # ~840 tests, about 2 minutes
node web/docparse.test.js                  # ID-card text parser
python -m eval.run_eval && python -m eval.redteam      # scorecard + red-team
```

### Environment (`.env`)

| Variable | Used for |
|----------|----------|
| `SARVAM_API_KEY`, `GEMINI_API_KEY` | LLM routing (`config/models.yaml`); without a working model the agents fall back to deterministic text |
| `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | the server checks the browser's sign-in token (the publishable key is the public client key) |
| `CHAUKAS_CONSUMER_AUTH` | `supabase` (default when `SUPABASE_URL` is set) or `off` (local only) |
| `CHAUKAS_STAFF_KEY` | agent-desk key that approves a human review; the demo falls back to `demo-staff`, set your own when deployed |
| `CHAUKAS_DB_PATH`, `CHAUKAS_REPLAY_MODE` | database file; record/replay of LLM answers |

## Repo map

```
chaukas/consumer/    health insurance + personal loan: models, engine, guardrail, store, auth, assistant, api
chaukas/agents/      merchant agents: intent, context, guardrail, action, handoff, reply, notify
chaukas/core/        shared models, orchestrator, plans, proactive event watcher
chaukas/policy/      merchant guardrail engine (10 checks)         chaukas/privacy/  mask, tokenise, scrub
chaukas/integrations mock partners (payments, tickets, lender, insurer, devices)
config/              every limit and fact: policy, consumer, consumer_kb, lender_sim, models, ...
prompts/             every LLM prompt, versioned (*.v1.txt)
data/                schema.sql (merchant), consumer_schema.sql, fixtures, golden + red-team sets, seed.py
web/                 the static frontend (sign-in, Paytm-style home, insurance, loan, assistant) - see web/README.md
ui/                  older Lovable merchant UI build output (no source here; web/ is the live frontend)
eval/  tests/        scorecard, red-team, ~840 tests
docs/archive/        finals-day plans and change logs (history only)
```

## Documents

`CLAUDE.md` working rules and non-negotiables · `CONTRACTS.md` shared models, guardrail, APIs, layout · `COMPLIANCE.md` regulation → control map · `DEMO_SCRIPT.md` what to show · `CHAUKAS_EXPLAINED.md` the design and why (Hinglish) · `CHAUKAS_SYSTEM_DESIGN.md` HLD/LLD · `CHAUKAS_PROBLEM_STATEMENT.md` problem and scope.

## Deploy notes

- **Backend (Render):** redeploy with the consumer code and set `SARVAM_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `CHAUKAS_STAFF_KEY`. Free tier sleeps: wake it before a demo.
- **Frontend (Vercel):** Root Directory `web`, Framework Preset `Other`, no build command.
- **Supabase:** add the deployed frontend URL under Auth → URL Configuration → Redirect URLs (confirmation and reset links).

Say "aligned with RBI FREE-AI, RBI Digital Lending Directions and the DPDP Act 2023", never "compliant" or "certified".
