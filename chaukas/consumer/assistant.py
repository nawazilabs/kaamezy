"""Consumer assistant agent: answers health-insurance and loan questions in the person's language.

Where the LLM is used, and where it is not (same split as the merchant side, CHAUKAS_EXPLAINED §3):
  LLM   understands the message (topic + language) and words a short reply, using ONLY the facts in
        config/consumer_kb.yaml. It is the one place with ambiguity, so it is the one place with a model.
  CODE  everything else: masks PII before the prompt, spots prompt injection before any model call, checks the reply
        (no number or promise that is not in the knowledge base, no personal data, right language), maps the topic to
        the on-screen action (the model never chooses an action), and writes the audit row.
  The assistant cannot buy, issue, approve or send anything. Money steps stay on the screens, behind the guardrail.

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
from chaukas.consumer import store
from chaukas.core import m2_shim
from chaukas.integrations._common import load_config
from chaukas.llm.client import LLMUnavailable
from chaukas.llm.prompt import LLMOutputError, esc, run_validated, to_xml

HISTORY_WINDOW = 6
MIN_CONFIDENCE = 0.5
_LANG_KEY = {"hi-IN": "hi", "hi-Latn": "hl"}  # every other language is answered in English


def kb() -> dict:
    return load_config("consumer_kb")


def topics() -> dict:
    return kb()["topics"]


def kb_text(topic: str, lang: str) -> str:
    return topics()[topic][_LANG_KEY.get(lang, "en")]


def disclosure() -> dict:
    """AI disclosure (RBI FREE-AI) from config/disclosure.yaml, plus the 'what I can do' line from the knowledge base."""
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
    r"|\bi\s+(?:have\s+|'ve\s+)?(?:approved|issued|disbursed|sent|bought)\b|guarantee"
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


def answer(conn, user_id: str, session_id: str, text: str, screen: str = "home") -> dict:
    masked = m2_shim.mask_text(text)
    history = store.recent_turns(conn, user_id, session_id, HISTORY_WINDOW)
    facts = {"has_policy": bool(store.active_policies(conn, user_id)), "active_loans": len(store.active_loans(conn, user_id)),
             "identity_verified": store.get_kyc(conn, user_id) is not None}
    lang, topic, source, reply, conf, llm, note = _keyword_lang(text), "unknown", "keyword", "", 0.0, None, ""

    signals = injection_signals(text)
    if signals:  # decided in code, before any model sees the message
        topic, source, conf = "injection_refusal", "guard", 1.0
        reply = kb_text(topic, lang)
        store.audit(conn, user_id, "guardrail", "answer_question", "BLOCK", "SUSPECTED_INJECTION", note="signals=" + ",".join(signals))
    else:
        try:
            out, llm = run_validated(
                "consumer_assistant", "consumer_assistant.v1.txt",
                {"context": _context(screen, facts, history), "knowledge": to_xml("knowledge", {t: v["en"] for t, v in topics().items() if t not in ("unknown", "injection_refusal")}),
                 "user_question": esc(masked)}, AssistantOutput, replay_key=f"consumer_assistant|{masked}|{screen}")
            res = out.assistant_result
            lang, conf = _script_lang(text, res.lang), res.confidence
            topic = res.intent if res.intent in topics() and res.intent != "injection_refusal" else "unknown"
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

    action = topics()[topic].get("action")
    store.add_turn(conn, session_id, user_id, "user", masked, topic, None)
    store.add_turn(conn, session_id, user_id, "assistant", m2_shim.scrub(reply), topic, source)
    if source != "guard":
        store.audit(conn, user_id, "agent", "answer_question", "ALLOW", "READ_ONLY", note=f"topic={topic}; source={source}" + (f"; {note}" if note else ""))
    if action == "handoff_human":
        store.audit(conn, user_id, "agent", "request_human", "ALLOW", "HUMAN_REQUESTED", note="person asked for a human advisor")
    conn.commit()
    return {"session_id": session_id, "reply": reply, "topic": topic, "lang": lang, "source": source, "action": action,
            "confidence": conf, "latency_ms": llm.latency_ms if llm else 0, "cost_inr": llm.cost_inr if llm else 0.0,
            "model": llm.model if llm else None}
