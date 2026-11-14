"""Advisory risk flags and a settlement outlook for one claim application.

Why this exists (CLAUDE.md rules 2, 3, 11): the document checks in verify.py say whether the papers agree. This module adds the
softer signals a claims reviewer looks at (a claim very soon after the policy began, a bill close to the whole cover, a very
high cost per day, a round total, an approval far above the estimate) and asks a person to look. It is ADVISORY:
  * it never approves, blocks or sizes anything, and never sets an amount (those come from engine.py and the guardrail);
  * a flag only moves a `consistent` application to `needs_review`; a failed check stays failed, BLOCK stays final;
  * it never accuses anyone: every flag says what was noticed and what a person should check;
  * the ML flag is a small logistic model trained on SYNTHETIC claims (data/train_risk_model.py): it learns our own idea of
    "unusual", is explainable (the three features that pushed the score most are returned) and is advisory like the rules;
  * the settlement outlook is a transparent rule table, not a fitted model: days from approval to payout, with a range.
Deterministic, integer money, no model call, nothing stored here.

Owner: Member 1 · config: config/risk.yaml, config/risk_model.yaml (generated)
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent / "config"


def cfg() -> dict:
    return yaml.safe_load((_ROOT / "risk.yaml").read_text(encoding="utf-8"))


def model() -> dict:
    return yaml.safe_load((_ROOT / "risk_model.yaml").read_text(encoding="utf-8"))


def inr(n: int) -> str:
    s = str(abs(int(n)))
    head, tail = (s[:-3], s[-3:]) if len(s) > 3 else ("", s)
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "₹" + ",".join(parts + [tail])


def _flag(code: str, source: str, message: str, check: str) -> dict:
    return {"code": code, "source": source, "message": message, "what_a_person_should_check": check}


# ------------------------------------------------------------------ the rules
def rule_flags(x: dict) -> list[dict]:
    r = cfg()["rules"]
    out: list[dict] = []
    bill, cover, stay, age = x.get("bill_total_inr"), x.get("sum_insured_inr"), x.get("stay_days"), x.get("policy_age_days")
    if age is not None and 0 <= age <= r["early_claim_days"]:
        out.append(_flag("EARLY_CLAIM", "rule", f"The admission was {age} day(s) after the policy began.",
                         "That the cover was not bought because the treatment was already planned, and that the policy start date is right."))
    if bill and cover and bill * 100 >= cover * r["near_cover_pct"]:
        out.append(_flag("BILL_NEAR_COVER", "rule", f"The bill ({inr(bill)}) is {r['near_cover_pct']}% or more of the sum insured ({inr(cover)}).",
                         "That the bill is itemised and each large charge is explained."))
    if bill and stay is not None and stay <= r["short_stay_days"] and bill >= r["short_stay_bill_inr"]:
        out.append(_flag("SHORT_STAY_HIGH_BILL", "rule", f"A stay of {stay} day(s) with a bill of {inr(bill)}.",
                         "The discharge summary and the procedure against the bill."))
    if bill and stay and stay > 0 and bill // stay > r["cost_per_day_inr"]:
        out.append(_flag("HIGH_COST_PER_DAY", "rule", f"The bill works out to about {inr(bill // stay)} for each day of stay.",
                         "The item-wise bill, especially the largest lines."))
    if x.get("no_item_breakdown") and bill and bill >= r["round_total_min_inr"] and bill % r["round_total_multiple_inr"] == 0:
        out.append(_flag("ROUND_BILL_TOTAL", "rule", f"The bill has no item-wise breakdown and its total ({inr(bill)}) is an exact round figure.",
                         "That the total is the sum of the real items and nothing was rounded or typed in."))
    est, appr = x.get("estimated_inr"), x.get("approved_inr")
    if est is not None and appr is not None and est > 0 and appr * 100 > est * (100 + r["approved_over_estimate_pct"]):
        out.append(_flag("APPROVED_ABOVE_ESTIMATE", "rule", f"The letter approves {inr(appr)}, above the {inr(est)} the policy terms suggest.",
                         "The policy terms against the approval letter."))
    return out


# ------------------------------------------------------------------ the ML flag
def features(x: dict) -> tuple[list[float | None], list[str]]:
    """The model's inputs in order; None where the application does not have the value (it is then treated as ordinary)."""
    bill, cover, stay, age = x.get("bill_total_inr"), x.get("sum_insured_inr"), x.get("stay_days"), x.get("policy_age_days")
    claimed, appr, est = x.get("claimed_inr"), x.get("approved_inr"), x.get("estimated_inr")
    v: dict[str, float | None] = {
        "ln_bill": math.log(bill) if bill and bill > 0 else None,
        "stay_days": float(min(max(stay, 1), 30)) if stay else None,
        "ln_cost_per_day": math.log(bill / stay) if bill and stay and bill > 0 and stay > 0 else None,
        "bill_over_cover": min(bill / cover, 1.5) if bill is not None and cover else None,
        "ln_policy_age_days": math.log(1 + max(age, 0)) if age is not None else None,
        "approved_over_claimed": min(appr / claimed, 1.0) if appr is not None and claimed else None,
        "round_total": (1.0 if bill >= 50000 and bill % 10000 == 0 else 0.0) if bill is not None else None,
        "approved_over_estimate": min(appr / est, 3.0) if appr is not None and est else None,
    }
    m = model()
    return [v[f] for f in m["features"]], m["features"]


