"""Reads the TEXT of an uploaded document (policy, hospital bill, discharge summary, claim acknowledgement, claim
approval letter) into cited, typed fields.

How the document gets here (CLAUDE.md rules 5, 11, 12): the browser reads the PDF text layer, or OCRs a scan or photo,
ON THE DEVICE (web/docread.js). The file never leaves the phone; only the extracted text arrives. This module then
reads that text again, in code, because the browser is not trusted.

What this module does, and does not do:
  * Deterministic code classifies the document and finds every labelled value. Each field comes back with the page, the
    line it was read from, how it was found and a confidence. A document that cannot be classified is "unknown" and no
    field is guessed. A value below `min_field_confidence`, or a required field that is missing, is REPORTED
    (needs_review / missing_required), never silently accepted.
  * Money is `int` rupees (Decimal arithmetic, never float). Paise are rounded half up and flagged.
  * An optional AI helper (policy only) may LOCATE free-form sentences the label search cannot reach (a waiting period
    written as a paragraph). It sees only clause lines (no names, no contact or identity lines), after PII masking, and
    only if the document shows no prompt-injection pattern. Code then checks that the quoted sentence really is in the
    document and READS THE NUMBER OUT OF IT ITSELF; a quote that is not in the text, or has no readable number, is
    dropped. AI findings are always listed as needing the person's confirmation.
  * Nothing read here is an approval, a rate or a loan amount. These are INPUTS that the person confirms; the claim
    estimate, the financing amount and the decision come only from the rule engine and the guardrail (rule 2).
  * The Aadhaar number is never kept: it is blanked before anything is read. A PAN, a mobile number, an e-mail and a
    labelled account number are masked in every quote. The text itself is not stored or logged by anyone.

Owner: Member 1 · config: config/consumer_docs.yaml · prompt: prompts/consumer_doc_clauses.v1.txt
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from chaukas.integrations._common import load_config
from chaukas.llm.client import LLMUnavailable
from chaukas.llm.prompt import LLMOutputError, esc, run_validated
from chaukas.privacy.pii import mask_text

DOC_TYPES = ("policy", "hospital_bill", "discharge_summary", "claim_ack", "claim_approval")

# What each field is, so the right reader is used. (A schema, not a tunable: the words and limits are in the YAML.)
FIELD_KIND: dict[str, str] = {
    "insurer_name": "text", "policy_number": "ref", "holder_name": "name", "plan_name": "text",
    "policy_start": "date", "policy_end": "date", "continuous_since": "date", "sum_insured_inr": "money", "copay_pct": "percent",
    "deductible_inr": "money_or_nil", "room_rent": "room_rent", "initial_waiting_days": "days",
    "ped_waiting_months": "months", "specific_waiting_months": "months", "cashless_available": "yesno",
    "hospital_name": "text", "patient_name": "name", "bill_number": "ref", "uhid": "ref",
    "admission_date": "date", "discharge_date": "date", "diagnosis": "text", "procedure": "text",
    "treating_doctor": "name", "total_inr": "money_last", "advance_paid_inr": "money",
    "claim_reference": "ref", "claimed_inr": "money", "claim_date": "date", "approved_inr": "money",
    "deduction_inr": "money_or_nil", "settlement_status": "status", "expected_payment_date": "date",
    "payment_mode": "text",
}

FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "policy": ("insurer_name", "policy_number", "holder_name", "plan_name", "policy_start", "policy_end", "continuous_since", "sum_insured_inr",
               "copay_pct", "deductible_inr", "room_rent", "initial_waiting_days", "ped_waiting_months",
               "specific_waiting_months", "cashless_available"),
    "hospital_bill": ("hospital_name", "patient_name", "bill_number", "uhid", "admission_date", "discharge_date", "diagnosis",
                      "total_inr", "advance_paid_inr"),
    "discharge_summary": ("hospital_name", "patient_name", "uhid", "admission_date", "discharge_date", "diagnosis",
                          "procedure", "treating_doctor"),
    "claim_ack": ("claim_reference", "policy_number", "insurer_name", "patient_name", "hospital_name", "claimed_inr", "claim_date"),
    "claim_approval": ("claim_reference", "policy_number", "insurer_name", "patient_name", "hospital_name", "claimed_inr",
                       "approved_inr", "deduction_inr", "settlement_status", "expected_payment_date", "payment_mode"),
}
# Bulleted lists read from a document, by type (exclusions and sub-limits of a policy, the reasons on an approval letter)
LISTS_BY_TYPE = {"policy": ("exclusions", "sub_limits"), "claim_approval": ("deduction_reasons",)}


def cfg() -> dict:
    return load_config("consumer_docs")


# ------------------------------------------------------------------ text hygiene
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AADHAAR_SPACED = re.compile(r"(?<!\d)\d{4}[ \-]\d{4}[ \-]\d{4}(?!\d)")
_AADHAAR_CUE = re.compile(r"(aadhaar|aadhar|uidai|\buid\b)[^\n]{0,30}?(?<!\d)\d{12}(?!\d)", re.I)
_PAN = re.compile(r"(?<![A-Za-z0-9])(?!(?i:x{5}))[A-Za-z]{5}\d{4}[A-Za-z](?![A-Za-z0-9])")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[ \-]?)?[6-9]\d{4}[ \-]?\d{5}(?!\d)")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_ACCOUNT_CUE = re.compile(r"((?:a/c|account)\s*(?:no\.?|number|#)?\s*[:\-]?\s*)(\d{9,18})", re.I)


def normalise(text: str) -> str:
    """NFKC, no control or zero-width characters, tabs become a column gap (three spaces), CRLF to LF."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _ZERO_WIDTH.sub("", text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "   ").replace("\u00a0", " ")
    return _CTRL.sub("", text)


