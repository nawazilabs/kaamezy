"""Cross-checking the documents of one application: they must agree with each other, an approval letter must match the
insurer's own (simulated) record, nothing may have been used before, and a disagreement is reported to a person, never
settled by a guess. The five fixture documents in tests/fixtures/consumer_docs are one consistent story (Rahul Sharma,
City Care Hospital, claim CLM/2026/00012345); each test breaks one thing.

Owner: Member 1
"""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas.consumer import api, docs, insurer_registry, store, verify

FX = Path(__file__).parent / "fixtures" / "consumer_docs"
BASE = "/api/consumer"
NAMES = ("policy", "bill", "discharge", "claim_ack", "claim_approval")


def text(name: str) -> str:
    return (FX / f"{name}.txt").read_text(encoding="utf-8")


def texts(**changes) -> dict[str, str]:
    """The five documents; `changes` is {name: (old, new)} to alter a document the way a mistake or a forgery would."""
    out = {n: text(n) for n in NAMES}
    for n, (old, new) in changes.items():
        assert old in out[n], (n, old)
        out[n] = out[n].replace(old, new)
    return out


def readings(t: dict[str, str], drop=(), corrections=None) -> list[dict]:
    rs = []
    for n in NAMES:
        if n in drop:
            continue
        r = docs.read_document([t[n]], use_ai=False)
        docs.apply_corrections(r, (corrections or {}).get(n))
        rs.append(r)
    return rs


def check(rep: dict, code: str) -> dict:
    return next(c for c in rep["checks"] if c["code"] == code)


def codes(rep: dict, status: str) -> list[str]:
    return [c["code"] for c in rep["checks"] if c["status"] == status]


def run(t=None, workflow="approved_claim", drop=(), corrections=None, **kw):
    kw.setdefault("declared_pre_existing", False)
    kw.setdefault("already_claimed_inr", 0)
    return verify.verify(readings(t or texts(), drop, corrections), workflow, **kw)


REC = insurer_registry.lookup("CLM/2026/00012345")


# ------------------------------------------------------------------ the consistent story
def test_the_five_documents_of_one_claim_are_consistent():
    rep = run()
    assert rep["verdict"] == "consistent" and rep["failed"] == [] and rep["missing_documents"] == []
    assert codes(rep, "warn") == [] and "not_checked" not in [c["status"] for c in rep["checks"]]
    for ok in ("DOCUMENTS_COMPLETE", "PATIENT_NAME_MATCHES", "POLICY_NUMBER_MATCHES", "INSURER_MATCHES", "HOSPITAL_MATCHES", "CLAIM_REFERENCE_MATCHES",
               "STAY_DATES_MATCH", "ADMITTED_INSIDE_POLICY_PERIOD", "CLAIMED_WITHIN_BILL", "CLAIMED_AMOUNT_MATCHES", "APPROVED_WITHIN_CLAIMED",
               "APPROVAL_CLOSE_TO_ESTIMATE", "INSURER_RECORD_CONFIRMS"):
        assert check(rep, ok)["status"] == "pass", ok


def test_the_report_carries_the_facts_the_next_step_needs_with_where_each_came_from():
    rep = run()
    f = {k: v["value"] for k, v in rep["facts"].items()}
    assert f["approved_inr"] == 132500 and f["claimed_inr"] == 200000 and f["bill_total_inr"] == 200000 and f["claim_reference"] == "CLM/2026/00012345"
    assert rep["facts"]["approved_inr"]["from"] == "claim_approval" and rep["facts"]["approved_inr"]["corrected"] is False
    assert rep["estimate"]["estimated_claim_inr"] == 129037 and rep["registry"] == {"found": True, "status": "approved", "claimed_inr": 200000, "approved_inr": 132500, "payment_expected": "2026-03-28"}


def test_same_documents_same_report_and_nothing_in_it_calls_anyone_a_fraudster():
    a, b = run(), run()
    assert a == b
    assert "fraud" not in json.dumps(a).lower()


# ------------------------------------------------------------------ names and organisations
@pytest.mark.parametrize("a,b,want", [("Rahul Sharma", "RAHUL SHARMA", "exact"), ("Mr. Rahul Sharma", "Rahul Sharma", "exact"), ("Sharma Rahul", "Rahul Sharma", "exact"),
                                      ("Rahul Kumar Sharma", "Rahul Sharma", "close"), ("R Sharma", "Rahul Sharma", "close"), ("Rahul K Sharma", "Rahul Sharma", "close"),
                                      ("Rohit Sharma", "Rahul Sharma", None), ("Rahul Sharma", "Rahul Verma", None), ("Sharma", "Rahul Sharma", None), ("", "Rahul Sharma", None)])
