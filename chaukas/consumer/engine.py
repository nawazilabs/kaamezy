"""Deterministic rule engine for the consumer flows: health-insurance premiums and the simulated lender.

Same input -> same output, no LLM, no network. Every number comes from config/consumer.yaml. Amounts are int rupees:
factors are floats, but every money result is rounded half-up to an int at the end (like the merchant side).

Owner: Member 1 (integration) / Member 2 (rules)
"""
from __future__ import annotations

import hashlib
import json
import math

from chaukas.consumer.models import InsuranceProposal, LoanApplication
from chaukas.integrations._common import load_config


def cfg() -> dict:
    return load_config("consumer")


def rnd(x: float) -> int:
    """Round half up (the same rule the web demo used), never banker's rounding."""
    return int(math.floor(x + 0.5))


def params_hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:20]


# ------------------------------------------------------------------ health insurance
def city_tier(pincode: str) -> str:
    return "metro" if pincode[:3] in cfg()["health_insurance"]["metro_pincode_prefixes"] else "other"


def _base_for_age(age: int) -> int | None:
    for max_age, base in cfg()["health_insurance"]["age_base_inr"]:
        if age <= max_age:
            return base
    return None  # above the last band: priced by a human underwriter, not here


def insurance_load(p: InsuranceProposal) -> float:
    c = cfg()["health_insurance"]
    unknown = [k for k in p.conditions if k not in c["conditions"]]
    if unknown:
        raise ValueError("unknown condition: " + ", ".join(sorted(unknown)))
    bmi_high = any(m.height_cm and m.weight_kg and m.weight_kg / ((m.height_cm / 100) ** 2) >= c["bmi"]["threshold"]
                   for m in p.members)
    load = sum(c["conditions"][k]["load"] for k in p.conditions)
    load += c["smoker_load"] if p.smoker else 0
    load += c["surgery_load"] if p.surgery_last_4y else 0
    load += c["bmi"]["load"] if bmi_high else 0
    return min(c["load_cap"], load)


def quotes(p: InsuranceProposal) -> list[dict]:
    c = cfg()["health_insurance"]
    if p.cover_inr not in c["cover_factor"]:
        raise ValueError("cover must be one of " + ", ".join(str(x) for x in c["covers_inr"]))
    bases = sorted((b for b in (_base_for_age(m.age) for m in p.members) if b is not None), reverse=True)
    sub = sum(b if i == 0 else b * c["extra_member_factor"] for i, b in enumerate(bases))
    sub *= c["cover_factor"][p.cover_inr] * (c["metro_factor"] if city_tier(p.pincode) == "metro" else 1)
    sub *= 1 + insurance_load(p)
    out = []
    for ins in c["insurers"]:
        total = rnd(rnd(sub * ins["price_multiplier"]) * (1 + c["gst"]))
        out.append({"insurer_id": ins["id"], "name": ins["name"], "tag": ins["tag"], "points": list(ins["points"]),
                    "premium_inr": total, "monthly_inr": rnd(total / 12), "cover_inr": p.cover_inr})
    return out


def recommend(qs: list[dict], p: InsuranceProposal) -> dict:
    c = cfg()["health_insurance"]
    older = any(m.age > c["older_member_age"] for m in p.members)
    if older or p.conditions:
        why = ("You have a health condition on record" if p.conditions else "A member is above %d" % c["older_member_age"]) \
            + ", so a plan with no co-pay and a 3-year wait for existing illness is the safest middle path."
        return {"insurer_id": c["balanced_insurer_id"], "why": why}
    cheapest = min(qs, key=lambda q: q["premium_inr"])
    return {"insurer_id": cheapest["insurer_id"],
            "why": "Everyone is young and healthy, so the lowest premium gives you the same core cover."}


def member_signature(p: InsuranceProposal) -> str:
    return ",".join(sorted(f"{m.rel}{m.age}" for m in p.members))


