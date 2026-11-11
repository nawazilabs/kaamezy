"""Fintech journeys for the consumer app: payment help (F1), bill and recharge intents (F2), spend summary (F3) and a
read-only settlement view (F4). Every partner (bank, UPI network, biller, settlement bank) is a SIMULATED mock.

Rules this module keeps (CLAUDE.md, docs/KAAMEZY_FINTECH_SPEC.md):
  * money amounts come from the DB / config, never from the request, the client or a model; amounts are int rupees
  * totals are summed here in Python, on every call, and never cached
  * a refund state is computed from the age of the transaction on every call, never stored
  * an account number is validated, then masked at once; only the last 4 digits are kept
  * nothing here moves money. Paying is guardrail.check_bill_payment + the route in api.py.

Config: config/consumer_fintech.yaml (illustrative placeholders). Tables: data/consumer_schema.sql.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from chaukas import db
from chaukas.consumer import engine, store
from chaukas.integrations._common import load_config


def cfg() -> dict:
    return load_config("consumer_fintech")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ demo data (idempotent, gated by config)
def ensure_demo(conn: sqlite3.Connection, user_id: str, now: datetime | None = None) -> None:
    """Give a user with no history the demo history and wallet from config. Does nothing when demo_seed is false."""
    c = cfg()
    now = now or datetime.now(timezone.utc)
    if conn.execute("SELECT 1 FROM consumer_wallet WHERE user_id=?", (user_id,)).fetchone() is None:
        conn.execute("INSERT INTO consumer_wallet(user_id,balance_inr) VALUES(?,?)",
                     (user_id, int(c["wallet_start_inr"]) if c.get("demo_seed") else 0))
    if c.get("demo_seed") and conn.execute("SELECT 1 FROM consumer_transactions WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is None:
        for h in c["demo_history"]:
            ts = now - timedelta(days=h.get("days_ago", 0), hours=h.get("hours_ago", 0))
            conn.execute("INSERT INTO consumer_transactions(id,user_id,ts,kind,category,payee_label,amount_inr,status,debited,fail_cause,ref_masked) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (store._id("TXN"), user_id, _iso(ts), h["kind"], h["category"], h["payee"], int(h["amount_inr"]),
                          h["status"], int(h.get("debited", 0)), h.get("fail_cause"), h.get("ref")))
    conn.commit()


def wallet_balance(conn: sqlite3.Connection, user_id: str) -> int:
    ensure_demo(conn, user_id)
    return conn.execute("SELECT balance_inr FROM consumer_wallet WHERE user_id=?", (user_id,)).fetchone()[0]


# ------------------------------------------------------------------ F1: transactions, refund state, disputes
def _txn_view(t: dict) -> dict:
    return {"id": t["id"], "ts": t["ts"], "kind": t["kind"], "category": t["category"], "payee": t["payee_label"],
            "amount_inr": t["amount_inr"], "status": t["status"], "debited": bool(t["debited"]),
            "ref": ("••••" + t["ref_masked"]) if t["ref_masked"] else None,
            "money_stuck": t["status"] == "failed" and bool(t["debited"])}


def list_transactions(conn: sqlite3.Connection, user_id: str, limit: int = 30) -> list[dict]:
    ensure_demo(conn, user_id)
    rows = conn.execute("SELECT * FROM consumer_transactions WHERE user_id=? ORDER BY ts DESC, id LIMIT ?", (user_id, limit)).fetchall()
    return [_txn_view(dict(r)) for r in rows]


def get_transaction(conn: sqlite3.Connection, user_id: str, txn_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM consumer_transactions WHERE id=? AND user_id=?", (txn_id, user_id)).fetchone()
    return dict(r) if r else None


def _add_business_days(start: datetime, n: int) -> datetime:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def refund_view(t: dict, now: datetime | None = None) -> dict | None:
    """Refund state for a failed-and-debited transaction, from its age. None for anything else. Computed fresh each call."""
    if not (t["status"] == "failed" and t["debited"]):
        return None
    c = cfg()["refund"]
    now = now or datetime.now(timezone.utc)
    made = _parse(t["ts"])
    hours = max(0.0, (now - made).total_seconds() / 3600)
    state = next((s["state"] for s in c["stages"] if hours <= s["upto_hours"]), c["stages"][-1]["state"])
    sla = int(c["sla_business_days"].get(t["kind"], max(c["sla_business_days"].values())))
    by = _add_business_days(made, sla).date()
    return {"state": state, "state_text": c["state_text"][state], "overdue": state == "refund_overdue",
            "eta_text": f"Refunds usually reach your account within {sla} business days of the payment, by {by.strftime('%d %b %Y')}.",
            "cause_text": c["causes"].get(t["fail_cause"] or "", c["default_cause"])}


def dispute_for(conn: sqlite3.Connection, user_id: str, txn_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM consumer_disputes WHERE user_id=? AND txn_id=?", (user_id, txn_id)).fetchone()
    return dispute_view(dict(r)) if r else None


def dispute_view(d: dict) -> dict:
    return {"id": d["id"], "txn_id": d["txn_id"], "status": d["status"], "created_at": d["created_at"], "escalated_at": d["escalated_at"]}


def txn_status(conn: sqlite3.Connection, user_id: str, txn_id: str, now: datetime | None = None) -> dict | None:
    t = get_transaction(conn, user_id, txn_id)
    if not t:
        return None
    return {"txn": _txn_view(t), "refund": refund_view(t, now), "dispute": dispute_for(conn, user_id, txn_id)}


def raise_dispute(conn: sqlite3.Connection, user_id: str, txn_id: str) -> tuple[dict, bool]:
    """(dispute, created). The caller has already checked the transaction is the user's own failed-and-debited one."""
    existing = dispute_for(conn, user_id, txn_id)
    if existing:
        return existing, False
    did = store._id("DSP")
    conn.execute("INSERT INTO consumer_disputes(id,user_id,txn_id,reason_code,created_at) VALUES(?,?,?,?,?)",
                 (did, user_id, txn_id, "PAYMENT_FAILED_DEBITED", db.now_iso()))
    return dispute_for(conn, user_id, txn_id), True


