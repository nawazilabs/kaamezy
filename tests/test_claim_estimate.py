"""Claim estimator: what a policy would probably pay on a bill. Known answers worked out by hand, every rule on its own,
the switches that change the order of the steps, and invariants that must hold for ANY input.

An estimate is not an approval: it comes with a disclaimer, a confidence, what is missing and what is uncertain, and a
conservative figure. Nothing is stored or paid because of it.

Owner: Member 1
"""
import copy
import json
import random
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chaukas.consumer import api, estimate, store
from chaukas.consumer.models import BillFacts, ClaimEstimateRequest, PolicyTerms

BASE = "/api/consumer"
START = "2025-04-01"


def terms(**over) -> PolicyTerms:
    t = {"sum_insured_inr": 500000, "room_rent": {"kind": "per_day_inr", "inr": 5000}, "copay_pct": 10, "deductible_inr": 5000,
         "initial_waiting_days": 30, "ped_waiting_months": 36, "specific_waiting_months": 24, "policy_start": START,
         "refs": {"room_rent": {"text": "Room Rent: Up to Rs. 5,000 per day", "page": 1}, "copay_pct": {"text": "Co-payment: 10%", "page": 2}}}
    t.update(over)
    return PolicyTerms.model_validate(t)


def item(desc, cat, amount, days=None, rate=None) -> dict:
    d = {"description": desc, "category": cat, "amount_inr": amount}
    if days:
        d.update(days=days, rate_inr_per_day=rate)
    return d


# the bill of tests/fixtures/consumer_docs/bill.txt
SAMPLE = [item("Room Rent (Deluxe Room)", "room", 24000, 3, 8000), item("ICU Nursing Charges", "room", 9000),
          item("Consultant Visit Charges", "doctor", 12000), item("Surgeon Fees", "doctor", 40000),
          item("Operation Theatre Charges", "surgery", 25000), item("Pharmacy and Medicines", "medicine", 31500),
          item("Investigations (Lab, Ultrasound, CT scan)", "diagnostic", 38500), item("Anaesthesia Charges", "doctor", 10000),
          item("Miscellaneous Charges", "other", 10000)]


def bill(items=None, **over) -> BillFacts:
    b = {"items": items or SAMPLE, "total_inr": None, "admission_date": "2026-03-12", "discharge_date": "2026-03-15",
         "diagnosis": "Acute Appendicitis", "procedure": "Laparoscopic Appendectomy", "hospital_network": True}
    b.update(over)
    return BillFacts.model_validate(b)


def run(t=None, b=None, **kw):
    kw.setdefault("declared_pre_existing", False)
    kw.setdefault("utilised_inr", 0)
    return estimate.estimate_claim(t or terms(), b or bill(), **kw)


def codes(r) -> list[str]:
    return [s["code"] for s in r["steps"]]


def step(r, code) -> dict:
    return next(s for s in r["steps"] if s["code"] == code)


# ------------------------------------------------------------------ known answers, worked out by hand
def test_the_sample_bill_known_answer():
    r = run()
    # 2,00,000 - 10,000 (miscellaneous) - 9,000 (room 8,000 -> 5,000 a day for 3 days)
    #          - 32,625 (doctors, surgeon, anaesthesia, OT: 87,000 x 15,000/24,000 = 54,375) - 5,000 deductible
    #          = 1,43,375, less 10% co-pay rounded up (14,338) = 1,29,037
    assert codes(r) == ["BILL", "NOT_PAYABLE", "ROOM_RENT_CAP", "ROOM_RENT_PROPORTIONATE", "DEDUCTIBLE", "COPAY"]
    assert [s["payable_after_inr"] for s in r["steps"]] == [200000, 190000, 181000, 148375, 143375, 129037]
    assert (step(r, "ROOM_RENT_CAP")["deduction_inr"], step(r, "ROOM_RENT_PROPORTIONATE")["deduction_inr"], step(r, "COPAY")["deduction_inr"]) == (9000, 32625, 14338)
    assert r["estimated_claim_inr"] == 129037 and r["deductions_total_inr"] == 70963 and r["status"] == "likely_eligible"
    assert r["confidence"] == {"score": 1.0, "level": "high", "haircut_pct": 5}
    assert r["conservative_inr"] == 129037 * 95 // 100 == 122585


