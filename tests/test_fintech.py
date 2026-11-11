"""Fintech journeys (payment help, bills and recharge, spend summary, settlements). All partners are SIMULATED.

The contract from docs/KAAMEZY_FINTECH_SPEC.md: amounts come from config / the DB, never the request; voice and chat can only
prepare a payment; the guardrail decides on the server; BLOCK is final; an account number is kept as its last 4 digits only.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas import db
from chaukas.consumer import api, billpay, fintech, guardrail, store
from chaukas.consumer.auth import current_user
from data.seed import seed

OTP = "123456"
JIO_NO, DTH_NO = "9876543210", "123456789012"


def make_client(user="demo-user"):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "fin.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    return make_client()


@pytest.fixture
def merchant_client(tmp_path, monkeypatch):
    path = tmp_path / "fin_merchant.db"
    seed(path)
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(path))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    return make_client()


def verify(c):
    assert c.post("/api/consumer/kyc/digilocker-demo").json()["verified"] is True


def txns(c):
    return c.get("/api/consumer/transactions?limit=100").json()


def stuck(c):
    return sorted((t for t in txns(c) if t["money_stuck"]), key=lambda t: t["ts"], reverse=True)


def intent(c, biller="bescom", account=DTH_NO, plan=None, expect=200):
    body = {"biller_id": biller, "account": account}
    if plan is not None:
        body["plan_amount_inr"] = plan
    r = c.post("/api/consumer/pay/intent", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def pay(c, intent_id, otp=None):
    return c.post("/api/consumer/pay/confirm", json={"intent_id": intent_id, **({"otp": otp} if otp else {})})


# ------------------------------------------------------------------ F1: payment help
def test_the_demo_history_is_seeded_once_and_has_a_failed_debited_payment(client):
    first = txns(client)
    assert len(txns(client)) == len(first)  # idempotent: a second read adds nothing
    assert len(stuck(client)) == 2
    kinds = [t["kind"] for t in first]
    assert kinds.count("bill") >= 2 and kinds.count("upi") >= 3 and kinds.count("recharge") >= 1


def test_the_seed_is_off_when_the_config_says_so(client, monkeypatch):
    monkeypatch.setattr(fintech, "cfg", lambda: {**fintech.load_config("consumer_fintech"), "demo_seed": False})
    assert txns(client) == []
    assert client.get("/api/consumer/wallet").json()["balance_inr"] == 0


def test_the_refund_state_follows_the_age_of_the_payment(client):
    recent, old = stuck(client)
    s1 = client.get(f"/api/consumer/transactions/{recent['id']}/status").json()
    assert s1["refund"]["state"] == "refund_initiated" and s1["refund"]["overdue"] is False
    assert "business days" in s1["refund"]["eta_text"] and s1["refund"]["cause_text"]
    s2 = client.get(f"/api/consumer/transactions/{old['id']}/status").json()
    assert s2["refund"]["state"] == "refund_overdue" and s2["refund"]["overdue"] is True
    now = datetime.now(timezone.utc)
    t = {"status": "failed", "debited": 1, "kind": "upi", "ts": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "fail_cause": "bank_timeout"}
    assert fintech.refund_view(t, now)["state"] == "debit_under_review"


def test_only_a_failed_and_debited_payment_has_a_refund(client):
    ok = next(t for t in txns(client) if t["status"] == "success")
    assert client.get(f"/api/consumer/transactions/{ok['id']}/status").json()["refund"] is None
    t = {"status": "failed", "debited": 0, "kind": "upi", "ts": "2026-01-01T00:00:00Z", "fail_cause": "insufficient_balance"}
    assert fintech.refund_view(t) is None


def test_a_dispute_is_for_your_own_failed_and_debited_payment_and_never_duplicates(client, tmp_path):
    t = stuck(client)[0]
    d1 = client.post("/api/consumer/disputes", json={"txn_id": t["id"]})
    d2 = client.post("/api/consumer/disputes", json={"txn_id": t["id"]})
    assert d1.status_code == d2.status_code == 200
    assert d1.json()["id"] == d2.json()["id"] and d1.json()["created"] is True and d2.json()["created"] is False
    assert store.connect().execute("SELECT COUNT(*) FROM consumer_disputes").fetchone()[0] == 1
    assert client.get(f"/api/consumer/transactions/{t['id']}/status").json()["dispute"]["status"] == "open"
    ok = next(x for x in txns(client) if x["status"] == "success")
    assert client.post("/api/consumer/disputes", json={"txn_id": ok["id"]}).status_code == 422
    other = make_client("someone-else")  # another user's transaction id looks like it does not exist
    assert other.post("/api/consumer/disputes", json={"txn_id": t["id"]}).status_code == 404
    assert other.get(f"/api/consumer/transactions/{t['id']}/status").status_code == 404


def test_escalation_is_refused_until_the_refund_is_overdue(client):
    recent, old = stuck(client)
    dr = client.post("/api/consumer/disputes", json={"txn_id": recent["id"]}).json()
    r = client.post(f"/api/consumer/disputes/{dr['id']}/escalate")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_OVERDUE"
    do = client.post("/api/consumer/disputes", json={"txn_id": old["id"]}).json()
    e = client.post(f"/api/consumer/disputes/{do['id']}/escalate")
    assert e.status_code == 200 and e.json()["status"] == "escalated"
    assert client.post(f"/api/consumer/disputes/{do['id']}/escalate").json()["status"] == "escalated"  # a retry replays
    assert client.post("/api/consumer/disputes/DSP-00000000/escalate").status_code == 404
    acts = [a["action"] for a in client.get("/api/consumer/activity").json()]
    assert "raise_dispute" in acts and "escalate_dispute" in acts


# ------------------------------------------------------------------ F3: spends
def test_spend_totals_equal_a_hand_computed_sum_of_the_seeded_rows(client):
    out = client.get("/api/consumer/spends?days=30").json()
    c = fintech.cfg()
    expected = sum(h["amount_inr"] for h in c["demo_history"] if h["status"] == "success" and h["debited"] and h["days_ago"] < 30)
    assert out["total_inr"] == expected
    assert sum(x["amount_inr"] for x in out["by_category"]) == expected
    assert all(isinstance(x["share_pct"], int) for x in out["by_category"])
    prev = sum(h["amount_inr"] for h in c["demo_history"] if h["status"] == "success" and h["debited"] and 30 <= h["days_ago"] < 60)
    assert out["vs_previous"] == {"previous_total_inr": prev, "delta_inr": expected - prev}
    assert "Fresh Basket" not in [p["payee"] for p in out["top_payees"]]  # the failed payment is excluded
    assert client.get("/api/consumer/spends?days=30").json() == out  # recomputed each time, same data -> same answer


def test_a_failed_or_not_debited_row_and_a_transfer_payee_never_count(client):
    conn = store.connect()
    fintech.ensure_demo(conn, "demo-user")
    before = client.get("/api/consumer/spends").json()["total_inr"]
    now = db.now_iso()
    for sid, st, deb, cat in (("TXN-X1", "failed", 1, "food"), ("TXN-X2", "success", 0, "food"), ("TXN-X3", "pending", 1, "food")):
        conn.execute("INSERT INTO consumer_transactions(id,user_id,ts,kind,category,payee_label,amount_inr,status,debited) VALUES(?,?,?,?,?,?,?,?,?)",
                     (sid, "demo-user", now, "upi", cat, "Ghost Cafe", 5000, st, deb))
    conn.execute("INSERT INTO consumer_transactions(id,user_id,ts,kind,category,payee_label,amount_inr,status,debited) VALUES(?,?,?,?,?,?,?,?,?)",
                 ("TXN-X4", "demo-user", now, "upi", "transfers", "Some Person", 700, "success", 1))
    conn.commit()
    out = client.get("/api/consumer/spends").json()
    assert out["total_inr"] == before + 700
    assert "Ghost Cafe" not in [p["payee"] for p in out["top_payees"]] and "Some Person" not in [p["payee"] for p in out["top_payees"]]


# ------------------------------------------------------------------ F4: settlements
def test_a_linked_user_sees_their_merchants_settlements_and_the_plain_cause(merchant_client):
    c = merchant_client
    out = c.get("/api/consumer/settlements").json()
    assert out["linked"] is True and {s["id"] for s in out["settlements"]} >= {"SET-99213", "SET-48219"}
    failed = next(s for s in out["settlements"] if s["id"] == "SET-99213")
    assert failed["amount_inr"] == 18400 and failed["status"] == "failed"
    assert failed["cause_text"] == fintech.cfg()["settlement_causes"]["IFSC_MISMATCH"]
    assert next(s for s in out["settlements"] if s["id"] == "SET-48219")["cause_text"] is None
    assert all("amount" not in s for s in out["settlements"])


def test_an_unlinked_user_sees_no_settlements_and_nothing_is_changed(merchant_client):
    before = db.connect().execute("SELECT id,amount,status,cause,retry_count,payout_ref FROM settlements ORDER BY id").fetchall()
    out = make_client("not-linked").get("/api/consumer/settlements").json()
    assert out["linked"] is False and out["settlements"] == [] and "No merchant account" in out["message"]
    merchant_client.get("/api/consumer/settlements")
    after = db.connect().execute("SELECT id,amount,status,cause,retry_count,payout_ref FROM settlements ORDER BY id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_settlements_work_without_the_merchant_tables(client):
    out = client.get("/api/consumer/settlements").json()
    assert out["linked"] is True and out["settlements"] == []


# ------------------------------------------------------------------ F2: prepare, then pay
def test_a_bill_intent_takes_its_amount_from_the_biller_and_stores_only_four_digits(client):
    v = intent(client, "bescom", DTH_NO)
    assert v["amount_inr"] == 1840 and v["account"] == "••••9012" and v["plan_label"] is None
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM consumer_payments").fetchone()[0] == 0  # preparing pays nothing
    assert client.get("/api/consumer/wallet").json()["balance_inr"] == 25000


def test_a_recharge_amount_must_be_a_listed_plan_and_is_never_clamped(client):
    assert intent(client, "jio", JIO_NO, plan=199)["amount_inr"] == 199
    assert intent(client, "jio", JIO_NO, plan=500, expect=422).get("detail", {}).get("code") == "BAD_PLAN"
    assert intent(client, "jio", JIO_NO, expect=422)["detail"]["code"] == "BAD_PLAN"
    assert intent(client, "nope", JIO_NO, expect=422)["detail"]["code"] == "UNKNOWN_BILLER"
    assert intent(client, "jio", "12345", plan=199, expect=422)["detail"]["code"] == "BAD_ACCOUNT"
    assert intent(client, "bescom", "12ab", expect=422)["detail"]["code"] == "BAD_ACCOUNT"


def test_paying_needs_identity_and_then_moves_the_wallet_exactly_once(client):
    i = intent(client, "airtel_dth", DTH_NO)
    blocked = pay(client, i["intent_id"]).json()
    assert blocked["decision"]["verdict"] == "BLOCK" and blocked["decision"]["reason_code"] == "KYC_PENDING" and blocked["payment"] is None
    assert client.get("/api/consumer/wallet").json()["balance_inr"] == 25000
    verify(client)
    ok = pay(client, i["intent_id"]).json()
    assert ok["decision"]["verdict"] == "ALLOW" and ok["payment"]["amount_inr"] == 520 and ok["balance_inr"] == 24480
    again = pay(client, i["intent_id"]).json()  # same tap again: replayed, not charged
    assert again["replayed"] is True and again["payment"]["payment_id"] == ok["payment"]["payment_id"]
    assert client.get("/api/consumer/wallet").json()["balance_inr"] == 24480
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM consumer_payments").fetchone()[0] == 1
    assert next(t for t in txns(client) if t["payee"] == "DTH (Airtel)" and t["ref"] == "••••9012")["status"] == "success"


def test_the_decision_carries_the_full_checks_list(client):
    verify(client)
    d = pay(client, intent(client, "airtel_dth", DTH_NO)["intent_id"]).json()["decision"]
    assert len(d["checks"]) == 8 and all(c["ok"] is True for c in d["checks"])


def test_over_the_auto_limit_needs_the_otp_and_yes_does_nothing(client):
    verify(client)
    i = intent(client, "jio", JIO_NO, plan=666)  # under the limit: no OTP
    assert pay(client, i["intent_id"]).json()["decision"]["verdict"] == "ALLOW"
    big = intent(client, "bescom", DTH_NO)       # 1840 is under 2000; lower the limit to test the step
    cfgd = fintech.load_config("consumer_fintech")
    cfgd["payment_guardrail"]["auto_limit_inr"] = 1000
    orig = fintech.cfg
    fintech.cfg = lambda: cfgd
    try:
        d = pay(client, big["intent_id"]).json()["decision"]
        assert d["verdict"] == "NEEDS_CONFIRMATION" and d["kind"] == "otp" and d["reason_code"] == "OVER_AUTO_LIMIT"
        assert pay(client, big["intent_id"], otp="yes").json()["decision"]["verdict"] == "NEEDS_CONFIRMATION"
        assert pay(client, big["intent_id"], otp="000000").json()["decision"]["verdict"] == "NEEDS_CONFIRMATION"
        assert client.get("/api/consumer/wallet").json()["balance_inr"] == 25000 - 666
        ok = pay(client, big["intent_id"], otp=OTP).json()
        assert ok["decision"]["verdict"] == "ALLOW" and ok["payment"]["amount_inr"] == 1840
    finally:
        fintech.cfg = orig


def test_amount_mismatch_when_the_stored_amount_no_longer_matches_config(client, monkeypatch):
    verify(client)
    i = intent(client, "bescom", DTH_NO)
    cfgd = fintech.load_config("consumer_fintech")
    cfgd = {**cfgd, "billers": [{**b, "mock_bill_inr": 1999} if b["id"] == "bescom" else b for b in cfgd["billers"]]}
    monkeypatch.setattr(fintech, "cfg", lambda: cfgd)
    d = pay(client, i["intent_id"]).json()["decision"]
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "AMOUNT_MISMATCH"


def test_a_tampered_intent_row_is_refused(client):
    verify(client)
    i = intent(client, "jio", JIO_NO, plan=199)
    conn = store.connect()
    conn.execute("UPDATE consumer_pay_intents SET amount_inr=299 WHERE id=?", (i["intent_id"],))  # 299 is a plan, but the hash says 199
    conn.commit()
    d = pay(client, i["intent_id"]).json()["decision"]
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "AMOUNT_MISMATCH"


def test_insufficient_balance_duplicate_and_daily_limit_are_block_and_an_otp_does_not_change_them(client):
    verify(client)
    conn = store.connect()
    # insufficient balance
    fintech.ensure_demo(conn, "demo-user")
    conn.execute("UPDATE consumer_wallet SET balance_inr=100 WHERE user_id='demo-user'")
    conn.commit()
    d = pay(client, intent(client, "airtel_dth", DTH_NO)["intent_id"], otp=OTP).json()["decision"]
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "INSUFFICIENT_BALANCE"
    conn.execute("UPDATE consumer_wallet SET balance_inr=25000 WHERE user_id='demo-user'")
    conn.commit()
    # duplicate: same biller, account and amount again inside the window (a fresh intent, so not a replay)
    assert pay(client, intent(client, "airtel_dth", DTH_NO)["intent_id"]).json()["decision"]["verdict"] == "ALLOW"
    d = pay(client, intent(client, "airtel_dth", DTH_NO)["intent_id"], otp=OTP).json()["decision"]
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "DUPLICATE_PAYMENT"
    # daily limit: lowered to 600 for the test (two payments already made today)
    cfgd = fintech.load_config("consumer_fintech")
    cfgd["payment_guardrail"]["daily_limit_inr"] = 600
    orig = fintech.cfg
    fintech.cfg = lambda: cfgd
    try:
        d = pay(client, intent(client, "bescom", DTH_NO)["intent_id"], otp=OTP).json()["decision"]
        assert d["verdict"] == "BLOCK" and d["reason_code"] == "DAILY_LIMIT_EXCEEDED"
    finally:
        fintech.cfg = orig


def test_an_expired_intent_and_another_users_intent_are_refused(client):
    verify(client)
    i = intent(client, "bescom", DTH_NO)
    assert make_client("someone-else").post("/api/consumer/pay/confirm", json={"intent_id": i["intent_id"]}).status_code == 404
    conn = store.connect()
    conn.execute("UPDATE consumer_pay_intents SET expires_at='2020-01-01T00:00:00Z' WHERE id=?", (i["intent_id"],))
    conn.commit()
    r = pay(client, i["intent_id"])
    assert r.status_code == 410 and r.json()["detail"]["code"] == "INTENT_EXPIRED"
    assert pay(client, "INT-00000000").status_code == 404
    d = guardrail.check_bill_payment(intent={**dict(conn.execute("SELECT * FROM consumer_pay_intents WHERE id=?", (i["intent_id"],)).fetchone())},
                                     user_id="demo-user", kyc_verified=True, wallet_balance=99999, recent_payments=[], otp_ok=False)
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "INTENT_INVALID"


def test_the_database_refuses_a_payment_without_an_allow_decision(client):
    conn = store.connect()
    block = store.audit(conn, "demo-user", "guardrail", "pay_bill", "BLOCK", "KYC_PENDING")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="requires an ALLOW"):
        conn.execute("INSERT INTO consumer_payments(id,user_id,intent_id,audit_id,idempotency_key,amount_inr,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("PAY-1", "demo-user", "INT-1", block, "k1", 100, db.now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO consumer_payments(id,user_id,intent_id,audit_id,idempotency_key,amount_inr,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("PAY-2", "demo-user", "INT-1", 999999, "k2", 100, db.now_iso()))


def test_an_account_number_is_never_stored_in_full(client):
    verify(client)
    pay(client, intent(client, "airtel_dth", DTH_NO)["intent_id"])
    pay(client, intent(client, "jio", JIO_NO, plan=199)["intent_id"])
    conn = store.connect()
    dump = "".join(str(tuple(r)) for t in ("consumer_pay_intents", "consumer_audit", "consumer_payments", "consumer_transactions", "consumer_turns")
                   for r in conn.execute(f"SELECT * FROM {t}"))
    assert DTH_NO not in dump and JIO_NO not in dump
    assert "9012" in dump and "3210" in dump
    assert not [r for r in conn.execute("SELECT note FROM consumer_audit") if r[0] and (DTH_NO in r[0] or JIO_NO in r[0])]


# ------------------------------------------------------------------ voice / chat -> a suggestion only
def test_extract_payment_finds_the_biller_and_only_a_listed_plan():
    a = billpay.extract_payment("bijli bill bhar do")
    assert a["biller_id"] == "bescom" and a["plan_amount_inr"] is None and "account" in a["missing"] and "amount_inr" not in a
    b = billpay.extract_payment("199 ka jio recharge")
    assert (b["biller_id"], b["plan_amount_inr"]) == ("jio", 199)
    c = billpay.extract_payment("500 ka jio recharge")  # not a plan: dropped, never clamped to the nearest one
    assert c["biller_id"] == "jio" and c["plan_amount_inr"] is None and c["dropped"] == ["plan_amount_inr"]
    assert billpay.extract_payment("please pay something") is None


def test_a_spoken_or_typed_account_number_is_never_returned():
    for text in (f"jio recharge for {JIO_NO}", f"199 ka jio recharge {JIO_NO}", f"bijli bill consumer number {DTH_NO}"):
        out = billpay.extract_payment(text)
        assert out and JIO_NO not in str(out) and DTH_NO not in str(out) and "account" in out["missing"]
        assert set(out) == {"biller_id", "plan_amount_inr", "fields", "missing", "dropped"}
