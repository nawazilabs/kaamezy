"""Phase 6, Workflow B: an advance sized from VERIFIED facts (POST /bridge/from-documents).

The amount and the payout date come from the insurer's own (simulated) record, never from the letter or the client. A claim is
created only for a `consistent` set; a claim backs one advance for everybody; the advance goes only to the policy holder named on
the documents; the usual consent / KYC / OTP ladder follows. Also the demo switch for a policy bought in this app.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas.consumer import api, estimate, store
from tests.test_doc_verify import NAMES, texts
from tests.test_claim_bridge import PAN, MOBILE, OTP, accept, decide, shown

BASE = "/api/consumer"
HOLDER = "Rahul Sharma"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "bridge_docs.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    monkeypatch.setenv("CHAUKAS_REPLAY_MODE", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def post(c, t=None, **over):
    t = t or texts()
    b = {"documents": [{"pages": [t[n]], "page_sources": ["text"]} for n in NAMES], "declared_pre_existing": False, "already_claimed_inr": 0, "use_ai": False}
    b.update(over)
    r = c.post(f"{BASE}/bridge/from-documents", json=b)
    assert r.status_code == 200, r.text
    return r.json()


def rows(table="consumer_verified_claims"):
    return store.connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_verified_claim_is_sized_from_the_insurer_record(client):
    r = post(client)
    assert r["eligible"] is True and r["reasons"] == [] and r["verdict"] == "consistent"
    c = r["claim"]
    assert c["id"].startswith("VCL-") and c["approved_inr"] == 132500 and c["claimed_inr"] == 200000 and c["state"] == "approved_payout_pending"
    assert c["bridge_up_to_inr"] == 105000                      # 80% of 1,32,500 = 1,06,000, rounded down to 5,000
    d = decide(client, c["id"])
    assert d["decision"]["eligible"] and d["decision"]["amount_inr"] == 105000 and d["decision"]["days"] == 10
    assert d["kfs"]["repaid_from"] == "your insurance claim payout" and d["kfs"]["insurance_required"] is False


def test_a_client_cannot_supply_an_amount_a_date_or_a_name(client):
    r = post(client, approved_inr=999999, payout_due="2030-01-01", holder_name="Someone Else", claim={"approved_inr": 999999})
    assert r["claim"]["approved_inr"] == 132500 and r["claim"]["payout_due"] != "2030-01-01"


def test_the_whole_ladder_ends_in_an_advance_to_the_holder_only(client):
    c = post(client)["claim"]
    assert client.post(f"{BASE}/kyc/digilocker-demo").json()["verified"] is True
    d = decide(client, c["id"])
    shown(client, d["quote_id"])
    wrong = accept(client, d["quote_id"], holder_name="Imran Khan", otp=OTP)
    assert wrong["decision"]["verdict"] == "BLOCK" and wrong["decision"]["reason_code"] == "CLAIM_HOLDER_MISMATCH" and wrong["loan"] is None
    assert accept(client, d["quote_id"], holder_name=HOLDER)["decision"]["verdict"] == "NEEDS_CONFIRMATION"      # 1,05,000 is over the auto limit
    ok = accept(client, d["quote_id"], holder_name=HOLDER, otp=OTP)
    assert ok["decision"]["verdict"] == "ALLOW" and ok["loan"]["amount_inr"] == 105000
    again = post(client)                                           # the same documents again: the claim is already backing an advance
    assert again["claim"]["bridge_loan_id"] and again["claim"]["bridge_up_to_inr"] is None and again["claim"]["bridge_blocked"] == "ADVANCE_ALREADY_TAKEN"


def test_asking_again_replays_and_creates_nothing_new(client):
    a, b = post(client), post(client)
    assert a["claim"]["id"] == b["claim"]["id"] and rows() == 1


def test_a_real_claim_backs_one_advance_whoever_uploads_it(client):
    first = post(client)["claim"]
    conn = store.connect()
    conn.execute("UPDATE consumer_verified_claims SET user_id='someone-else' WHERE id=?", (first["id"],))
    conn.commit()
    r = post(client)
    assert r["eligible"] is False and r["reasons"] == ["CLAIM_ALREADY_USED"] and r["claim"] is None and rows() == 1


@pytest.mark.parametrize("changes,reason,verdict", [
    ({"claim_approval": ("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 1,90,000")}, "DOCUMENTS_INCONSISTENT", "inconsistent"),
    ({"bill": ("Rahul Sharma", "Someone Else")}, "DOCUMENTS_INCONSISTENT", "inconsistent"),
])
def test_documents_that_contradict_make_no_claim_and_no_advance(client, changes, reason, verdict):
    r = post(client, texts(**changes))
    assert r["eligible"] is False and r["reasons"] == [reason] and r["verdict"] == verdict and r["claim"] is None
    assert rows() == 0 and rows("consumer_doc_fingerprints") == 0


def test_a_claim_the_insurer_does_not_know_goes_to_a_person_not_to_an_advance(client):
    t = texts()
    for n in ("claim_ack", "claim_approval"):
        t[n] = t[n].replace("CLM/2026/00012345", "CLM/2026/00099999")
    r = post(client, t)
    assert r["eligible"] is False and r["reasons"] == ["DOCUMENTS_NEEDS_REVIEW"] and rows() == 0


def test_a_missing_document_means_no_claim(client):
    t = texts()
    r = client.post(f"{BASE}/bridge/from-documents", json={"documents": [{"pages": [t["policy"]]}], "use_ai": False}).json()
    assert r["eligible"] is False and r["reasons"] == ["DOCUMENTS_INCOMPLETE"]


def test_an_unusual_but_consistent_set_still_waits_for_a_person(client):
    t = texts()
    t["policy"] = t["policy"].replace("01/04/2025", "01/03/2026").replace("Policy Period To: 31/03/2026", "Policy Period To: 28/02/2027")
    r = post(client, t)
    assert r["eligible"] is False and rows() == 0 and r["verdict"] != "consistent"


# ------------------------------------------------------------------ the demo switch for a policy bought in this app
def _bill_and_policy(c):
    from tests.test_claim_estimate import bill, buy
    import json
    from datetime import date, timedelta
    pol = buy(c)
    start = date.fromisoformat(pol["start_date"])
    return pol, json.loads(bill(admission_date=(start + timedelta(days=3)).isoformat()).model_dump_json())


def test_the_demo_switch_is_off_by_default_and_a_new_policy_is_not_paid(client):
    pol, b = _bill_and_policy(client)
    r = client.post(f"{BASE}/claims/estimate", json={"policy_id": pol["id"], "bill": b, "declared_pre_existing": False}).json()
    assert r["status"] == "likely_ineligible" and r["demo_waivers"] == [] and r["demo_notice"] is None


def test_the_demo_switch_shows_the_waiting_period_but_does_not_apply_it_and_says_so(client, monkeypatch):
    monkeypatch.setenv("KAAMEZY_DEMO_WAIVE_WAITING", "1")
    pol, b = _bill_and_policy(client)
    r = client.post(f"{BASE}/claims/estimate", json={"policy_id": pol["id"], "bill": b, "declared_pre_existing": False}).json()
    assert r["status"] == "likely_eligible" and [w["code"] for w in r["demo_waivers"]] == ["WAITING_INITIAL"]
    assert "DEMO MODE" in r["demo_notice"] and "would not pay" in r["demo_notice"]


def test_the_demo_switch_never_applies_to_a_policy_read_from_a_document(client, monkeypatch):
    monkeypatch.setenv("KAAMEZY_DEMO_WAIVE_WAITING", "1")
    from chaukas.consumer import docs
    pol = docs.read_document([texts()["policy"]], use_ai=False)
    assert estimate.terms_from_reading(pol).waiting_waived_demo is False
