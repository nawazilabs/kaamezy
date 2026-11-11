"""Claims and the claim-backed bridge advance: the rule engine, the guardrail and the API.

The advance is sized only from the (simulated) insurer's approved claim and the days to the payout. It needs the same
ladder as a personal loan (identity, Key Facts shown, consent, OTP above the auto limit), BLOCK is final, and a claim
can back only one advance. The personal-loan route refuses a claim-backed quote.

Owner: Member 1
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas.consumer import api, engine, store

PAN, MOBILE, NAME, OTP = "ABCDE1234F", "9876543210", "Demo Sharma", "123456"
BASE = "/api/consumer"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "claims.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def policy(c) -> dict:
    assert c.post(f"{BASE}/kyc/digilocker-demo").json()["verified"] is True
    q = c.post(f"{BASE}/insurance/quote", json={"members": [{"rel": "Self", "age": 32, "gender": "M", "height_cm": 170, "weight_kg": 70}],
                                               "pincode": "400001", "conditions": [], "smoker": False, "surgery_last_4y": False,
                                               "cover_inr": 1000000}).json()
    r = c.post(f"{BASE}/insurance/purchase", json={"quote_id": q["quote_id"], "insurer_id": "suraksha", "consent_share_health": True,
                                                   "pan": PAN, "mobile": MOBILE, "holder_name": NAME, "nominee_name": "Demo Sharma Jr",
                                                   "nominee_relation": "Spouse"}).json()
    assert r["decision"]["verdict"] == "ALLOW", r
    return r["policy"]


def claim(c, pol, amount=100000):
    r = c.post(f"{BASE}/claims", json={"policy_id": pol["id"], "claimed_inr": amount})
    assert r.status_code == 200, r.text
    return r.json()


def decide(c, claim_id):
    return c.post(f"{BASE}/bridge/decide", json={"claim_id": claim_id}).json()


def accept(c, qid, **over):
    body = {"quote_id": qid, "consent_offer": True, "consent_claim_share": True, "pan": PAN, "mobile": MOBILE,
            "holder_name": NAME, "pincode": "400001"}
    body.update(over)
    return c.post(f"{BASE}/bridge/accept", json=body).json()


def shown(c, qid):
    assert c.post(f"{BASE}/loan/quotes/{qid}/kfs-shown").json()["kfs_shown"] is True


def age_claim(days):
    conn = store.connect()
    when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE consumer_claims SET created_at=?", (when,))   # claims (unlike the audit log) are ordinary rows
    conn.commit()


# ------------------------------------------------------------------ rule engine
def test_the_simulated_insurer_approves_a_share_and_never_above_the_cover():
    assert engine.decide_claim(100000, 1000000)["approved_inr"] == 90000
    assert engine.decide_claim(1000000, 1000000)["approved_inr"] == 900000
    with pytest.raises(ValueError):
        engine.decide_claim(1000001, 1000000)
    with pytest.raises(ValueError):
        engine.decide_claim(4999, 1000000)


def _claim_row(approved, made):
    return {"id": "CLM-1", "approved_inr": approved, "bridge_loan_id": None, "created_at": made.strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_the_advance_numbers_and_that_money_is_int_rupees():
    now = datetime.now(timezone.utc)
    d = engine.bridge_decide(_claim_row(180000, now), now)
    assert (d["amount_inr"], d["days"], d["interest_inr"], d["fee_inr"], d["fee_gst_inr"], d["net_inr"], d["total_inr"]) == \
        (140000, 10, 690, 1400, 252, 138348, 140690)      # 80% of 180000 = 144000, rounded down to 140000
    for k in ("amount_inr", "interest_inr", "fee_inr", "fee_gst_inr", "net_inr", "total_inr", "days"):
        assert isinstance(d[k], int)
    assert engine.bridge_decide(_claim_row(900000, now), now)["amount_inr"] == 200000   # capped


def test_the_advance_follows_the_claim_state_and_nothing_else():
    now = datetime.now(timezone.utc)
    assert engine.bridge_decide(_claim_row(90000, now - timedelta(days=11)), now)["reasons"] == ["CLAIM_ALREADY_PAID"]
    assert engine.bridge_decide(_claim_row(10000, now), now)["reasons"] == ["AMOUNT_TOO_LOW"]
    taken = {**_claim_row(90000, now), "bridge_loan_id": "LN-1"}
    assert engine.bridge_decide(taken, now)["reasons"] == ["ADVANCE_ALREADY_TAKEN"]
    later = engine.bridge_decide(_claim_row(180000, now - timedelta(days=5)), now)
    assert later["days"] == 5 and later["interest_inr"] < 690        # fewer days to the payout, less interest


# ------------------------------------------------------------------ claims
def test_a_claim_is_recorded_with_the_insurers_decision_and_audited(client):
    pol = policy(client)
    c = claim(client, pol, 200000)
    assert (c["claimed_inr"], c["approved_inr"], c["state"], c["bridge_up_to_inr"]) == (200000, 180000, "approved_payout_pending", 140000)
    assert client.get(f"{BASE}/claims").json()[0]["id"] == c["id"]
    acts = [a["action"] for a in client.get(f"{BASE}/activity").json()]
    assert "file_claim" in acts


def test_a_claim_needs_an_own_active_policy_and_a_sane_amount(client):
    pol = policy(client)
    assert client.post(f"{BASE}/claims", json={"policy_id": "PHI-00000000", "claimed_inr": 50000}).status_code == 404
    assert client.post(f"{BASE}/claims", json={"policy_id": pol["id"], "claimed_inr": 2000000}).status_code == 422
    assert client.post(f"{BASE}/claims", json={"policy_id": pol["id"], "claimed_inr": 100}).status_code == 422


# ------------------------------------------------------------------ the advance, end to end
def test_a_small_advance_goes_through_and_the_claim_cannot_back_a_second(client):
    pol = policy(client)
    c = claim(client, pol, 100000)                       # approved 90000 -> advance 70000, below the OTP limit
    d = decide(client, c["id"])
    assert d["decision"]["eligible"] and d["decision"]["amount_inr"] == 70000
    assert d["kfs"]["insurance_required"] is False and d["kfs"]["repaid_from"] == "your insurance claim payout"
    shown(client, d["quote_id"])
    r = accept(client, d["quote_id"])
    assert r["decision"]["verdict"] == "ALLOW" and r["loan"]["amount_inr"] == 70000
    assert r["loan"]["purpose"].startswith("Medical bridge") and r["loan"]["total_inr"] == d["decision"]["total_inr"]
    assert client.get(f"{BASE}/claims").json()[0]["bridge_loan_id"] == r["loan"]["id"]
    again = accept(client, d["quote_id"])                 # a retry replays; nothing is created twice
    assert again["replayed"] is True and len(client.get(f"{BASE}/loans").json()) == 1
    assert decide(client, c["id"])["decision"]["reasons"] == ["ADVANCE_ALREADY_TAKEN"]


def test_a_large_advance_needs_the_one_time_code(client):
    pol = policy(client)
    c = claim(client, pol, 200000)                       # advance 140000, above the 100000 limit
    d = decide(client, c["id"])
    shown(client, d["quote_id"])
    r = accept(client, d["quote_id"])
    assert r["decision"]["verdict"] == "NEEDS_CONFIRMATION" and r["decision"]["kind"] == "otp" and r["loan"] is None
    assert accept(client, d["quote_id"], otp="000000")["decision"]["verdict"] == "NEEDS_CONFIRMATION"
    ok = accept(client, d["quote_id"], otp=OTP)
    assert ok["decision"]["verdict"] == "ALLOW" and ok["loan"]["amount_inr"] == 140000


@pytest.mark.parametrize("over,code", [({"consent_offer": False}, "CONSENT_MISSING"), ({"consent_claim_share": False}, "CONSENT_MISSING"),
                                       ({"pan": "BAD"}, "IDENTITY_DETAILS_INVALID"), ({"mobile": "123"}, "IDENTITY_DETAILS_INVALID")])
def test_consent_and_identity_are_blocks_and_an_otp_does_not_change_them(client, over, code):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    shown(client, d["quote_id"])
    r = accept(client, d["quote_id"], otp=OTP, **over)
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reason_code"] == code and r["loan"] is None


def test_the_key_facts_must_be_on_screen_first(client):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    r = accept(client, d["quote_id"], otp=OTP)
    assert r["decision"]["reason_code"] == "KFS_NOT_SHOWN" and r["loan"] is None


def test_without_verified_identity_nothing_is_advanced(client):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    shown(client, d["quote_id"])
    conn = store.connect()
    conn.execute("DELETE FROM consumer_kyc")                   # kyc is a plain table; the guardrail reads it fresh
    conn.commit()
    assert accept(client, d["quote_id"], otp=OTP)["decision"]["reason_code"] == "KYC_PENDING"


def test_a_claim_that_is_paid_by_acceptance_time_is_a_block(client):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    shown(client, d["quote_id"])
    age_claim(11)                                                # the insurer has paid in the meantime
    r = accept(client, d["quote_id"], otp=OTP)
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reason_code"] == "CLAIM_NOT_PAYABLE"
    assert decide(client, client.get(f"{BASE}/claims").json()[0]["id"])["decision"]["reasons"] == ["CLAIM_ALREADY_PAID"]


def test_an_offer_that_no_longer_matches_the_lenders_rule_is_refused(client):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    shown(client, d["quote_id"])
    conn = store.connect()
    conn.execute("UPDATE consumer_claims SET approved_inr=approved_inr*2")   # someone changed the approved amount
    conn.commit()
    assert accept(client, d["quote_id"], otp=OTP)["decision"]["reason_code"] == "AMOUNT_MISMATCH"


def test_a_claim_backed_quote_cannot_be_accepted_as_a_personal_loan(client):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    shown(client, d["quote_id"])
    r = client.post(f"{BASE}/loan/accept", json={"quote_id": d["quote_id"], "consent_offer": True, "consent_credit_check": True,
                                                 "pan": PAN, "mobile": MOBILE, "holder_name": NAME, "pincode": "400001"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "WRONG_OFFER_TYPE"


def test_one_person_cannot_use_anothers_claim(client):
    pol = policy(client)
    c = claim(client, pol, 100000)
    conn = store.connect()
    conn.execute("UPDATE consumer_claims SET user_id='someone-else'")
    conn.commit()
    assert client.post(f"{BASE}/bridge/decide", json={"claim_id": c["id"]}).status_code == 404
    assert client.get(f"{BASE}/claims").json() == []


def test_an_advance_makes_the_audit_trail_without_personal_data(client):
    pol = policy(client)
    d = decide(client, claim(client, pol, 100000)["id"])
    shown(client, d["quote_id"])
    accept(client, d["quote_id"])
    rows = store.connect().execute("SELECT action, note FROM consumer_audit").fetchall()
    assert {"bridge_decision", "disburse_bridge"} <= {r["action"] for r in rows}
    assert not [r for r in rows if r["note"] and (PAN in r["note"] or MOBILE in r["note"] or NAME in r["note"])]
