"""SQLite access for the consumer flows. Tables come from data/consumer_schema.sql (additive, idempotent).

The audit log and consents are append-only (DB triggers). An execution row needs an ALLOW audit row (DB trigger).
Nothing here stores raw PII: callers pass masked values.

Owner: Member 1
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from chaukas import db

SCHEMA = db.ROOT / "data" / "consumer_schema.sql"
_ready: set[str] = set()


def connect() -> sqlite3.Connection:
    conn = db.connect()
    key = db.db_path()
    if key not in _ready:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        _ready.add(key)
    return conn


def _id(prefix: str, digits: int = 8) -> str:
    return prefix + "-" + str(secrets.randbelow(9 * 10 ** (digits - 1)) + 10 ** (digits - 1))


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None


# ---------------------------------------------------------------- quotes
def save_quote(conn, user_id: str, kind: str, params: dict, result: dict, phash: str) -> str:
    qid = _id("Q", 10)
    conn.execute("INSERT INTO consumer_quotes(id,user_id,kind,params_json,result_json,params_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                 (qid, user_id, kind, json.dumps(params), json.dumps(result), phash, db.now_iso()))
    conn.commit()
    return qid


def get_quote(conn, user_id: str, quote_id: str, kind: str) -> dict | None:
    r = conn.execute("SELECT * FROM consumer_quotes WHERE id=? AND user_id=? AND kind=?", (quote_id, user_id, kind)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["params"], d["result"] = json.loads(d.pop("params_json")), json.loads(d.pop("result_json"))
    return d


def quote_expired(q: dict, ttl_minutes: int) -> bool:
    made = datetime.fromisoformat(q["created_at"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - made > timedelta(minutes=ttl_minutes)


def mark_kfs_shown(conn, user_id: str, quote_id: str) -> bool:
    cur = conn.execute("UPDATE consumer_quotes SET kfs_shown_at=COALESCE(kfs_shown_at,?) WHERE id=? AND user_id=? AND kind='loan'",
                       (db.now_iso(), quote_id, user_id))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------- kyc
def set_kyc(conn, user_id: str, ref_last4: str, method: str) -> dict:
    conn.execute("INSERT INTO consumer_kyc(user_id,ref_last4,method,verified_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET ref_last4=excluded.ref_last4, method=excluded.method, verified_at=excluded.verified_at",
                 (user_id, ref_last4, method, db.now_iso()))
    conn.commit()
    return get_kyc(conn, user_id)


def get_kyc(conn, user_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM consumer_kyc WHERE user_id=?", (user_id,)).fetchone())


# ---------------------------------------------------------------- audit + consents (append-only)
def audit(conn, user_id: str, actor: str, action: str, verdict: str, reason_code: str,
          amount: int | None = None, note: str | None = None, quote_id: str | None = None) -> int:
    cur = conn.execute("INSERT INTO consumer_audit(ts,user_id,actor,action,verdict,reason_code,amount,note,quote_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (db.now_iso(), user_id, actor, action, verdict, reason_code, amount, note, quote_id))
    return cur.lastrowid


def consent(conn, user_id: str, purpose: str, quote_id: str | None, action: str = "granted") -> None:
    conn.execute("INSERT INTO consumer_consents(ts,user_id,purpose,action,quote_id) VALUES(?,?,?,?,?)",
                 (db.now_iso(), user_id, purpose, action, quote_id))


def activity(conn, user_id: str, limit: int = 200) -> list[dict]:
    rows = conn.execute("SELECT id,ts,actor,action,verdict,reason_code,amount,note FROM consumer_audit WHERE user_id=? ORDER BY id DESC LIMIT ?",
                        (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- handoffs (a human decides; the approval is bound to the exact parameters)
def open_handoff(conn, user_id: str, quote_id: str, phash: str, reason_code: str, brief: str) -> dict:
    existing = conn.execute("SELECT * FROM consumer_handoffs WHERE user_id=? AND quote_id=? AND params_hash=? AND status='open'",
                            (user_id, quote_id, phash)).fetchone()
    if existing:
        return dict(existing)
    hid = _id("HND", 5)
    conn.execute("INSERT INTO consumer_handoffs(id,user_id,quote_id,params_hash,reason_code,brief,created_at) VALUES(?,?,?,?,?,?,?)",
                 (hid, user_id, quote_id, phash, reason_code, brief, db.now_iso()))
    return dict(conn.execute("SELECT * FROM consumer_handoffs WHERE id=?", (hid,)).fetchone())


def get_handoff(conn, handoff_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM consumer_handoffs WHERE id=?", (handoff_id,)).fetchone())


def human_approved(conn, user_id: str, handoff_id: str | None, quote_id: str, phash: str) -> bool:
    if not handoff_id:
        return False
    h = get_handoff(conn, handoff_id)
    return bool(h and h["user_id"] == user_id and h["quote_id"] == quote_id and h["params_hash"] == phash
                and h["status"] == "approved")


def resolve_handoff(conn, handoff_id: str, decision: str, staff_id: str) -> dict | None:
    cur = conn.execute("UPDATE consumer_handoffs SET status=?, resolved_at=?, staff_id=? WHERE id=? AND status='open'",
                       (decision, db.now_iso(), staff_id, handoff_id))
    conn.commit()
    return get_handoff(conn, handoff_id) if cur.rowcount else None


# ---------------------------------------------------------------- policies and loans
def active_policies(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM consumer_policies WHERE user_id=? AND status='active'", (user_id,))]


def active_loans(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM consumer_loans WHERE user_id=? AND status='active'", (user_id,))]


def execution_by_key(conn, key: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM consumer_executions WHERE idempotency_key=?", (key,)).fetchone())


def record_execution(conn, user_id: str, kind: str, ref_id: str, audit_id: int, key: str) -> None:
    conn.execute("INSERT INTO consumer_executions(user_id,kind,ref_id,audit_id,idempotency_key,created_at) VALUES(?,?,?,?,?,?)",
                 (user_id, kind, ref_id, audit_id, key, db.now_iso()))


def new_claim_id() -> str:
    return _id("CLM")


def create_claim(conn, user_id: str, policy_id: str, claimed_inr: int, approved_inr: int) -> dict:
    cid = new_claim_id()
    conn.execute("INSERT INTO consumer_claims(id,user_id,policy_id,claimed_inr,approved_inr,created_at) VALUES(?,?,?,?,?,?)",
                 (cid, user_id, policy_id, claimed_inr, approved_inr, db.now_iso()))
    return get_claim(conn, user_id, cid)


def get_claim(conn, user_id: str, claim_id: str) -> dict | None:
    """An in-app claim (CLM-) or a claim verified from documents (VCL-, with payout_due and holder_name)."""
    table = "consumer_verified_claims" if claim_id.startswith("VCL-") else "consumer_claims"
    return _row(conn.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (claim_id, user_id)).fetchone())


def create_verified_claim(conn, user_id: str, application_id: str, claim_hash: str, holder_name: str, claimed_inr: int,
                          approved_inr: int, payout_due: str) -> dict:
    cid = _id("VCL")
    conn.execute("INSERT INTO consumer_verified_claims(id,user_id,application_id,claim_hash,holder_name,claimed_inr,approved_inr,payout_due,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (cid, user_id, application_id, claim_hash, holder_name, claimed_inr, approved_inr, payout_due, db.now_iso()))
    return get_claim(conn, user_id, cid)


def verified_claim_by_hash(conn, claim_hash: str) -> dict | None:
    """Any user's claim with this hash (a claim backs one advance, whoever uploads it)."""
    return _row(conn.execute("SELECT * FROM consumer_verified_claims WHERE claim_hash=?", (claim_hash,)).fetchone())