def escalate_dispute(conn: sqlite3.Connection, user_id: str, dispute_id: str) -> dict | None:
    """Mark the dispute escalated to a human (a status change; the history is in the append-only audit log)."""
    conn.execute("UPDATE consumer_disputes SET status='escalated', escalated_at=COALESCE(escalated_at,?) "
                 "WHERE id=? AND user_id=? AND status='open'", (db.now_iso(), dispute_id, user_id))
    r = conn.execute("SELECT * FROM consumer_disputes WHERE id=? AND user_id=?", (dispute_id, user_id)).fetchone()
    return dispute_view(dict(r)) if r else None


# ------------------------------------------------------------------ F3: spend summary
def spend_summary(conn: sqlite3.Connection, user_id: str, days: int = 30, now: datetime | None = None) -> dict:
    ensure_demo(conn, user_id)
    now = now or datetime.now(timezone.utc)
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM consumer_transactions WHERE user_id=? AND status='success' AND debited=1 AND ts>? AND ts<=?",
        (user_id, _iso(prev_start), _iso(now)))]
    cur = [r for r in rows if _parse(r["ts"]) > start]
    prev_total = sum(r["amount_inr"] for r in rows if _parse(r["ts"]) <= start)
    total = sum(r["amount_inr"] for r in cur)
    by_cat: dict[str, int] = {}
    for r in cur:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["amount_inr"]
    cats = [{"category": k, "amount_inr": v, "share_pct": engine.rnd(100 * v / total) if total else 0}
            for k, v in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))]
    payees: dict[str, int] = {}
    for r in cur:
        if r["category"] != "transfers":  # a transfer to a person is never shown as a payee
            payees[r["payee_label"]] = payees.get(r["payee_label"], 0) + r["amount_inr"]
    top = [{"payee": k, "amount_inr": v} for k, v in sorted(payees.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    return {"days": days, "total_inr": total, "by_category": cats, "top_payees": top,
            "vs_previous": {"previous_total_inr": prev_total, "delta_inr": total - prev_total}}


# ------------------------------------------------------------------ F4: settlements (read-only, existing merchant table)
def linked_merchant(user_id: str) -> str | None:
    return (cfg().get("settlement_links") or {}).get(user_id)


def settlements(conn: sqlite3.Connection, user_id: str, limit: int = 10) -> dict:
    mid = linked_merchant(user_id)
    if not mid:
        return {"linked": False, "message": "No merchant account is linked to this login.", "settlements": [], "next_expected_text": None}
    c = cfg()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,amount,status,cause,settlement_date FROM settlements WHERE merchant_id=? ORDER BY settlement_date DESC, id LIMIT ?",
            (mid, limit))]
    except sqlite3.OperationalError:  # the merchant tables are not in this database
        rows = []
    out = [{"id": r["id"], "amount_inr": r["amount"], "status": r["status"], "status_text": c["settlement_status_text"].get(r["status"], r["status"]),
            "settlement_date": r["settlement_date"],
            "cause_text": c["settlement_causes"].get(r["cause"]) if r["status"] != "success" and r["cause"] else None} for r in rows]
    nxt = None
    pend = next((r for r in rows if r["status"] == "pending" and r["cause"] == "BANK_DOWN"), None)
    if pend:  # the only ETA the data supports: the mock bank-down hold time
        hours = load_config("mock").get("bank_down_eta_hours")
        if hours:
            nxt = f"A pending payout is on hold because the bank was down. It is expected within {hours} hours."
    return {"linked": True, "message": None, "settlements": out, "next_expected_text": nxt}


# ------------------------------------------------------------------ F2: billers, accounts, intents
def biller(biller_id: str) -> dict | None:
    return next((b for b in cfg()["billers"] if b["id"] == biller_id), None)


def public_billers() -> list[dict]:
    c = cfg()
    return [{"id": b["id"], "name": b["name"], "kind": b["kind"], "account": b["account"],
             "bill_inr": b.get("mock_bill_inr"), "plans": c["recharge_plans"].get(b["id"], []) if b["kind"] == "recharge" else []}
            for b in c["billers"]]


