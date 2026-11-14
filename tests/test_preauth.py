"""Phase 7, Workflow A: an advance BEFORE the claim, for the hospital deposit.

Sized only from the conservative figure of a verified estimate (an estimate is not an approval), paid to a partner hospital from the
registry (never to the person, never a typed payee), always behind a one-time code, one estimate backs one advance, and it is closed
by the approved claim that follows. The fixture policy + bill stand in for the policy and the hospital cost estimate.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas.consumer import api, engine, hospital_registry, store
from tests.test_claim_bridge import MOBILE, OTP, PAN, shown
from tests.test_doc_verify import texts

BASE = "/api/consumer"
HOLDER = "Rahul Sharma"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "preauth.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    monkeypatch.setenv("CHAUKAS_REPLAY_MODE", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def post(c, t=None, names=("policy", "bill"), **over):
    t = t or texts()
    b = {"documents": [{"pages": [t[n]], "page_sources": ["text"]} for n in names], "declared_pre_existing": False, "already_claimed_inr": 0, "use_ai": False}
    b.update(over)
    r = c.post(f"{BASE}/preauth/from-documents", json=b)
    assert r.status_code == 200, r.text
    return r.json()


def decide(c, pid):
    return c.post(f"{BASE}/preauth/decide", json={"preauth_id": pid}).json()


def accept(c, qid, **over):
    body = {"quote_id": qid, "consent_offer": True, "consent_claim_share": True, "pan": PAN, "mobile": MOBILE, "holder_name": HOLDER, "pincode": "400001"}
    body.update(over)
    return c.post(f"{BASE}/preauth/accept", json=body).json()


def ready(c):
    pre = post(c)["preauth"]
    assert c.post(f"{BASE}/kyc/digilocker-demo").json()["verified"] is True
    d = decide(c, pre["id"])
    shown(c, d["quote_id"])
    return pre, d


def rows(table):
    return store.connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_an_offer_is_sized_from_the_conservative_estimate_only(client):
    r = post(client)
    assert r["eligible"] is True and r["verdict"] == "consistent"
    p, e = r["preauth"], r["estimate"]
    assert p["id"].startswith("PRE-") and p["payee_name"] == "City Care Hospital" and p["state"] == "open"
    assert p["conservative_inr"] == e["conservative_inr"] < p["estimated_inr"] == e["estimated_claim_inr"]
    cfg = engine.cfg()["preauth"]
    expect = min(cfg["cap_inr"], p["conservative_inr"] * cfg["share_pct"] // 100) // cfg["round_to_inr"] * cfg["round_to_inr"]
    assert p["advance_up_to_inr"] == expect > 0
    d = decide(client, p["id"])
    k = d["kfs"]
    assert d["decision"]["amount_inr"] == expect and k["payee_name"] == "City Care Hospital" and "never to you" in k["paid_to"]
    assert "not an approval" in k["basis"] and k["insurance_required"] is False


def test_the_client_cannot_choose_the_payee_the_amount_or_the_hospital(client):
    r = post(client, payee_name="My Own Account", hospital_id="HSP-9999", amount_inr=999999, conservative_inr=999999)
    assert r["preauth"]["payee_name"] == "City Care Hospital" and r["preauth"]["conservative_inr"] == r["estimate"]["conservative_inr"]


def test_the_ladder_always_needs_a_one_time_code_and_ends_in_an_advance_to_the_hospital(client):
    pre, d = ready(client)
    wrong = accept(client, d["quote_id"], holder_name="Imran Khan", otp=OTP)
    assert wrong["decision"]["reason_code"] == "PREAUTH_HOLDER_MISMATCH" and wrong["loan"] is None
    first = accept(client, d["quote_id"])
    assert first["decision"]["verdict"] == "NEEDS_CONFIRMATION" and first["decision"]["reason_code"] == "OTP_REQUIRED"
    assert accept(client, d["quote_id"], otp="000000")["decision"]["verdict"] == "NEEDS_CONFIRMATION"
    ok = accept(client, d["quote_id"], otp=OTP)
    assert ok["decision"]["verdict"] == "ALLOW" and ok["paid_to"] == "City Care Hospital" and "paid to City Care Hospital" in ok["loan"]["purpose"]
    again = accept(client, d["quote_id"], otp=OTP)
    assert again["replayed"] is True and rows("consumer_loans") == 1
    assert decide(client, pre["id"])["decision"]["reasons"] == ["ADVANCE_ALREADY_TAKEN"]


def test_consents_and_kyc_are_checked_on_the_server(client):
    pre = post(client)["preauth"]
    d = decide(client, pre["id"])
    assert accept(client, d["quote_id"], otp=OTP)["decision"]["reason_code"] == "KYC_PENDING"
    client.post(f"{BASE}/kyc/digilocker-demo")
    assert accept(client, d["quote_id"], otp=OTP)["decision"]["reason_code"] == "KFS_NOT_SHOWN"
    shown(client, d["quote_id"])
    assert accept(client, d["quote_id"], consent_claim_share=False, otp=OTP)["decision"]["reason_code"] == "CONSENT_MISSING"
    assert rows("consumer_loans") == 0


def test_a_personal_loan_route_refuses_this_offer(client):
    _, d = ready(client)
    r = client.post(f"{BASE}/loan/accept", json={"quote_id": d["quote_id"], "consent_offer": True, "consent_credit_check": True, "pan": PAN,
                                                 "mobile": MOBILE, "holder_name": HOLDER, "pincode": "400001"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "WRONG_OFFER_TYPE"


def test_asking_again_replays_and_one_estimate_backs_one_advance(client):
    a, b = post(client)["preauth"], post(client)["preauth"]
    assert a["id"] == b["id"] and rows("consumer_preauth") == 1
    conn = store.connect()
    conn.execute("UPDATE consumer_preauth SET user_id='someone-else'")
    conn.commit()
    r = post(client)
    assert r["eligible"] is False and r["reasons"] == ["ESTIMATE_ALREADY_USED"] and r["preauth"] is None


def test_a_hospital_that_is_not_a_partner_gets_no_offer_and_is_not_called_fake(client):
    t = texts()
    t["bill"] = t["bill"].replace("City Care Hospital", "Corner Clinic").replace("CITY CARE HOSPITAL", "CORNER CLINIC")
    r = post(client, t)
    assert r["eligible"] is False and r["reasons"] == ["HOSPITAL_NOT_A_PARTNER"] and rows("consumer_preauth") == 0
    assert hospital_registry.lookup("Patel Memorial Hospital") is None
    assert hospital_registry.lookup("City Care Hospital")["id"] == "HSP-1001"


def test_documents_that_disagree_or_are_incomplete_make_no_offer(client):
    t = texts()
    t["bill"] = t["bill"].replace("Rahul Sharma", "Someone Else")
    assert post(client, t)["reasons"] == ["DOCUMENTS_NEEDS_REVIEW"]      # a patient who is not the holder: a person decides
    assert post(client, names=("policy",))["reasons"] == ["DOCUMENTS_INCOMPLETE"]
    assert rows("consumer_preauth") == 0


# ------------------------------------------------------------------ closing the advance with the claim that follows
def _issue(c):
    pre, d = ready(c)
    ok = accept(c, d["quote_id"], otp=OTP)
    assert ok["decision"]["verdict"] == "ALLOW"
    return pre, ok["loan"]


def _claim(c):
    from tests.test_bridge_documents import post as post_claim
    r = post_claim(c)
    assert r["eligible"], r
    return r["claim"]


def test_the_approved_claim_closes_the_advance_and_cannot_back_another(client):
    pre, loan = _issue(client)
    claim = _claim(client)
    r = client.post(f"{BASE}/preauth/close", json={"preauth_id": pre["id"], "claim_id": claim["id"]})
    assert r.status_code == 200 and r.json()["state"] == "closed" and r.json()["claim_id"] == claim["id"]
    again = client.post(f"{BASE}/preauth/close", json={"preauth_id": pre["id"], "claim_id": claim["id"]})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "ALREADY_CLOSED"
    advance = client.post(f"{BASE}/bridge/decide", json={"claim_id": claim["id"]}).json()["decision"]
    assert advance["eligible"] is False and advance["reasons"] == ["ADVANCE_ALREADY_TAKEN"]


def test_a_claim_too_small_for_what_is_owed_does_not_close_it(client):
    pre, loan = _issue(client)
    claim = _claim(client)
    conn = store.connect()
    conn.execute("UPDATE consumer_verified_claims SET approved_inr=? WHERE id=?", (loan["total_inr"] - 1, claim["id"]))
    conn.commit()
    r = client.post(f"{BASE}/preauth/close", json={"preauth_id": pre["id"], "claim_id": claim["id"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLAIM_TOO_SMALL"


def test_closing_needs_an_advance_and_the_same_holder(client):
    pre, d = ready(client)
    claim = _claim(client)
    r = client.post(f"{BASE}/preauth/close", json={"preauth_id": pre["id"], "claim_id": claim["id"]})
    assert r.json()["detail"]["code"] == "NO_ADVANCE_TO_CLOSE"
    assert accept(client, d["quote_id"], otp=OTP)["decision"]["verdict"] == "ALLOW"
    conn = store.connect()
    conn.execute("UPDATE consumer_verified_claims SET holder_name='Imran Khan' WHERE id=?", (claim["id"],))
    conn.commit()
    assert client.post(f"{BASE}/preauth/close", json={"preauth_id": pre["id"], "claim_id": claim["id"]}).json()["detail"]["code"] == "CLAIM_HOLDER_MISMATCH"
    assert client.post(f"{BASE}/preauth/close", json={"preauth_id": pre["id"], "claim_id": "CLM-12345678"}).status_code == 422


def test_an_old_estimate_is_not_financed(client):
    pre = post(client)["preauth"]
    conn = store.connect()
    conn.execute("UPDATE consumer_preauth SET created_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert decide(client, pre["id"])["decision"]["reasons"] == ["ESTIMATE_TOO_OLD"]
