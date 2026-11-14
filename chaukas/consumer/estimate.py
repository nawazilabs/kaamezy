"""The claim ESTIMATOR: what a health policy would probably pay on a hospital bill, step by step.

What this is, and is not (CLAUDE.md rules 1, 2, 7, 11):
  * Deterministic code. Same input, same output. No LLM, no network. Every rule, keyword and number comes from
    config/consumer.yaml (`claim_estimate`), and the policy's own terms come from the policy: nothing here is "the formula
    of insurance". Different policies differ, so the ORDER of the deductible and the co-pay, and whether a room above the
    limit scales the other charges, are switches in the config.
  * Money is int rupees, integer arithmetic only. Wherever a figure must be rounded the estimate is rounded the
    cautious way (a co-pay up, a scaled charge down), so a rounding error never makes the estimate larger.
  * It is an ESTIMATE. It is never an approval and never a promise: the insurer alone decides a claim. Every result says
    so (`disclaimer`), lists what is missing and what is uncertain, gives a confidence, and gives a `conservative_inr`
    (the estimate less a haircut that grows as confidence falls) which is the only figure financing may use.
  * Every deduction comes back as a step with its code, the rupees taken off, what is left, why, and the clause of the
    policy it came from (page and sentence), when the terms carry one.
  * The inputs are FACTS the person confirmed (the terms read from the policy, the bill read from the hospital's bill, the
    diagnosis). They are never the estimate itself. Nothing is stored here.
  * Only fields that bear on the claim are read: no name, age, gender, location or language (fairness).

Owner: Member 1 · config: config/consumer.yaml `claim_estimate`
"""
from __future__ import annotations

import math
import os
import re
from datetime import date
from fractions import Fraction

from chaukas.consumer import engine
from chaukas.consumer.models import BillFacts, BillItem, Clause, PolicyTerms


def cfg() -> dict:
    return engine.cfg()["claim_estimate"]


# ------------------------------------------------------------------ small helpers
def months_between(start: date, end: date) -> int:
    """Whole months from start to end (a month is complete on the same day of the month)."""
    m = (end.year - start.year) * 12 + end.month - start.month
    return m - 1 if end.day < start.day else m


def _key_words(text: str) -> list[str]:
    """The words of an exclusion or a sub-limit that show it applies (generic words such as 'surgery' are left out)."""
    ce = cfg()
    generic = set(ce["generic_words"])
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= ce["min_keyword_len"] and w not in generic]


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def _clause(text: str | None, page: int | None) -> dict | None:
    return {"text": text or "", "page": page} if (text or page) else None


def _ref(terms: PolicyTerms, field: str) -> dict | None:
    r = terms.refs.get(field)
    return _clause(r.text, r.page) if r else None


def terms_for_policy(policy: dict) -> PolicyTerms:
    """The terms of a policy bought in this app: the insurer's terms and the usual exclusions from config, the cover, the
    start date and the insurer's name from the policy itself."""
    insurer = next((i for i in engine.cfg()["health_insurance"]["insurers"] if i["id"] == policy["insurer_id"]), None)
    if not insurer or "terms" not in insurer:
        raise ValueError("this insurer has no terms to estimate against")
    t = {**insurer["terms"], "sum_insured_inr": policy["cover_inr"], "policy_start": policy["start_date"],
         "exclusions": [{"text": x} for x in cfg()["default_exclusions"]], "waiting_waived_demo": demo_waives_waiting()}
    return PolicyTerms.model_validate(t)