def list_claims(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM consumer_claims WHERE user_id=? ORDER BY created_at, id", (user_id,))]


def new_application_id() -> str:
    return _id("APP")


def fingerprint_used_elsewhere(conn, user_id: str, application_id: str | None, kind: str, fingerprint: str, *, own_preauth_ok: bool = False) -> bool:
    """True if this hash was recorded by ANOTHER application. The same user's own application (same id) does not count. With
    `own_preauth_ok`, neither do the same user's own pre-authorisation offers: the claim that follows an advance legitimately
    reuses the same policy and bill (it is still caught if ANOTHER user, or another claim, used them)."""
    extra = " AND NOT (user_id=? AND application_id IN (SELECT application_id FROM consumer_preauth WHERE user_id=?))" if own_preauth_ok else ""
    args = (kind, fingerprint, user_id, application_id or "") + ((user_id, user_id) if own_preauth_ok else ())
    row = conn.execute("SELECT 1 FROM consumer_doc_fingerprints WHERE kind=? AND fingerprint=? AND NOT (user_id=? AND application_id=?)" + extra + " LIMIT 1", args).fetchone()
    return row is not None


def record_fingerprints(conn, user_id: str, application_id: str, fps: list[dict]) -> None:
    """Remember the hashes of an application's documents (once each: recording the same application again adds nothing)."""
    for f in fps:
        if not conn.execute("SELECT 1 FROM consumer_doc_fingerprints WHERE user_id=? AND application_id=? AND kind=? AND fingerprint=?",
                            (user_id, application_id, f["kind"], f["fingerprint"])).fetchone():
            conn.execute("INSERT INTO consumer_doc_fingerprints(user_id,application_id,kind,fingerprint,created_at) VALUES(?,?,?,?,?)",
                         (user_id, application_id, f["kind"], f["fingerprint"], db.now_iso()))


def application_owned(conn, user_id: str, application_id: str) -> bool:
    return conn.execute("SELECT 1 FROM consumer_doc_fingerprints WHERE user_id=? AND application_id=? LIMIT 1", (user_id, application_id)).fetchone() is not None


def claims_used(conn, user_id: str, policy_id: str) -> int:
    """Rupees of the cover that earlier claims on this policy have used (what the insurer approved for them)."""
    return conn.execute("SELECT COALESCE(SUM(approved_inr),0) FROM consumer_claims WHERE user_id=? AND policy_id=?", (user_id, policy_id)).fetchone()[0]


def set_claim_bridge(conn, claim_id: str, loan_id: str) -> None:
    table = "consumer_verified_claims" if claim_id.startswith("VCL-") else "consumer_claims"
    conn.execute(f"UPDATE {table} SET bridge_loan_id=? WHERE id=? AND bridge_loan_id IS NULL", (loan_id, claim_id))


def new_policy_id() -> str:
    return _id("PHI")


def new_loan_id() -> str:
    return _id("LN")


# ---------------------------------------------------------------- assistant chat (masked text only)
def create_chat_session(conn, user_id: str) -> str:
    sid = _id("CHT", 10)
    conn.execute("INSERT INTO consumer_chat_sessions(id,user_id,created_at) VALUES(?,?,?)", (sid, user_id, db.now_iso()))
    conn.commit()
    return sid


def get_chat_session(conn, user_id: str, session_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM consumer_chat_sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone())


def add_turn(conn, session_id: str, user_id: str, role: str, text_masked: str, topic: str | None, source: str | None) -> None:
    conn.execute("INSERT INTO consumer_turns(session_id,user_id,role,text_masked,topic,source,ts) VALUES(?,?,?,?,?,?,?)",
                 (session_id, user_id, role, text_masked, topic, source, db.now_iso()))


def recent_turns(conn, user_id: str, session_id: str, limit: int) -> list[dict]:
    rows = conn.execute("SELECT role,text_masked FROM consumer_turns WHERE session_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                        (session_id, user_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def turn_count(conn, session_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM consumer_turns WHERE session_id=? AND role='user'", (session_id,)).fetchone()[0]


# ---------------------------------------------------------------- Workflow A: an advance before the claim
def create_preauth(conn, user_id: str, application_id: str, estimate_key: str, holder_name: str, hospital_id: str, payee_name: str,
                   bill_total_inr: int, estimated_inr: int, conservative_inr: int, confidence: str, repay_days: int) -> dict:
    pid = _id("PRE")
    conn.execute("INSERT INTO consumer_preauth(id,user_id,application_id,estimate_key,holder_name,hospital_id,payee_name,bill_total_inr,estimated_inr,"
                 "conservative_inr,confidence,repay_days,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, user_id, application_id, estimate_key, holder_name, hospital_id, payee_name, bill_total_inr, estimated_inr,
                  conservative_inr, confidence, repay_days, db.now_iso()))
    return get_preauth(conn, user_id, pid)


