"""Consumer flows: health insurance + personal loan (all partners are [MOCK]).

models.py     request bodies, validated by Pydantic BEFORE the guardrail sees anything
engine.py     deterministic rule engine (premiums) and simulated lender; the ONLY source of amounts
guardrail.py  deterministic checks, no LLM: ALLOW / NEEDS_CONFIRMATION / BLOCK before any money step
store.py      SQLite tables from data/consumer_schema.sql (append-only audit, ALLOW-only executions)
auth.py       who is calling (Supabase access token, checked server side)
api.py        FastAPI router, mounted at /api/consumer

Same rules as the merchant side: amounts never come from the client or an LLM, BLOCK is final, no raw PII is stored
or logged, config lives in config/consumer.yaml.

Owner: Member 1 (API + integration) / Member 2 (rules)
"""
