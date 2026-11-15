"""Loan intake: the assistant asks, one question at a time, for the form fields the person did not say.

Why: "I need a loan" on its own used to open an empty form, and a message with some figures left the rest to be typed.
Now the chat keeps going until the loan form is complete ("How many months?", "What is your monthly income?", ...), and then
fills the form exactly as `prefill.py` always did.

Where the LLM is used: NOWHERE in this module. The questions are fixed sentences from config/consumer_kb.yaml
(`loan_intake`), the next question is whichever required field is still missing (code), and an answer is read by the same
deterministic parser as the prefill, so a figure is accepted only if the person said it, and only if it is in range
(`prefill.validate`: out of range is dropped, never clamped). Only the fields the lender actually uses are asked: never a
name, PAN or mobile number (those belong to the KYC step).

Nothing is stored. The draft lives in the browser and comes back with every message; the server re-validates all of it, so
a tampered draft can at worst put a wrong figure into a form the person is about to read and confirm themselves. The result
is a SUGGESTION: no quote and no credit check runs here, and the person still taps "Check my offer" (CLAUDE.md rules 2 and 11).

Language: the app language wins when it is Hindi / Hinglish. With English chosen, the questions follow the language the person
actually writes or speaks (Hinglish in, Hinglish out), and that choice is kept for the rest of the questions.
"""
from __future__ import annotations

import re
from typing import Any

from chaukas.consumer import prefill
from chaukas.integrations._common import load_config

ORDER = prefill.REQUIRED  # amount, tenure, income, EMI, age, employment, purpose: the order the questions are asked in
_COL = {"hi-IN": "hi", "hi-Latn": "hl"}
_NO_EMI = re.compile(r"\b(?:none|nil|zero|no|nope|nahi|nahin|nhi)\b|koi\s+nahi|कोई\s+नहीं|^\s*नहीं\s*$|ज़ीरो|जीरो|शून्य", re.I)
_OTHER = re.compile(r"\bother\b|\bkuch\s+aur\b|कुछ\s+और|अन्य", re.I)


def _cfg() -> dict:
    from chaukas.consumer import assistant
    return assistant.kb()["loan_intake"]


def inr(n: int) -> str:
    return "₹" + _indian(int(n))


def _indian(n: int) -> str:
    s = str(n)
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def resolve_lang(detected: str, chosen: str | None, previous: str | None) -> str:
    """The app language (Hindi / Hinglish / a translated one) wins; with English chosen, a message clearly in Hindi or Hinglish
    switches the questions to it, and a bare answer such as "24" keeps whatever was chosen earlier."""
    if chosen:
        return chosen
    if detected != "en-IN":
        return detected
    return previous or "en-IN"


def _pick(block: dict, lang: str) -> str:
    if lang in _COL:
        return block[_COL[lang]]
    return block["en"]


def _fill(text: str, field: str | None = None) -> str:
    lim = prefill._limits()
    options = lim["purposes"] if field == "purpose" else lim["tenures"]
    return (text.replace("{min}", inr(lim["min_amount"])).replace("{max}", inr(lim["max_amount"]))
            .replace("{options}", ", ".join(str(o) for o in options)))


def question(field: str, lang: str) -> str:
    """The fixed question for a field, in the person's language (a translated one if the app language has it, else English)."""
    from chaukas.consumer import assistant
    if lang not in _COL and lang != "en-IN":
        other = assistant._translated("loan_intake.question." + field, lang)
        if other:
            return _fill(other, field)
    return _fill(_pick(_cfg()["questions"][field], lang), field)


def line(key: str, lang: str) -> str:
    """A fixed sentence of the intake (start, retry, cancelled) in the person's language."""
    from chaukas.consumer import assistant
    if lang not in _COL and lang != "en-IN":
        other = assistant._translated("loan_intake." + key, lang)
        if other:
            return other
    return _pick(_cfg()[key], lang)


def choices(field: str) -> list[str] | None:
    """Tap-to-answer labels for the fields with a short fixed list. The label is sent back as the person's message and read
    by the same parser as typed or spoken words."""
    lim = prefill._limits()
    if field == "tenure_months":
        return [f"{t} months" for t in lim["tenures"]]
    if field == "employment":
        return ["Salaried", "Self-employed"]
    if field == "purpose":
        return list(lim["purposes"])
    return None