def redact(text: str) -> tuple[str, bool]:
    """Blank the Aadhaar number (always), mask PAN, mobile, e-mail and a labelled account number. Returns (text, aadhaar_seen)."""
    seen = bool(_AADHAAR_SPACED.search(text) or _AADHAAR_CUE.search(text))
    text = _AADHAAR_SPACED.sub("XXXX XXXX XXXX", text)
    text = _AADHAAR_CUE.sub(lambda m: re.sub(r"\d{12}", "XXXXXXXXXXXX", m.group(0)), text)
    text = _PAN.sub(lambda m: "XXXXX" + m.group(0)[5:9] + m.group(0)[9].upper(), text)
    text = _PHONE.sub(lambda m: "XXXXXX" + re.sub(r"\D", "", m.group(0))[-4:], text)
    text = _EMAIL.sub("[email]", text)
    text = _ACCOUNT_CUE.sub(lambda m: m.group(1) + "XXXXXX" + m.group(2)[-4:], text)
    return text, seen


# ------------------------------------------------------------------ small readers (all deterministic)
_CUR = r"(?:(?:rs|inr)\.?|₹|\?)"
_UNIT = {"lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "crore": 10_000_000, "crores": 10_000_000}
_MONEY = re.compile(rf"(?<![\w/.\-])(?:{_CUR}\s*)?(\d{{1,3}}(?:,\d{{2,3}})+|\d+)(?:\.(\d+))?(?:\s*/-)?(?:\s*(lakhs?|lacs?|crores?)\b)?", re.I)
_NIL = re.compile(r"^\W*(nil|none|not applicable|n/?a|zero|no)\b", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_NAMES = "|".join(["jan(?:uary)?", "feb(?:ruary)?", "mar(?:ch)?", "apr(?:il)?", "may", "jun(?:e)?", "jul(?:y)?", "aug(?:ust)?", "sep(?:t(?:ember)?)?", "oct(?:ober)?", "nov(?:ember)?", "dec(?:ember)?"])
_DATE_NUM = re.compile(r"(?<![\w/\-.])(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*((?:19|20)\d{2})(?![\w/\-.]*\d)")
_DATE_ISO = re.compile(r"(?<![\w/\-.])((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})(?!\d)")
_DATE_WORD = re.compile(rf"(?<![\w])(\d{{1,2}})(?:st|nd|rd|th)?[\s\-,.]+({_MONTH_NAMES})[a-z]*[\s\-,.]+((?:19|20)\d{{2}})(?!\d)", re.I)
_DATE_WORD_US = re.compile(rf"(?<![\w])({_MONTH_NAMES})[a-z]*[\s.]+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})(?!\d)", re.I)
_DATE_SHAPE = re.compile(r"(?<![A-Za-z0-9])[0-9OoIl|]{1,2}\s*[/\-.]\s*[0-9OoIl|]{1,2}\s*[/\-.]\s*[12][0-9OoIl|]{3}(?![A-Za-z0-9])")
_OCR_DIGIT = {"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\-_.]{3,31}")
_NAME = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,59}$")
_HONORIFIC = re.compile(r"^(mr|mrs|ms|miss|shri|smt|sri|dr|master|baby)\.?\s+", re.I)


def fix_ocr_dates(text: str) -> str:
    """OCR reads the digits of a date as letters. Fix only inside something shaped like a date."""
    return _DATE_SHAPE.sub(lambda m: re.sub(r"[OoIl|]", lambda c: _OCR_DIGIT[c.group(0)], m.group(0)), text)


def _to_int_money(whole: str, frac: str | None, unit: str | None) -> tuple[int, bool]:
    """('2,00,000', '50', None) -> (200001, True). Decimal arithmetic; paise rounded half up. Returns (rupees, rounded)."""
    d = Decimal(whole.replace(",", "") + ("." + frac if frac else ""))
    if unit:
        d *= _UNIT[unit.lower()]
    r = int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return r, d != r


def money_tokens(s: str) -> list[dict]:
    """Every amount in `s` as {value, start, end, rounded}. Dates and long reference numbers are not amounts."""
    limit = cfg()["limits"]["max_money_inr"]
    s = _DATE_NUM.sub(lambda m: " " * len(m.group(0)), s)
    out = []
    for m in _MONEY.finditer(s):
        whole, frac, unit = m.group(1), m.group(2), m.group(3)
        if len(whole.replace(",", "")) > 10:
            continue
        v, rounded = _to_int_money(whole, frac, unit)
        if 0 < v <= limit:
            out.append({"value": v, "start": m.start(), "end": m.end(), "rounded": rounded,
                        "has_cur": bool(re.match(rf"\s*{_CUR}", s[m.start():m.end()], re.I)), "grouped": "," in whole or bool(frac) or bool(unit)})
    return out


def parse_money(s: str, which: str = "first", anchored: bool = True) -> tuple[int, bool] | None:
    """The first (or last) amount in `s`. Anchored: nothing but digits and punctuation may come before the first amount,
    so 'Deductions  10,000' is not read as the value of a 'Total' label."""
    toks = money_tokens(s)
    if not toks:
        return None
    if anchored and re.search(r"[A-Za-z]", s[:toks[0]["start"]]):
        return None
    t = toks[0] if which == "first" else toks[-1]
    return t["value"], t["rounded"]


def parse_date(s: str) -> str | None:
    """First real date in `s` as ISO yyyy-mm-dd, or None. 31/02 is not a date."""
    s = fix_ocr_dates(s)
    cands: list[tuple[int, int, int, int]] = []   # (pos, y, m, d)
    for m in _DATE_NUM.finditer(s):
        cands.append((m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _DATE_ISO.finditer(s):
        cands.append((m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _DATE_WORD.finditer(s):
        cands.append((m.start(), int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
    for m in _DATE_WORD_US.finditer(s):
        cands.append((m.start(), int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
    for _, y, mo, d in sorted(cands):
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def _count(s: str, units: tuple[str, ...]) -> int | None:
    """'30 days' -> 30 when units=('day','days'). Number words one..twelve are read as well."""
    alt = "|".join(units)
    m = re.search(rf"(\d+)\s*(?:{alt})\b", s, re.I)
    if m:
        return int(m.group(1))
    m = re.search(rf"\b({'|'.join(_WORD_NUM)})\s*(?:{alt})\b", s, re.I)
    return _WORD_NUM[m.group(1).lower()] if m else None


def parse_months(s: str) -> int | None:
    y = _count(s, ("year", "years", "yr", "yrs"))
    if y is not None:
        return y * 12
    return _count(s, ("month", "months", "mth", "mths"))


def parse_days(s: str) -> int | None:
    return _count(s, ("day", "days"))


def parse_percent(s: str) -> float | None:
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|per cent)", s, re.I)
    if not m:
        return None
    v = float(m.group(1))
    return v if 0 <= v <= 100 else None


def parse_room_rent(s: str) -> dict | None:
    w = cfg()["room_rent_words"]
    low = s.lower()
    if any(p in low for p in w["no_limit"]):
        return {"kind": "no_limit"}
    pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*(?:%|percent)\s*(?:of\s+)?(?:the\s+)?(?:basic\s+)?(?:sum\s+insured|si)\b", low)
    if pct:
        return {"kind": "pct_of_sum_insured_per_day", "pct": float(pct.group(1))}
    toks = money_tokens(s)
    if toks and re.search(r"per\s*day|/\s*day|a\s*day|daily|per\s*night|each\s*day|/\s*night", low):
        return {"kind": "per_day_inr", "inr": toks[0]["value"]}
    if any(p in low for p in w["single_private"]):
        return {"kind": "single_private_room"}
    if any(p in low for p in w["shared"]):
        return {"kind": "shared_room"}
    return None


def parse_yesno(s: str) -> bool | None:
    low = s.lower()
    if re.search(r"\b(not available|not offered|no cashless|not applicable|unavailable)\b|^\W*no\b", low):
        return False
    if re.search(r"\b(yes|available|offered|applicable|at (?:all )?network|across)\b", low):
        return True
    return None


def parse_status(s: str) -> str | None:
    low = s.lower()
    for status, words in cfg()["settlement_status_words"].items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            return status
    return None


_NEXT_LABEL = re.compile(r"\s+[A-Za-z][A-Za-z/.]{1,24}\s*:(?=\s|\d|$)")


def _clean_text(v: str) -> str:
    v = re.split(r"\s{3,}|\s\|\s|\|", v.strip())[0]
    v = _NEXT_LABEL.split(v)[0]                     # "Rahul Sharma Age/Sex: 34/M" -> "Rahul Sharma"
    return v.strip(" \t:-–—=|_.,;<>~*'’‘`\"“”")        # ... and the stray marks OCR leaves at the end


def parse_value(kind: str, raw: str, anchored: bool = True) -> tuple[Any, bool] | None:
    """(value, rounded_flag) read from the text AFTER a label, or None if it does not look like this kind of value.
    `anchored=False` is for a whole sentence (the AI helper's quote), where the number is not at the start."""
    v = raw.strip(" \t:-–—=|_.")
    if not v:
        return None
    if kind in ("money", "money_last", "money_or_nil"):
        if kind == "money_or_nil" and anchored and _NIL.match(v):
            return 0, False
        return parse_money(v, "last" if kind == "money_last" else "first", anchored)
    if kind == "date":
        d = parse_date(v)
        return (d, False) if d else None
    if kind == "percent":
        if _NIL.match(v):
            return 0.0, False
        p = parse_percent(v)
        return (p, False) if p is not None else None
    if kind == "days":
        n = parse_days(v)
        return (n, False) if n is not None else None
    if kind == "months":
        n = parse_months(v)
        return (n, False) if n is not None else None
    if kind == "room_rent":
        r = parse_room_rent(v)
        return (r, False) if r else None
    if kind == "yesno":
        b = parse_yesno(v)
        return (b, False) if b is not None else None
    if kind == "status":
        st = parse_status(v)
        return (st, False) if st else None
    if kind == "ref":
        m = _REF.match(_clean_text(v))
        return (m.group(0).rstrip(".-/_"), False) if m and re.search(r"\d", m.group(0)) else None
    if kind == "name":
        t = re.split(r"\s*\d", _clean_text(v))[0].strip(" /,-")     # "Rahul Sharma 34/M" -> "Rahul Sharma"
        t = _HONORIFIC.sub("", t).strip()
        return (t, False) if _NAME.match(t) and len(t.split()) <= 6 else None
    if kind == "text":
        t = _clean_text(v)
        return (t, False) if 2 <= len(t) <= 120 and re.search(r"[A-Za-z]{2}", t) else None
    return None


# ------------------------------------------------------------------ the document as lines
class _Line:
    __slots__ = ("page", "n", "text", "ocr")

    def __init__(self, page: int, n: int, text: str, ocr: bool):
        self.page, self.n, self.text, self.ocr = page, n, text, ocr


def _column_splitter() -> re.Pattern:
    """A 'Label:' in the MIDDLE of a line starts a new column. OCR collapses the gap between two columns to one space
    ("Bill No: 9812 UHID: 4582"), so the gap is put back. Only multi-word labels and the listed one-word ones are used:
    a lone "Total:" after "Sub" is part of "Sub Total", not a new label."""
    c = cfg()
    labels = {l for ls in c["labels"].values() for l in ls if len(re.split(r"[\s\-]+", l.strip())) >= 2}
    labels |= set(c.get("column_single_labels", []))
    alt = "|".join(r"[\s\-]*".join(re.escape(w) for w in re.split(r"[\s\-]+", l.strip()) if w)
                   for l in sorted(labels, key=len, reverse=True))
    return re.compile(rf"(?<=\S)[ ]+((?:{alt})\s*:)", re.I)


_OCR_LEAD = re.compile(r"^[\s'’`‘.,:;~|_]+")      # marks OCR invents at the start of a line (bullets and hyphens are kept)
_OCR_TAIL = re.compile(r"(?:\s+(?:[iIlj]|[’‘'`\"“”<>~«»|_*]{1,3}))+\s*$")    # ... and at the end (a lone i or l, quotes, < >); a % or ) is real and stays


def _lines(pages: list[str], sources: list[str]) -> list[_Line]:
    out = []
    split = _column_splitter()
    for pi, page in enumerate(pages, 1):
        ocr = (sources[pi - 1] if pi - 1 < len(sources) else "text") == "ocr"
        for n, raw in enumerate(page.split("\n")):
            t = split.sub(lambda m: "   " + m.group(1), raw.strip())
            if ocr:
                t = _OCR_LEAD.sub("", t)
                t = _OCR_TAIL.sub("", t)
            if t:
                out.append(_Line(pi, n, t, ocr))
    return out


def _quote(t: str) -> str:
    t = re.sub(r"\s{3,}", "   ", t.strip())
    return t[:160]


def _conf(how: str, ocr: bool) -> float:
    c = cfg()["confidence"]
    base = {"same_line": c["same_line_label"], "next_line": c["next_line_label"], "heuristic": c["heuristic"],
            "letterhead": c["letterhead"]}[how]
    return round(base * (c["ocr_factor"] if ocr else 1.0), 2)


def _label_rx(label: str, loose_sep: bool) -> re.Pattern:
    """A label at the start of a line (or after a column gap or '|'), then a separator, then the value."""
    words = r"[\s\-]*".join(re.escape(w) for w in re.split(r"[\s\-]+", label.strip()) if w)
    sep = r"(?:[\s:\-–—=.|_]*)" if loose_sep else r"(?:\s*[:\-–—=|]\s*|\s{3,})"
    return re.compile(rf"(?:^|\|\s*|\s{{3,}})\s*{words}{sep}(.*)$", re.I)


def find_field(lines: list[_Line], field: str) -> dict | None:
    """The first labelled value of `field`: labels are tried most specific first, each against every line."""
    kind = FIELD_KIND[field]
    loose = kind.startswith("money") or kind in ("percent", "days", "months", "date", "room_rent", "yesno", "status")
    for label in cfg()["labels"][field]:
        rx = _label_rx(label, loose)
        rx_alone = re.compile(rf"^\s*{re.escape(label)}\s*[:\-–—=]?\s*$", re.I)
        for i, ln in enumerate(lines):
            m = rx.search(ln.text)
            if m and m.group(1).strip():
                got = parse_value(kind, m.group(1))
                if got:
                    return {"value": got[0], "page": ln.page, "quote": _quote(ln.text), "how": "same_line",
                            "confidence": _conf("same_line", ln.ocr), "rounded": got[1]}
            elif (m and not m.group(1).strip() or rx_alone.match(ln.text)) and i + 1 < len(lines):
                nxt = lines[i + 1]
                got = parse_value(kind, nxt.text)
                if got:
                    return {"value": got[0], "page": nxt.page, "quote": _quote(ln.text + " / " + nxt.text), "how": "next_line",
                            "confidence": _conf("next_line", nxt.ocr), "rounded": got[1]}
    return None


# ------------------------------------------------------------------ classification
def classify(text: str) -> tuple[str, float, dict[str, int]]:
    """(type, confidence, scores). 'unknown' when the best score is low or does not beat the second by the margin."""
    c = cfg()["classify"]
    low = re.sub(r"\s+", " ", text.lower())
    scores = {}
    for t in DOC_TYPES:
        scores[t] = sum(w * min(len(re.findall(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low)), 3) for k, w in c[t].items())
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, s1), (_, s2) = ranked[0], ranked[1]
    if s1 < c["min_score"] or s1 - s2 < c["min_margin"]:
        return "unknown", round(s1 / (s1 + s2 + 1), 2), scores
    return best, round(min(0.99, s1 / (s1 + s2 + 1)), 2), scores


# ------------------------------------------------------------------ letterhead fallbacks (heuristic, lower confidence)
def _letterhead(lines: list[_Line], must: re.Pattern, also: re.Pattern | None = None, top: int = 12) -> dict | None:
    for ln in lines[:top]:
        head = re.split(r"\s+[:|]", ln.text)[0]                       # drop what follows a stray ':' or '|'
        if ln.ocr:
            head = re.sub(r"(?:\s+\S{1,2})+$", "", head)             # ... and stray one or two letter marks at the end of the line
        if must.search(head) and (also is None or also.search(head)) and len(head) <= 90 and not re.search(r"[:|]", head):
            t = _clean_text(head)
            if 4 <= len(t) <= 90:
                return {"value": t, "page": ln.page, "quote": _quote(ln.text), "how": "letterhead",
                        "confidence": _conf("letterhead", ln.ocr), "rounded": False}
    return None


_INSURER_HEAD = re.compile(r"\binsurance\b", re.I)
_INSURER_CO = re.compile(r"\b(ltd|limited|company|co\.|general|health)\b", re.I)
_HOSPITAL_HEAD = re.compile(r"\b(hospital|hospitals|medical cent(?:re|er)|nursing home|clinic|healthcare|institute of medical)\b", re.I)


# ------------------------------------------------------------------ lists: exclusions, sub-limits, deduction reasons
_BULLET = re.compile(r"^\s*(?:[•●▪■◦·\-–—*>]+|\(?[a-z0-9]{1,2}[.)])\s+(?=\S)", re.I)


def _is_heading(text: str, names: list[str]) -> bool:
    t = re.sub(r"^\s*(?:\d+[.)]|[a-z][.)]|[IVX]+[.)])\s*", "", text.strip(), flags=re.I).strip(" :-–—").lower()
    return any(t == n for n in names) and len(t.split()) <= 6


def read_list(lines: list[_Line], section: str) -> list[dict]:
    c = cfg()
    heads = c["list_sections"][section]
    stops = [s for s in c["list_stop_headings"] if s not in heads]
    items: list[dict] = []
    for i, ln in enumerate(lines):
        if not _is_heading(ln.text, heads):
            continue
        cur: dict | None = None
        bulleted = False                        # once a list uses bullets, an unbulleted line is a wrapped line, not a new item
        for nxt in lines[i + 1:]:
            if _is_heading(nxt.text, stops) or _is_heading(nxt.text, [h for h in heads]):
                break
            txt = nxt.text
            if _BULLET.match(txt):
                bulleted = True
                if cur:
                    items.append(cur)
                cur = {"text": _BULLET.sub("", txt).strip(), "page": nxt.page}
            elif cur and (bulleted or txt[:1].islower()):     # a wrapped line continues the item above
                cur["text"] = (cur["text"] + " " + txt).strip()
            else:                               # an unbulleted line starts its own item (some layouts have no bullets)
                if cur:
                    items.append(cur)
                cur = {"text": txt, "page": nxt.page}
            if len(items) >= c["max_section_items"]:
                break
        if cur:
            items.append(cur)
        if items:
            break
    out = []
    for it in items[:c["max_section_items"]]:
        t = it["text"][:c["max_list_item_chars"]]
        out.append({"text": redact(t)[0], "page": it["page"]})
    return out


def _sub_limit(item: dict) -> dict:
    toks = [t for t in money_tokens(item["text"]) if t["has_cur"] or t["grouped"] or t["value"] >= 1000]   # "25%" is not Rs 25
    pct = parse_percent(item["text"])
    out = {"text": item["text"], "page": item["page"], "limit_inr": toks[0]["value"] if toks else None,
           "limit_pct_of_sum_insured": pct if (pct is not None and not toks) else None}
    return out


# ------------------------------------------------------------------ hospital bill line items
_LEAD_NOISE = re.compile(r"^\s*(?:\d{1,3}[.)]\s+|[•\-*]\s+|\d{1,3}\s+(?=[A-Za-z(])|[Iil|!]{1,2}\s+(?=[A-Z(]))")   # a serial number, or one OCR misread ("il" for 7)
_UNIT_WORDS = re.compile(r"(?:days?|nights?|nos?|qty|units?|pcs)", re.I)


def read_bill_items(lines: list[_Line]) -> list[dict]:
    c = cfg()
    skip = re.compile(r"^\W*(?:" + "|".join(re.escape(w) for w in c["bill_skip_words"]) + r")\b", re.I)
    label_words = {l for f in ("hospital_name", "patient_name", "bill_number", "uhid", "admission_date", "discharge_date", "diagnosis",
                               "procedure", "treating_doctor", "policy_number", "claim_reference")
                   for l in c["labels"].get(f, [])}
    label_rx = re.compile(r"^\W*(?:" + "|".join(re.escape(w) for w in sorted(label_words, key=len, reverse=True)) + r")\s*[:\-–—=|]", re.I)
    items = []
    for ln in lines:
        text = _LEAD_NOISE.sub("", ln.text)
        if skip.search(text) or label_rx.search(text):
            continue
        toks = money_tokens(text)
        # the amount is the last real amount on the line (grouped, with a currency sign, or at least 100): a bare small number
        # such as the "3" of "3 days" is not one. What follows it may only be OCR noise (one or two stray characters).
        strong = [t for t in toks if t["grouped"] or t["has_cur"] or t["value"] >= 100]
        if not strong:
            continue
        last = strong[-1]
        tail = text[last["end"]:].strip(" /-.|")
        if tail and any(len(w) > 2 for w in tail.split()):
            continue
        toks = [t for t in toks if t["end"] <= last["end"]]
        head = text[:toks[0]["start"]]
        if len(re.findall(r"[A-Za-z]", head)) < 3:      # a serial or quantity column comes first: blank every amount instead
            head = text
            for t in reversed(toks):
                head = head[:t["start"]] + " " + head[t["end"]:]
        desc = re.sub(r"\s+", " ", _UNIT_WORDS.sub(" ", head)).strip(" :-–—=|.xX@*₹")
        desc = re.sub(r"[\s\-–—=.|x*@(’'`<>~]*$", "", desc)
        if len(re.findall(r"[A-Za-z]", desc)) < 3:
            continue
        cat = _category(desc)
        if cat == "other" and not (last["grouped"] or last["has_cur"]):
            continue        # an unknown description with a bare number (an address and its pincode) is not trusted as an item
        item = {"description": desc[:100], "amount_inr": last["value"], "page": ln.page, "category": cat}
        days = _count(text, ("day", "days", "night", "nights"))
        if item["category"] == "room" and days:
            item["days"] = days
            rate = next((t["value"] for t in toks[:-1] if t["value"] * days == last["value"]), None)
            if rate is None and last["value"] % days == 0:
                rate = last["value"] // days     # the rate is not printed (or was misread): it is derived from the total, and flagged below
                item["rate_derived"] = True
            if rate:
                item["rate_inr_per_day"] = rate
        items.append(item)
    return items


def _category(desc: str) -> str:
    low = " " + desc.lower() + " "
    for cat, words in cfg()["bill_categories"].items():
        if any(w in low for w in words):
            return cat
    return "other"


# ------------------------------------------------------------------ the reader
def _sources_for(pages: list[str], sources: list[str] | None) -> list[str]:
    return [(sources[i] if sources and i < len(sources) and sources[i] in ("text", "ocr") else "text") for i in range(len(pages))]


def check_size(pages: list[str]) -> str | None:
    """A message if the upload is too large to read, else None."""
    lim = cfg()["limits"]
    if not pages or not any(p.strip() for p in pages):
        return "No text was read from this document. Try a clearer photo or PDF."
    if len(pages) > lim["max_pages"]:
        return f"That document has too many pages (the limit is {lim['max_pages']})."
    if any(len(p) > lim["max_chars_per_page"] for p in pages) or sum(len(p) for p in pages) > lim["max_total_chars"]:
        return "That document is too large to read. Please upload fewer pages."
    return None


def read_document(pages: list[str], page_sources: list[str] | None = None, use_ai: bool = True) -> dict:
    """The structured reading of a document's text. Pure function of its inputs plus (optionally) one AI call."""
    srcs = _sources_for(pages, page_sources)
    clean_pages, aadhaar_seen = [], False
    for p in pages:
        t, seen = redact(fix_ocr_dates(normalise(p)))
        clean_pages.append(t)
        aadhaar_seen = aadhaar_seen or seen
    lines = _lines(clean_pages, srcs)
    full = "\n".join(clean_pages)
    doc_type, tconf, scores = classify(full)
    warnings: list[dict] = []
    if aadhaar_seen:
        warnings.append({"code": "AADHAAR_IGNORED", "message": "An Aadhaar number was found in this document. It was ignored and is never kept."})
    fp = hashlib.sha256(re.sub(r"[^a-z0-9]", "", full.lower()).encode()).hexdigest()
    result: dict[str, Any] = {"doc_type": doc_type, "type_confidence": tconf, "scores": scores, "fields": {}, "lists": {},
                              "missing_required": [], "needs_review": [], "warnings": warnings, "pages": len(pages),
                              "chars": len(full), "text_sha256": fp, "ai": {"used": False, "reason": "not_policy"}}
    if doc_type == "unknown":
        warnings.append({"code": "DOC_NOT_RECOGNISED", "message": "This does not look like a policy, hospital bill, discharge summary or claim letter, so nothing was read from it."})
        return result

    fields: dict[str, dict] = {}
    for f in FIELDS_BY_TYPE[doc_type]:
        got = find_field(lines, f)
        if got is None and f == "insurer_name":
            got = _letterhead(lines, _INSURER_HEAD, _INSURER_CO)
        if got is None and f == "hospital_name":
            got = _letterhead(lines, _HOSPITAL_HEAD)
        if got is not None:
            if got.pop("rounded", False):
                warnings.append({"code": "PAISE_ROUNDED", "message": f"{f}: the amount has paise; it was rounded to whole rupees."})
            fields[f] = got
    if doc_type == "claim_approval" and "settlement_status" not in fields:
        low = re.sub(r"\s+", " ", full.lower())
        for status, phrases in cfg()["status_phrases"].items():
            hit = next((p for p in phrases if p in low), None)
            if hit:
                ln = next((l for l in lines if hit in re.sub(r"\s+", " ", l.text.lower())), lines[0])
                fields["settlement_status"] = {"value": status, "page": ln.page, "quote": _quote(ln.text), "how": "heuristic", "confidence": _conf("heuristic", ln.ocr)}
                break

    lists: dict[str, list] = {}
    for sec in LISTS_BY_TYPE.get(doc_type, ()):
        items = read_list(lines, sec)
        if items:
            lists[sec] = [_sub_limit(i) for i in items] if sec == "sub_limits" else items
    if doc_type == "hospital_bill":
        items = read_bill_items(lines)
        lists["line_items"] = items
        _bill_checks(fields, items, result, warnings)

    if doc_type == "policy" and use_ai:
        _ai_pass(clean_pages, lines, fields, lists, result, warnings)

    result["fields"], result["lists"] = fields, lists
    _validate(doc_type, fields, lists, warnings)
    floor = cfg()["limits"]["min_field_confidence"]
    result["missing_required"] = [f for f in cfg()["required"][doc_type] if f not in fields]
    result["needs_review"] = sorted(f for f, v in fields.items() if v["confidence"] < floor)
    return result


# ------------------------------------------------------------------ checks inside one document
def _bill_checks(fields: dict, items: list[dict], result: dict, warnings: list[dict]) -> None:
    totals: dict[str, int] = {}
    for it in items:
        totals[it["category"]] = totals.get(it["category"], 0) + it["amount_inr"]
    result["category_totals_inr"] = totals
    s = sum(totals.values())
    result["items_sum_inr"] = s
    if "total_inr" in fields and items:
        diff = fields["total_inr"]["value"] - s
        result["items_match_total"] = diff == 0
        if diff != 0:
            warnings.append({"code": "BILL_ITEMS_DO_NOT_ADD_UP",
                             "message": f"The line items add up to {s}, but the bill total reads {fields['total_inr']['value']} (difference {diff}). Taxes, discounts or a misread line can cause this; please check."})
    elif items and "total_inr" not in fields:
        warnings.append({"code": "BILL_TOTAL_NOT_FOUND", "message": "No bill total was found."})
    if any(it.get("rate_derived") for it in items):
        warnings.append({"code": "ROOM_RATE_DERIVED", "message": "The room rent per day was worked out from the total and the days; please check it."})


def _validate(doc_type: str, fields: dict, lists: dict, warnings: list[dict]) -> None:
    def v(k):
        return fields[k]["value"] if k in fields else None

    a, d = v("admission_date"), v("discharge_date")
    if a and d and a > d:
        warnings.append({"code": "DATES_OUT_OF_ORDER", "message": "The admission date is after the discharge date."})
    if v("policy_start") and v("policy_end") and v("policy_start") >= v("policy_end"):
        warnings.append({"code": "POLICY_DATES_OUT_OF_ORDER", "message": "The policy start date is not before its end date."})
    cl, ap, de = v("claimed_inr"), v("approved_inr"), v("deduction_inr")
    if cl is not None and ap is not None and ap > cl:
        warnings.append({"code": "APPROVED_ABOVE_CLAIMED", "message": "The approved amount is above the claimed amount."})
    if cl is not None and ap is not None and de is not None and cl - de != ap:
        warnings.append({"code": "APPROVAL_DOES_NOT_ADD_UP", "message": "Claimed amount minus deductions does not equal the approved amount."})
    if v("sum_insured_inr") and v("deductible_inr") and v("deductible_inr") >= v("sum_insured_inr"):
        warnings.append({"code": "DEDUCTIBLE_ABOVE_COVER", "message": "The deductible is not below the sum insured."})


# ------------------------------------------------------------------ the optional AI helper (policy clauses)
class _Clause(BaseModel):
    model_config = {"extra": "ignore"}
    kind: str = "other"
    field: str | None = None
    quote: str = ""
    page: int | None = None


class ClauseOutput(BaseModel):
    thought_process: str = ""
    clauses: list[_Clause] = []


_AI_FIELDS = {"copay_pct", "deductible_inr", "room_rent", "initial_waiting_days", "ped_waiting_months", "specific_waiting_months"}
_AI_KINDS = {"exclusion", "waiting_period", "sub_limit", "copay", "deductible", "room_rent", "other"}


def _minimised(lines: list[_Line]) -> str:
    """The clause lines only: any line labelled with personal data is dropped, then everything is PII-masked."""
    a = cfg()["ai"]
    words = re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in a["drop_line_labels"]) + r")(?![A-Za-z])", re.I)
    out, page = [], 0
    for ln in lines:
        head = ln.text.split(":", 1)[0] if ":" in ln.text[:48] else ""
        if (head and words.search(head)) or re.match(r"^\W*dear\b", ln.text, re.I):
            continue
        if ln.page != page:
            page = ln.page
            out.append(f"[page {page}]")
        out.append(mask_text(ln.text))
    return "\n".join(out)[:a["max_chars"]]


def _norm_cmp(s: str) -> str:
    return re.sub(r"[^a-z0-9%.]+", " ", s.lower()).strip()


def _ai_pass(pages: list[str], lines: list[_Line], fields: dict, lists: dict, result: dict, warnings: list[dict]) -> None:
    from chaukas.agents.intent import injection_signals

    a = cfg()["ai"]
    if not a["enabled"]:
        result["ai"] = {"used": False, "reason": "disabled"}
        return
    body = "\n".join(l.text for l in lines)
    if injection_signals(body):
        warnings.append({"code": "DOC_INJECTION_SUSPECTED", "message": "This document contains text that looks like an instruction to an AI. The AI helper was not used on it."})
        result["ai"] = {"used": False, "reason": "injection_suspected"}
        return
    try:
        out, llm = run_validated("consumer_assistant", "consumer_doc_clauses.v1.txt", {"document": esc(_minimised(lines)),
                                 "fields": ", ".join(sorted(_AI_FIELDS))}, ClauseOutput,
                                 replay_key="consumer_doc_clauses|" + result["text_sha256"])
    except (LLMUnavailable, LLMOutputError, ValidationError):
        result["ai"] = {"used": False, "reason": "unavailable"}
        return
    page_norm = [_norm_cmp(p) for p in pages]
    kept = dropped = 0
    known = {_norm_cmp(i["text"]) for sec in ("exclusions", "sub_limits") for i in lists.get(sec, [])}
    ai_items: list[dict] = []
    for cl in out.clauses[:a["max_clauses"]]:
        q = (cl.quote or "").strip()[:a["max_quote_chars"]]
        nq = _norm_cmp(q)
        pg = next((i + 1 for i, p in enumerate(page_norm) if nq and nq in p), None)
        if pg is None or len(nq) < 12 or cl.kind not in _AI_KINDS:
            dropped += 1                # the sentence is not in the document: it is dropped, never "fixed"
            continue
        if nq in known:
            continue
        shown, _ = redact(q)
        if cl.field in _AI_FIELDS:
            if cl.field in fields:
                continue                # a labelled value already exists: the model never overrides it
            got = parse_value(FIELD_KIND[cl.field], q, anchored=False)
            if got is None:
                dropped += 1            # code could not read the number out of the quoted sentence: dropped
                continue
            fields[cl.field] = {"value": got[0], "page": pg, "quote": _quote(shown), "how": "model",
                                "confidence": cfg()["confidence"]["clause_from_model"]}
            kept += 1
        else:
            ai_items.append({"kind": cl.kind, "text": shown, "page": pg, "how": "model",
                             "confidence": cfg()["confidence"]["clause_from_model"]})
            kept += 1
    if ai_items:
        lists["ai_clauses"] = ai_items
    result["ai"] = {"used": True, "kept": kept, "dropped": dropped, "model": llm.model}


# ------------------------------------------------------------------ what the person corrected
def apply_corrections(reading: dict, corrections: dict[str, Any] | None) -> dict:
    """Put the person's corrections on a reading. A corrected field is marked `how: "user"` with confidence 1.0, so every later
    step can tell a value that was READ from one that was TYPED. A correction must be a field this document type has and must
    read as that kind of value (a money field as an amount, a date as a date); otherwise ValueError names the field.
    Line items and lists cannot be corrected here: re-upload a clearer document instead."""
    if not corrections:
        return reading
    allowed = FIELDS_BY_TYPE.get(reading["doc_type"], ())
    for field, raw in corrections.items():
        if field not in allowed:
            raise ValueError(f"{field}: this document type has no such field")
        kind = FIELD_KIND[field]
        text = str(raw).strip() if not isinstance(raw, bool) else ("yes" if raw else "no")
        got = parse_value(kind, text)
        if kind.startswith("money") and isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            got = (raw, False)
        if got is None:
            raise ValueError(f"{field}: '{text[:40]}' does not look like a valid {kind.replace('_', ' ')}")
        reading["fields"][field] = {"value": got[0], "page": None, "quote": "(corrected by you)", "how": "user",
                                    "confidence": 1.0, "corrected": True}
    floor = cfg()["limits"]["min_field_confidence"]
    reading["missing_required"] = [f for f in cfg()["required"][reading["doc_type"]] if f not in reading["fields"]]
    reading["needs_review"] = sorted(f for f, v in reading["fields"].items() if v["confidence"] < floor)
    return reading
