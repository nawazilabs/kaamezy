"""Loan-form prefill from a chat message ("1 lakh ka loan, 12 mahine, income 60000").

Where the LLM is used, and where it is not (CLAUDE.md rule 11, as updated for this feature):
  LLM   reads the message and copies out the figures the person STATED, as structured fields. Nothing else.
  CODE  decides everything that matters:
          - the message is masked first (rule 5) and was already screened for prompt injection by assistant.answer;
          - every number the model returns must appear in the person's own message (lakh / k / years turned into plain
            integers by code), so the model cannot invent or adjust a figure;
          - every field is range-checked against config/consumer.yaml (amount band, tenure list, purposes, age);
            a value that fails is dropped, never clamped or "fixed";
          - a deterministic parser fills the same fields when the model is down, and fills gaps the model left.
  The result is a SUGGESTION for the form. It is not stored, it runs no credit check and creates no quote. The person
  reviews the form and taps "Check my offer"; the amount and the decision still come only from engine.lender_decide and
  the guardrail (rule 2). Fields the person did not state stay empty: nothing is defaulted on their behalf.

Owner: Member 1 · prompt: prompts/consumer_extract.v1.txt · limits: config/consumer.yaml personal_loan
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ValidationError

from chaukas.integrations._common import load_config
from chaukas.llm.client import LLMResult, LLMUnavailable
from chaukas.llm.prompt import LLMOutputError, esc, run_validated

# every field the form needs the person to state; employment and purpose are required too (no silent default)
REQUIRED = ("amount_inr", "tenure_months", "monthly_income_inr", "existing_emi_inr", "age", "employment", "purpose")

_UNIT = {"crore": 10_000_000, "cr": 10_000_000, "करोड़": 10_000_000, "करोड": 10_000_000,
         "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "लाख": 100_000,
         "thousand": 1_000, "hazaar": 1_000, "hazar": 1_000, "k": 1_000, "हज़ार": 1_000, "हजार": 1_000}
_UNITS = "|".join(sorted(_UNIT, key=len, reverse=True))
_NUM = re.compile(rf"(?<![\w.])(\d{{1,3}}(?:,\d{{2,3}})+|\d+)(?:\.(\d+))?(?:\s*({_UNITS})(?![A-Za-zऀ-ॿ]))?", re.I)

_MONTHS = re.compile(r"(\d+)\s*(?:months?|mths?|mahine|mahina|mahinon|महीने|महीना|महिने|महिन)", re.I)
_YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?|saal|sal|साल|वर्ष)(\s*(?:old|ka|ki|ke|का|की|के)\b)?", re.I)
_AGE_NUM = re.compile(r"(?:\bage\b|\bumar\b|\bumr\b|उम्र|आयु)\s*(?:is|hai|है|:|-)?\s*(\d{2})\b", re.I)
_AGE_CUE = re.compile(r"(?:\bage\b|\bumar\b|\bumr\b|उम्र|आयु)\s*(?:is|hai|है|:|-)?\s*$", re.I)

_ROLE_WORDS = {
    "emi": re.compile(r"\bemis?\b|ईएमआई|installment|\bkist\b|किस्त", re.I),
    "income": re.compile(r"income|salary|\bearn|kamai|kamat|कमाई|कमात|तनख|वेतन|\bpagar\b|पगार", re.I),
    "amount": re.compile(r"\bloan|\bneed|\bwant|borrow|chahiye|chahie|udhaar|udhar|लोन|कर्ज|उधार|चाहिए", re.I),
}
_ROLE_FIELD = {"emi": "existing_emi_inr", "income": "monthly_income_inr", "amount": "amount_inr"}
_NEAR = 30  # characters between a figure and the word that says what it is

_SELF = re.compile(r"self[\s-]?employed|\bbusiness\s?(?:man|owner)|\bshop\s?(?:owner|keeper)|\bapna (?:kaam|business)|स्वरोज़गार|स्व-रोज़गार|अपना (?:काम|बिज़नेस)", re.I)
_SALARIED = re.compile(r"\bsalaried|\bjob\b|\bnaukri|नौकरी|तनख्वाह|तनख़्वाह", re.I)
_PURPOSE_WORDS = {"Medical": r"medical|hospital|treatment|ilaaj|इलाज", "Education": r"education|school|college|fees?\b|padhai|पढ़ाई",
                  "Home": r"\bhome\b|\bhouse\b|\bghar\b|renovat|घर", "Business": r"\bbusiness\b|\bshop\b|dukaan|दुकान"}


class _Fields(BaseModel):
    model_config = {"extra": "ignore"}
    amount_inr: Any = None
    tenure_months: Any = None
    monthly_income_inr: Any = None
    existing_emi_inr: Any = None
    employment: Any = None
    purpose: Any = None
    age: Any = None


class ExtractOutput(BaseModel):
    thought_process: str = ""
    fields: _Fields


def _limits() -> dict:
    c = load_config("consumer")["personal_loan"]
    lo, hi = c["form_age"]
    return {"min_amount": c["lender"]["min_amount_inr"], "max_amount": c["lender"]["cap_inr"], "tenures": list(c["tenures_months"]),
            "purposes": list(c["purposes"]), "min_age": lo, "max_age": hi}


def _ascii_digits(s: str) -> str:
    """Devanagari / Gujarati digits -> ASCII, so the same number is found whatever script it was typed in."""
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in s)


# ------------------------------------------------------------------ deterministic reading of the message
def _figures(text: str) -> list[dict]:
    """Every figure in the text as {value, start, end}: '1 lakh' -> 100000, '60k' -> 60000, '1,00,000' -> 100000."""
    out = []
    for m in _NUM.finditer(text):
        whole, frac, unit = m.group(1).replace(",", ""), m.group(2), (m.group(3) or "").lower()
        if len(whole) >= 9 and not unit:
            continue  # a mobile, account or Aadhaar number is never a loan figure and must not take a field
        value = float(f"{whole}.{frac}") if frac else float(whole)
        value *= _UNIT.get(unit, 1)
        if abs(value - round(value)) < 1e-6:
            out.append({"value": int(round(value)), "start": m.start(), "end": m.end(), "unit": bool(unit)})
    return out


def _tenure_and_age(text: str) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """(tenure candidates, age candidates) as (months-or-years, start, end)."""
    tenures = [(int(m.group(1)), m.start(), m.end()) for m in _MONTHS.finditer(text)]
    ages = [(int(m.group(1)), m.start(1), m.end(1)) for m in _AGE_NUM.finditer(text)]
    for m in _YEARS.finditer(text):
        years = float(m.group(1))
        before = text[max(0, m.start() - 14):m.start()]
        if m.group(2) or _AGE_CUE.search(before):
            if years == int(years):
                ages.append((int(years), m.start(), m.end()))
        elif years * 12 == int(years * 12):
            tenures.append((int(years * 12), m.start(), m.end()))
    return tenures, ages


def _gap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(b_start - a_end, a_start - b_end, 0)


def grounded_values(text: str) -> set[int]:
    """Every integer a field may legitimately take, read from the person's own words."""
    text = _ascii_digits(text)
    tenures, ages = _tenure_and_age(text)
    return {f["value"] for f in _figures(text)} | {t[0] for t in tenures} | {a[0] for a in ages}


