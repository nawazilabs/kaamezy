"""Consumer assistant agent: answers insurance, loan, payment and bill questions in the person's language.

Where the LLM is used, and where it is not (same split as the merchant side, CHAUKAS_EXPLAINED §3):
  LLM   understands the message (topic + language) and words a short reply, using ONLY the facts in
        config/consumer_kb.yaml. It is the one place with ambiguity, so it is the one place with a model.
  CODE  everything else: masks PII before the prompt, spots prompt injection before any model call, checks the reply
        (no number or promise that is not in the knowledge base, no personal data, right language), maps the topic to
        the on-screen action (the model never chooses an action), and writes the audit row.
  The assistant cannot buy, issue, approve, pay, refund or send anything. Money steps stay on the screens, behind the guardrail.
  Payments: a message about a bill or recharge can only PRE-SELECT a biller and plan on the screen (billpay.py); the person
  types the account number and taps Pay, and the guardrail decides.

If the model is down, unsure, or its reply fails a check, the knowledge-base text is sent as it is (`source` says which).

Owner: Member 1 · prompt: prompts/consumer_assistant.v1.txt · facts: config/consumer_kb.yaml
"""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field, field_validator

from chaukas.agents.intent import _keyword_lang, injection_signals
from chaukas.agents.reply import _right_language
from chaukas.agents.schemas import normalize_lang
from chaukas.consumer import billpay, loan_intake, prefill, store
from chaukas.core import m2_shim
from chaukas.integrations._common import load_config
from chaukas.llm.client import LLMUnavailable
from chaukas.llm.prompt import LLMOutputError, esc, run_validated, to_xml

HISTORY_WINDOW = 6
CODE_ONLY_TOPICS = ("unknown", "injection_refusal", "loan_prefill")  # the model may never choose these
MIN_CONFIDENCE = 0.5
_LANG_KEY = {"hi-IN": "hi", "hi-Latn": "hl"}  # every other language is answered in English


def kb() -> dict:
    return load_config("consumer_kb")


def topics() -> dict:
    return kb()["topics"]


def kb_text(topic: str, lang: str) -> str:
    return topics()[topic][_LANG_KEY.get(lang, "en")]


def kb_i18n() -> dict:
    """The knowledge base in other languages (config/consumer_kb_i18n.yaml, built by scripts/build_i18n.py with Sarvam
    Translate; every entry passed the figures check). Missing file or entry means: no translation, use the normal path."""
    try:
        return load_config("consumer_kb_i18n").get("languages") or {}
    except FileNotFoundError:
        return {}


def ui_language(code: str | None) -> str | None:
    """The app language the person chose, if the assistant can answer in it; None means 'decide from the message'."""
    if code in ("hi-IN", "hi-Latn") or code in kb_i18n():
        return code
    return None


def _translated(key: str, ui_lang: str) -> str | None:
    """The knowledge-base sentence `key` ('topic.<name>', 'session_intro', 'disclosure') in the app language."""
    if ui_lang in _LANG_KEY:  # hi-IN / hi-Latn: the hand-written Hindi and Hinglish texts
        if key.startswith("topic."):
            return kb_text(key[6:], ui_lang)
        return kb()["session_intro"][_LANG_KEY[ui_lang]] if key == "session_intro" else None
    return (kb_i18n().get(ui_lang) or {}).get(key)


def disclosure(ui_lang: str | None = None) -> dict:
    """AI disclosure (RBI FREE-AI) from config/disclosure.yaml, plus the 'what I can do' line from the knowledge base.
    In another app language the same two sentences come from the translated knowledge base."""
    ui_lang = ui_language(ui_lang)
    if ui_lang:
        d = load_config("disclosure")["messages"].get(ui_lang, {}).get("ai_disclosure") if ui_lang in _LANG_KEY else _translated("disclosure", ui_lang)
        intro = _translated("session_intro", ui_lang)
        if d and intro:
            return {"disclosure": d, "intro": intro}
    d = load_config("disclosure")["messages"]["hi-Latn"]["ai_disclosure"]
    return {"disclosure": d, "intro": kb()["session_intro"]["en"]}


# ------------------------------------------------------------------ model output (validated before anything uses it)
class AssistantResult(BaseModel):
    intent: str
    lang: str = "en-IN"
    confidence: float = Field(ge=0.0, le=1.0)
    reply: str = Field(min_length=1, max_length=900)

    @field_validator("lang", mode="before")
    @classmethod
    def _lang(cls, v):  # a wrong language code never throws away a correct topic
        return normalize_lang(v)


