"""Phase 5: advisory risk flags, the ML flag and the settlement outlook (consumer/risk.py).

The contract: signals only ask a person to look. They never turn a failed check into a pass, never allow or block, never set an
amount, and never accuse anyone. The ML model is trained on SYNTHETIC claims and its committed file must match a re-run.
"""
import json
from datetime import date
from pathlib import Path

import yaml

from chaukas.consumer import docs, risk, verify
from data import train_risk_model

FX = Path(__file__).parent / "fixtures" / "consumer_docs"
CLEAN = {"bill_total_inr": 187500, "stay_days": 5, "sum_insured_inr": 500000, "policy_age_days": 400, "claimed_inr": 180000,
         "approved_inr": 150000, "estimated_inr": 148000, "route": "cashless", "uncertainties": 0}


def codes(x: dict) -> list[str]:
    return [f["code"] for f in risk.rule_flags(x)]


def test_an_ordinary_claim_raises_nothing():
    out = risk.assess(CLEAN)
    assert out["flags"] == [] and out["review"] is False
    assert out["ml"]["ran"] is True and out["ml"]["flagged"] is False


def test_each_rule_fires_on_its_own_pattern():
    assert "EARLY_CLAIM" in codes({**CLEAN, "policy_age_days": 10})
    assert "BILL_NEAR_COVER" in codes({**CLEAN, "bill_total_inr": 460000})
    assert "SHORT_STAY_HIGH_BILL" in codes({**CLEAN, "stay_days": 1, "bill_total_inr": 200000})
    assert "HIGH_COST_PER_DAY" in codes({**CLEAN, "stay_days": 2, "bill_total_inr": 200000})
    assert "ROUND_BILL_TOTAL" in codes({**CLEAN, "bill_total_inr": 200000, "no_item_breakdown": True})
    assert "ROUND_BILL_TOTAL" not in codes({**CLEAN, "bill_total_inr": 200000})          # an itemised round total is not flagged
    assert "APPROVED_ABOVE_ESTIMATE" in codes({**CLEAN, "approved_inr": 190000})


def test_a_renewal_counts_from_the_first_inception():
    # verify passes continuous_since as the start when there is one, so a long-held policy is not an early claim
    assert "EARLY_CLAIM" not in codes({**CLEAN, "policy_age_days": 900})


def test_missing_inputs_do_not_crash_and_the_model_says_it_did_not_run():
    out = risk.assess({"bill_total_inr": 120000})
    assert out["ml"]["ran"] is False and out["ml"]["score"] is None and out["ml"]["flagged"] is False
    assert out["settlement_outlook"]["likely_days"] > 0


def test_the_ml_flag_explains_itself_and_needs_no_rule_to_fire():
    x = {**CLEAN, "policy_age_days": 5, "bill_total_inr": 480000, "stay_days": 1, "approved_inr": 480000, "claimed_inr": 480000, "estimated_inr": 250000}
    out = risk.assess(x)
    assert out["ml"]["flagged"] and 0.9 <= out["ml"]["score"] <= 1
    assert 1 <= len(out["ml"]["top_reasons"]) <= 3 and all(r["push"] > 0 for r in out["ml"]["top_reasons"])
    assert out["review"] is True


def test_the_model_file_says_it_is_synthetic_and_matches_a_retrain():
    committed = yaml.safe_load((Path(__file__).parent.parent / "config" / "risk_model.yaml").read_text(encoding="utf-8"))
    assert committed["trained_on"]["kind"] == "synthetic"
    assert committed["held_out"]["flag_at"] == risk.cfg()["ml"]["flag_at"] == train_risk_model.FLAG_AT
    fresh = train_risk_model.train()
    assert fresh["features"] == committed["features"]
    for k in ("mean", "std", "weights"):
        assert all(abs(a - b) < 1e-4 for a, b in zip(fresh[k], committed[k])), k
    assert committed["held_out"]["auc"] > 0.9


def test_the_settlement_outlook_is_a_rule_table_with_a_range():
    cashless = risk.settlement_outlook({**CLEAN, "approval_date": date(2026, 3, 1)})
    slow = risk.settlement_outlook({**CLEAN, "route": "reimbursement", "approved_inr": 350000, "uncertainties": 2})
    assert cashless["likely_days"] < slow["likely_days"] and cashless["could_take_up_to_days"] > cashless["likely_days"]
    assert cashless["likely_by"] == "2026-03-10" and "not a fitted model" in cashless["basis"]


def test_a_payout_date_far_sooner_than_the_outlook_is_flagged():
    out = risk.assess({**CLEAN, "route": "reimbursement", "approval_date": date(2026, 3, 1), "expected_payment_date": date(2026, 3, 4)})
    assert "PAYOUT_SOONER_THAN_OUTLOOK" in [f["code"] for f in out["flags"]]
    ok = risk.assess({**CLEAN, "approval_date": date(2026, 3, 1), "expected_payment_date": date(2026, 3, 9)})
    assert "PAYOUT_SOONER_THAN_OUTLOOK" not in [f["code"] for f in ok["flags"]]


def test_it_is_deterministic_and_never_accuses_anyone():
    x = {**CLEAN, "policy_age_days": 5, "bill_total_inr": 480000}
    assert risk.assess(x) == risk.assess(x)
    blob = json.dumps(risk.assess(x), default=str).lower()
    assert "fraud" not in blob and "forg" not in blob


def _readings(names=("policy", "bill", "discharge", "claim_ack", "claim_approval"), change=None):
    out = []
    for n in names:
        t = (FX / f"{n}.txt").read_text(encoding="utf-8")
        if change and n in change:
            t = t.replace(*change[n])
        out.append(docs.read_document([t], use_ai=False))
    return out


def test_the_clean_story_stays_consistent_and_carries_the_outlook():
    rep = verify.verify(_readings(), "approved_claim", declared_pre_existing=False, already_claimed_inr=0)
    assert rep["verdict"] == "consistent" and rep["risk"]["review"] is False
    assert rep["risk"]["settlement_outlook"]["likely_days"] > 0


def test_a_flag_moves_consistent_to_needs_review_but_never_the_other_way():
    # shorten the policy history so the admission falls just after the policy began
    rs = _readings()
    pol = next(r for r in rs if r["doc_type"] == "policy")
    pol["fields"]["policy_start"]["value"] = "2026-02-20"
    pol["fields"].pop("continuous_since", None)
    rep = verify.verify(rs, "approved_claim", declared_pre_existing=False, already_claimed_inr=0)
    assert "EARLY_CLAIM" in [f["code"] for f in rep["risk"]["flags"]]
    assert rep["verdict"] in ("needs_review", "inconsistent")
    # a failed check is not softened by a flag
    broken = verify.verify(_readings(change={"bill": ("Rahul Sharma", "Someone Else")}), "approved_claim", declared_pre_existing=False, already_claimed_inr=0)
    assert broken["verdict"] == "inconsistent"
