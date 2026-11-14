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

-- A claim whose approval was VERIFIED from documents (consumer/verify.py): the approved amount and the payout date come from the
-- insurer's own (simulated) record, never from the client. claim_hash is the hash of insurer + claim reference and is UNIQUE across
-- all users: one real claim can back one advance, whoever uploads it. holder_name is the policy holder on the documents; the
-- advance is only accepted for that person. bridge_loan_id is set once (a claim can back at most one advance).
CREATE TABLE IF NOT EXISTS consumer_verified_claims (
  id              TEXT PRIMARY KEY,     -- VCL-12345678
  user_id         TEXT NOT NULL,
  application_id  TEXT NOT NULL,
  claim_hash      TEXT NOT NULL UNIQUE,
  holder_name     TEXT NOT NULL,
  claimed_inr     INTEGER NOT NULL CHECK (claimed_inr > 0),
  approved_inr    INTEGER NOT NULL CHECK (approved_inr >= 0),
  payout_due      TEXT NOT NULL,        -- ISO date from the insurer's record
  bridge_loan_id  TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cvclaims_user ON consumer_verified_claims(user_id, created_at);
CREATE TRIGGER IF NOT EXISTS consumer_vclaims_no_delete BEFORE DELETE ON consumer_verified_claims
BEGIN SELECT RAISE(ABORT, 'consumer_verified_claims rows are never deleted'); END;

-- Workflow A (an advance before the claim). The offer is sized from the conservative figure of a VERIFIED estimate; estimate_key is
-- a hash of hospital + patient + policy + bill total, UNIQUE across all users (one estimate backs one advance). The advance is
-- paid to the hospital (payee_*), never to the person. It is closed by attaching the approved claim (claim_id) that follows.
CREATE TABLE IF NOT EXISTS consumer_preauth (
  id                 TEXT PRIMARY KEY,   -- PRE-12345678
  user_id            TEXT NOT NULL,
  application_id     TEXT NOT NULL,
  estimate_key       TEXT NOT NULL UNIQUE,
  holder_name        TEXT NOT NULL,
  hospital_id        TEXT NOT NULL,      -- from the partner registry, not from the client
  payee_name         TEXT NOT NULL,
  bill_total_inr     INTEGER NOT NULL,
  estimated_inr      INTEGER NOT NULL,
  conservative_inr   INTEGER NOT NULL,   -- the ONLY figure an advance is sized from
  confidence         TEXT NOT NULL,
  repay_days         INTEGER NOT NULL,   -- the payout outlook plus the claim buffer
  loan_id            TEXT,               -- set once, when the advance is made
  claim_id           TEXT,               -- set once, when the approved claim closes it
  closed_at          TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cpreauth_user ON consumer_preauth(user_id, created_at);
CREATE TRIGGER IF NOT EXISTS consumer_preauth_no_delete BEFORE DELETE ON consumer_preauth
BEGIN SELECT RAISE(ABORT, 'consumer_preauth rows are never deleted'); END;

-- The agent log of the claim-financing pipeline (consumer/pipeline.py): per run, the stages with their status, headline, codes and
-- whole-rupee figures. Never document text, names or reference numbers. Append-only, like the audit log.
CREATE TABLE IF NOT EXISTS consumer_pipeline_runs (
  id          TEXT PRIMARY KEY,         -- RUN-12345678
  user_id     TEXT NOT NULL,
  workflow    TEXT NOT NULL CHECK (workflow IN ('approved_claim','preapproval')),
  outcome     TEXT NOT NULL CHECK (outcome IN ('offer_prepared','needs_a_person','no_offer')),
  steps_json  TEXT NOT NULL,
  ref_id      TEXT,                     -- the VCL- claim or PRE- offer that was prepared
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cpipe_user ON consumer_pipeline_runs(user_id, created_at);
CREATE TRIGGER IF NOT EXISTS consumer_pipe_no_update BEFORE UPDATE ON consumer_pipeline_runs
BEGIN SELECT RAISE(ABORT, 'consumer_pipeline_runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS consumer_pipe_no_delete BEFORE DELETE ON consumer_pipeline_runs
BEGIN SELECT RAISE(ABORT, 'consumer_pipeline_runs is append-only'); END;

-- Hashes of the documents of an application, so the SAME file, hospital bill or claim used for another application can be
-- found. Only SHA-256 hashes are kept: no document text, no name, no number a person could be recognised by. Append-only.
CREATE TABLE IF NOT EXISTS consumer_doc_fingerprints (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        TEXT NOT NULL,
  application_id TEXT NOT NULL,         -- APP-12345678
  kind           TEXT NOT NULL CHECK (kind IN ('file','bill','claim')),
  fingerprint    TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cdocfp ON consumer_doc_fingerprints(kind, fingerprint);
CREATE TRIGGER IF NOT EXISTS consumer_docfp_no_update BEFORE UPDATE ON consumer_doc_fingerprints
BEGIN SELECT RAISE(ABORT, 'consumer_doc_fingerprints is append-only'); END;
CREATE TRIGGER IF NOT EXISTS consumer_docfp_no_delete BEFORE DELETE ON consumer_doc_fingerprints
BEGIN SELECT RAISE(ABORT, 'consumer_doc_fingerprints is append-only'); END;

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


-- ---------------------------------------------------------------------------------------------------------------------
-- Fintech journeys (payment help, bills and recharge, spend summary). ADDITIVE. All partners are SIMULATED.
-- Account numbers are never stored in full: only the last 4 digits (account_masked / ref_masked).
-- ---------------------------------------------------------------------------------------------------------------------

-- Mock transaction history. Seeded for the demo user from config/consumer_fintech.yaml (demo_seed: true).
CREATE TABLE IF NOT EXISTS consumer_transactions (
  id            TEXT PRIMARY KEY,                 -- TXN-xxxxxxxx
  user_id       TEXT NOT NULL,
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('upi','card','bill','recharge','wallet_topup')),
  category      TEXT NOT NULL,
  payee_label   TEXT NOT NULL,                    -- merchant or biller NAME only
  amount_inr    INTEGER NOT NULL CHECK (amount_inr > 0),
  status        TEXT NOT NULL CHECK (status IN ('success','failed','pending')),
  debited       INTEGER NOT NULL DEFAULT 0 CHECK (debited IN (0,1)),   -- 1 = money left the account
  fail_cause    TEXT,
  ref_masked    TEXT                              -- last 4 of the reference only
);
CREATE INDEX IF NOT EXISTS idx_ctxn_user ON consumer_transactions(user_id, ts);

-- One dispute per transaction: a retry replays, never duplicates. The status moves (open -> escalated); the audit log is the history.
CREATE TABLE IF NOT EXISTS consumer_disputes (
  id           TEXT PRIMARY KEY,                  -- DSP-xxxxxxxx
  user_id      TEXT NOT NULL,
  txn_id       TEXT NOT NULL REFERENCES consumer_transactions(id),
  reason_code  TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','escalated','resolved')),
  created_at   TEXT NOT NULL,
  escalated_at TEXT,
  UNIQUE (user_id, txn_id)
);

-- What voice / chat / the screen PREPARED. Nothing is paid until the user taps Pay and the guardrail says ALLOW.
CREATE TABLE IF NOT EXISTS consumer_pay_intents (
  id             TEXT PRIMARY KEY,                -- INT-xxxxxxxx
  user_id        TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('bill','recharge')),
  biller_id      TEXT NOT NULL,
  account_masked TEXT NOT NULL,
  amount_inr     INTEGER NOT NULL,                -- set by the server from the mock bill or the plan table
  params_hash    TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL
);

-- Executed payments. Like consumer_executions, a row needs an ALLOW audit row (DB trigger).
CREATE TABLE IF NOT EXISTS consumer_payments (
  id               TEXT PRIMARY KEY,              -- PAY-xxxxxxxx
  user_id          TEXT NOT NULL,
  intent_id        TEXT NOT NULL,
  audit_id         INTEGER NOT NULL REFERENCES consumer_audit(id),
  idempotency_key  TEXT NOT NULL UNIQUE,          -- a retried request replays the old result
  amount_inr       INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success','failed')),
  created_at       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS consumer_pay_require_allow BEFORE INSERT ON consumer_payments
WHEN (SELECT verdict FROM consumer_audit WHERE id = NEW.audit_id) IS NOT 'ALLOW'
BEGIN SELECT RAISE(ABORT, 'a payment requires an ALLOW decision in consumer_audit'); END;

-- Mock wallet balance (simulated). Reduced in the same DB transaction as the payment row.
CREATE TABLE IF NOT EXISTS consumer_wallet (
  user_id      TEXT PRIMARY KEY,
  balance_inr  INTEGER NOT NULL CHECK (balance_inr >= 0)
);