class AssistantOutput(BaseModel):
    thought_process: str = Field(default="", max_length=1500)
    assistant_result: AssistantResult


# ------------------------------------------------------------------ deterministic fallback
def keyword_topic(text: str) -> str:
    for t in kb()["order"]:
        for rx in topics()[t].get("keywords") or []:
            if re.search(rx, text, re.I):
                return t
    return "unknown"


# ------------------------------------------------------------------ checks on the model's reply
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PROMISE = re.compile(
    r"(you\s+are\s+(?:now\s+)?covered|polic(?:y|ies)\s+(?:has\s+been\s+|have\s+been\s+|is\s+|was\s+)?(?:issued|activated|bought)"
    r"|loan\s+(?:has\s+been\s+|is\s+|was\s+)?(?:approved|sanctioned|disbursed|credited)"
    r"|payment\s+(?:has\s+been\s+|have\s+been\s+|is\s+|was\s+)?(?:done|successful|completed|made|processed)"
    r"|(?:refund|money)\s+(?:has\s+been\s+|have\s+been\s+|is\s+|was\s+)?(?:issued|refunded|credited|returned|sent)"
    r"|\bi\s+(?:have\s+|'ve\s+)?(?:approved|issued|disbursed|sent|bought|paid|refunded)\b|guarantee"
    r"|पॉलिसी\s*(?:जारी|एक्टिवेट)|लोन\s*(?:मंज़ूर|मंजूर|पास)|मंज़ूर\s*कर\s*दिया|jaari\s*kar\s*di|manzoor\s*kar\s*diya)", re.I)
_ASKS_SECRET = re.compile(r"\b(?:share|send|enter|give|tell|type|bhejiye|bataiye|batao)\b.{0,30}\b(?:otp|pin|password|aadhaar number|pan number|card number|account number)\b", re.I)


def _ascii_digits(s: str) -> str:
    """Devanagari / Gujarati digits -> ASCII, so a number cannot hide in another script."""
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in s)


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(_ascii_digits(s))}


def allowed_numbers() -> set[str]:
    texts = [v for t in topics().values() for k, v in t.items() if k in ("en", "hi", "hl")]
    texts += list(kb()["session_intro"].values())
    return set().union(*(_numbers(x) for x in texts))


def reply_problem(reply: str, lang: str) -> str | None:
    """None if the reply may be sent, else a short reason (kept in the audit note, never shown to the person)."""
    if _numbers(reply) - allowed_numbers():
        return "number_not_in_knowledge"
    if "₹" in reply or re.search(r"\b(?:rs\.?|inr|rupees?)\b", reply, re.I):
        return "rupee_amount"
    if _PROMISE.search(reply):
        return "promise_or_claim_of_action"
    if _ASKS_SECRET.search(reply):
        return "asks_for_secret"
    if m2_shim.detect(reply):
        return "personal_data"
    if not _right_language(reply, lang if lang in _LANG_KEY or lang == "en-IN" else "en-IN"):  # gu / mr are answered in English
        return "wrong_language"
    return None


# ------------------------------------------------------------------ the agent
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _script_lang(text: str, model_lang: str) -> str:
    """The script of the message settles Hindi vs Hinglish, whatever the model said: Latin letters are never hi-IN and
    Devanagari is never hi-Latn / en-IN (Marathi, also Devanagari, is left to the model)."""
    has_dev = bool(_DEVANAGARI.search(text))
    if model_lang == "hi-IN" and not has_dev:
        return "hi-Latn"
    if has_dev and model_lang in ("hi-Latn", "en-IN"):
        return "hi-IN"
    return model_lang


def _context(screen: str, facts: dict, history: list[dict]) -> str:
    return to_xml("assistant_context", {"screen": screen, "has_policy": facts["has_policy"], "active_loans": facts["active_loans"],
                                        "identity_verified": facts["identity_verified"],
                                        "recent_turns": [{"role": h["role"], "text": h["text_masked"]} for h in history] or None})


def _topic_text(topic: str, lang: str) -> str:
    """The knowledge-base sentence of a topic in `lang` (a translated one if the app language has it, else English)."""
    return (_translated("topic." + topic, lang) if lang != "en-IN" else None) or kb_text(topic, lang)