def test_names(a, b, want):
    assert verify.names_match(a, b) == want


@pytest.mark.parametrize("a,b,want", [("City Care Hospital", "CITY CARE HOSPITAL", True), ("Demo Suraksha Health Insurance Co. Ltd.", "DEMO SURAKSHA HEALTH INSURANCE CO. LTD", True),
                                      ("City Hospital", "City Care Hospital", False), ("City Care Hospital", "Sunrise Hospital", False), ("Demo Suraksha Health Insurance", "Demo Arogya Plus Insurance", False)])
def test_organisations(a, b, want):
    assert verify.org_match(a, b) is want


# ------------------------------------------------------------------ the documents contradict each other
def test_a_different_patient_name_is_a_fail():
    rep = run(texts(bill=("Patient Name: Rahul Sharma 34/M", "Patient Name: Rohit Verma 34/M")))
    assert check(rep, "PATIENT_NAME_DIFFERS")["status"] == "fail" and rep["verdict"] == "inconsistent"


def test_a_name_written_a_little_differently_is_a_warning_for_a_person():
    rep = run(texts(bill=("Patient Name: Rahul Sharma 34/M", "Patient Name: Rahul Kumar Sharma 34/M")))
    assert check(rep, "PATIENT_NAME_WRITTEN_DIFFERENTLY")["status"] == "warn" and rep["verdict"] == "needs_review"


def test_a_patient_who_is_not_the_policy_holder_is_a_warning_not_a_fail():
    t = texts(
        bill=("Patient Name: Rahul Sharma 34/M", "Patient Name: Priya Sharma 31/F"), discharge=("Patient Name: Rahul Sharma", "Patient Name: Priya Sharma"),
        claim_ack=("Patient Name: Rahul Sharma", "Patient Name: Priya Sharma"))
    rep = run(t, lookup=lambda ref: {**REC, "patient_name": "Priya Sharma"})
    assert check(rep, "PATIENT_IS_NOT_THE_HOLDER")["status"] == "warn" and rep["failed"] == [] and rep["verdict"] == "needs_review"


def test_a_different_policy_number_insurer_hospital_or_claim_reference_is_a_fail():
    assert "POLICY_NUMBER_DIFFERS" in run(texts(claim_approval=("Policy Number: SURK/H/2026/00045678", "Policy Number: SURK/H/2026/00099999")))["failed"]
    assert "INSURER_DIFFERS" in run(texts(claim_ack=("DEMO SURAKSHA HEALTH INSURANCE CO. LTD.", "DEMO AROGYA PLUS INSURANCE CO. LTD.")))["failed"]
    assert "HOSPITAL_DIFFERS" in run(texts(discharge=("CITY CARE HOSPITAL", "SUNRISE HOSPITAL")))["failed"]
    assert "CLAIM_REFERENCE_DIFFERS" in run(texts(claim_ack=("CLM/2026/00012345", "CLM/2026/00054321")))["failed"]


def test_a_reference_written_with_other_punctuation_is_still_the_same_reference():
    rep = run(texts(claim_ack=("CLM/2026/00012345", "CLM-2026-00012345")))
    assert check(rep, "CLAIM_REFERENCE_MATCHES")["status"] == "pass"


# ------------------------------------------------------------------ dates and amounts
def test_the_stay_on_the_bill_must_be_the_stay_on_the_discharge_summary():
    rep = run(texts(discharge=("15 March 2026", "18 March 2026")))
    assert "STAY_DATES_DIFFER" in rep["failed"]


def test_an_admission_outside_the_policy_period_is_a_fail():
    t = texts(bill=("Date of Admission: 12/03/2026    Date of Discharge: 15/03/2026", "Date of Admission: 12/03/2024    Date of Discharge: 15/03/2024"),
              discharge=("12 March 2026", "12 March 2024"))
    t["discharge"] = t["discharge"].replace("15 March 2026", "15 March 2024")
    assert "ADMITTED_OUTSIDE_POLICY_PERIOD" in run(t)["failed"]