def rule_fields(text: str) -> dict:
    """The deterministic reading: a figure is assigned to a field only when a cue word sits next to it."""
    text = _ascii_digits(text)
    found: dict[str, Any] = {}
    tenures, ages = _tenure_and_age(text)
    spans = [(s, e) for _, s, e in tenures + ages]
    if tenures:
        found["tenure_months"] = tenures[0][0]
    if ages:
        found["age"] = ages[0][0]

    pairs = []  # (distance, field, figure index): a figure takes the field whose cue word is nearest, each used once
    figs = [f for f in _figures(text) if not any(_gap(f["start"], f["end"], s, e) == 0 for s, e in spans)]
    for i, f in enumerate(figs):  # figures that are the "12" of "12 months" or the "34" of "34 years old" are skipped
        for role, rx in _ROLE_WORDS.items():
            d = min((_gap(f["start"], f["end"], m.start(), m.end()) for m in rx.finditer(text)), default=None)
            if d is not None and d <= _NEAR:
                pairs.append((d, _ROLE_FIELD[role], i))
    used_fig = set()
    for d, field, i in sorted(pairs):
        if field not in found and i not in used_fig:
            found[field] = figs[i]["value"]
            used_fig.add(i)

    if re.search(r"\bno\s+emis?\b|\bemi\s+(?:nahi|nahin|zero|nil)\b|कोई\s+ईएमआई\s+नहीं|\bkoi\s+emi\s+nahi", text, re.I):
        found.setdefault("existing_emi_inr", 0)
    if _SELF.search(text):
        found["employment"] = "self_employed"
    elif _SALARIED.search(text):
        found["employment"] = "salaried"
    for purpose, rx in _PURPOSE_WORDS.items():
        if re.search(rx, text, re.I):
            found["purpose"] = purpose
            break
    return found