def test_a_plain_bill_with_a_room_over_the_limit():
    # room 3 x 8,000 = 24,000 and 1,76,000 of medicines: room capped to 15,000 (-9,000); 1,91,000 - 5,000 = 1,86,000; co-pay 18,600
    r = run(b=bill([item("Room Rent", "room", 24000, 3, 8000), item("Pharmacy", "medicine", 176000)]))
    assert r["estimated_claim_inr"] == 167400 and codes(r) == ["BILL", "ROOM_RENT_CAP", "DEDUCTIBLE", "COPAY"]


def test_no_room_limit_means_no_room_deduction():
    r = run(terms(room_rent={"kind": "no_limit"}))
    assert "ROOM_RENT_CAP" not in codes(r)
    assert r["estimated_claim_inr"] == 190000 - 5000 - 18500 == 166500


def test_a_limit_as_a_share_of_the_sum_insured_equals_the_same_amount_per_day():
    a = run(terms(room_rent={"kind": "pct_of_sum_insured_per_day", "pct": 1}))
    assert a["estimated_claim_inr"] == run()["estimated_claim_inr"] == 129037


def test_a_room_category_limit_is_flagged_not_guessed():
    r = run(terms(room_rent={"kind": "single_private_room"}))
    assert "ROOM_RENT_CAP" not in codes(r) and "ROOM_CATEGORY_UNCHECKED" in [u["code"] for u in r["uncertainties"]]
    assert r["confidence"]["score"] == 0.92


def test_icu_charges_are_not_scaled_down_with_the_room():
    r = run()
    assert step(r, "ROOM_RENT_PROPORTIONATE")["deduction_inr"] == 32625          # doctors, surgeon, anaesthesia, OT only (87,000)
    assert 15000 + 9000 + 54375 + 31500 + 38500 == 148375         # room (capped) + ICU (untouched) + scaled charges + medicines + tests


def test_scaled_charges_round_down_and_the_copay_rounds_up():
    r = run(terms(deductible_inr=0, copay_pct=0), bill([item("Room Rent", "room", 24000, 3, 8000), item("Surgeon Fees", "doctor", 10001)]))
    assert step(r, "ROOM_RENT_PROPORTIONATE")["deduction_inr"] == 10001 - 10001 * 15000 // 24000 == 3751
    r = run(terms(deductible_inr=0, copay_pct=10), bill([item("Pharmacy", "medicine", 10005)]))
    assert step(r, "COPAY")["deduction_inr"] == 1001 and r["estimated_claim_inr"] == 9004     # 1,000.5 rounds UP to 1,001


def test_charges_a_policy_does_not_pay_are_taken_off_in_full():
    r = run(b=bill([item("Pharmacy", "medicine", 50000), item("Registration Charges", "other", 500), item("Attendant Food", "other", 1500)]), t=terms(deductible_inr=0, copay_pct=0))
    assert [s["deduction_inr"] for s in r["steps"] if s["code"] == "NOT_PAYABLE"] == [500, 1500] and r["estimated_claim_inr"] == 50000


def test_a_treatment_limit_caps_the_claim():
    t = terms(deductible_inr=0, copay_pct=0, policy_start="2020-01-01", sub_limits=[{"text": "Cataract surgery: Rs. 40,000 per eye", "page": 3, "limit_inr": 40000}])
    r = run(t, bill([item("Surgeon Fees", "doctor", 30000), item("Lens", "medicine", 40000)], diagnosis="Cataract right eye", procedure="Phacoemulsification"))
    assert step(r, "SUB_LIMIT")["deduction_inr"] == 30000 and r["estimated_claim_inr"] == 40000
    assert step(r, "SUB_LIMIT")["clause"] == {"text": "Cataract surgery: Rs. 40,000 per eye", "page": 3}
    assert "SUB_LIMIT_MATCHED_BY_WORDS" in [u["code"] for u in r["uncertainties"]]       # matched by words: always flagged