def _intake_reply(conn, user_id: str, session_id: str, text: str, masked: str, res: dict, draft, ui_lang: str | None,
                  detected: str, llm, suggestion: dict | None = None) -> dict:
    """The reply while the loan questions are being asked (consumer/loan_intake.py): a fixed question from the knowledge base,
    the draft handed back to the browser, and at the end the same form suggestion the prefill always made. No model words it."""
    lang = loan_intake.resolve_lang(detected, ui_language(ui_lang), draft.lang if draft else None)
    state = res["state"]
    out: dict = {"session_id": session_id, "lang": lang, "source": "kb", "confidence": 1.0, "latency_ms": llm.latency_ms if llm else 0,
                 "cost_inr": llm.cost_inr if llm else 0.0, "model": llm.model if llm else None, "action": None}
    if state == "done":
        topic, reply = "loan_prefill", _topic_text("loan_prefill", lang)
        out.update(topic=topic, action=topics()[topic]["action"], reply=reply,
                   prefill={"fields": res["fields"], "missing": [], "dropped": res.get("dropped") or {}})
        action, note = "prefill_loan_form", f"fields={','.join(res['fields'])}; missing=-; via=intake; no quote, no credit check"
    elif state == "cancelled":
        topic, reply = "loan_intake", loan_intake.line("cancelled", lang)
        out.update(topic=topic, reply=reply)
        action, note = "loan_intake_stopped", "the person stopped the loan questions; nothing was kept"
    else:
        topic, reply = "loan_intake", loan_intake.compose(res, lang)
        out.update(topic=topic, reply=reply,
                   collect={"fields": res["fields"], "asking": res["asking"], "lang": lang, "choices": loan_intake.choices(res["asking"])})
        action, note = "loan_intake_question", f"asked={res['asking']}; have={','.join(res['fields']) or '-'}"
    if suggestion and suggestion.get("llm"):  # the extractor already ran for this message: keep its cost visible
        s_llm = suggestion["llm"]
        out["cost_inr"] += s_llm.cost_inr
        out["latency_ms"] += s_llm.latency_ms
        out["model"] = out["model"] or s_llm.model
    store.add_turn(conn, session_id, user_id, "user", masked, topic, None)
    store.add_turn(conn, session_id, user_id, "assistant", m2_shim.scrub(reply), topic, "kb")
    store.audit(conn, user_id, "agent", action, "ALLOW", "FORM_PREFILL_ONLY", note=note)  # field NAMES only, never a figure
    conn.commit()
    return out


