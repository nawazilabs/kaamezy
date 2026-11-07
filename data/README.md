# data/

| File | What it is |
|------|------------|
| `schema.sql` | Merchant-side DB structure (source of truth). Triggers: `audit_log` and `consents` are append-only, `executions` need an ALLOW audit row. |
| `consumer_schema.sql` | Consumer flows (health insurance, personal loan, assistant chat). **Additive**: a separate file, created idempotently on first use by `chaukas/consumer/store.py`. Same triggers, same ideas. Stores masked values only. |
| `merchant_fixtures.json` | Seed data: 20 scenario merchants (structure and each scenario: the `_meta` section and `CHAUKAS_EXPLAINED.md` §8). |
| `golden_set.json` | Eval cases through the whole loop (intent → plan → guardrail). Current intents and actions. |
| `redteam_set.json` | Hand-crafted steps handed straight to the guardrail ("assume the LLM is compromised"). Current. |
| `seed.py` | `python -m data.seed` writes `data/chaukas.db` (gitignored). |

## Intent list (final: `CONTRACTS.md` §C1.1, `chaukas/core/models.py`)

The merchant-side agent classifies each message into one of:

```
settlement_delay   money has not arrived (failed or pending settlement)
loan_inquiry       wants a loan or credit line
insurance_inquiry  asks about shop insurance
device_issue       soundbox / POS / QR not working -> ticket
device_upgrade     asks for a new or better device
refund_request     wants to refund a customer -> always a handoff (there is deliberately no refund tool)
confirm            "yes" to a pending step (only valid when something is pending)
deny               "no" to a pending step
human_request      "talk to a human" / complaint -> always a handoff
unknown            unclear or low confidence -> handoff
```

The consumer assistant has its own topic list in `config/consumer_kb.yaml` (claim help, waiting period, EMI, charges, privacy, ... plus `unknown`); the model picks a topic, code maps it to an on-screen action.

Set `CHAUKAS_DB_PATH` to use another database file (tests do, one per test).