def test_a_limit_given_as_a_share_of_the_sum_insured():
    t = terms(deductible_inr=0, copay_pct=0, specific_waiting_months=0, sub_limits=[{"text": "Knee replacement: 25% of the sum insured", "limit_pct_of_sum_insured": 25}])
    r = run(t, bill([item("Surgeon Fees", "doctor", 200000)], diagnosis="Osteoarthritis", procedure="Total knee replacement"))
    assert r["estimated_claim_inr"] == 125000


def test_the_estimate_cannot_exceed_the_cover_that_is_left():
    r = run(terms(sum_insured_inr=100000, deductible_inr=0, copay_pct=0), bill([item("Pharmacy", "medicine", 150000)]), utilised_inr=30000)
    assert step(r, "CAP_SUM_INSURED")["deduction_inr"] == 80000 and r["estimated_claim_inr"] == 70000
    assert r["coverage"] == {"sum_insured_inr": 100000, "used_before_inr": 30000, "left_before_inr": 70000, "left_after_inr": 0}
    assert run(terms(sum_insured_inr=100000), bill([item("Pharmacy", "medicine", 50000)]), utilised_inr=100000)["estimated_claim_inr"] == 0


# ------------------------------------------------------------------ waiting periods and exclusions: no payout expected
def blocked(r, code):
    assert r["status"] == "likely_ineligible" and r["estimated_claim_inr"] == 0 and r["conservative_inr"] == 0 and code in codes(r)
    assert sum(s["deduction_inr"] for s in r["steps"]) == r["bill_total_inr"]


def test_the_initial_waiting_period():
    r = run(b=bill(admission_date=(date.fromisoformat(START) + timedelta(days=10)).isoformat()))
    blocked(r, "WAITING_INITIAL")
    assert step(r, "WAITING_INITIAL")["clause"] is None            # this policy gave no clause for it
    ok = run(b=bill(admission_date=(date.fromisoformat(START) + timedelta(days=30)).isoformat()))      # day 30 is out of the wait
    assert ok["status"] == "likely_eligible"
    acc = run(b=bill(admission_date=(date.fromisoformat(START) + timedelta(days=10)).isoformat()), is_accident=True)
    assert acc["status"] == "likely_eligible"                                                            # accidents are exempt


def test_admitted_before_the_policy_started():
    blocked(run(b=bill(admission_date="2025-03-30")), "POLICY_NOT_STARTED")


def test_a_pre_existing_condition_within_its_waiting_period():
    blocked(run(declared_pre_existing=True), "WAITING_PRE_EXISTING")                  # 11 months in, the wait is 36
    assert run(terms(ped_waiting_months=6), declared_pre_existing=True)["status"] == "likely_eligible"


def test_not_knowing_whether_a_condition_was_pre_existing_is_reported_and_lowers_confidence():
    r = run(declared_pre_existing=None)
    assert r["status"] == "likely_eligible" and [m["field"] for m in r["missing_information"]] == ["declared_pre_existing"]
    assert r["confidence"]["score"] == 0.9


def test_a_specific_disease_within_its_waiting_period():
    blocked(run(b=bill(diagnosis="Inguinal hernia", procedure="Hernioplasty")), "WAITING_SPECIFIC_DISEASE")     # 11 months in, the wait is 24
    assert run(terms(policy_start="2023-01-01"), bill(diagnosis="Inguinal hernia"))["status"] == "likely_eligible"