def needs_underwriter(p: InsuranceProposal) -> bool:
    g = cfg()["health_insurance"]["guardrail"]
    heart = "heart" in p.conditions and p.cover_inr > g["heart_review_above_cover_inr"]
    always = any(k in p.conditions for k in g["human_review_conditions"])
    return heart or always or any(m.age > g["max_proposer_age"] for m in p.members)


# ------------------------------------------------------------------ personal loan (simulated lender)
def _emi(principal: int, apr: float, months: int) -> int:
    r = apr / 1200
    f = (1 + r) ** months
    return rnd(principal * r * f / (f - 1))


def _supportable(room: float, apr: float, months: int) -> float:
    r = apr / 1200
    f = (1 + r) ** months
    return room * (f - 1) / (r * f)


def lender_decide(a: LoanApplication) -> dict:
    """Uses ONLY age, income, existing EMIs and the requested amount/tenure. Never name, gender, location or language."""
    L = cfg()["personal_loan"]["lender"]
    if a.tenure_months not in cfg()["personal_loan"]["tenures_months"]:
        raise ValueError("tenure must be one of " + ", ".join(str(x) for x in cfg()["personal_loan"]["tenures_months"]))
    apr = next(rate for cap, rate in L["apr_bands"] if cap is None or a.monthly_income_inr <= cap)
    reasons: list[str] = []
    if a.age < L["min_age"] or a.age > L["max_age"]:
        reasons.append("AGE_OUT_OF_RANGE")
    if a.monthly_income_inr < L["min_income_inr"]:
        reasons.append("INCOME_TOO_LOW")
    room = math.floor(a.monthly_income_inr * L["foir"] - a.existing_emi_inr)
    if not reasons and room <= 0:
        reasons.append("OBLIGATIONS_TOO_HIGH")
    max_amount = 0
    if not reasons:
        max_amount = min(L["cap_inr"], a.monthly_income_inr * L["income_multiple"],
                         math.floor(_supportable(room, apr, a.tenure_months)))
        max_amount = max_amount // L["round_to_inr"] * L["round_to_inr"]
        if max_amount < L["min_amount_inr"]:
            reasons.append("CAPACITY_TOO_LOW")
    if reasons:
        return {"eligible": False, "reasons": reasons, "apr_pct": apr}
    amount = min(a.amount_inr // L["round_to_inr"] * L["round_to_inr"], max_amount)
    if amount < L["min_amount_inr"]:
        return {"eligible": False, "reasons": ["AMOUNT_TOO_LOW"], "apr_pct": apr}
    emi = _emi(amount, apr, a.tenure_months)
    total = emi * a.tenure_months
    fee = rnd(amount * L["processing_fee_pct"] / 100)
    fee_gst = rnd(fee * L["gst"])
    return {"eligible": True, "reasons": [], "amount_inr": amount, "requested_inr": a.amount_inr, "max_inr": max_amount,
            "apr_pct": apr, "tenure_months": a.tenure_months, "emi_inr": emi, "total_inr": total,
            "interest_inr": total - amount, "fee_inr": fee, "fee_gst_inr": fee_gst, "net_inr": amount - fee - fee_gst}


def build_kfs(d: dict) -> dict:
    """Key Facts Statement (RBI Digital Lending): every cost, the lender's name, cooling-off and grievance contact."""
    L = cfg()["personal_loan"]["lender"]
    return {"lender_name": L["name"], "amount_inr": d["amount_inr"], "tenure_months": d["tenure_months"],
            "apr_pct": d["apr_pct"], "emi_inr": d["emi_inr"], "interest_inr": d["interest_inr"],
            "processing_fee_inr": d["fee_inr"], "processing_fee_gst_inr": d["fee_gst_inr"],
            "net_disbursal_inr": d["net_inr"], "total_repayable_inr": d["total_inr"],
            "cooling_off_days": L["cooling_off_days"], "grievance_contact": L["grievance_contact"],
            "insurance_required": False}
