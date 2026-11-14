"""Cross-checks the documents of ONE application against each other, against the insurer's own record, and against
the estimate from the policy terms.

Why this exists (CLAUDE.md rules 2, 3, 11, 12): a document can be altered, blurred or belong to someone else, so no document
is trusted on its own. The policy, the bill, the discharge summary, the claim acknowledgement and the approval letter must
tell the same story (one patient, one policy, one hospital, one claim, dates in order, amounts that add up), an approval
letter must match the insurer's record, and a document must not have been used for another application.

What it decides, and what it does not:
  * Deterministic code, no model. Same readings in, same report out. Nothing is stored here.
  * Each check has a status: pass | warn | fail | not_checked. The verdict is
        inconsistent  at least one fail: the documents contradict each other or the insurer. No financing can follow;
        incomplete    a needed document is missing;
        needs_review  nothing contradicts, but something could not be confirmed (no insurer record, a name written
                      differently, a document used before, an approval far above the estimate): a person must look;
        consistent    everything that could be checked agrees.
  * It never calls anyone a fraudster. A disagreement is reported in plain words and routed to a person; it is never
    "settled" by guessing which document is right.
  * It reads only what bears on the claim. The `facts` it returns are the person's own values, shown back to them.

Owner: Member 1 · config: config/consumer_docs.yaml `verify`, config/insurer_registry_sim.yaml
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Callable

from chaukas.consumer import docs, estimate, insurer_registry, risk

WORKFLOWS = ("approved_claim", "preapproval")


def cfg() -> dict:
    return docs.cfg()["verify"]


def inr(n: int | None) -> str:
    """₹2,00,000 (Indian grouping) for a message."""
    if n is None:
        return "—"
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + parts + [tail])
    return ("-" if n < 0 else "") + "₹" + s


# ------------------------------------------------------------------ comparing names, organisations, references
def _words(text: str) -> list[str]:
    hon = set(cfg()["honorifics"])
    return [w for w in re.findall(r"[a-z]+", str(text or "").lower()) if w not in hon]


def names_match(a: str | None, b: str | None) -> str | None:
    """'exact' | 'close' | None. 'close' = one name is the other with a middle name added or an initial for a first name."""
    ta, tb = _words(a), _words(b)
    fa, fb = {w for w in ta if len(w) > 1}, {w for w in tb if len(w) > 1}
    ia, ib = {w for w in ta if len(w) == 1}, {w for w in tb if len(w) == 1}
    if not fa or not fb:
        return None
    if fa == fb:
        return "exact" if ia == ib else "close"
    small, big = (fa, fb) if len(fa) <= len(fb) else (fb, fa)
    if len(small) >= 2 and small <= big:
        return "close"
    overlap = fa & fb
    if overlap:                      # "R Sharma" and "Rahul Sharma": the initial stands for the first name that is left over
        left_a, left_b = fa - overlap, fb - overlap
        if (ia and not left_a and ia <= {w[0] for w in left_b}) or (ib and not left_b and ib <= {w[0] for w in left_a}):
            return "close"
    return None


def _org_tokens(name: str | None) -> set[str]:
    gen = set(cfg()["org_generic_words"])
    return {w for w in re.findall(r"[a-z]+", str(name or "").lower()) if w not in gen}


def org_match(a: str | None, b: str | None) -> bool:
    """Same company or hospital: the same telling words, or one name wholly inside the other (when it has enough words)."""
    ta, tb = _org_tokens(a), _org_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small) >= cfg()["min_org_tokens_for_partial_match"] and small <= big


def _ref(x: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(x or "").upper())


# ------------------------------------------------------------------ the report
class _Report:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, code: str, status: str, message: str, docs_: list[str] | None = None) -> None:
        self.checks.append({"code": code, "status": status, "message": message, "documents": docs_ or []})

    def failed(self) -> list[str]:
        return [c["code"] for c in self.checks if c["status"] == "fail"]


def _val(reading: dict | None, field: str):
    return reading["fields"][field]["value"] if reading and field in reading["fields"] else None


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def fingerprints(by_type: dict[str, dict], readings: list[dict]) -> list[dict]:
    """Hashes (no personal data) that identify a file, a bill and a claim, so reuse in another application can be found."""
    out = [{"kind": "file", "fingerprint": r["text_sha256"]} for r in readings if r["doc_type"] != "unknown"]
    bill = by_type.get("hospital_bill")
    if bill and all(_val(bill, k) for k in ("hospital_name", "bill_number", "total_inr")):
        key = f"{'-'.join(sorted(_org_tokens(_val(bill, 'hospital_name'))))}|{_ref(_val(bill, 'bill_number'))}|{_val(bill, 'total_inr')}"
        out.append({"kind": "bill", "fingerprint": hashlib.sha256(key.encode()).hexdigest()})
    for t in ("claim_approval", "claim_ack"):
        d = by_type.get(t)
        if d and _val(d, "claim_reference") and _val(d, "insurer_name"):
            key = f"{'-'.join(sorted(_org_tokens(_val(d, 'insurer_name'))))}|{_ref(_val(d, 'claim_reference'))}"
            out.append({"kind": "claim", "fingerprint": hashlib.sha256(key.encode()).hexdigest()})
            break
    seen, uniq = set(), []
    for f in out:
        if (f["kind"], f["fingerprint"]) not in seen:
            seen.add((f["kind"], f["fingerprint"]))
            uniq.append(f)
    return uniq


def verify(readings: list[dict], workflow: str, *, lookup: Callable[[str | None], dict | None] = insurer_registry.lookup,
           used_elsewhere: Callable[[str, str], bool] | None = None, declared_pre_existing: bool | None = None,
           already_claimed_inr: int | None = None) -> dict:
    """`readings` are docs.read_document results (corrections already applied). `used_elsewhere(kind, fingerprint)` says whether
    that hash was recorded by ANOTHER application."""
    if workflow not in WORKFLOWS:
        raise ValueError("unknown workflow")
    c = cfg()
    rep = _Report()
    by_type: dict[str, dict] = {}
    for r in readings:
        if r["doc_type"] == "unknown":
            rep.add("DOCUMENT_NOT_RECOGNISED", "warn", "A document was not recognised as a policy, bill or claim letter and was not used.")
            continue
        if r["doc_type"] in by_type:
            rep.add("MORE_THAN_ONE_OF_A_TYPE", "warn", f"More than one {r['doc_type'].replace('_', ' ')} was given. Only the first was used.", [r["doc_type"]])
            continue
        by_type[r["doc_type"]] = r
    pol, bill, dis = by_type.get("policy"), by_type.get("hospital_bill"), by_type.get("discharge_summary")
    ack, apr = by_type.get("claim_ack"), by_type.get("claim_approval")

    # ---- the set is complete, and each document was read well enough
    missing = [t for t in c["required_documents"][workflow] if t not in by_type]
    if missing:
        rep.add("DOCUMENTS_MISSING", "not_checked", "Still needed: " + ", ".join(m.replace("_", " ") for m in missing) + ".", missing)
    else:
        rep.add("DOCUMENTS_COMPLETE", "pass", "Every document needed for this application is here.")
    for t, r in by_type.items():
        if r["missing_required"]:
            rep.add("DOCUMENT_FIELDS_MISSING", "warn", f"The {t.replace('_', ' ')} is missing: " + ", ".join(r["missing_required"]) + ". Please type them in.", [t])
        if r["needs_review"]:
            rep.add("DOCUMENT_NEEDS_A_SECOND_LOOK", "warn", f"Please check these values read from the {t.replace('_', ' ')}: " + ", ".join(r["needs_review"]) + ".", [t])
        bad = [w["code"] for w in r["warnings"] if w["code"] in ("BILL_ITEMS_DO_NOT_ADD_UP", "APPROVAL_DOES_NOT_ADD_UP", "APPROVED_ABOVE_CLAIMED", "DATES_OUT_OF_ORDER", "POLICY_DATES_OUT_OF_ORDER", "DOC_INJECTION_SUSPECTED")]
        if bad:
            rep.add("DOCUMENT_INTERNAL_PROBLEM", "warn", f"The {t.replace('_', ' ')} does not add up by itself ({', '.join(bad)}).", [t])
    same = {}
    for r in readings:
        if r["doc_type"] != "unknown":
            same.setdefault(r["text_sha256"], []).append(r["doc_type"])
    for types in same.values():
        if len(types) > 1:
            rep.add("SAME_DOCUMENT_TWICE", "fail", "The same document was uploaded more than once as different documents.", types)

    # ---- one patient, one policy, one insurer, one hospital, one claim
    named = [(t, _val(by_type[t], "patient_name")) for t in ("hospital_bill", "discharge_summary", "claim_ack", "claim_approval") if t in by_type and _val(by_type[t], "patient_name")]
    if len(named) >= 2:
        bad = [t for t, n in named[1:] if not names_match(named[0][1], n)]
        close = [t for t, n in named[1:] if names_match(named[0][1], n) == "close"]
        if bad:
            rep.add("PATIENT_NAME_DIFFERS", "fail", "The patient's name is not the same on every document (" + ", ".join(t.replace("_", " ") for t in [named[0][0]] + bad) + ").", [named[0][0]] + bad)
        elif close:
            rep.add("PATIENT_NAME_WRITTEN_DIFFERENTLY", "warn", "The patient's name is written slightly differently on some documents. Please check it is the same person.", [named[0][0]] + close)
        else:
            rep.add("PATIENT_NAME_MATCHES", "pass", "The patient's name is the same on every document.", [t for t, _ in named])
    else:
        rep.add("PATIENT_NAME_MATCHES", "not_checked", "The patient's name appears on fewer than two documents, so it could not be compared.")
    holder, patient = _val(pol, "holder_name"), (named[0][1] if named else None)
    if holder and patient and not names_match(holder, patient):
        rep.add("PATIENT_IS_NOT_THE_HOLDER", "warn", "The patient is not the policy holder. That is fine if the patient is an insured member of the policy: please check.", ["policy", named[0][0]])

    def same_field(code: str, label: str, field: str, types: tuple[str, ...], eq: Callable[[str, str], bool], fail_status: str = "fail") -> str | None:
        vals = [(t, _val(by_type.get(t), field)) for t in types if _val(by_type.get(t), field)]
        if len(vals) < 2:
            rep.add(code, "not_checked", f"The {label} appears on fewer than two documents, so it could not be compared.")
            return vals[0][1] if vals else None
        bad = [t for t, v in vals[1:] if not eq(vals[0][1], v)]
        if bad:
            rep.add(code + "_DIFFERS", fail_status, f"The {label} is not the same on every document ({', '.join(t.replace('_', ' ') for t in [vals[0][0]] + bad)}).", [vals[0][0]] + bad)
        else:
            rep.add(code + "_MATCHES", "pass", f"The {label} is the same on every document.", [t for t, _ in vals])
        return vals[0][1]

    same_field("POLICY_NUMBER", "policy number", "policy_number", ("policy", "claim_ack", "claim_approval"), lambda a, b: _ref(a) == _ref(b))
    same_field("INSURER", "insurer", "insurer_name", ("policy", "claim_ack", "claim_approval"), org_match)
    same_field("HOSPITAL", "hospital", "hospital_name", ("hospital_bill", "discharge_summary", "claim_ack", "claim_approval"), org_match)
    same_field("CLAIM_REFERENCE", "claim reference", "claim_reference", ("claim_ack", "claim_approval"), lambda a, b: _ref(a) == _ref(b))

    # ---- dates
    adm, dsc = _d(_val(bill, "admission_date") or _val(dis, "admission_date")), _d(_val(bill, "discharge_date") or _val(dis, "discharge_date"))
    for f in ("admission_date", "discharge_date"):
        a, b = _val(bill, f), _val(dis, f)
        if a and b and a != b:
            rep.add("STAY_DATES_DIFFER", "fail", f"The {f.replace('_', ' ')} on the bill ({a}) is not the one on the discharge summary ({b}).", ["hospital_bill", "discharge_summary"])
    if not any(x["code"] == "STAY_DATES_DIFFER" for x in rep.checks):
        both = bool(_val(bill, "admission_date") and _val(dis, "admission_date"))
        rep.add("STAY_DATES_MATCH", "pass" if both else "not_checked",
                "The bill and the discharge summary give the same stay." if both else "The stay dates are on only one document, so they could not be compared.")
    start, end = _d(_val(pol, "policy_start")), _d(_val(pol, "policy_end"))
    if adm and start and end:
        if start <= adm <= end:
            rep.add("ADMITTED_INSIDE_POLICY_PERIOD", "pass", "The admission is inside the policy period.", ["policy", "hospital_bill"])
        else:
            rep.add("ADMITTED_OUTSIDE_POLICY_PERIOD", "fail", f"The admission ({adm.isoformat()}) is outside the policy period ({start.isoformat()} to {end.isoformat()}).", ["policy", "hospital_bill"])
    cdate = _d(_val(ack, "claim_date"))
    if cdate and adm and cdate < adm:
        rep.add("CLAIM_BEFORE_ADMISSION", "fail", "The claim is dated before the admission.", ["claim_ack", "hospital_bill"])
    elif cdate and dsc and cdate < dsc:
        rep.add("CLAIM_BEFORE_DISCHARGE", "warn", "The claim is dated before the discharge. That is possible for a claim made during the stay: please check.", ["claim_ack", "discharge_summary"])
    pdate = _d(_val(apr, "expected_payment_date"))
    if pdate and cdate and pdate < cdate:
        rep.add("PAYMENT_DATE_BEFORE_CLAIM", "warn", "The expected payment date is before the claim date. Please check it.", ["claim_approval", "claim_ack"])

    # ---- amounts
    total, claimed_ack = _val(bill, "total_inr"), _val(ack, "claimed_inr")
    claimed_apr, approved = _val(apr, "claimed_inr"), _val(apr, "approved_inr")
    if claimed_ack is not None and total is not None:
        rep.add("CLAIMED_WITHIN_BILL" if claimed_ack <= total else "CLAIMED_ABOVE_BILL", "pass" if claimed_ack <= total else "fail",
                f"The claim ({inr(claimed_ack)}) is within the bill ({inr(total)})." if claimed_ack <= total else f"The claim ({inr(claimed_ack)}) is more than the bill ({inr(total)}).", ["claim_ack", "hospital_bill"])
    if claimed_ack is not None and claimed_apr is not None:
        rep.add("CLAIMED_AMOUNT_MATCHES" if claimed_ack == claimed_apr else "CLAIMED_AMOUNT_DIFFERS", "pass" if claimed_ack == claimed_apr else "fail",
                "The approval letter and the acknowledgement name the same claimed amount." if claimed_ack == claimed_apr else f"The acknowledgement says {inr(claimed_ack)} was claimed, the approval letter says {inr(claimed_apr)}.", ["claim_ack", "claim_approval"])
    if approved is not None and (claimed_apr or claimed_ack) is not None:
        ceiling = claimed_apr if claimed_apr is not None else claimed_ack
        rep.add("APPROVED_WITHIN_CLAIMED" if approved <= ceiling else "APPROVED_ABOVE_CLAIMED", "pass" if approved <= ceiling else "fail",
                "The approved amount is within the claim." if approved <= ceiling else f"The approved amount ({inr(approved)}) is more than the claim ({inr(ceiling)}).", ["claim_approval"])
    if apr and _val(apr, "settlement_status") in ("settled", "rejected"):
        s = _val(apr, "settlement_status")
        rep.add("LETTER_SAYS_" + s.upper(), "fail", "The letter says this claim is already paid, so there is nothing to advance against." if s == "settled" else "The letter says this claim was rejected.", ["claim_approval"])
    elif apr and _val(apr, "settlement_status") is None:
        rep.add("LETTER_STATUS_UNKNOWN", "warn", "The letter does not say clearly that the claim is approved. Please check.", ["claim_approval"])

    # ---- the estimate from the policy terms
    est = None
    if pol and bill:
        try:
            est = estimate.estimate_claim(estimate.terms_from_reading(pol), estimate.bill_from_readings(bill, dis),
                                          declared_pre_existing=declared_pre_existing, utilised_inr=already_claimed_inr)
        except ValueError as e:
            rep.add("ESTIMATE_NOT_POSSIBLE", "not_checked", f"An estimate from the policy terms could not be worked out: {e}.", ["policy", "hospital_bill"])
    if est and approved is not None:
        e = est["estimated_claim_inr"]
        tol = c["estimate_tolerance_pct"]
        if e == 0:
            rep.add("APPROVAL_AGAINST_ESTIMATE", "warn", f"The policy terms suggest this claim would not be paid, but the letter approves {inr(approved)}. A person should check.", ["claim_approval", "policy"])
        elif approved * 100 > e * (100 + tol):
            rep.add("APPROVAL_ABOVE_ESTIMATE", "warn", f"The letter approves {inr(approved)}, more than the {inr(e)} the policy terms suggest (more than {tol}% above). A person should check.", ["claim_approval", "policy"])
        else:
            rep.add("APPROVAL_CLOSE_TO_ESTIMATE", "pass", f"The approved amount ({inr(approved)}) is in line with the estimate from the policy terms ({inr(e)}).", ["claim_approval", "policy"])

    # ---- the insurer's own record
    registry = None
    if apr:
        ref = _val(apr, "claim_reference") or _val(ack, "claim_reference")
        rec = lookup(ref)
        if rec is None:
            rep.add("INSURER_RECORD_NOT_FOUND", "warn", "This claim could not be confirmed with the insurer. A person must check the letter before any financing.", ["claim_approval"])
        else:
            registry = {"found": True, "status": rec["status"], "claimed_inr": rec["claimed_inr"], "approved_inr": rec["approved_inr"], "payment_expected": rec.get("payment_expected")}
            diffs = []
            if approved is not None and approved != rec["approved_inr"]:
                diffs.append(f"approved amount (letter {inr(approved)}, insurer {inr(rec['approved_inr'])})")
            if (claimed_apr if claimed_apr is not None else claimed_ack) not in (None, rec["claimed_inr"]):
                diffs.append(f"claimed amount (letter {inr(claimed_apr if claimed_apr is not None else claimed_ack)}, insurer {inr(rec['claimed_inr'])})")
            if _val(apr, "policy_number") and _ref(_val(apr, "policy_number")) != _ref(rec["policy_number"]):
                diffs.append("policy number")
            if _val(apr, "insurer_name") and not org_match(_val(apr, "insurer_name"), rec["insurer"]):
                diffs.append("insurer")
            who = _val(apr, "patient_name") or patient
            if who and not names_match(who, rec["patient_name"]):
                diffs.append("patient name")
            if diffs:
                rep.add("INSURER_RECORD_DIFFERS", "fail", "The letter does not match the insurer's own record: " + "; ".join(diffs) + ".", ["claim_approval"])
            elif rec["status"] == "settled":
                rep.add("INSURER_SAYS_SETTLED", "fail", "The insurer's record shows this claim is already paid, so there is nothing to advance against.", ["claim_approval"])
            elif rec["status"] in ("rejected", "pending"):
                rep.add("INSURER_SAYS_" + rec["status"].upper(), "fail", f"The letter says approved, but the insurer's record shows this claim as {rec['status']}.", ["claim_approval"])
            else:
                rep.add("INSURER_RECORD_CONFIRMS", "pass", "The insurer's own record confirms this approval.", ["claim_approval"])

    # ---- used before
    fps = fingerprints(by_type, readings)
    if used_elsewhere:
        reused = sorted({f["kind"] for f in fps if used_elsewhere(f["kind"], f["fingerprint"])})
        if reused:
            what = {"file": "a document", "bill": "this hospital bill", "claim": "this claim"}
            rep.add("USED_IN_ANOTHER_APPLICATION", "warn", "Already used for another application: " + ", ".join(what[k] for k in reused) + ". A person must check this before any financing.", [])

    # ---- advisory signals: they only ask a person to look (risk.py); they can never clear or fail anything
    risk_out = None
    if apr or bill:
        since = _d(_val(pol, "continuous_since")) or start
        stay = (dsc - adm).days if adm and dsc and dsc >= adm else None
        risk_out = risk.assess({
            "bill_total_inr": total, "stay_days": max(stay, 1) if stay is not None else None, "sum_insured_inr": _val(pol, "sum_insured_inr"),
            "policy_age_days": (adm - since).days if adm and since else None, "claimed_inr": claimed_apr if claimed_apr is not None else claimed_ack,
            "approved_inr": approved, "estimated_inr": est["estimated_claim_inr"] if est else None,
            "route": est["route"] if est else None, "no_item_breakdown": bool(est) and any(u.get("code") == "NO_ITEM_BREAKDOWN" for u in est["uncertainties"]), "uncertainties": len(est["uncertainties"]) if est else 0,
            "approval_date": cdate, "expected_payment_date": pdate})
        if risk_out["review"]:
            rep.add("RISK_SIGNALS", "warn", "A few things look unusual and a person should look: " + "; ".join(f["message"] for f in risk_out["flags"]), [])

    statuses = [x["status"] for x in rep.checks]
    verdict = "inconsistent" if "fail" in statuses else "incomplete" if missing else "needs_review" if "warn" in statuses else "consistent"
    pick = lambda f, *srcs: next(((_val(by_type.get(s), f), s) for s in srcs if _val(by_type.get(s), f) is not None), (None, None))   # noqa: E731
    facts: dict = {}
    for name, field, srcs in (("patient_name", "patient_name", ("claim_approval", "claim_ack", "hospital_bill", "discharge_summary")),
                              ("policy_number", "policy_number", ("claim_approval", "claim_ack", "policy")),
                              ("insurer_name", "insurer_name", ("policy", "claim_approval", "claim_ack")),
                              ("holder_name", "holder_name", ("policy",)),
                              ("hospital_name", "hospital_name", ("hospital_bill", "discharge_summary", "claim_ack", "claim_approval")),
                              ("claim_reference", "claim_reference", ("claim_approval", "claim_ack")),
                              ("bill_total_inr", "total_inr", ("hospital_bill",)), ("claimed_inr", "claimed_inr", ("claim_approval", "claim_ack")),
                              ("approved_inr", "approved_inr", ("claim_approval",)), ("deduction_inr", "deduction_inr", ("claim_approval",)),
                              ("sum_insured_inr", "sum_insured_inr", ("policy",)), ("admission_date", "admission_date", ("hospital_bill", "discharge_summary")),
                              ("discharge_date", "discharge_date", ("hospital_bill", "discharge_summary")),
                              ("expected_payment_date", "expected_payment_date", ("claim_approval",)), ("diagnosis", "diagnosis", ("discharge_summary", "hospital_bill"))):
        v, src = pick(field, *srcs)
        if v is not None:
            facts[name] = {"value": v, "from": src, "corrected": bool(by_type[src]["fields"][field].get("corrected"))}
    return {"workflow": workflow, "verdict": verdict, "checks": rep.checks, "failed": rep.failed(), "missing_documents": missing,
            "documents": [{"doc_type": r["doc_type"], "pages": r["pages"], "corrected": sorted(f for f, v in r["fields"].items() if v.get("corrected"))} for r in readings],
            "facts": facts, "estimate": est, "registry": registry, "risk": risk_out, "fingerprints": fps}


def preauth_key(facts: dict) -> str | None:
    """A hash that identifies ONE estimate (hospital, patient, policy, bill total) so it can back only one advance. No personal data."""
    v = lambda k: (facts.get(k) or {}).get("value")      # noqa: E731
    if not all(v(k) for k in ("hospital_name", "patient_name", "policy_number", "bill_total_inr")):
        return None
    key = f"{'-'.join(sorted(_org_tokens(v('hospital_name'))))}|{'-'.join(sorted(_words(v('patient_name'))))}|{_ref(v('policy_number'))}|{v('bill_total_inr')}"
    return hashlib.sha256(key.encode()).hexdigest()