# ------------------------------------------------------------------ from what the document reader found
def terms_from_reading(policy: dict) -> PolicyTerms:
    """The policy terms in a reading of a POLICY document (docs.read_document). Each value keeps the page and sentence it
    came from, so a deduction can point at its clause. Raises ValueError if the sum insured was not read."""
    f, lists = policy.get("fields", {}), policy.get("lists", {})
    if "sum_insured_inr" not in f:
        raise ValueError("the sum insured was not found in the policy")
    v = lambda k: f[k]["value"] if k in f else None            # noqa: E731
    refs = {k: {"text": x["quote"], "page": x["page"]} for k, x in f.items() if x.get("quote")}
    return PolicyTerms.model_validate({
        "sum_insured_inr": v("sum_insured_inr"), "room_rent": v("room_rent"), "copay_pct": v("copay_pct"),
        "deductible_inr": v("deductible_inr"), "initial_waiting_days": v("initial_waiting_days"),
        "ped_waiting_months": v("ped_waiting_months"), "specific_waiting_months": v("specific_waiting_months"),
        "policy_start": v("policy_start"), "continuous_since": v("continuous_since"),
        "exclusions": [{"text": x["text"], "page": x.get("page")} for x in lists.get("exclusions", [])],
        "sub_limits": [{"text": x["text"], "page": x.get("page"), "limit_inr": x.get("limit_inr"),
                        "limit_pct_of_sum_insured": x.get("limit_pct_of_sum_insured")} for x in lists.get("sub_limits", [])],
        "refs": refs})


def bill_from_readings(bill: dict, discharge: dict | None = None) -> BillFacts:
    """The bill facts in a reading of a HOSPITAL BILL, with the diagnosis and procedure from the discharge summary when there
    is one (a bill often names neither). Raises ValueError if the bill has no line items."""
    items = bill.get("lists", {}).get("line_items") or []
    if not items:
        raise ValueError("no item-wise charges were found on the bill")
    bf, df = bill.get("fields", {}), (discharge or {}).get("fields", {})
    pick = lambda k: (bf.get(k) or df.get(k) or {}).get("value")   # noqa: E731
    return BillFacts.model_validate({
        "items": [{"description": i["description"], "category": i["category"], "amount_inr": i["amount_inr"],
                   "days": i.get("days"), "rate_inr_per_day": i.get("rate_inr_per_day")} for i in items],
        "total_inr": pick("total_inr"), "admission_date": pick("admission_date"), "discharge_date": pick("discharge_date"),
        "diagnosis": (df.get("diagnosis") or bf.get("diagnosis") or {}).get("value"),
        "procedure": (df.get("procedure") or {}).get("value")})


# ------------------------------------------------------------------ the estimate
class _Run:
    """Collects the steps, the gaps and the doubts of one estimate."""

    def __init__(self):
        self.steps: list[dict] = []
        self.missing: list[dict] = []
        self.unsure: list[dict] = []
        self.waived: list[dict] = []

    def step(self, code: str, label: str, deduction: int, payable_after: int, basis: str, clause: dict | None = None) -> None:
        self.steps.append({"code": code, "label": label, "deduction_inr": deduction, "payable_after_inr": payable_after,
                           "basis": basis, "clause": clause})

    def need(self, field: str, message: str) -> None:
        if not any(m["field"] == field for m in self.missing):
            self.missing.append({"field": field, "message": message})

    def doubt(self, code: str, message: str) -> None:
        if not any(u["code"] == code for u in self.unsure):
            self.unsure.append({"code": code, "message": message})


def _room_limit(terms: PolicyTerms, run: _Run) -> int | None:
    """The room rent limit per day in rupees, or None when there is no numeric limit to apply."""
    r = terms.room_rent
    if r is None:
        run.need("room_rent", "The room rent limit of the policy was not found, so no room rent deduction could be worked out.")
        return None
    if r.kind == "no_limit":
        return None
    if r.kind == "per_day_inr" and r.inr:
        return r.inr
    if r.kind == "pct_of_sum_insured_per_day" and r.pct:
        return math.floor(Fraction(terms.sum_insured_inr) * Fraction(str(r.pct)) / 100)
    run.doubt("ROOM_CATEGORY_UNCHECKED", "The policy limits the room by category (for example a single private room), not by an amount, "
                                         "so it was not checked whether the room taken is within it.")
    return None