# ------------------------------------------------------------------ validation (the only gate between model and form)
def _int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).replace(",", "").strip())
    except ValueError:
        return None
    return int(f) if f == int(f) else None


def validate(raw: dict, allowed: set[int]) -> tuple[dict, dict[str, str]]:
    """Keep only what is stated and in range. Returns (fields, dropped {field: reason}); nothing is ever clamped."""
    lim = _limits()
    fields: dict[str, Any] = {}
    dropped: dict[str, str] = {}

    def num(name: str, ok, reason: str, must_be_stated: bool = True):
        v = _int(raw.get(name))
        if raw.get(name) is None:
            return
        if v is None or (must_be_stated and v not in allowed and not (v == 0 and name == "existing_emi_inr")):
            dropped[name] = "not_in_message"
        elif not ok(v):
            dropped[name] = reason
        else:
            fields[name] = v

    num("amount_inr", lambda v: lim["min_amount"] <= v <= lim["max_amount"], "outside_range")
    num("tenure_months", lambda v: v in lim["tenures"], "not_offered")
    num("monthly_income_inr", lambda v: 0 < v <= 100_000_000, "outside_range")
    num("existing_emi_inr", lambda v: 0 <= v <= 100_000_000, "outside_range")
    num("age", lambda v: lim["min_age"] <= v <= lim["max_age"], "outside_range")
    emp = str(raw.get("employment") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if emp in ("salaried", "self_employed"):
        fields["employment"] = emp
    purpose = str(raw.get("purpose") or "").strip().title()
    if purpose in lim["purposes"]:
        fields["purpose"] = purpose
    return fields, dropped


def _mentions_figures(text: str) -> bool:
    return bool(_NUM.search(_ascii_digits(text)))


def extract(raw_text: str, masked_text: str) -> dict | None:
    """Fields to suggest for the loan form, or None if the message states no figure at all (no model call is made then).

    `raw_text` is read in memory only, by the deterministic parser, and is never sent anywhere; the model sees
    `masked_text`. Returns {"fields", "missing", "dropped", "via", "llm"}.
    """
    if not _mentions_figures(raw_text):
        return None
    allowed = grounded_values(raw_text)
    llm: LLMResult | None = None
    model_raw: dict = {}
    try:
        out, llm = run_validated(
            "consumer_assistant", "consumer_extract.v1.txt",
            {"user_message": esc(masked_text), "purposes": ", ".join(_limits()["purposes"])}, ExtractOutput,
            replay_key=f"consumer_extract|{masked_text}")
        model_raw = out.fields.model_dump()
    except (LLMUnavailable, LLMOutputError, ValidationError):
        pass
    fields, dropped = validate(model_raw, allowed)
    rules, rules_dropped = validate(rule_fields(raw_text), allowed)
    filled_by_rules = False
    for k, v in rules.items():
        # the model's value for this field was invented (not in the message) or empty: the grounded reading may fill it;
        # if the model's value was out of range the person DID state something, so it stays dropped
        if k not in fields and dropped.get(k, "not_in_message") == "not_in_message":
            fields[k] = v
            dropped.pop(k, None)
            filled_by_rules = True
    for k, why in rules_dropped.items():
        if k not in fields:
            dropped.setdefault(k, why)
    via = "rules" if not model_raw else ("llm+rules" if filled_by_rules else "llm")
    return {"fields": fields, "missing": [k for k in REQUIRED if k not in fields], "dropped": dropped, "via": via, "llm": llm}