def test_a_claim_dated_before_the_admission_is_a_fail():
    assert "CLAIM_BEFORE_ADMISSION" in run(texts(claim_ack=("Date of Claim: 18/03/2026", "Date of Claim: 01/03/2026")))["failed"]


def test_a_claim_for_more_than_the_bill_is_a_fail():
    rep = run(texts(claim_ack=("Claimed Amount: Rs. 2,00,000", "Claimed Amount: Rs. 3,00,000")))
    assert "CLAIMED_ABOVE_BILL" in rep["failed"] and "CLAIMED_AMOUNT_DIFFERS" in rep["failed"]


def test_an_approval_above_the_claim_is_a_fail():
    rep = run(texts(claim_approval=("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 2,50,000")))
    assert "APPROVED_ABOVE_CLAIMED" in rep["failed"]


# ------------------------------------------------------------------ the approval letter against the insurer's own record
def test_an_altered_approval_amount_does_not_match_the_insurer():
    rep = run(texts(claim_approval=("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 1,90,000")))
    c = check(rep, "INSURER_RECORD_DIFFERS")
    assert c["status"] == "fail" and "₹1,90,000" in c["message"] and "₹1,32,500" in c["message"] and rep["verdict"] == "inconsistent"


def test_a_claim_the_insurer_does_not_know_is_not_confirmed_and_goes_to_a_person():
    rep = run(texts(claim_ack=("CLM/2026/00012345", "CLM/2026/00099999"), claim_approval=("CLM/2026/00012345", "CLM/2026/00099999")))
    assert check(rep, "INSURER_RECORD_NOT_FOUND")["status"] == "warn" and rep["failed"] == [] and rep["verdict"] == "needs_review" and rep["registry"] is None


@pytest.mark.parametrize("status,code", [("settled", "INSURER_SAYS_SETTLED"), ("rejected", "INSURER_SAYS_REJECTED"), ("pending", "INSURER_SAYS_PENDING")])
def test_an_insurer_record_that_is_not_an_unpaid_approval_is_a_fail(status, code):
    assert code in run(lookup=lambda ref: {**REC, "status": status})["failed"]


def test_a_letter_that_says_the_claim_is_already_paid_or_rejected_is_a_fail():
    assert "LETTER_SAYS_SETTLED" in run(texts(claim_approval=("Claim Status: Approved, payment pending", "Claim Status: Settled and paid")))["failed"]
    assert "LETTER_SAYS_REJECTED" in run(texts(claim_approval=("Claim Status: Approved, payment pending", "Claim Status: Rejected")))["failed"]


def test_an_approval_far_above_the_estimate_is_flagged_for_a_person_even_if_the_insurer_record_agrees():
    t = texts(claim_approval=("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 1,90,000"))
    t["claim_approval"] = t["claim_approval"].replace("Total Deductions: Rs. 67,500", "Total Deductions: Rs. 10,000")
    rep = run(t, lookup=lambda ref: {**REC, "approved_inr": 190000})
    assert check(rep, "APPROVAL_ABOVE_ESTIMATE")["status"] == "warn" and check(rep, "INSURER_RECORD_CONFIRMS")["status"] == "pass" and rep["verdict"] == "needs_review"


def test_an_approval_where_the_policy_terms_suggest_nothing_would_be_paid_is_flagged():
    rep = run(declared_pre_existing=True)                     # 11 months in, the pre-existing wait is 36: the estimate is 0
    assert check(rep, "APPROVAL_AGAINST_ESTIMATE")["status"] == "warn"


# ------------------------------------------------------------------ an incomplete set
def test_a_missing_document_makes_the_set_incomplete_and_skips_what_needs_it():
    rep = run(drop=("claim_approval",))
    assert rep["verdict"] == "incomplete" and rep["missing_documents"] == ["claim_approval"] and rep["registry"] is None
    assert check(rep, "DOCUMENTS_MISSING")["status"] == "not_checked"


def test_the_pre_approval_workflow_needs_only_the_policy_and_the_bill():
    rep = run(workflow="preapproval", drop=("discharge", "claim_ack", "claim_approval"))
    assert rep["missing_documents"] == [] and rep["verdict"] == "consistent" and rep["estimate"]["estimated_claim_inr"] == 129037
    assert run(workflow="preapproval", drop=("policy", "discharge", "claim_ack", "claim_approval"))["verdict"] == "incomplete"


