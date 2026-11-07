"""Consumer flows (health insurance + personal loan): rule engine, deterministic guardrail, API, DB safety, PII.

The numbers asserted for the rule engine are the ones the web demo showed before it was wired to the backend, so
this file also proves the backend and the demo agree.

Owner: Member 1 / Member 2
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas import db
from chaukas.consumer import api, engine, guardrail, store
from chaukas.consumer.models import InsuranceProposal, LoanApplication

PAN, MOBILE, NAME = "ABCDE1234F", "9876543210", "Demo Sharma"
OTP = "123456"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "consumer.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def me(c):  # the single demo user's connection, for asserting on what was stored
    return store.connect()


def proposal(**over):
    p = {"members": [{"rel": "Self", "age": 32, "gender": "M", "height_cm": 170, "weight_kg": 70}], "pincode": "400001",
         "conditions": [], "smoker": False, "surgery_last_4y": False, "cover_inr": 1000000}
    p.update(over)
    return p


FAMILY = [{"rel": "Self", "age": 34, "height_cm": 172, "weight_kg": 72}, {"rel": "Spouse", "age": 32, "gender": "F", "height_cm": 160, "weight_kg": 58},
          {"rel": "Son", "age": 6, "height_cm": 115, "weight_kg": 20}]


def buy(c, quote_id, insurer="suraksha", **over):
    body = {"quote_id": quote_id, "insurer_id": insurer, "consent_share_health": True, "pan": PAN, "mobile": MOBILE,
            "holder_name": NAME, "nominee_name": "Demo Sharma Jr", "nominee_relation": "Spouse"}
    body.update(over)
    return c.post("/api/consumer/insurance/purchase", json=body).json()


def verify(c):
    assert c.post("/api/consumer/kyc/digilocker-demo").json()["verified"] is True


def loan_app(**over):
    a = {"amount_inr": 100000, "tenure_months": 12, "purpose": "Medical", "employment": "salaried",
         "monthly_income_inr": 60000, "existing_emi_inr": 5000, "age": 34}
    a.update(over)
    return a


def decide(c, **over):
    return c.post("/api/consumer/loan/decide", json=loan_app(**over)).json()


def accept(c, qid, **over):
    body = {"quote_id": qid, "consent_offer": True, "consent_credit_check": True, "pan": PAN, "mobile": MOBILE,
            "holder_name": NAME, "pincode": "400001"}
    body.update(over)
    return c.post("/api/consumer/loan/accept", json=body).json()


# ------------------------------------------------------------------ rule engine (parity with the web demo)
def test_insurance_premiums_match_the_demo():
    p = InsuranceProposal(**proposal())
    assert {q["insurer_id"]: q["premium_inr"] for q in engine.quotes(p)} == {"suraksha": 6894, "arogya": 7493, "navjeevan": 8618}
    fam = InsuranceProposal(**proposal(members=FAMILY))
    assert engine.quotes(fam)[0]["premium_inr"] == 13575
    heart = InsuranceProposal(**proposal(conditions=["heart"], cover_inr=2500000))
    assert engine.quotes(heart)[1]["premium_inr"] == 15293


def test_money_is_always_int_rupees():
    for q in engine.quotes(InsuranceProposal(**proposal(members=FAMILY))):
        assert isinstance(q["premium_inr"], int) and isinstance(q["monthly_inr"], int)
    d = engine.lender_decide(LoanApplication(**loan_app()))
    for k in ("amount_inr", "emi_inr", "total_inr", "interest_inr", "fee_inr", "fee_gst_inr", "net_inr"):
        assert isinstance(d[k], int)


def test_recommendation_is_deterministic_and_explained():
    healthy = InsuranceProposal(**proposal())
    assert engine.recommend(engine.quotes(healthy), healthy)["insurer_id"] == "suraksha"
    sick = InsuranceProposal(**proposal(conditions=["diabetes"]))
    r = engine.recommend(engine.quotes(sick), sick)
    assert r["insurer_id"] == "arogya" and "health condition" in r["why"]


def test_loan_numbers_match_the_demo():
    d = engine.lender_decide(LoanApplication(**loan_app()))
    assert (d["amount_inr"], d["apr_pct"], d["emi_inr"], d["fee_inr"], d["fee_gst_inr"], d["net_inr"]) == (100000, 19, 9216, 2000, 360, 97640)
    big = engine.lender_decide(LoanApplication(**loan_app(amount_inr=200000)))
    assert (big["emi_inr"], big["interest_inr"], big["net_inr"]) == (18431, 21172, 195280)


def test_lender_caps_the_amount_and_declines_for_plain_reasons():
    capped = engine.lender_decide(LoanApplication(**loan_app(amount_inr=500000)))
    assert capped["eligible"] and capped["amount_inr"] == capped["max_inr"] == 270000
    assert engine.lender_decide(LoanApplication(**loan_app(monthly_income_inr=10000)))["reasons"] == ["INCOME_TOO_LOW"]
    assert engine.lender_decide(LoanApplication(**loan_app(age=19)))["reasons"] == ["AGE_OUT_OF_RANGE"]
    assert engine.lender_decide(LoanApplication(**loan_app(existing_emi_inr=40000)))["reasons"] == ["OBLIGATIONS_TOO_HIGH"]


def test_lender_never_sees_name_gender_location_or_language():
    """Fairness (RBI FREE-AI): the application model has no such field, so the lender cannot use one."""
    assert set(LoanApplication.model_fields) == {"amount_inr", "tenure_months", "purpose", "employment",
                                                 "monthly_income_inr", "existing_emi_inr", "age"}
    a = LoanApplication(**loan_app())
    assert engine.lender_decide(a) == engine.lender_decide(LoanApplication(**loan_app()))


# ------------------------------------------------------------------ insurance: the full path
def test_buy_a_policy_end_to_end(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal(members=FAMILY)).json()
    assert q["city_tier"] == "metro" and q["recommended"]["insurer_id"] == "suraksha"
    verify(client)
    r = buy(client, q["quote_id"])
    assert r["decision"]["verdict"] == "ALLOW" and r["decision"]["reason_code"] == "WITHIN_POLICY"
    assert r["policy"]["premium_inr"] == 13575 and r["policy"]["id"].startswith("PHI-")
    assert all(c["ok"] for c in r["decision"]["checks"]) and len(r["decision"]["checks"]) == 8
    assert len(client.get("/api/consumer/policies").json()) == 1


def test_a_retried_purchase_replays_and_creates_nothing_twice(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    verify(client)
    first, second = buy(client, q), buy(client, q)
    assert second["replayed"] is True and second["policy"]["id"] == first["policy"]["id"]
    assert len(client.get("/api/consumer/policies").json()) == 1


def test_same_cover_for_the_same_people_is_blocked_as_duplicate(client):
    verify(client)
    q1 = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    assert buy(client, q1)["decision"]["verdict"] == "ALLOW"
    q2 = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    r = buy(client, q2)
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reason_code"] == "DUPLICATE_POLICY" and r["policy"] is None


@pytest.mark.parametrize("over,code", [
    ({"consent_share_health": False}, "CONSENT_MISSING"),
    ({"pan": "BADPAN"}, "IDENTITY_DETAILS_INVALID"),
    ({"pan": None}, "IDENTITY_DETAILS_INVALID"),
    ({"mobile": "12345"}, "IDENTITY_DETAILS_INVALID"),
    ({"nominee_name": ""}, "IDENTITY_DETAILS_INVALID"),
])
def test_insurance_blocks_bad_input(client, over, code):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    verify(client)
    r = buy(client, q, **over)
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reason_code"] == code and r["policy"] is None


def test_no_policy_without_verified_identity(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    r = buy(client, q)
    assert r["decision"]["reason_code"] == "KYC_PENDING" and r["decision"]["verdict"] == "BLOCK"
    assert [c["ok"] for c in r["decision"]["checks"]][:2] == [False, None]  # the rest were not run


@pytest.mark.parametrize("age", [17, 66])
def test_proposer_age_is_a_final_block(client, age):
    q = client.post("/api/consumer/insurance/quote", json=proposal(members=[{"rel": "Self", "age": age}])).json()["quote_id"]
    verify(client)
    r = buy(client, q, otp=OTP)
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reason_code"] == "PROPOSER_AGE_OUT_OF_RANGE"


def test_a_big_premium_needs_the_one_time_code_checked_on_the_server(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal(members=FAMILY, cover_inr=5000000, conditions=["diabetes", "bp"])).json()["quote_id"]
    verify(client)
    first = buy(client, q)
    assert first["decision"]["verdict"] == "NEEDS_CONFIRMATION" and first["decision"]["kind"] == "otp"
    wrong = buy(client, q, otp="000000")
    assert wrong["decision"]["verdict"] == "NEEDS_CONFIRMATION" and wrong["policy"] is None
    right = buy(client, q, otp=OTP)
    assert right["decision"]["verdict"] == "ALLOW" and right["policy"]["premium_inr"] == 35194


def test_a_human_underwriter_is_needed_and_the_browser_cannot_approve_itself(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal(conditions=["heart"], cover_inr=2500000)).json()["quote_id"]
    verify(client)
    r = buy(client, q)
    assert r["decision"]["verdict"] == "NEEDS_CONFIRMATION" and r["decision"]["kind"] == "human"
    hid = r["handoff"]["id"]
    assert "Heart condition" in r["handoff"]["brief"]
    assert buy(client, q, handoff_id=hid)["decision"]["kind"] == "human"                  # still pending
    assert client.post(f"/api/consumer/handoffs/{hid}/resolve", json={"decision": "approved", "staff_id": "S1"}).status_code == 403
    assert client.post(f"/api/consumer/handoffs/{hid}/resolve", json={"decision": "approved", "staff_id": "S1"},
                       headers={"X-Staff-Key": "wrong"}).status_code == 403
    ok = client.post(f"/api/consumer/handoffs/{hid}/resolve", json={"decision": "approved", "staff_id": "S1"}, headers={"X-Staff-Key": "demo-staff"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    done = buy(client, q, handoff_id=hid)
    assert done["decision"]["verdict"] == "ALLOW" and done["policy"]["cover_inr"] == 2500000


def test_a_human_approval_is_bound_to_the_exact_insurer(client):
    """Bait and switch: approved for one insurer does not carry over to another."""
    q = client.post("/api/consumer/insurance/quote", json=proposal(conditions=["heart"], cover_inr=2500000)).json()["quote_id"]
    verify(client)
    hid = buy(client, q, insurer="suraksha")["handoff"]["id"]
    client.post(f"/api/consumer/handoffs/{hid}/resolve", json={"decision": "approved", "staff_id": "S1"}, headers={"X-Staff-Key": "demo-staff"})
    swapped = buy(client, q, insurer="navjeevan", handoff_id=hid)
    assert swapped["decision"]["verdict"] == "NEEDS_CONFIRMATION" and swapped["decision"]["kind"] == "human"
    assert buy(client, q, insurer="suraksha", handoff_id=hid)["decision"]["verdict"] == "ALLOW"


def test_cancer_history_always_goes_to_a_human(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal(conditions=["cancer"], cover_inr=500000)).json()["quote_id"]
    verify(client)
    assert buy(client, q)["decision"]["reason_code"] == "UNDERWRITER_REVIEW"


def test_bad_proposals_and_unknown_things_are_rejected_with_a_clear_error(client):
    assert client.post("/api/consumer/insurance/quote", json=proposal(cover_inr=123)).status_code == 422
    assert client.post("/api/consumer/insurance/quote", json=proposal(conditions=["nonsense"])).status_code == 422
    assert client.post("/api/consumer/insurance/quote", json=proposal(pincode="12")).status_code == 422
    assert client.post("/api/consumer/insurance/quote", json=proposal(members=[])).status_code == 422
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    assert client.post("/api/consumer/insurance/purchase", json={"quote_id": q, "insurer_id": "nope"}).status_code == 422
    assert client.post("/api/consumer/insurance/purchase", json={"quote_id": "Q-0", "insurer_id": "suraksha"}).status_code == 404


def test_an_expired_quote_cannot_be_bought(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    c = me(client)
    c.execute("UPDATE consumer_quotes SET created_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), q))
    c.commit()
    verify(client)
    assert client.post("/api/consumer/insurance/purchase", json={"quote_id": q, "insurer_id": "suraksha"}).status_code == 410


# ------------------------------------------------------------------ guardrail on its own
def test_guardrail_refuses_a_premium_that_differs_from_the_rule_engine():
    p = InsuranceProposal(**proposal())
    stored = {"quotes": [dict(q, premium_inr=1) for q in engine.quotes(p)]}  # a tampered stored amount
    d = guardrail.check_policy_purchase(proposal=p, stored_quote=stored, insurer_id="suraksha", pan=PAN, mobile=MOBILE,
                                        holder_name=NAME, nominee_name="N N", consent=True, kyc_verified=True,
                                        active_policies=[], otp_ok=False, human_ok=False)
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "AMOUNT_MISMATCH"


def test_guardrail_refuses_a_loan_that_differs_from_the_lender():
    a = LoanApplication(**loan_app())
    stored = engine.lender_decide(a)
    stored["emi_inr"] += 1
    d = guardrail.check_loan_accept(application=a, stored_quote={"decision": stored}, pan=PAN, mobile=MOBILE, holder_name=NAME,
                                    pincode="400001", consent_offer=True, consent_credit=True, kyc_verified=True,
                                    kfs_shown=True, active_loans=[], otp_ok=True, human_ok=True)
    assert d["verdict"] == "BLOCK" and d["reason_code"] == "AMOUNT_MISMATCH"


def test_guardrail_is_deterministic():
    p = InsuranceProposal(**proposal(members=FAMILY))
    args = dict(proposal=p, stored_quote={"quotes": engine.quotes(p)}, insurer_id="arogya", pan=PAN, mobile=MOBILE, holder_name=NAME,
                nominee_name="N N", consent=True, kyc_verified=True, active_policies=[], otp_ok=False, human_ok=False)
    assert guardrail.check_policy_purchase(**args) == guardrail.check_policy_purchase(**args)


# ------------------------------------------------------------------ loan: the full path
def test_take_a_loan_end_to_end(client):
    d = decide(client)
    assert d["decision"]["eligible"] and d["kfs"]["insurance_required"] is False and d["kfs"]["cooling_off_days"] == 3
    verify(client)
    assert client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown").json()["kfs_shown"] is True
    r = accept(client, d["quote_id"])
    assert r["decision"]["verdict"] == "ALLOW" and r["loan"]["id"].startswith("LN-")
    assert (r["loan"]["amount_inr"], r["loan"]["emi_inr"], r["loan"]["net_inr"]) == (100000, 9216, 97640)
    assert len(client.get("/api/consumer/loans").json()) == 1


def test_no_loan_before_the_key_facts_were_shown(client):
    d = decide(client)
    verify(client)
    r = accept(client, d["quote_id"])
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reason_code"] == "KFS_NOT_SHOWN" and r["loan"] is None


def test_no_loan_without_both_consents_or_identity(client):
    d = decide(client)
    client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
    assert accept(client, d["quote_id"])["decision"]["reason_code"] == "KYC_PENDING"
    verify(client)
    assert accept(client, d["quote_id"], consent_credit_check=False)["decision"]["reason_code"] == "CONSENT_MISSING"
    assert accept(client, d["quote_id"], consent_offer=False)["decision"]["reason_code"] == "CONSENT_MISSING"
    assert accept(client, d["quote_id"], pincode="12")["decision"]["reason_code"] == "IDENTITY_DETAILS_INVALID"


def test_the_amount_comes_from_the_lender_never_from_the_request(client):
    """The accept body has no amount field. Even extra fields cannot change what is disbursed."""
    d = decide(client)
    verify(client)
    client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
    r = accept(client, d["quote_id"], amount_inr=1000000, net_inr=999999)
    assert r["loan"]["amount_inr"] == 100000


def test_a_retried_loan_replays_and_the_same_loan_twice_is_blocked(client):
    verify(client)
    d1 = decide(client)
    client.post(f"/api/consumer/loan/quotes/{d1['quote_id']}/kfs-shown")
    first = accept(client, d1["quote_id"])
    assert accept(client, d1["quote_id"])["replayed"] is True
    d2 = decide(client)
    client.post(f"/api/consumer/loan/quotes/{d2['quote_id']}/kfs-shown")
    r = accept(client, d2["quote_id"])
    assert r["decision"]["reason_code"] == "DUPLICATE_LOAN" and first["loan"]["id"] in r["decision"]["reason"]


def test_at_most_two_active_loans(client):
    verify(client)
    for amount in (50000, 60000):
        d = decide(client, amount_inr=amount)
        client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
        assert accept(client, d["quote_id"])["decision"]["verdict"] == "ALLOW"
    d = decide(client, amount_inr=70000)
    client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
    assert accept(client, d["quote_id"])["decision"]["reason_code"] == "TOO_MANY_LOANS"


def test_a_big_loan_needs_the_one_time_code(client):
    verify(client)
    d = decide(client, amount_inr=200000)
    client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
    r = accept(client, d["quote_id"])
    assert r["decision"]["kind"] == "otp" and r["loan"] is None
    assert accept(client, d["quote_id"], otp="111111")["loan"] is None
    assert accept(client, d["quote_id"], otp=OTP)["loan"]["amount_inr"] == 200000


def test_a_big_self_employed_loan_needs_a_human_then_the_code(client):
    verify(client)
    d = decide(client, amount_inr=200000, employment="self_employed")
    client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
    r = accept(client, d["quote_id"])
    assert r["decision"]["reason_code"] == "INCOME_PROOF_REVIEW" and "self-employed" in r["handoff"]["brief"]
    hid = r["handoff"]["id"]
    client.post(f"/api/consumer/handoffs/{hid}/resolve", json={"decision": "approved", "staff_id": "S1"}, headers={"X-Staff-Key": "demo-staff"})
    assert accept(client, d["quote_id"], handoff_id=hid)["decision"]["kind"] == "otp"
    assert accept(client, d["quote_id"], handoff_id=hid, otp=OTP)["loan"] is not None


def test_a_declined_application_cannot_be_accepted(client):
    verify(client)
    d = decide(client, monthly_income_inr=10000)
    assert d["decision"]["eligible"] is False and d["kfs"] is None
    assert client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown").status_code == 404
    assert client.post("/api/consumer/loan/accept", json={"quote_id": d["quote_id"], "consent_offer": True,
                                                         "consent_credit_check": True}).status_code == 409


def test_bad_loan_input_is_rejected(client):
    assert client.post("/api/consumer/loan/decide", json=loan_app(tenure_months=7)).status_code == 422
    assert client.post("/api/consumer/loan/decide", json=loan_app(employment="pirate")).status_code == 422
    assert client.post("/api/consumer/loan/decide", json=loan_app(monthly_income_inr=-1)).status_code == 422


# ------------------------------------------------------------------ database safety
def test_audit_log_and_consents_are_append_only(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    verify(client)
    buy(client, q)
    c = me(client)
    for sql in ("UPDATE consumer_audit SET verdict='ALLOW'", "DELETE FROM consumer_audit",
                "UPDATE consumer_consents SET action='withdrawn'", "DELETE FROM consumer_consents"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(sql)


def test_the_database_refuses_an_execution_without_an_allow_decision(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    r = buy(client, q)  # KYC pending -> BLOCK
    c = me(client)
    with pytest.raises(sqlite3.IntegrityError, match="ALLOW"):
        c.execute("INSERT INTO consumer_executions(user_id,kind,ref_id,audit_id,idempotency_key,created_at) VALUES('demo-user','issue_policy','PHI-1',?,'k1','now')", (r["audit_id"],))


def test_other_users_data_is_not_reachable(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    c = me(client)
    assert store.get_quote(c, "demo-user", q, "health") is not None
    assert store.get_quote(c, "someone-else", q, "health") is None
    assert store.get_quote(c, "demo-user", q, "loan") is None  # right owner, wrong kind


# ------------------------------------------------------------------ privacy and auth
def test_no_raw_pii_is_stored_anywhere(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal(members=FAMILY)).json()["quote_id"]
    verify(client)
    assert buy(client, q)["policy"] is not None
    d = decide(client)
    client.post(f"/api/consumer/loan/quotes/{d['quote_id']}/kfs-shown")
    accept(client, d["quote_id"])
    c = db.connect()
    dump = ""
    for t in [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'consumer_%'")]:
        dump += repr([tuple(r) for r in c.execute(f"SELECT * FROM {t}")])
    for secret in (PAN, MOBILE, NAME, "400001", "Demo Sharma Jr"):
        assert secret not in dump, f"raw PII leaked into storage: {secret}"
    assert "XXXXX1234F" in dump  # the masked form is what is kept


def test_activity_lists_the_decisions_and_never_pii(client):
    q = client.post("/api/consumer/insurance/quote", json=proposal()).json()["quote_id"]
    verify(client)
    buy(client, q)
    rows = client.get("/api/consumer/activity").json()
    actions = {(r["actor"], r["action"], r["verdict"]) for r in rows}
    assert ("guardrail", "issue_policy", "ALLOW") in actions and ("agent", "issue_policy", "ALLOW") in actions
    assert PAN not in repr(rows) and MOBILE not in repr(rows)


def test_signed_out_callers_are_refused_when_auth_is_on(client, monkeypatch):
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "supabase")
    assert client.get("/api/consumer/policies").status_code == 401
    assert client.get("/api/consumer/policies", headers={"Authorization": "Bearer abc"}).status_code == 503  # not configured here


def test_public_config_needs_no_login_and_lists_the_options(client, monkeypatch):
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "supabase")
    c = client.get("/api/consumer/config").json()
    assert c["loan"]["tenures_months"] == [6, 12, 18, 24, 36] and c["insurance"]["auto_limit_inr"] == 30000
