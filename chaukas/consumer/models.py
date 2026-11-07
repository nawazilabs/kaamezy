"""Request bodies for the consumer API. Pydantic validates shape and ranges here, BEFORE the guardrail runs.

PII fields (pan, mobile, holder_name, nominee_name, pincode) are used in memory for format checks and masked
for storage. They are never logged and never sent to an LLM.

Owner: Member 1
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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


class HandoffResolve(BaseModel):
    decision: Literal["approved", "rejected"]
    staff_id: str = Field(min_length=1, max_length=40)


class AssistantMessage(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=500)
    screen: Literal["home", "insurance", "loan"] = "home"
    ui_lang: str | None = Field(default=None, max_length=10, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")  # the app language, e.g. ta-IN


class VoiceSpeak(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=450)
    lang: str = Field(max_length=10, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")
