"""Request bodies for the consumer API. Pydantic validates shape and ranges here, BEFORE the guardrail runs.

PII fields (pan, mobile, holder_name, nominee_name, pincode) are used in memory for format checks and masked
for storage. They are never logged and never sent to an LLM.

Owner: Member 1
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Relation = Literal["Self", "Spouse", "Son", "Daughter", "Father", "Mother"]


class Member(BaseModel):
    rel: Relation
    age: int = Field(ge=0, le=99)
    gender: Literal["M", "F"] = "M"
    height_cm: int | None = Field(default=None, ge=40, le=230)
    weight_kg: float | None = Field(default=None, ge=2, le=300)


class InsuranceProposal(BaseModel):
    members: list[Member] = Field(min_length=1, max_length=7)
    pincode: str = Field(pattern=r"^[0-9]{6}$")
    conditions: list[str] = Field(default_factory=list, max_length=10)
    smoker: bool = False
    surgery_last_4y: bool = False
    cover_inr: int


class PolicyPurchase(BaseModel):
    quote_id: str
    insurer_id: str
    consent_share_health: bool = False
    pan: str | None = None
    mobile: str = ""
    holder_name: str = ""
    nominee_name: str = ""
    nominee_relation: str = ""
    otp: str | None = None
    handoff_id: str | None = None


class LoanApplication(BaseModel):
    amount_inr: int = Field(ge=1)
    tenure_months: int
    purpose: str = Field(min_length=1, max_length=60)
    employment: Literal["salaried", "self_employed"]
    monthly_income_inr: int = Field(ge=0, le=100_000_000)
    existing_emi_inr: int = Field(ge=0, le=100_000_000)
    age: int = Field(ge=0, le=120)


class LoanAccept(BaseModel):
    quote_id: str
    consent_offer: bool = False
    consent_credit_check: bool = False
    pan: str | None = None
    mobile: str = ""
    holder_name: str = ""
    pincode: str = ""
    otp: str | None = None
    handoff_id: str | None = None


class ClaimFile(BaseModel):
    policy_id: str
    claimed_inr: int = Field(ge=1, le=100_000_000)


# ------------------------------------------------------------------ claim estimate: the policy terms and the bill, as FACTS
# These are the inputs the person confirmed after the document reader (or the terms of a policy bought here). They are
# stated facts, like the claimed amount of a claim: the ESTIMATE is never in a request body, the server works it out.
class RoomRentTerm(BaseModel):
    kind: Literal["no_limit", "per_day_inr", "pct_of_sum_insured_per_day", "single_private_room", "shared_room"]
    inr: int | None = Field(default=None, ge=1, le=10_000_000)
    pct: float | None = Field(default=None, gt=0, le=100)


class Clause(BaseModel):
    """A sentence of the policy and the page it was on, so a deduction can point at its clause."""
    text: str = Field(default="", max_length=300)
    page: int | None = Field(default=None, ge=1, le=60)


class SubLimitTerm(Clause):
    limit_inr: int | None = Field(default=None, ge=1, le=100_000_000)
    limit_pct_of_sum_insured: float | None = Field(default=None, gt=0, le=100)


class PolicyTerms(BaseModel):
    sum_insured_inr: int = Field(ge=1, le=100_000_000)
    room_rent: RoomRentTerm | None = None
    copay_pct: float | None = Field(default=None, ge=0, le=100)
    deductible_inr: int | None = Field(default=None, ge=0, le=100_000_000)
    initial_waiting_days: int | None = Field(default=None, ge=0, le=1500)
    ped_waiting_months: int | None = Field(default=None, ge=0, le=120)
    specific_waiting_months: int | None = Field(default=None, ge=0, le=120)
    policy_start: date | None = None                # the start of the CURRENT policy period
    waiting_waived_demo: bool = False               # DEMO ONLY: waiting periods are shown but not applied (see config claim_estimate.demo)
    continuous_since: date | None = None            # when cover with this insurer first began, if it has been renewed without a break
    exclusions: list[Clause] = Field(default_factory=list, max_length=60)
    sub_limits: list[SubLimitTerm] = Field(default_factory=list, max_length=60)
    refs: dict[str, Clause] = Field(default_factory=dict, max_length=30)    # field name -> the clause it was read from


class BillItem(BaseModel):
    description: str = Field(min_length=1, max_length=100)
    category: Literal["room", "doctor", "medicine", "surgery", "diagnostic", "other"] = "other"
    amount_inr: int = Field(ge=0, le=100_000_000)
    days: int | None = Field(default=None, ge=1, le=365)
    rate_inr_per_day: int | None = Field(default=None, ge=1, le=10_000_000)


class BillFacts(BaseModel):
    items: list[BillItem] = Field(min_length=1, max_length=200)
    total_inr: int | None = Field(default=None, ge=1, le=100_000_000)
    admission_date: date | None = None
    discharge_date: date | None = None
    diagnosis: str | None = Field(default=None, max_length=200)
    procedure: str | None = Field(default=None, max_length=200)
    hospital_network: bool | None = None


class ClaimEstimateRequest(BaseModel):
    """Either `terms` (read from the policy document, confirmed by the person) or `policy_id` (a policy bought here, whose
    terms come from config) must be given. With `policy_id` the server also knows the cover left and the policy start."""
    policy_id: str | None = None
    terms: PolicyTerms | None = None
    bill: BillFacts
    declared_pre_existing: bool | None = None      # did the person have this condition before the policy started?
    is_accident: bool = False
    already_claimed_inr: int | None = Field(default=None, ge=0, le=100_000_000)   # used only without policy_id

    @model_validator(mode="after")
    def _terms_or_policy(self):
        if self.terms is None and not self.policy_id:
            raise ValueError("give the policy terms or a policy_id")
        return self


class BridgeDecide(BaseModel):
    claim_id: str


class BridgeAccept(BaseModel):
    quote_id: str
    consent_offer: bool = False          # accept this advance and repay it from the claim payout
    consent_claim_share: bool = False    # share the claim details with the lender, for this advance only
    pan: str | None = None
    mobile: str = ""
    holder_name: str = ""
    pincode: str = ""
    otp: str | None = None


class HandoffResolve(BaseModel):
    decision: Literal["approved", "rejected"]
    staff_id: str = Field(min_length=1, max_length=40)


class LoanDraft(BaseModel):
    """The loan questions asked so far, kept IN THE BROWSER and sent back with each message (the server stores nothing).
    The server re-validates every field, so it is a suggestion for a form, never a source of money."""
    model_config = {"extra": "ignore"}
    active: bool = False  # True while the assistant is asking the loan questions
    fields: dict[str, int | str] = Field(default_factory=dict, max_length=12)
    lang: str | None = Field(default=None, max_length=10, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")  # language the questions started in


class AssistantMessage(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=500)
    screen: Literal["home", "insurance", "loan"] = "home"
    ui_lang: str | None = Field(default=None, max_length=10, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")  # the app language, e.g. ta-IN
    loan_draft: LoanDraft | None = None  # sent by the web app: asks the assistant to collect the missing loan fields; absent = old behaviour


class VoiceSpeak(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=450)
    lang: str = Field(max_length=10, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")


class DocInput(BaseModel):
    """One document of an application: its text (read on the device) and, optionally, the values the person corrected."""
    pages: list[str] = Field(min_length=1, max_length=60)
    page_sources: list[Literal["text", "ocr"]] | None = Field(default=None, max_length=60)
    corrections: dict[str, str | int] | None = Field(default=None, max_length=20)


class DocVerify(BaseModel):
    """All the documents of one application, to be cross-checked. The server reads every one of them again from its text."""
    workflow: Literal["approved_claim", "preapproval"]
    documents: list[DocInput] = Field(min_length=1, max_length=8)
    declared_pre_existing: bool | None = None
    already_claimed_inr: int | None = Field(default=None, ge=0, le=100_000_000)
    application_id: str | None = Field(default=None, pattern=r"^APP-[0-9]{8}$")
    record: bool = False            # remember these documents as used for this application (only if nothing contradicts)
    use_ai: bool = True


class BridgeFromDocuments(BaseModel):
    """The documents of an approved claim, to size an advance from VERIFIED facts. No amount, date or name is taken from here:
    the server reads every document again, cross-checks them and uses the insurer's own record."""
    documents: list[DocInput] = Field(min_length=1, max_length=8)
    declared_pre_existing: bool | None = None
    already_claimed_inr: int | None = Field(default=None, ge=0, le=100_000_000)
    use_ai: bool = True