def get_preauth(conn, user_id: str, preauth_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM consumer_preauth WHERE id=? AND user_id=?", (preauth_id, user_id)).fetchone())


def preauth_by_key(conn, estimate_key: str) -> dict | None:
    """Any user's offer on this estimate (one estimate backs one advance, whoever uploads it)."""
    return _row(conn.execute("SELECT * FROM consumer_preauth WHERE estimate_key=?", (estimate_key,)).fetchone())


def set_preauth_loan(conn, preauth_id: str, loan_id: str) -> None:
    conn.execute("UPDATE consumer_preauth SET loan_id=? WHERE id=? AND loan_id IS NULL", (loan_id, preauth_id))


def close_preauth(conn, preauth_id: str, claim_id: str) -> None:
    conn.execute("UPDATE consumer_preauth SET claim_id=?, closed_at=? WHERE id=? AND claim_id IS NULL", (claim_id, db.now_iso(), preauth_id))


# ---------------------------------------------------------------- the pipeline's agent log (append-only)
def save_pipeline_run(conn, user_id: str, workflow: str, outcome: str, steps: list[dict], ref_id: str | None) -> str:
    rid = _id("RUN")
    conn.execute("INSERT INTO consumer_pipeline_runs(id,user_id,workflow,outcome,steps_json,ref_id,created_at) VALUES(?,?,?,?,?,?,?)",
                 (rid, user_id, workflow, outcome, json.dumps(steps), ref_id, db.now_iso()))
    return rid


def _run_view(r) -> dict:
    d = dict(r)
    return {"id": d["id"], "workflow": d["workflow"], "outcome": d["outcome"], "ref_id": d["ref_id"], "created_at": d["created_at"], "steps": json.loads(d["steps_json"])}


def get_pipeline_run(conn, user_id: str, run_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM consumer_pipeline_runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
    return _run_view(r) if r else None


def list_pipeline_runs(conn, user_id: str, limit: int = 20) -> list[dict]:
    return [_run_view(r) for r in conn.execute("SELECT * FROM consumer_pipeline_runs WHERE user_id=? ORDER BY rowid DESC LIMIT ?", (user_id, limit))]