def ml_flag(x: dict) -> dict:
    m, c = model(), cfg()["ml"]
    vals, names = features(x)
    contrib, missing = [], []
    for name, val, mu, sd, w in zip(names, vals, m["mean"], m["std"], m["weights"]):
        if val is None:
            missing.append(name)
            contrib.append(0.0)
        else:
            contrib.append(w * (val - mu) / sd)
    if missing:      # trained on whole approved claims: with an input missing the score would be a guess, so it is not shown
        return {"score": None, "flagged": False, "top_reasons": [], "missing_inputs": missing, "ran": False,
                "note": "Not run: the model needs the bill, the stay, the policy and the approval, and some of these are not here yet."}
    z = sum(contrib) + m["bias"]
    score = 1 / (1 + math.exp(-max(min(z, 30), -30)))
    top = [{"feature": names[i], "push": round(contrib[i], 2)} for i in sorted(range(len(names)), key=lambda i: -contrib[i])
           if contrib[i] > 0.25][: c["max_reasons"]]
    flagged = score >= c["flag_at"]
    return {"ran": True, "score": round(score, 3), "flagged": flagged, "top_reasons": top if flagged else [], "missing_inputs": missing,
            "model": {"kind": "logistic regression", "trained_on": m["trained_on"]["kind"], "claims": m["trained_on"]["claims"],
                      "held_out_auc": m["held_out"]["auc"]},
            "note": "Advisory. A small model trained on synthetic claims; it can only ask a person to look, never decide."}


# ------------------------------------------------------------------ the settlement outlook
def settlement_outlook(x: dict) -> dict:
    s = cfg()["settlement"]
    route = x.get("route") if x.get("route") in s["base_days"] else "unknown"
    days = s["base_days"][route]
    appr = x.get("approved_inr") or x.get("estimated_inr") or 0
    for t in s["amount_tiers"]:
        if appr > t["above_inr"]:
            days += t["add_days"]
    days += s["per_uncertainty_days"] * int(x.get("uncertainties", 0))
    late = days + s["late_extra_days"]
    out = {"likely_days": days, "could_take_up_to_days": late, "route": route,
           "basis": "A rule table (route, size of the payout, doubts in the estimate), not a fitted model. The insurer's own date is what the advance is repaid against."}
    start = x.get("approval_date")
    if isinstance(start, date):
        out["likely_by"] = (start + timedelta(days=days)).isoformat()
        out["could_take_up_to"] = (start + timedelta(days=late)).isoformat()
    return out


# ------------------------------------------------------------------ everything together
def assess(x: dict) -> dict:
    """`x` is plain numbers and dates taken from the verified facts; see verify.risk_inputs. Returns the flags, the ML score
    and the settlement outlook, and whether a person should look (`review`). Nothing here can allow or block."""
    flags = rule_flags(x)
    ml = ml_flag(x)
    if ml["flagged"] and not flags:
        flags.append(_flag("UNUSUAL_PATTERN", "model", "Taken together, the figures look unusual next to ordinary claims.",
                           "The bill, the dates and the approval, one by one."))
    outlook = settlement_outlook(x)
    r = cfg()["rules"]
    pay = x.get("expected_payment_date")
    start = x.get("approval_date")
    if isinstance(pay, date) and isinstance(start, date):
        promised = (pay - start).days
        if promised + r["payout_sooner_than_outlook_days"] <= outlook["likely_days"]:
            flags.append(_flag("PAYOUT_SOONER_THAN_OUTLOOK", "rule",
                               f"The letter expects payment in {promised} day(s); a claim like this usually takes about {outlook['likely_days']}.",
                               "The expected payment date with the insurer, because the advance is repaid from the payout."))
    return {"flags": flags, "review": bool(flags), "ml": ml, "settlement_outlook": outlook,
            "advisory": "These signals only ask a person to look. They never approve, block or set an amount, and nobody is accused of anything."}