def test_a_document_that_is_not_recognised_is_reported_and_not_used():
    rs = readings(texts()) + [docs.read_document(["Grocery list\nMilk Rs. 60\nThank you"], use_ai=False)]
    rep = verify.verify(rs, "approved_claim", declared_pre_existing=False, already_claimed_inr=0)
    assert check(rep, "DOCUMENT_NOT_RECOGNISED")["status"] == "warn" and rep["verdict"] == "needs_review"


def test_the_same_document_uploaded_twice_is_a_fail_and_a_second_bill_is_not_used():
    rs = readings(texts())
    rs.append(docs.read_document([text("bill")], use_ai=False))
    rep = verify.verify(rs, "approved_claim", declared_pre_existing=False, already_claimed_inr=0)
    assert "SAME_DOCUMENT_TWICE" in rep["failed"] and "MORE_THAN_ONE_OF_A_TYPE" in codes(rep, "warn")


# ------------------------------------------------------------------ a reading the person corrected
def test_a_value_the_person_typed_is_marked_as_typed_and_does_not_bypass_the_insurer_record():
    t = texts()
    t["claim_approval"] = t["claim_approval"].replace("Approved Amount: Rs. 1,32,500\n", "")
    missing = run(t)
    assert check(missing, "DOCUMENT_FIELDS_MISSING")["status"] == "warn" and missing["verdict"] == "needs_review"
    fixed = run(t, corrections={"claim_approval": {"approved_inr": 132500}})
    assert fixed["verdict"] == "consistent" and fixed["facts"]["approved_inr"]["corrected"] is True
    assert [d["corrected"] for d in fixed["documents"] if d["doc_type"] == "claim_approval"] == [["approved_inr"]]
    lied = run(t, corrections={"claim_approval": {"approved_inr": 190000}})
    assert "INSURER_RECORD_DIFFERS" in lied["failed"]          # typing a bigger number changes nothing: the insurer's record decides


def test_corrections_must_name_a_real_field_and_read_as_that_kind_of_value():
    r = docs.read_document([text("claim_approval")], use_ai=False)
    for bad in ({"nope": 1}, {"approved_inr": "lots"}, {"expected_payment_date": "tomorrow"}):
        with pytest.raises(ValueError):
            docs.apply_corrections(r, bad)
    docs.apply_corrections(r, {"expected_payment_date": "30/03/2026", "approved_inr": "1,40,000"})
    assert r["fields"]["expected_payment_date"]["value"] == "2026-03-30" and r["fields"]["approved_inr"]["value"] == 140000 and r["fields"]["approved_inr"]["how"] == "user"


# ------------------------------------------------------------------ used before
def test_a_document_used_for_another_application_goes_to_a_person():
    rep = run(used_elsewhere=lambda kind, fp: kind == "claim")
    assert check(rep, "USED_IN_ANOTHER_APPLICATION")["status"] == "warn" and rep["verdict"] == "needs_review" and "this claim" in check(rep, "USED_IN_ANOTHER_APPLICATION")["message"]


def test_fingerprints_are_hashes_only_and_change_when_the_bill_changes():
    fps = run()["fingerprints"]
    assert {f["kind"] for f in fps} == {"file", "bill", "claim"} and len([f for f in fps if f["kind"] == "file"]) == 5
    assert all(len(f["fingerprint"]) == 64 and set(f["fingerprint"]) <= set("0123456789abcdef") for f in fps)
    assert fps == run()["fingerprints"]
    other = run(texts(bill=("CCH/IP/26/009812", "CCH/IP/26/009999")))["fingerprints"]
    assert [f for f in fps if f["kind"] == "bill"] != [f for f in other if f["kind"] == "bill"]


# ------------------------------------------------------------------ the API
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "verify.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    monkeypatch.setenv("CHAUKAS_REPLAY_MODE", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def body(t=None, drop=(), **over) -> dict:
    t = t or texts()
    d = {"workflow": "approved_claim", "documents": [{"pages": [t[n]], "page_sources": ["text"]} for n in NAMES if n not in drop],
         "declared_pre_existing": False, "already_claimed_inr": 0, "use_ai": False}
    d.update(over)
    return d


def post(c, **kw):
    r = c.post(f"{BASE}/docs/verify", json=body(**kw))
    assert r.status_code == 200, r.text
    return r.json()