def test_an_exclusion_that_matches_stops_the_claim_and_cites_its_clause():
    t = terms(exclusions=[{"text": "Hair transplant and baldness treatment.", "page": 4}])
    r = run(t, bill(diagnosis="Male pattern baldness", procedure="Hair transplant"))
    blocked(r, "EXCLUSION")
    assert step(r, "EXCLUSION")["clause"] == {"text": "Hair transplant and baldness treatment.", "page": 4}


def test_an_exclusion_with_a_condition_is_flagged_not_applied():
    t = terms(exclusions=[{"text": "Cosmetic or plastic surgery unless needed after an accident.", "page": 1}])
    r = run(t, bill(diagnosis="Cosmetic rhinoplasty"))
    assert r["status"] == "likely_eligible" and "EXCLUSION_MAY_APPLY" in [u["code"] for u in r["uncertainties"]]
    assert "EXCLUSION_MAY_APPLY" not in [u["code"] for u in run(t, bill(diagnosis="Cosmetic rhinoplasty"), is_accident=True)["uncertainties"]]


def test_a_general_word_alone_does_not_trigger_an_exclusion():
    t = terms(exclusions=[{"text": "Cosmetic or plastic surgery unless needed after an accident.", "page": 1}])
    assert run(t, bill(diagnosis="Appendicitis", procedure="Appendicectomy surgery"))["uncertainties"] == []        # "surgery" is generic


# ------------------------------------------------------------------ gaps are reported, never filled with a guess
def test_nothing_is_assumed_silently_when_terms_are_missing():
    t = PolicyTerms.model_validate({"sum_insured_inr": 500000})
    b = BillFacts.model_validate({"items": [item("Hospital charges", "other", 100000)]})
    r = estimate.estimate_claim(t, b)
    assert {m["field"] for m in r["missing_information"]} == {"room_rent", "admission_date", "policy_start", "diagnosis", "declared_pre_existing",
                                                             "deductible_inr", "copay_pct", "already_claimed"}
    assert "NO_ITEM_BREAKDOWN" in [u["code"] for u in r["uncertainties"]]
    assert r["confidence"] == {"score": 0.1, "level": "low", "haircut_pct": 30}
    assert r["estimated_claim_inr"] == 100000 and r["conservative_inr"] == 70000


def test_a_bill_total_that_differs_from_its_items_is_flagged_and_the_items_are_used():
    r = run(b=bill(total_inr=250000))
    assert r["bill_total_inr"] == 200000 and "BILL_TOTAL_MISMATCH" in [u["code"] for u in r["uncertainties"]]


def test_confidence_and_haircut_levels():
    assert run()["confidence"]["level"] == "high"
    r = run(terms(room_rent=None, deductible_inr=None, copay_pct=None))           # three gaps: 1.0 - 0.30
    assert r["confidence"] == {"score": 0.7, "level": "medium", "haircut_pct": 15}
    assert r["conservative_inr"] == r["estimated_claim_inr"] * 85 // 100


# ------------------------------------------------------------------ the rules are switches in config, not code
def with_cfg(monkeypatch, **changes):
    c = copy.deepcopy(estimate.cfg())
    c.update(changes)
    monkeypatch.setattr(estimate, "cfg", lambda: c)


def test_copay_before_deductible_is_a_switch(monkeypatch):
    b = bill([item("Pharmacy", "medicine", 191000)])
    default = run(b=b)
    with_cfg(monkeypatch, copay_after_deductible=False)
    flipped = run(b=b)
    assert default["estimated_claim_inr"] == 191000 - 5000 - 18600 == 167400
    assert flipped["estimated_claim_inr"] == 191000 - 19100 - 5000 == 166900 and codes(flipped) == ["BILL", "COPAY", "DEDUCTIBLE"]


def test_proportionate_deduction_is_a_switch(monkeypatch):
    with_cfg(monkeypatch, proportionate_deduction=False)
    r = run()
    assert "ROOM_RENT_PROPORTIONATE" not in codes(r) and "ROOM_RENT_CAP" in codes(r)


