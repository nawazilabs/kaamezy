-- Consumer flows (health insurance + personal loan). ADDITIVE: does not touch data/schema.sql.
-- Created idempotently at first use by chaukas/consumer/store.py. Owner: Member 1.
-- Same safety ideas as the merchant schema: the audit log and consents are append-only (DB triggers), and an
-- execution row can only exist if its linked audit decision is ALLOW (DB trigger), so a bug in code still cannot
-- issue a policy or disburse a loan without the guardrail.
-- No raw PII is stored: PAN, name and mobile are kept masked only. Money is INTEGER rupees.

CREATE TABLE IF NOT EXISTS consumer_quotes (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('health','loan')),
  params_json   TEXT NOT NULL,          -- non-PII inputs (ages, cover, income band...)
  result_json   TEXT NOT NULL,          -- what the rule engine / lender returned: the ONLY source of amounts
  params_hash   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  kfs_shown_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_cquotes_user ON consumer_quotes(user_id, created_at);

CREATE TABLE IF NOT EXISTS consumer_kyc (
  user_id      TEXT PRIMARY KEY,
  ref_last4    TEXT NOT NULL,           -- only the last 4 digits ever reach us, never the Aadhaar number
  method       TEXT NOT NULL,
  verified_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumer_audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  actor        TEXT NOT NULL CHECK (actor IN ('user','agent','guardrail','lender','human','system')),
  action       TEXT NOT NULL,
  verdict      TEXT NOT NULL CHECK (verdict IN ('ALLOW','BLOCK','NEEDS_CONFIRMATION')),
  reason_code  TEXT NOT NULL,
  amount       INTEGER,
  note         TEXT,
  quote_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_caudit_user ON consumer_audit(user_id, ts);
CREATE TRIGGER IF NOT EXISTS consumer_audit_no_update BEFORE UPDATE ON consumer_audit
BEGIN SELECT RAISE(ABORT, 'consumer_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS consumer_audit_no_delete BEFORE DELETE ON consumer_audit
BEGIN SELECT RAISE(ABORT, 'consumer_audit is append-only'); END;

CREATE TABLE IF NOT EXISTS consumer_consents (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  user_id   TEXT NOT NULL,
  purpose   TEXT NOT NULL,              -- share_health_data | loan_offer | credit_check
  action    TEXT NOT NULL CHECK (action IN ('granted','withdrawn')),
  quote_id  TEXT
);
CREATE TRIGGER IF NOT EXISTS consumer_consents_no_update BEFORE UPDATE ON consumer_consents
BEGIN SELECT RAISE(ABORT, 'consumer_consents is append-only'); END;
CREATE TRIGGER IF NOT EXISTS consumer_consents_no_delete BEFORE DELETE ON consumer_consents
BEGIN SELECT RAISE(ABORT, 'consumer_consents is append-only'); END;

CREATE TABLE IF NOT EXISTS consumer_handoffs (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL,
  quote_id     TEXT NOT NULL,
  params_hash  TEXT NOT NULL,           -- an approval is valid only for exactly these parameters (no bait-and-switch)
  reason_code  TEXT NOT NULL,
  brief        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','approved','rejected')),
  created_at   TEXT NOT NULL,
  resolved_at  TEXT,
  staff_id     TEXT
);

CREATE TABLE IF NOT EXISTS consumer_policies (
  id              TEXT PRIMARY KEY,     -- PHI-12345678
  user_id         TEXT NOT NULL,
  quote_id        TEXT NOT NULL,
  insurer_id      TEXT NOT NULL,
  insurer_name    TEXT NOT NULL,
  member_sig      TEXT NOT NULL,
  members_json    TEXT NOT NULL,
  cover_inr       INTEGER NOT NULL,
  premium_inr     INTEGER NOT NULL,
  holder_masked   TEXT NOT NULL,
  pan_masked      TEXT NOT NULL,
  nominee_masked  TEXT NOT NULL,
  start_date      TEXT NOT NULL,
  end_date        TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cpol_user ON consumer_policies(user_id, status);

CREATE TABLE IF NOT EXISTS consumer_loans (
  id               TEXT PRIMARY KEY,    -- LN-12345678
  user_id          TEXT NOT NULL,
  quote_id         TEXT NOT NULL,
  lender_name      TEXT NOT NULL,
  amount_inr       INTEGER NOT NULL,
  tenure_months    INTEGER NOT NULL,
  apr_pct          REAL NOT NULL,
  emi_inr          INTEGER NOT NULL,
  total_inr        INTEGER NOT NULL,
  fee_inr          INTEGER NOT NULL,
  fee_gst_inr      INTEGER NOT NULL,
  net_inr          INTEGER NOT NULL,
  purpose          TEXT NOT NULL,
  employment       TEXT NOT NULL,
  pan_masked       TEXT NOT NULL,
  kfs_shown_at     TEXT NOT NULL,
  consent_at       TEXT NOT NULL,
  first_emi_date   TEXT NOT NULL,
  cooling_off_end  TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed','cancelled')),
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cloans_user ON consumer_loans(user_id, status);

CREATE TABLE IF NOT EXISTS consumer_executions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id          TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('issue_policy','disburse_loan')),
  ref_id           TEXT NOT NULL,
  audit_id         INTEGER NOT NULL REFERENCES consumer_audit(id),
  idempotency_key  TEXT NOT NULL UNIQUE,   -- a retried request replays the old result, nothing is created twice
  status           TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success','failed','rolled_back')),
  created_at       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS consumer_exec_require_allow BEFORE INSERT ON consumer_executions
WHEN (SELECT verdict FROM consumer_audit WHERE id = NEW.audit_id) IS NOT 'ALLOW'
BEGIN SELECT RAISE(ABORT, 'an execution requires an ALLOW decision in consumer_audit'); END;

-- Health claims (simulated insurer). approved_inr is decided by the rule engine when the claim is filed; the claim state
-- (under review / approved, payout pending / paid) is computed from created_at on every read, never stored.
-- bridge_loan_id is set once, when a claim-backed advance is made: a claim can back at most one advance.
CREATE TABLE IF NOT EXISTS consumer_claims (
  id              TEXT PRIMARY KEY,     -- CLM-12345678
  user_id         TEXT NOT NULL,
  policy_id       TEXT NOT NULL REFERENCES consumer_policies(id),
  claimed_inr     INTEGER NOT NULL CHECK (claimed_inr > 0),
  approved_inr    INTEGER NOT NULL CHECK (approved_inr >= 0),
  bridge_loan_id  TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cclaims_user ON consumer_claims(user_id, created_at);

-- Assistant chat. Only MASKED text is stored (no raw PII in turns). The model never sees anything else either.
CREATE TABLE IF NOT EXISTS consumer_chat_sessions (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_turns (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL REFERENCES consumer_chat_sessions(id),
  user_id      TEXT NOT NULL,
  role         TEXT NOT NULL CHECK (role IN ('user','assistant')),
  text_masked  TEXT NOT NULL,
  topic        TEXT,
  source       TEXT,                   -- llm | kb | keyword | guard (who wrote the reply)
  ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cturns_session ON consumer_turns(session_id, id);
