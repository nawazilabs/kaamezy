"""Deterministic guardrail for the consumer flows. NO LLM anywhere in here.

Same input -> same decision. Checks run in a fixed order and the first failure decides. Every check reads facts
the SERVER holds (stored quote, stored KYC, stored policies and loans, stored human approval, the server's own OTP
check). Nothing that decides money is taken from the client's word, and an amount is always recomputed by the
rule engine and compared with the stored one.

BLOCK is final: no OTP and no human approval turns a BLOCK into an ALLOW.

Owner: Member 2 (rules) / Member 1 (integration) · mirrors chaukas/policy/engine.py for the merchant side
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from chaukas.consumer import engine
from chaukas.consumer.models import InsuranceProposal, LoanApplication

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")
PIN_RE = re.compile(r"^[0-9]{6}$")

ALLOW, BLOCK, CONFIRM = "ALLOW", "BLOCK", "NEEDS_CONFIRMATION"


def inr(n: int) -> str:
    s = str(int(n))
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "₹" + ",".join(parts) + "," + tail


class _Run:
    """Runs named checks in order; the first failure ends the run, the rest are reported as not run."""

    def __init__(self, names: list[str]):
        self.names = names
        self.checks: list[dict] = []

    def step(self, ok: bool, fail: dict) -> dict | None:
        self.checks.append({"name": self.names[len(self.checks)], "ok": bool(ok)})
        return None if ok else fail

    def out(self, fail: dict) -> dict:
        while len(self.checks) < len(self.names):
            self.checks.append({"name": self.names[len(self.checks)], "ok": None})
        return {**fail, "checks": self.checks}

    def allow(self) -> dict:
        return {"verdict": ALLOW, "kind": None, "reason_code": "WITHIN_POLICY", "reason": "Every check passed.",
                "checks": self.checks}


def _block(code: str, reason: str) -> dict:
    return {"verdict": BLOCK, "kind": None, "reason_code": code, "reason": reason}


def _confirm(kind: str, code: str, reason: str) -> dict:
    return {"verdict": CONFIRM, "kind": kind, "reason_code": code, "reason": reason}


# ------------------------------------------------------------------ issue a health policy
def check_policy_purchase(*, proposal: InsuranceProposal, stored_quote: dict, insurer_id: str, pan: str | None,
                          mobile: str, holder_name: str, nominee_name: str, consent: bool, kyc_verified: bool,
                          active_policies: list[dict], otp_ok: bool, human_ok: bool) -> dict:
    g = engine.cfg()["health_insurance"]["guardrail"]
    run = _Run(["Identity verified (DigiLocker)", "Consent recorded",
                f"Proposer age {g['min_proposer_age']}–{g['max_proposer_age']}", "Identity details valid",
                "Premium matches rule engine", "No duplicate active policy", "Medical review not required",
                f"Premium within auto limit {inr(g['auto_limit_inr'])}"])
    f = run.step(kyc_verified, _block("KYC_PENDING", "Identity is not verified yet. Verify with DigiLocker first; "
                                                    "Kaamezy never asks for your Aadhaar number."))
    if f: return run.out(f)
    f = run.step(consent, _block("CONSENT_MISSING", "No consent to share health details with the insurer. "
                                                    "Nothing can be bought without it."))
    if f: return run.out(f)
    proposer = next((m for m in proposal.members if m.rel == "Self"), proposal.members[0])
    f = run.step(g["min_proposer_age"] <= proposer.age <= g["max_proposer_age"],
                 _block("PROPOSER_AGE_OUT_OF_RANGE", f"The policy holder must be between {g['min_proposer_age']} and "
                                                     f"{g['max_proposer_age']}. This cannot be overridden by a confirmation."))
    if f: return run.out(f)
    id_ok = bool(pan and PAN_RE.match(pan) and MOBILE_RE.match(mobile or "") and len((holder_name or "").strip()) >= 2
                 and len((nominee_name or "").strip()) >= 2)
    f = run.step(id_ok, _block("IDENTITY_DETAILS_INVALID",
                               "PAN, mobile, name or nominee details are missing or in the wrong format."))
    if f: return run.out(f)
    chosen = next((q for q in stored_quote["quotes"] if q["insurer_id"] == insurer_id), None)
    fresh = next((q for q in engine.quotes(proposal) if q["insurer_id"] == insurer_id), None)
    f = run.step(chosen is not None and fresh is not None and chosen["premium_inr"] == fresh["premium_inr"],
                 _block("AMOUNT_MISMATCH", "The premium does not match the rule engine's figure, so it was refused."))
    if f: return run.out(f)
    sig = engine.member_signature(proposal)
    dup = next((p for p in active_policies if p["insurer_id"] == insurer_id and p["member_sig"] == sig), None)
    f = run.step(dup is None, _block("DUPLICATE_POLICY", "An active policy with " + chosen["name"] +
                                     " already covers these members (" + (dup["id"] if dup else "") + ")."))
    if f: return run.out(f)
    f = run.step(not engine.needs_underwriter(proposal) or human_ok,
                 _confirm("human", "UNDERWRITER_REVIEW",
                          f"A heart condition with cover above {inr(g['heart_review_above_cover_inr'])}, a cancer "
                          f"history, or a member above {g['max_proposer_age']} needs a human underwriter to approve."))
    if f: return run.out(f)
    f = run.step(chosen["premium_inr"] <= g["auto_limit_inr"] or otp_ok,
                 _confirm("otp", "OVER_AUTO_LIMIT", f"The premium is above the {inr(g['auto_limit_inr'])} automatic limit, "
                                                    "so a one-time code is required. Typing 'yes' is not enough."))
    if f: return run.out(f)
    return run.allow()


# ------------------------------------------------------------------ accept a personal loan
def check_loan_accept(*, application: LoanApplication, stored_quote: dict, pan: str | None, mobile: str,
                      holder_name: str, pincode: str, consent_offer: bool, consent_credit: bool, kyc_verified: bool,
                      kfs_shown: bool, active_loans: list[dict], otp_ok: bool, human_ok: bool,
                      now: datetime | None = None) -> dict:
    g = engine.cfg()["personal_loan"]["guardrail"]
    now = now or datetime.now(timezone.utc)
    stored = stored_quote["decision"]
    run = _Run(["Identity verified (DigiLocker)", "Key Facts shown before consent",
                "Consent recorded (offer and credit check)", "Identity details valid",
                "Offer matches lender decision", "No duplicate loan, within loan limit",
                "Income review not required", f"Amount within auto limit {inr(g['auto_limit_inr'])}"])
    f = run.step(kyc_verified, _block("KYC_PENDING", "Identity is not verified yet. Verify with DigiLocker first; "
                                                    "Kaamezy never asks for your Aadhaar number."))
    if f: return run.out(f)
    f = run.step(kfs_shown, _block("KFS_NOT_SHOWN", "The Key Facts Statement must be shown before you can accept."))
    if f: return run.out(f)
    f = run.step(consent_offer and consent_credit,
                 _block("CONSENT_MISSING", "Both consents are needed: accepting this offer, and sharing your PAN and "
                                           "income for the credit check."))
    if f: return run.out(f)
    id_ok = bool(pan and PAN_RE.match(pan) and MOBILE_RE.match(mobile or "") and PIN_RE.match(pincode or "")
                 and len((holder_name or "").strip()) >= 2)
    f = run.step(id_ok, _block("IDENTITY_DETAILS_INVALID",
                               "PAN, mobile, pincode or name is missing or in the wrong format."))
    if f: return run.out(f)
    fresh = engine.lender_decide(application)
    same = fresh.get("eligible") and stored.get("eligible") and all(
        fresh[k] == stored[k] for k in ("amount_inr", "apr_pct", "emi_inr"))
    f = run.step(same, _block("AMOUNT_MISMATCH", "The offer does not match the lender's own figures, so it was refused."))
    if f: return run.out(f)
    cutoff = now - timedelta(hours=g["duplicate_window_hours"])
    dup = next((x for x in active_loans if x["amount_inr"] == stored["amount_inr"]
                and x["tenure_months"] == stored["tenure_months"]
                and datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")) > cutoff), None)
    too_many = len(active_loans) >= g["max_active_loans"]
    if dup:
        fail = _block("DUPLICATE_LOAN", f"A loan for the same amount and tenure was created in the last "
                                        f"{g['duplicate_window_hours']} hours ({dup['id']}).")
    else:
        fail = _block("TOO_MANY_LOANS", f"You already have {g['max_active_loans']} active loans through Kaamezy, "
                                        "which is the limit.")
    f = run.step(not dup and not too_many, fail)
    if f: return run.out(f)
    review = application.employment == "self_employed" and stored["amount_inr"] > g["self_employed_review_above_inr"]
    f = run.step(not review or human_ok,
                 _confirm("human", "INCOME_PROOF_REVIEW",
                          f"Self-employed income above {inr(g['self_employed_review_above_inr'])} needs a human to check "
                          "income proof (6 months of bank statements or 2 years of ITR)."))
    if f: return run.out(f)
    f = run.step(stored["amount_inr"] <= g["auto_limit_inr"] or otp_ok,
                 _confirm("otp", "OVER_AUTO_LIMIT", f"The loan is above the {inr(g['auto_limit_inr'])} automatic limit, "
                                                    "so a one-time code is required. Typing 'yes' is not enough."))
    if f: return run.out(f)
    return run.allow()


# ------------------------------------------------------------------ accept a claim-backed bridge advance
def check_bridge_accept(*, claim: dict | None, stored_quote: dict, pan: str | None, mobile: str, holder_name: str,
                        pincode: str, consent_offer: bool, consent_claim: bool, kyc_verified: bool, kfs_shown: bool,
                        active_loans: list[dict], otp_ok: bool, now: datetime | None = None) -> dict:
    """Same ladder as a personal loan, but the facts are the claim and the advance rule, never income. BLOCK is final."""
    g = engine.cfg()["bridge"]
    now = now or datetime.now(timezone.utc)
    stored = stored_quote["decision"]
    run = _Run(["Identity verified (DigiLocker)", "Key Facts shown before consent",
                "Consent recorded (advance and claim details)", "Identity details valid",
                "Claim is approved and the payout is pending", "Name matches the claim's policy holder", "No earlier advance on this claim, within loan limit",
                "Advance matches the lender's rule", f"Amount within auto limit {inr(g['auto_limit_inr'])}"])
    f = run.step(kyc_verified, _block("KYC_PENDING", "Identity is not verified yet. Verify with DigiLocker first; "
                                                    "Kaamezy never asks for your Aadhaar number."))
    if f: return run.out(f)
    f = run.step(kfs_shown, _block("KFS_NOT_SHOWN", "The Key Facts Statement must be shown before you can accept."))
    if f: return run.out(f)
    f = run.step(consent_offer and consent_claim,
                 _block("CONSENT_MISSING", "Both consents are needed: accepting this advance (repaid from your claim payout), "
                                           "and sharing the claim details with the lender for this advance only."))
    if f: return run.out(f)
    id_ok = bool(pan and PAN_RE.match(pan) and MOBILE_RE.match(mobile or "") and PIN_RE.match(pincode or "")
                 and len((holder_name or "").strip()) >= 2)
    f = run.step(id_ok, _block("IDENTITY_DETAILS_INVALID",
                               "PAN, mobile, pincode or name is missing or in the wrong format."))
    if f: return run.out(f)
    fresh = engine.bridge_decide({**claim, "bridge_loan_id": None}, now) if claim else {"eligible": False}
    state = engine.claim_state(claim, now)["state"] if claim else None
    f = run.step(bool(claim) and state == "approved_payout_pending",
                 _block("CLAIM_NOT_PAYABLE", "This claim is not in the 'approved, payout pending' state, so nothing can be "
                                             "advanced against it."))
    if f: return run.out(f)
    if claim.get("holder_name"):          # a claim verified from documents is advanced only to the policy holder on them
        from chaukas.consumer import verify
        f = run.step(bool(verify.names_match(claim["holder_name"], holder_name)),
                     _block("CLAIM_HOLDER_MISMATCH", "This claim belongs to the policy holder named on its documents. "
                                                      "The name you gave does not match, so no advance can be made."))
        if f: return run.out(f)
    too_many = len(active_loans) >= g["max_active_loans"]
    if claim.get("bridge_loan_id"):
        fail = _block("DUPLICATE_ADVANCE", f"This claim already backs an advance ({claim['bridge_loan_id']}). "
                                           "A claim can back only one.")
    else:
        fail = _block("TOO_MANY_LOANS", f"You already have {g['max_active_loans']} active loans through Kaamezy, "
                                        "which is the limit.")
    f = run.step(not claim.get("bridge_loan_id") and not too_many, fail)
    if f: return run.out(f)
    same = fresh.get("eligible") and stored.get("eligible") and all(fresh[k] == stored[k] for k in ("amount_inr", "apr_pct"))
    f = run.step(same, _block("AMOUNT_MISMATCH", "The advance does not match the lender's own figures, so it was refused."))
    if f: return run.out(f)
    f = run.step(stored["amount_inr"] <= g["auto_limit_inr"] or otp_ok,
                 _confirm("otp", "OVER_AUTO_LIMIT", f"The advance is above the {inr(g['auto_limit_inr'])} automatic limit, "
                                                    "so a one-time code is required. Typing 'yes' is not enough."))
    if f: return run.out(f)
    return run.allow()


# ------------------------------------------------------------------ pay a bill or recharge (the user tapped Pay on a prepared intent)
_MASKED_RE = re.compile(r"^••••[0-9]{4}$")


def check_bill_payment(*, intent: dict | None, user_id: str, kyc_verified: bool, wallet_balance: int, recent_payments: list[dict],
                       otp_ok: bool, now: datetime | None = None) -> dict:
    """Facts come from the DB and config (the stored intent, the wallet, earlier payments), never from the request body.
    First failure decides. BLOCK is final: a correct OTP does not change it."""
    from chaukas.consumer import fintech  # local import: fintech imports store/engine, keep this module light

    g = fintech.cfg()["payment_guardrail"]
    now = now or datetime.now(timezone.utc)
    run = _Run(["Identity verified (DigiLocker)", "Payment request is yours and still valid",
                "Biller allowed and account format valid", "Amount matches the biller's bill or plan",
                "Wallet balance is enough", "Not a duplicate payment", f"Within daily limit {inr(g['daily_limit_inr'])}",
                f"Amount within auto limit {inr(g['auto_limit_inr'])}"])
    f = run.step(kyc_verified, _block("KYC_PENDING", "Identity is not verified yet. Verify with DigiLocker first; "
                                                    "Kaamezy never asks for your Aadhaar number."))
    if f: return run.out(f)
    valid = bool(intent) and intent["user_id"] == user_id and not fintech.intent_expired(intent, now)
    f = run.step(valid, _block("INTENT_INVALID", "This payment request is missing, not yours, or has expired. Prepare it again."))
    if f: return run.out(f)
    b = fintech.biller(intent["biller_id"])
    f = run.step(b is not None and b["kind"] == intent["kind"] and bool(_MASKED_RE.match(intent["account_masked"])),
                 _block("BILLER_NOT_ALLOWED", "That biller or account is not allowed for this payment."))
    if f: return run.out(f)
    want = fintech.expected_amount(b, intent["amount_inr"])
    same_hash = intent["params_hash"] == fintech.intent_hash(intent["kind"], intent["biller_id"], intent["account_masked"], intent["amount_inr"])
    f = run.step(want is not None and want == intent["amount_inr"] and same_hash,
                 _block("AMOUNT_MISMATCH", "The amount does not match the biller's bill or plan, so it was refused."))
    if f: return run.out(f)
    f = run.step(wallet_balance >= intent["amount_inr"],
                 _block("INSUFFICIENT_BALANCE", f"Your balance is {inr(wallet_balance)}, which is not enough for {inr(intent['amount_inr'])}."))
    if f: return run.out(f)
    cutoff = now - timedelta(minutes=g["duplicate_window_minutes"])
    dup = next((p for p in recent_payments if p["biller_id"] == intent["biller_id"] and p["account_masked"] == intent["account_masked"]
                and p["amount_inr"] == intent["amount_inr"] and datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) > cutoff), None)
    f = run.step(dup is None, _block("DUPLICATE_PAYMENT", f"The same payment was made in the last {g['duplicate_window_minutes']} minutes "
                                                          "(" + (dup["id"] if dup else "") + "). Wait before paying it again."))
    if f: return run.out(f)
    day = now - timedelta(hours=24)
    spent = sum(p["amount_inr"] for p in recent_payments if datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) > day)
    f = run.step(spent + intent["amount_inr"] <= g["daily_limit_inr"],
                 _block("DAILY_LIMIT_EXCEEDED", f"This would take today's bill and recharge payments above {inr(g['daily_limit_inr'])}."))
    if f: return run.out(f)
    f = run.step(intent["amount_inr"] <= g["auto_limit_inr"] or otp_ok,
                 _confirm("otp", "OVER_AUTO_LIMIT", f"The payment is above the {inr(g['auto_limit_inr'])} automatic limit, "
                                                    "so a one-time code is required. Typing 'yes' is not enough."))
    if f: return run.out(f)
    return run.allow()


# ------------------------------------------------------------------ Workflow A: an advance before the claim, paid to the hospital
def check_preauth_accept(*, preauth: dict | None, stored_quote: dict, hospital: dict | None, pan: str | None, mobile: str, holder_name: str,
                         pincode: str, consent_offer: bool, consent_share: bool, kyc_verified: bool, kfs_shown: bool,
                         active_loans: list[dict], otp_ok: bool, now: datetime | None = None) -> dict:
    """Same ladder as the claim-backed advance, plus: the payee is a partner hospital from the registry (never typed by the person),
    the advance is made only to the policy holder named on the verified documents, and a one-time code is ALWAYS needed (money goes
    out before any claim exists). BLOCK is final."""
    from chaukas.consumer import verify
    P = engine.cfg()["preauth"]
    now = now or datetime.now(timezone.utc)
    stored = stored_quote["decision"]
    run = _Run(["Identity verified (DigiLocker)", "Key Facts shown before consent", "Consent recorded (advance and sharing the estimate)",
                "Identity details valid", "Offer is open and unused", "Hospital is a partner hospital", "Name matches the policy holder",
                "Within loan limit", "Advance matches the lender's rule", "One-time code confirmed"])
    f = run.step(kyc_verified, _block("KYC_PENDING", "Identity is not verified yet. Verify with DigiLocker first; Kaamezy never asks for your Aadhaar number."))
    if f: return run.out(f)
    f = run.step(kfs_shown, _block("KFS_NOT_SHOWN", "The Key Facts Statement must be shown before you can accept."))
    if f: return run.out(f)
    f = run.step(consent_offer and consent_share,
                 _block("CONSENT_MISSING", "Both consents are needed: accepting this advance (repaid from your claim payout), and sharing the estimate with the lender and the hospital for this advance only."))
    if f: return run.out(f)
    id_ok = bool(pan and PAN_RE.match(pan) and MOBILE_RE.match(mobile or "") and PIN_RE.match(pincode or "") and len((holder_name or "").strip()) >= 2)
    f = run.step(id_ok, _block("IDENTITY_DETAILS_INVALID", "PAN, mobile, pincode or name is missing or in the wrong format."))
    if f: return run.out(f)
    fresh = engine.preauth_decide({**preauth, "loan_id": None}, now) if preauth else {"eligible": False}
    f = run.step(bool(preauth) and not preauth.get("loan_id") and not preauth.get("claim_id"),
                 _block("OFFER_NOT_OPEN", "This offer has already been used or closed, so it cannot be accepted again."))
    if f: return run.out(f)
    f = run.step(bool(hospital) and hospital["id"] == preauth["hospital_id"],
                 _block("HOSPITAL_NOT_A_PARTNER", "The hospital on these documents is not a partner hospital, so an advance cannot be paid to it directly."))
    if f: return run.out(f)
    f = run.step(bool(verify.names_match(preauth["holder_name"], holder_name)),
                 _block("PREAUTH_HOLDER_MISMATCH", "This estimate belongs to the policy holder named on its documents. The name you gave does not match, so no advance can be made."))
    if f: return run.out(f)
    f = run.step(len(active_loans) < P["max_active_loans"], _block("TOO_MANY_LOANS", f"You already have {P['max_active_loans']} active loans through Kaamezy, which is the limit."))
    if f: return run.out(f)
    same = fresh.get("eligible") and stored.get("eligible") and all(fresh[k] == stored[k] for k in ("amount_inr", "apr_pct", "payee_name"))
    f = run.step(same, _block("AMOUNT_MISMATCH", "The advance does not match the lender's own figures, so it was refused."))
    if f: return run.out(f)
    f = run.step(otp_ok or not P["otp_always"],
                 _confirm("otp", "OTP_REQUIRED", "Money is paid to the hospital before any claim exists, so a one-time code is always required. Typing 'yes' is not enough."))
    if f: return run.out(f)
    return run.allow()