# ------------------------------------------------------------------ invariants that hold for any input
def test_invariants_over_many_random_cases():
    rng = random.Random(7)
    cats = ["room", "doctor", "medicine", "surgery", "diagnostic", "other"]
    names = {"room": ["Room Rent", "ICU Charges", "Nursing"], "doctor": ["Surgeon Fees", "Consultation"], "medicine": ["Pharmacy"],
             "surgery": ["OT Charges"], "diagnostic": ["Lab tests", "MRI"], "other": ["Miscellaneous", "Ambulance", "Registration"]}
    diag = ["Appendicitis", "Cataract", "Hernia", "Knee replacement", "Dengue", None]
    for _ in range(400):
        items = []
        for _ in range(rng.randint(1, 8)):
            c = rng.choice(cats)
            if c == "room" and rng.random() < 0.6:
                d, r_ = rng.randint(1, 10), rng.choice([2000, 5000, 8000, 12000])
                items.append(item("Room Rent", "room", d * r_, d, r_))
            else:
                items.append(item(rng.choice(names[c]), c, rng.randint(0, 300000)))
        if sum(i["amount_inr"] for i in items) == 0:
            continue
        t = terms(sum_insured_inr=rng.choice([100000, 500000, 1000000]), copay_pct=rng.choice([None, 0, 10, 20, 33.3]), deductible_inr=rng.choice([None, 0, 5000, 50000]),
                  room_rent=rng.choice([None, {"kind": "no_limit"}, {"kind": "per_day_inr", "inr": 4000}, {"kind": "pct_of_sum_insured_per_day", "pct": 1}]),
                  policy_start=rng.choice([START, "2020-01-01", None]),
                  sub_limits=[{"text": "Cataract: Rs. 40,000", "limit_inr": 40000}])
        b = bill(items, diagnosis=rng.choice(diag), admission_date=rng.choice(["2026-03-12", "2025-04-10", None]))
        r = run(t, b, declared_pre_existing=rng.choice([None, True, False]), is_accident=rng.random() < 0.2,
                utilised_inr=rng.choice([None, 0, 50000, 600000]))
        total = sum(i["amount_inr"] for i in items)
        assert 0 <= r["estimated_claim_inr"] <= total
        assert 0 <= r["conservative_inr"] <= r["estimated_claim_inr"]
        assert sum(s["deduction_inr"] for s in r["steps"]) == total - r["estimated_claim_inr"]       # every rupee that is not paid is explained
        assert r["steps"][-1]["payable_after_inr"] == r["estimated_claim_inr"] or r["steps"][-1]["code"] == "CAP_SUM_INSURED"
        assert all(isinstance(s["deduction_inr"], int) and s["deduction_inr"] >= 0 for s in r["steps"])
        assert isinstance(r["estimated_claim_inr"], int) and isinstance(r["conservative_inr"], int)
        assert r["disclaimer"]


def test_same_input_same_output_and_inputs_are_not_changed():
    t, b = terms(), bill()
    before = (t.model_dump_json(), b.model_dump_json())
    assert run(t, b) == run(t, b) and (t.model_dump_json(), b.model_dump_json()) == before


def test_the_disclaimer_says_this_is_not_an_approval():
    assert "estimate" in run()["disclaimer"].lower() and "insurer" in run()["disclaimer"].lower()


# ------------------------------------------------------------------ request validation
def test_a_request_needs_the_terms_or_a_policy():
    with pytest.raises(ValidationError):
        ClaimEstimateRequest.model_validate({"bill": {"items": SAMPLE}})
    with pytest.raises(ValidationError):
        BillFacts.model_validate({"items": [item("X", "other", -5)]})
    with pytest.raises(ValidationError):
        PolicyTerms.model_validate({"sum_insured_inr": 500000, "copay_pct": 150})