def test_verify_endpoint_happy_path(client):
    rep = post(client)
    assert rep["verdict"] == "consistent" and rep["recorded"] is False and rep["application_id"] is None and "fingerprints" not in rep


def test_recording_marks_the_documents_as_used_and_a_second_application_is_flagged(client):
    first = post(client, record=True)
    assert first["recorded"] is True and first["application_id"].startswith("APP-") and first["verdict"] == "consistent"
    again = post(client)                                              # a new application with the same documents
    assert check(again, "USED_IN_ANOTHER_APPLICATION")["status"] == "warn" and again["verdict"] == "needs_review"
    own = post(client, application_id=first["application_id"], record=True)    # the same application again: not a duplicate of itself
    assert own["verdict"] == "consistent" and own["application_id"] == first["application_id"]
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM consumer_doc_fingerprints").fetchone()[0] == 7           # 5 files + 1 bill + 1 claim, once


def test_nothing_is_recorded_when_the_documents_contradict_each_other_or_are_incomplete(client):
    bad = post(client, t=texts(claim_approval=("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 1,90,000")), record=True)
    assert bad["verdict"] == "inconsistent" and bad["recorded"] is False and bad["application_id"] is None
    short = post(client, drop=("claim_approval",), record=True)
    assert short["verdict"] == "incomplete" and short["recorded"] is False
    assert store.connect().execute("SELECT COUNT(*) FROM consumer_doc_fingerprints").fetchone()[0] == 0


def test_a_client_cannot_choose_the_id_of_an_application(client):
    rep = post(client, record=True, application_id="APP-11111111")
    assert rep["recorded"] is True and rep["application_id"] != "APP-11111111"


def test_another_user_reusing_the_documents_is_found_by_hash(client):
    conn = store.connect()
    fps = run()["fingerprints"]
    store.record_fingerprints(conn, "someone-else", "APP-22222222", fps)
    conn.commit()
    assert check(post(client), "USED_IN_ANOTHER_APPLICATION")["status"] == "warn"


def test_the_fingerprint_table_holds_only_hashes_and_is_append_only(client):
    post(client, record=True)
    conn = store.connect()
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM consumer_doc_fingerprints")])
    for word in ("Rahul", "Sharma", "City Care", "CLM/2026", "132500", "009812"):
        assert word not in dump
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE consumer_doc_fingerprints SET kind='file'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM consumer_doc_fingerprints")


def test_the_audit_row_has_the_verdict_and_no_words(client):
    post(client, t=texts(claim_approval=("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 1,90,000")))
    rows = [dict(r) for r in store.connect().execute("SELECT * FROM consumer_audit WHERE action='doc_verify'")]
    assert len(rows) == 1 and "verdict=inconsistent" in rows[0]["note"] and "INSURER_RECORD_DIFFERS" in rows[0]["note"] and rows[0]["amount"] is None
    dump = json.dumps(rows)
    for word in ("Rahul", "Sharma", "1,90,000", "190000", "City Care"):
        assert word not in dump


def test_verify_endpoint_applies_corrections_and_rejects_bad_ones(client):
    t = texts()
    t["claim_approval"] = t["claim_approval"].replace("Approved Amount: Rs. 1,32,500\n", "")
    b = body(t)
    b["documents"][4]["corrections"] = {"approved_inr": 132500}
    assert client.post(f"{BASE}/docs/verify", json=b).json()["verdict"] == "consistent"
    b["documents"][4]["corrections"] = {"approved_inr": "lots"}
    r = client.post(f"{BASE}/docs/verify", json=b)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BAD_CORRECTION"


def test_verify_endpoint_validation(client):
    assert client.post(f"{BASE}/docs/verify", json=body(workflow="nope")).status_code == 422
    assert client.post(f"{BASE}/docs/verify", json={**body(), "documents": []}).status_code == 422
    nine = {**body(), "documents": body()["documents"] * 2}
    assert client.post(f"{BASE}/docs/verify", json=nine).status_code == 422
    r = client.post(f"{BASE}/docs/verify", json={**body(), "documents": [{"pages": ["   "]}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DOC_UNREADABLE"
    assert client.post(f"{BASE}/docs/verify", json=body(application_id="not-an-id")).status_code == 422


def test_verifying_creates_no_claim_loan_quote_or_execution(client):
    post(client, record=True)
    conn = store.connect()
    for table in ("consumer_claims", "consumer_loans", "consumer_quotes", "consumer_policies", "consumer_executions"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