def demo_waives_waiting() -> bool:
    """DEMO switch: for a policy bought in THIS app, show the waiting periods but do not apply them, so an in-app policy bought
    minutes ago can be used on a stage. Off by default (config claim_estimate.demo.waive_waiting_for_in_app_policies), or set
    the environment variable KAAMEZY_DEMO_WAIVE_WAITING=1. Never applies to a policy read from a document."""
    return bool(cfg().get("demo", {}).get("waive_waiting_for_in_app_policies")) or os.environ.get("KAAMEZY_DEMO_WAIVE_WAITING") == "1"


def _block(out: list[dict], terms: PolicyTerms, run: _Run, b: dict) -> None:
    """A waiting-period block: recorded as a block, or (demo switch) recorded as waived so the screen still shows it."""
    if terms.waiting_waived_demo:
        run.waived.append({"code": b["code"], "label": b["label"], "basis": b["basis"], "clause": b["clause"]})
    else:
        out.append(b)


def _blocks(terms: PolicyTerms, bill: BillFacts, text: str, declared_pre_existing: bool | None, is_accident: bool, run: _Run) -> list[dict]:
    """Reasons the claim would probably not be paid at all. Each is {code, label, basis, clause}."""
    ce = cfg()
    out: list[dict] = []
    adm = bill.admission_date
    start = terms.continuous_since or terms.policy_start       # a renewed policy counts its waiting periods from the first inception
    if adm is None:
        run.need("admission_date", "The admission date was not found, so the waiting periods could not be checked.")
    if start is None:
        run.need("policy_start", "The policy start date was not found, so the waiting periods could not be checked.")
    ref_field = "continuous_since" if terms.continuous_since else "policy_start"
    if not text:
        run.need("diagnosis", "The diagnosis was not found, so exclusions, specific-disease waiting and treatment sub-limits could not be checked.")
    if declared_pre_existing is None:
        run.need("declared_pre_existing", "It is not known whether the condition existed before the policy started.")
    specific = [k for k in ce["specific_disease_keywords"] if k in text]

    if adm and start:
        days_in = (adm - start).days
        months_in = months_between(start, adm)
        if days_in < 0:
            out.append({"code": "POLICY_NOT_STARTED", "label": "Admitted before the policy started",
                        "basis": f"admitted {adm.isoformat()}, policy starts {start.isoformat()}", "clause": _ref(terms, ref_field)})
        elif terms.initial_waiting_days is None:
            run.need("initial_waiting_days", "The initial waiting period was not found.")
        elif days_in < terms.initial_waiting_days and not is_accident:
            _block(out, terms, run, {"code": "WAITING_INITIAL", "label": "Within the initial waiting period",
                        "basis": f"{days_in} days after the policy started; the initial waiting period is {terms.initial_waiting_days} days (accidents are exempt)",
                        "clause": _ref(terms, "initial_waiting_days")})
        if days_in >= 0:
            if declared_pre_existing is True:
                if terms.ped_waiting_months is None:
                    run.need("ped_waiting_months", "The waiting period for pre-existing diseases was not found.")
                elif months_in < terms.ped_waiting_months:
                    _block(out, terms, run, {"code": "WAITING_PRE_EXISTING", "label": "Pre-existing condition within its waiting period",
                                "basis": f"{months_in} months after the policy started; the waiting period for pre-existing diseases is {terms.ped_waiting_months} months",
                                "clause": _ref(terms, "ped_waiting_months")})
            if specific:
                if terms.specific_waiting_months is None:
                    run.need("specific_waiting_months", "The specific-disease waiting period was not found.")
                elif months_in < terms.specific_waiting_months and not is_accident:
                    _block(out, terms, run, {"code": "WAITING_SPECIFIC_DISEASE", "label": "Specific disease within its waiting period",
                                "basis": f"'{specific[0]}' is usually under the specific-disease waiting period of {terms.specific_waiting_months} months; {months_in} months have passed",
                                "clause": _ref(terms, "specific_waiting_months")})

    for ex in terms.exclusions:
        kw = _key_words(ex.text)
        if not text or not kw or not _has(text, kw):
            continue
        qualified = any(q in ex.text.lower() for q in ce["qualifier_words"])
        if qualified:
            if is_accident and "accident" in ex.text.lower():
                continue
            run.doubt("EXCLUSION_MAY_APPLY", "An exclusion of the policy may apply to this treatment, but it has a condition (such as 'unless'). "
                                             "Please check: \"" + ex.text[:120] + "\"")
        else:
            out.append({"code": "EXCLUSION", "label": "The treatment looks excluded by the policy", "basis": ex.text[:200],
                        "clause": _clause(ex.text, ex.page)})
    return out