# ------------------------------------------------------------------ the API
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "estimate.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def buy(c, insurer="suraksha") -> dict:
    assert c.post(f"{BASE}/kyc/digilocker-demo").json()["verified"] is True
    q = c.post(f"{BASE}/insurance/quote", json={"members": [{"rel": "Self", "age": 32, "gender": "M", "height_cm": 170, "weight_kg": 70}],
                                               "pincode": "400001", "conditions": [], "smoker": False, "surgery_last_4y": False, "cover_inr": 1000000}).json()
    r = c.post(f"{BASE}/insurance/purchase", json={"quote_id": q["quote_id"], "insurer_id": insurer, "consent_share_health": True, "pan": "ABCDE1234F",
                                                   "mobile": "9876543210", "holder_name": "Demo Sharma", "nominee_name": "Demo Sharma Jr", "nominee_relation": "Spouse"}).json()
    assert r["decision"]["verdict"] == "ALLOW", r
    return r["policy"]


def body(**over) -> dict:
    d = {"bill": json.loads(bill().model_dump_json()), "terms": json.loads(terms().model_dump_json()), "declared_pre_existing": False, "already_claimed_inr": 0}
    d.update(over)
    return d


def test_estimate_from_document_terms(client):
    r = client.post(f"{BASE}/claims/estimate", json=body())
    assert r.status_code == 200, r.text
    assert r.json()["estimated_claim_inr"] == 129037 and r.json()["steps"][-1]["clause"]["page"] == 2


def test_estimate_for_a_policy_bought_here_uses_its_terms_cover_and_earlier_claims(client):
    pol = buy(client)                                            # Suraksha, cover 10 lakh, room 5,000 a day, 30-day wait; starts today
    start = date.fromisoformat(pol["start_date"])
    b = json.loads(bill(admission_date=(start + timedelta(days=45)).isoformat(), discharge_date=(start + timedelta(days=48)).isoformat()).model_dump_json())
    r = client.post(f"{BASE}/claims/estimate", json={"policy_id": pol["id"], "bill": b, "declared_pre_existing": False}).json()
    assert r["status"] == "likely_eligible" and "ROOM_RENT_CAP" in codes(r) and r["coverage"] == {"sum_insured_inr": 1000000, "used_before_inr": 0, "left_before_inr": 1000000, "left_after_inr": 1000000 - r["estimated_claim_inr"]}
    assert r["estimated_claim_inr"] == 148375                    # Suraksha has no deductible and no co-pay: 2,00,000 - 10,000 - 9,000 - 32,625
    claim = client.post(f"{BASE}/claims", json={"policy_id": pol["id"], "claimed_inr": 100000}).json()
    again = client.post(f"{BASE}/claims/estimate", json={"policy_id": pol["id"], "bill": b, "declared_pre_existing": False}).json()
    assert again["coverage"]["used_before_inr"] == claim["approved_inr"] == 90000


def test_a_policy_bought_today_is_inside_its_initial_waiting_period(client):
    pol = buy(client)
    start = date.fromisoformat(pol["start_date"])
    b = json.loads(bill(admission_date=(start + timedelta(days=3)).isoformat()).model_dump_json())
    r = client.post(f"{BASE}/claims/estimate", json={"policy_id": pol["id"], "bill": b, "declared_pre_existing": False}).json()
    assert r["status"] == "likely_ineligible" and "WAITING_INITIAL" in codes(r)


def test_estimate_errors(client):
    assert client.post(f"{BASE}/claims/estimate", json=body(policy_id="PHI-00000000", terms=None)).json()["detail"]["code"] == "POLICY_NOT_FOUND"
    assert client.post(f"{BASE}/claims/estimate", json={"bill": body()["bill"]}).status_code == 422
    assert client.post(f"{BASE}/claims/estimate", json=body(already_claimed_inr=-1)).status_code == 422