def account_ok(b: dict, account: str) -> bool:
    return bool(re.match(cfg()["account_formats"][b["account"]], account or ""))


def mask_account(account: str) -> str:
    return "••••" + account[-4:]


def expected_amount(b: dict, stored_amount: int) -> int | None:
    """The amount the SERVER allows for this biller: the mock bill, or (for a recharge) the stored amount only if it is a listed plan."""
    if b["kind"] == "bill":
        return int(b["mock_bill_inr"])
    plans = {int(p["amount_inr"]) for p in cfg()["recharge_plans"].get(b["id"], [])}
    return stored_amount if stored_amount in plans else None


def intent_hash(kind: str, biller_id: str, account_masked: str, amount_inr: int) -> str:
    return engine.params_hash({"kind": kind, "biller": biller_id, "account": account_masked, "amount": amount_inr})


def create_intent(conn: sqlite3.Connection, user_id: str, b: dict, account: str, plan_amount_inr: int | None) -> dict:
    """Prepare (never pay). Raises ValueError with a plain message when the account or plan is not allowed."""
    if not account_ok(b, account):
        raise ValueError("BAD_ACCOUNT")
    if b["kind"] == "bill":
        amount = int(b["mock_bill_inr"])
    else:
        plans = {int(p["amount_inr"]) for p in cfg()["recharge_plans"].get(b["id"], [])}
        if plan_amount_inr not in plans:
            raise ValueError("BAD_PLAN")
        amount = int(plan_amount_inr)
    masked = mask_account(account)
    now = datetime.now(timezone.utc)
    iid = store._id("INT")
    conn.execute("INSERT INTO consumer_pay_intents(id,user_id,kind,biller_id,account_masked,amount_inr,params_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (iid, user_id, b["kind"], b["id"], masked, amount, intent_hash(b["kind"], b["id"], masked, amount), _iso(now),
                  _iso(now + timedelta(minutes=cfg()["payment_guardrail"]["intent_ttl_minutes"]))))
    return intent_view(get_intent(conn, user_id, iid))


def get_intent(conn: sqlite3.Connection, user_id: str, intent_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM consumer_pay_intents WHERE id=? AND user_id=?", (intent_id, user_id)).fetchone()
    return dict(r) if r else None


def intent_expired(i: dict, now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)) > _parse(i["expires_at"])


def intent_view(i: dict) -> dict:
    b = biller(i["biller_id"]) or {}
    plan = next((p["label"] for p in cfg()["recharge_plans"].get(i["biller_id"], []) if int(p["amount_inr"]) == i["amount_inr"]), None)
    return {"intent_id": i["id"], "kind": i["kind"], "biller_id": i["biller_id"], "biller_name": b.get("name"),
            "account": i["account_masked"], "amount_inr": i["amount_inr"], "plan_label": plan, "expires_at": i["expires_at"]}


def recent_payments(conn: sqlite3.Connection, user_id: str, since: datetime) -> list[dict]:
    """Successful payments in the window with the biller and masked account of their intent (for duplicate and daily-limit checks)."""
    rows = conn.execute("SELECT p.id, p.intent_id, p.amount_inr, p.created_at, i.biller_id, i.account_masked FROM consumer_payments p "
                        "JOIN consumer_pay_intents i ON i.id=p.intent_id WHERE p.user_id=? AND p.status='success' AND p.created_at>?",
                        (user_id, _iso(since))).fetchall()
    return [dict(r) for r in rows]


def record_payment(conn: sqlite3.Connection, user_id: str, intent: dict, audit_id: int, key: str) -> dict:
    """The money step, all in the caller's DB transaction: payment row, transaction row, wallet debit. The DB trigger refuses a
    payment row whose audit row is not ALLOW; the wallet CHECK refuses a negative balance."""
    b = biller(intent["biller_id"])
    pid = store._id("PAY")
    conn.execute("INSERT INTO consumer_payments(id,user_id,intent_id,audit_id,idempotency_key,amount_inr,created_at) VALUES(?,?,?,?,?,?,?)",
                 (pid, user_id, intent["id"], audit_id, key, intent["amount_inr"], db.now_iso()))
    conn.execute("INSERT INTO consumer_transactions(id,user_id,ts,kind,category,payee_label,amount_inr,status,debited,ref_masked) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (store._id("TXN"), user_id, db.now_iso(), intent["kind"], b["category"], b["name"], intent["amount_inr"], "success", 1,
                  intent["account_masked"][-4:]))
    cur = conn.execute("UPDATE consumer_wallet SET balance_inr=balance_inr-? WHERE user_id=? AND balance_inr>=?",
                       (intent["amount_inr"], user_id, intent["amount_inr"]))
    if cur.rowcount != 1:
        raise sqlite3.IntegrityError("insufficient wallet balance")
    return {"payment_id": pid, "amount_inr": intent["amount_inr"], "biller_name": b["name"], "account": intent["account_masked"]}


def payment_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    r = conn.execute("SELECT * FROM consumer_payments WHERE idempotency_key=?", (key,)).fetchone()
    return dict(r) if r else None