def answer(conn, user_id: str, session_id: str, text: str, screen: str = "home", ui_lang: str | None = None, loan_draft=None) -> dict:
    masked = m2_shim.mask_text(text)
    history = store.recent_turns(conn, user_id, session_id, HISTORY_WINDOW)
    facts = {"has_policy": bool(store.active_policies(conn, user_id)), "active_loans": len(store.active_loans(conn, user_id)),
             "identity_verified": store.get_kyc(conn, user_id) is not None}
    lang, topic, source, reply, conf, llm, note = _keyword_lang(text), "unknown", "keyword", "", 0.0, None, ""

    signals = injection_signals(text)
    if not signals and loan_draft is not None and loan_draft.active:  # the loan questions are being asked: read the answer, no model needed
        res = loan_intake.step(text, dict(loan_draft.fields), lang, keyword_topic(text))
        if res is not None:
            return _intake_reply(conn, user_id, session_id, text, masked, res, loan_draft, ui_lang, lang, None)
    if signals:  # decided in code, before any model sees the message
        topic, source, conf = "injection_refusal", "guard", 1.0
        reply = kb_text(topic, lang)
        store.audit(conn, user_id, "guardrail", "answer_question", "BLOCK", "SUSPECTED_INJECTION", note="signals=" + ",".join(signals))
    else:
        try:
            out, llm = run_validated(
                "consumer_assistant", "consumer_assistant.v1.txt",
                {"context": _context(screen, facts, history), "knowledge": to_xml("knowledge", {t: v["en"] for t, v in topics().items() if t not in CODE_ONLY_TOPICS}),
                 "user_question": esc(masked)}, AssistantOutput, replay_key=f"consumer_assistant|{masked}|{screen}")
            res = out.assistant_result
            lang, conf = _script_lang(text, res.lang), res.confidence
            topic = res.intent if res.intent in topics() and res.intent not in CODE_ONLY_TOPICS else "unknown"
            if topic == "unknown" or conf < MIN_CONFIDENCE:
                topic, source, reply = "unknown", "kb", kb_text("unknown", lang)
            else:
                problem = reply_problem(res.reply.strip(), lang)
                if problem:
                    source, reply, note = "kb", kb_text(topic, lang), "reply_rejected:" + problem
                else:
                    source, reply = "llm", res.reply.strip()
        except (LLMUnavailable, LLMOutputError):
            topic = keyword_topic(text)
            source, reply, conf = "keyword", kb_text(topic, lang), 0.75 if topic != "unknown" else 0.3

    suggestion = stated = None
    if topic == "loan_apply" and source != "guard":  # figures in the message -> a SUGGESTION for the form, never an action
        suggestion = stated = prefill.extract(text, masked)
        if suggestion and suggestion["fields"]:
            topic, reply, note = "loan_prefill", kb_text("loan_prefill", lang), "prefill_from=" + source
            source = "kb" if source == "llm" else source
        else:
            suggestion = None

    if loan_draft is not None and topic in ("loan_apply", "loan_prefill") and source != "guard":
        res = loan_intake.begin(stated["fields"] if stated else {}, stated["dropped"] if stated else {})  # `stated` keeps a dropped figure too
        if res["state"] == "asking":  # fields are still missing: ask for them instead of opening a half-empty form
            return _intake_reply(conn, user_id, session_id, text, masked, res, loan_draft, ui_lang, _script_lang(text, lang), llm, stated)

    # the app language wins over the language of the message: the reply is the knowledge-base sentence in that language
    # (the model only chose the topic), so a figure or a promise can never be added in translation
    chosen = ui_language(ui_lang)
    if chosen and chosen != "en-IN" and source != "guard":
        text_in_lang = _translated("topic." + topic, chosen)
        if text_in_lang:
            reply, lang, note = text_in_lang, chosen, (note + "; " if note else "") + "ui_lang=" + chosen
            source = "keyword" if source == "keyword" else "kb"
    elif chosen and source == "guard":
        reply, lang = _translated("topic." + topic, chosen) or reply, chosen

    payment = None
    if topic in ("pay_bill", "pay_recharge") and source != "guard":  # a biller / plan to PRE-SELECT on the screen, never a payment
        payment = billpay.extract_payment(text)

    action = topics()[topic].get("action")
    store.add_turn(conn, session_id, user_id, "user", masked, topic, None)
    store.add_turn(conn, session_id, user_id, "assistant", m2_shim.scrub(reply), topic, source)
    if source != "guard":
        store.audit(conn, user_id, "agent", "answer_question", "ALLOW", "READ_ONLY", note=f"topic={topic}; source={source}" + (f"; {note}" if note else ""))
    if suggestion:  # field NAMES only: the figures are income and EMI, kept out of the audit trail
        s_llm = suggestion["llm"]
        store.audit(conn, user_id, "agent", "prefill_loan_form", "ALLOW", "FORM_PREFILL_ONLY",
                    note=f"fields={','.join(suggestion['fields'])}; missing={','.join(suggestion['missing']) or '-'}; "
                         f"dropped={','.join(suggestion['dropped']) or '-'}; via={suggestion['via']}; no quote, no credit check")
        if s_llm:
            llm = s_llm if not llm else llm.model_copy(update={"cost_inr": llm.cost_inr + s_llm.cost_inr, "latency_ms": llm.latency_ms + s_llm.latency_ms})
    if payment:  # field NAMES only; the account number is never taken from the message
        store.audit(conn, user_id, "agent", "prefill_payment_form", "ALLOW", "FORM_PREFILL_ONLY",
                    note=f"fields={','.join(payment['fields'])}; missing={','.join(payment['missing'])}; "
                         f"dropped={','.join(payment['dropped']) or '-'}; nothing paid, no payment request created")
    if action == "handoff_human":
        store.audit(conn, user_id, "agent", "request_human", "ALLOW", "HUMAN_REQUESTED", note="person asked for a human advisor")
    conn.commit()
    return {"session_id": session_id, "reply": reply, "topic": topic, "lang": lang, "source": source, "action": action,
            "confidence": conf, "latency_ms": llm.latency_ms if llm else 0, "cost_inr": llm.cost_inr if llm else 0.0,
            "model": llm.model if llm else None,
            **({"prefill": {"fields": suggestion["fields"], "missing": suggestion["missing"], "dropped": suggestion["dropped"]}} if suggestion else {}),
            **({"payment_prefill": payment} if payment else {})}