def test_an_estimate_stores_nothing_and_logs_no_diagnosis(client):
    conn = store.connect()
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("consumer_claims", "consumer_loans", "consumer_quotes", "consumer_policies", "consumer_executions")}
    assert client.post(f"{BASE}/claims/estimate", json=body()).status_code == 200
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    rows = [dict(x) for x in conn.execute("SELECT * FROM consumer_audit WHERE action='claim_estimate'")]
    assert len(rows) == 1 and rows[0]["verdict"] == "ALLOW" and rows[0]["amount"] is None and "not an approval" in rows[0]["note"]
    dump = json.dumps(rows)
    for word in ("Appendicitis", "Appendectomy", "Surgeon", "129037"):
        assert word not in dump


# ------------------------------------------------------------------ from the document reader to the estimate
def test_from_the_text_of_a_policy_and_a_bill_to_an_estimate():
    """The whole chain without a hand-written fact: the text of the five fixture documents -> the reader -> the estimator."""
    from pathlib import Path

    from chaukas.consumer import docs

    fx = Path(__file__).parent / "fixtures" / "consumer_docs"
    rd = lambda n: docs.read_document([(fx / f"{n}.txt").read_text(encoding="utf-8")], use_ai=False)   # noqa: E731
    t = estimate.terms_from_reading(rd("policy"))
    b = estimate.bill_from_readings(rd("bill"), rd("discharge"))
    assert t.sum_insured_inr == 500000 and t.room_rent.inr == 5000 and t.copay_pct == 10 and str(t.policy_start) == "2025-04-01"
    assert len(t.exclusions) == 5 and t.refs["room_rent"].page == 1 and "Room Rent" in t.refs["room_rent"].text
    assert b.diagnosis == "Acute Appendicitis" and b.procedure == "Laparoscopic Appendectomy" and len(b.items) == 9 and b.total_inr == 200000
    r = estimate.estimate_claim(t, b, declared_pre_existing=False, utilised_inr=0)
    assert r["estimated_claim_inr"] == 129037 and step(r, "ROOM_RENT_CAP")["clause"]["page"] == 1
    assert r["uncertainties"] == [] and r["missing_information"] == []


def test_the_mappers_refuse_a_reading_that_lacks_what_the_estimate_needs():
    with pytest.raises(ValueError):
        estimate.terms_from_reading({"fields": {}, "lists": {}})
    with pytest.raises(ValueError):
        estimate.bill_from_readings({"fields": {}, "lists": {}})


# ------------------------------------------------------------------ a renewed policy counts its waiting periods from the first inception
def test_a_renewed_policy_counts_the_waiting_periods_from_the_first_inception():
    renewed = terms(policy_start="2025-04-01", continuous_since="2022-04-01")          # this period began 11 months ago, cover began 4 years ago
    gall = bill(diagnosis="Cholelithiasis (gall stones)", procedure="Laparoscopic Cholecystectomy")
    assert run(terms(policy_start="2025-04-01"), gall)["status"] == "likely_ineligible"          # a new policy: specific-disease wait applies
    ok = run(renewed, gall)
    assert ok["status"] == "likely_eligible" and codes(run(renewed, gall, declared_pre_existing=True)).count("WAITING_PRE_EXISTING") == 0
    assert run(terms(policy_start="2025-04-01", continuous_since="2025-04-01"), gall)["status"] == "likely_ineligible"


def test_continuous_cover_is_read_from_a_policy_document():
    from chaukas.consumer import docs

    r = docs.read_document(["DEMO HEALTH INSURANCE CO. LTD.\nPolicy Schedule\nPolicy Number: X/1/2026/000123\nPolicy Holder: Anita Verma\nSum Insured: Rs. 10,00,000\n"
                            "Policy Period From: 01/04/2025   Policy Period To: 31/03/2026\nContinuous Cover Since: 01/04/2022\nPremium: Rs. 20,000\nWaiting Period: see below\nExclusions\n- War.\n"], use_ai=False)
    assert r["fields"]["continuous_since"]["value"] == "2022-04-01"
    assert str(estimate.terms_from_reading(r).continuous_since) == "2022-04-01"