def estimate_claim(terms: PolicyTerms, bill: BillFacts, *, declared_pre_existing: bool | None = None, is_accident: bool = False,
                   utilised_inr: int | None = None) -> dict:
    """The estimate. `utilised_inr` is what earlier claims on this policy have already used of the cover (None = not known)."""
    ce = cfg()
    run = _Run()
    items: list[BillItem] = list(bill.items)
    bill_total = sum(i.amount_inr for i in items)
    if bill.total_inr is not None and bill.total_inr != bill_total:
        run.doubt("BILL_TOTAL_MISMATCH", f"The bill items add up to {bill_total}, but the bill total says {bill.total_inr}. The items were used.")
    no_breakdown = len(items) == 1
    if no_breakdown:
        run.doubt("NO_ITEM_BREAKDOWN", "The bill has no item-wise breakdown, so room rent, doctor and other charges could not be treated separately.")
    text = " ".join(x for x in (bill.diagnosis, bill.procedure) if x).lower()
    payable = bill_total
    run.step("BILL", "Hospital bill", 0, payable, f"{len(items)} item(s) added up")

    blocks = _blocks(terms, bill, text, declared_pre_existing, is_accident, run)
    if blocks:
        first = True
        for b in blocks:
            d = payable if first else 0
            run.step(b["code"], b["label"], d, 0, b["basis"], b["clause"])
            first = False
        payable = 0
    else:
        payable = _apply_rules(terms, items, text, payable, run)

    # the cover that is left
    sum_ins = terms.sum_insured_inr
    if utilised_inr is None:
        run.need("already_claimed", "Earlier claims on this policy in the year were not known, so the full cover was assumed to be left.")
        used = 0
    else:
        used = utilised_inr
    remaining = max(sum_ins - used, 0)
    if payable > remaining:
        run.step("CAP_SUM_INSURED", "Limited to the cover that is left", payable - remaining, remaining,
                 f"sum insured {sum_ins}, already used {used}", _ref(terms, "sum_insured_inr"))
        payable = remaining

    conf = cfg()["confidence"]
    score = 1.0 - conf["per_missing_information"] * len(run.missing) - conf["per_uncertainty"] * len(run.unsure) \
        - (conf["no_item_breakdown"] if no_breakdown else 0.0)
    score = round(max(score, conf["floor"]), 2)
    level = "high" if score >= conf["high_at"] else "medium" if score >= conf["medium_at"] else "low"
    haircut = ce["conservative_haircut_pct"][level]
    return {"estimated_claim_inr": payable, "conservative_inr": payable * (100 - haircut) // 100, "bill_total_inr": bill_total,
            "deductions_total_inr": bill_total - payable, "status": "likely_ineligible" if blocks else "likely_eligible",
            "route": "cashless" if bill.hospital_network else "reimbursement" if bill.hospital_network is False else "unknown",
            "demo_waivers": run.waived,
            "demo_notice": "DEMO MODE: the waiting period(s) listed in demo_waivers were NOT applied. A real policy bought today would not pay." if run.waived else None,
            "steps": run.steps, "missing_information": run.missing, "uncertainties": run.unsure,
            "confidence": {"score": score, "level": level, "haircut_pct": haircut},
            "coverage": {"sum_insured_inr": sum_ins, "used_before_inr": used, "left_before_inr": remaining,
                         "left_after_inr": max(remaining - payable, 0)},
            "disclaimer": ce["disclaimer"]}