class PreauthDecide(BaseModel):
    preauth_id: str = Field(pattern=r"^PRE-[0-9]{8}$")


class PreauthClose(BaseModel):
    preauth_id: str = Field(pattern=r"^PRE-[0-9]{8}$")
    claim_id: str = Field(pattern=r"^VCL-[0-9]{8}$")        # only a claim verified from documents can close an advance


class PipelineStep(BaseModel):
    """One stage of the claim-financing pipeline, as written to the agent log. Headlines are built from codes and whole-rupee
    figures only: never document text, names or reference numbers."""
    name: Literal["READ", "VERIFY", "ESTIMATE", "RISK", "GATE", "OFFER"]
    actor: str
    status: Literal["done", "flagged", "stopped", "skipped"]
    headline: str
    facts: dict = Field(default_factory=dict)


class PipelineRun(BaseModel):
    """What POST /pipeline/run asks for: the workflow and the documents of one application."""
    workflow: Literal["approved_claim", "preapproval"]
    documents: list[DocInput] = Field(min_length=1, max_length=8)
    declared_pre_existing: bool | None = None
    already_claimed_inr: int | None = Field(default=None, ge=0, le=100_000_000)
    use_ai: bool = True


class DocRead(BaseModel):
    """The TEXT of an uploaded document, read on the device (PDF text layer or OCR). The file itself never arrives.
    `page_sources[i]` says how page i was read: "text" (the PDF's own text) or "ocr" (a scan or photo)."""
    pages: list[str] = Field(min_length=1, max_length=60)
    page_sources: list[Literal["text", "ocr"]] | None = Field(default=None, max_length=60)
    use_ai: bool = True


# ------------------------------------------------------------------ fintech journeys (bodies only; amounts are never in them)
class DisputeCreate(BaseModel):
    txn_id: str = Field(min_length=1, max_length=20)


class PayIntentCreate(BaseModel):
    biller_id: str = Field(min_length=1, max_length=30)
    account: str = Field(default="", max_length=20)      # validated, then masked at once; the full value is never stored
    plan_amount_inr: int | None = Field(default=None, ge=1, le=100_000)  # a recharge plan; must equal a plan in config


class PayConfirm(BaseModel):
    intent_id: str = Field(min_length=1, max_length=20)
    otp: str | None = Field(default=None, max_length=12)