def missing(fields: dict) -> list[str]:
    return [k for k in ORDER if k not in fields]


def wants_to_stop(text: str) -> bool:
    return any(re.search(rx, text, re.I) for rx in _cfg()["cancel"])


# ------------------------------------------------------------------ reading one answer
def clean_draft(raw: dict[str, Any]) -> dict[str, Any]:
    """The draft the browser sent, checked again: unknown keys dropped, every value range-checked (nothing is trusted)."""
    ints = {int(v) for v in raw.values() if isinstance(v, int) and not isinstance(v, bool)}
    fields, _ = prefill.validate({k: v for k, v in raw.items() if k in ORDER}, ints)
    return fields


def read_answer(text: str, asking: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """(fields read from this message, dropped {field: reason}). Fields with a cue word in the message ("income 60000") are
    read wherever they are; a bare figure ("24") is read only for the field that was just asked."""
    t = prefill._ascii_digits(prefill.spoken_numbers(text))
    allowed = prefill.grounded_values(t)
    raw = prefill.rule_fields(t)
    if asking and asking not in raw:
        figs = [f["value"] for f in prefill._figures(t)]
        lim = prefill._limits()
        if asking == "existing_emi_inr" and _NO_EMI.search(t) and not figs:
            raw[asking] = 0
        elif asking == "purpose" and _OTHER.search(t):
            raw[asking] = "Other"
        elif figs and asking in ("amount_inr", "monthly_income_inr", "existing_emi_inr", "age"):
            raw[asking] = figs[0]
        elif figs and asking == "tenure_months":
            v = figs[0]
            if v in lim["tenures"]:
                raw[asking] = v
            elif v * 12 in lim["tenures"]:  # "2" or "two" on its own means years here
                raw[asking] = v * 12
                allowed = allowed | {v * 12}
            else:
                raw[asking] = v  # not an offered period: validate() drops it with a reason
    return prefill.validate(raw, allowed)


# ------------------------------------------------------------------ one step of the conversation
def step(text: str, draft_fields: dict, lang: str, topic_hint: str) -> dict | None:
    """Handle the person's reply while questions are being asked.

    Returns {"state": "asking" | "done" | "cancelled", "fields", "asking", "reply_key", "dropped"} or None when the person has
    moved on to another subject (`topic_hint` is the keyword topic of the message): the normal assistant then answers it and
    the draft is let go.
    """
    fields = clean_draft(draft_fields)
    if wants_to_stop(text):
        return {"state": "cancelled", "fields": fields, "asking": None, "dropped": {}, "retry": False}
    asking = (missing(fields) or [None])[0]
    new, dropped = read_answer(text, asking)
    if not new and not dropped:
        if topic_hint not in ("unknown", "loan_apply", "loan_prefill"):
            return None  # a different subject: let the normal assistant answer it
    fields.update(new)
    left = missing(fields)
    if not left:
        return {"state": "done", "fields": fields, "asking": None, "dropped": dropped, "retry": False}
    return {"state": "asking", "fields": fields, "asking": left[0], "dropped": dropped, "retry": not new}


def begin(fields: dict, dropped: dict | None = None) -> dict:
    """The first reply once the person asked for a loan: `fields` are any figures they already stated, `dropped` the ones
    that were out of range (the person is asked for that one again)."""
    dropped = dropped or {}
    left = missing(fields)
    if not left:
        return {"state": "done", "fields": fields, "asking": None, "dropped": dropped, "retry": False}
    return {"state": "asking", "fields": fields, "asking": left[0], "dropped": dropped, "retry": left[0] in dropped, "intro": True}


def compose(res: dict, lang: str) -> str:
    """The reply for a step: [intro or retry line] + the next question."""
    if res["state"] == "cancelled":
        return line("cancelled", lang)
    parts = []
    if res.get("intro"):
        parts.append(line("start", lang))
    if res.get("retry"):
        parts.append(line("retry", lang))
    parts.append(question(res["asking"], lang))
    return " ".join(parts)