def _apply_rules(terms: PolicyTerms, items: list[BillItem], text: str, payable: int, run: _Run) -> int:
    ce = cfg()
    # 1. charges that are never paid
    live: list[BillItem] = []
    for it in items:
        if _has(it.description.lower(), ce["not_payable_keywords"]):
            payable -= it.amount_inr
            run.step("NOT_PAYABLE", "Charge the policy does not pay: " + it.description, it.amount_inr, payable,
                     "usually not paid by a health policy (matched in the description)")
        else:
            live.append(it)

    # 2. room rent above the limit, and the charges that scale with the room
    limit = _room_limit(terms, run)
    room_lines = [i for i in live if _has(i.description.lower(), ce["room_line_keywords"])]
    if limit is not None and room_lines:
        charged = eligible = 0
        for i in room_lines:
            rate = i.rate_inr_per_day or (i.amount_inr // i.days if i.days and i.amount_inr % i.days == 0 else None)
            if not i.days or not rate:
                run.doubt("ROOM_DAYS_UNKNOWN", "A room line has no days or daily rate, so the room rent limit could not be applied to it.")
                charged += i.amount_inr
                eligible += i.amount_inr
                continue
            charged += rate * i.days
            eligible += min(rate, limit) * i.days
        if eligible < charged:
            payable -= charged - eligible
            run.step("ROOM_RENT_CAP", "Room rent above the policy limit", charged - eligible, payable,
                     f"the room costs more per day than the limit of {limit} per day", _ref(terms, "room_rent"))
            if ce["proportionate_deduction"]:
                group = [i for i in live if i not in room_lines and not _has(i.description.lower(), ce["proportionate_exempt_keywords"])
                         and (i.category in ce["proportionate_categories"] or _has(i.description.lower(), ce["proportionate_keywords"]))]
                gt = sum(i.amount_inr for i in group)
                scaled = gt * eligible // charged            # rounded down: never in favour of a larger estimate
                if gt - scaled:
                    payable -= gt - scaled
                    run.step("ROOM_RENT_PROPORTIONATE", "Doctor, surgery and nursing charges scaled down with the room", gt - scaled, payable,
                             f"{eligible} of {charged} of the room charge is allowed, so the charges that depend on the room are scaled the same way",
                             _ref(terms, "room_rent"))

    # 3. a limit on this treatment
    for sl in terms.sub_limits:
        head = sl.text.split(":", 1)[0]
        kw = _key_words(head)
        if not text or not kw or not _has(text, kw):
            continue
        lim = sl.limit_inr if sl.limit_inr else math.floor(Fraction(terms.sum_insured_inr) * Fraction(str(sl.limit_pct_of_sum_insured)) / 100) \
            if sl.limit_pct_of_sum_insured else None
        if lim is None:
            continue
        run.doubt("SUB_LIMIT_MATCHED_BY_WORDS", "A treatment limit of the policy was matched to this diagnosis by its words. Please check that it applies: \"" + head[:80] + "\"")
        if payable > lim:
            run.step("SUB_LIMIT", "Limit for this treatment: " + head[:60], payable - lim, lim, f"the policy pays at most {lim} for this treatment",
                     _clause(sl.text, sl.page))
            payable = lim
        break

    # 4. deductible and co-pay, in the order the policy uses
    ded = terms.deductible_inr
    if ded is None:
        run.need("deductible_inr", "The deductible of the policy was not found; none was assumed.")
        ded = 0
    pct = terms.copay_pct
    if pct is None:
        run.need("copay_pct", "The co-payment of the policy was not found; none was assumed.")
        pct = 0

    def deductible(p: int) -> int:
        d = min(ded, p)
        if d:
            run.step("DEDUCTIBLE", "Deductible", d, p - d, f"the policy makes you pay the first {ded} of a claim", _ref(terms, "deductible_inr"))
        return p - d

    def copay(p: int) -> int:
        c = math.ceil(Fraction(p) * Fraction(str(pct)) / 100)        # rounded up: the share you pay is never understated
        if c:
            run.step("COPAY", "Co-payment", c, p - c, f"you pay {pct:g}% of the claim", _ref(terms, "copay_pct"))
        return p - c

    return copay(deductible(payable)) if ce["copay_after_deductible"] else deductible(copay(payable))
