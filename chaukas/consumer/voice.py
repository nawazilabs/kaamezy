"""Voice for the consumer app: speech to text in, replies read aloud out. Both are Sarvam (Saaras, Bulbul).

What goes where (CLAUDE.md rules 5, 11, 12):
  * `transcribe` sends the recording to Sarvam once and returns text. The audio is never written to disk, the DB or a
    log, and the transcript is never logged or put in the audit trail (the audit row has the language and the length).
    The returned text is then sent by the browser through the normal assistant path, where PII is masked before any
    model sees it and prompt injection is screened first. Voice adds no new way to reach a model or the guardrail.
  * Two readings are returned: `text`, in the language spoken (to show the person), and `understood`, an English
    reading of the same speech (what the assistant and the loan-form prefill work from). The English reading is only
    ever used the way typed text is used: as a message. Numbers in it are re-checked against the message by code
    (prefill.py), not trusted.
  * `speak` reads a reply aloud. It refuses any text that is not one of the assistant's own recent replies in that
    chat, so it cannot be used as a free text-to-speech service or to read out anything else.
  Voice can fill a form and open a screen. It cannot consent, enter an OTP, verify identity or approve anything: those
  stay taps on the screen, checked on the server.

Owner: Member 1 · config: config/consumer_voice.yaml
"""
from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
from dotenv import load_dotenv

from chaukas.db import ROOT
from chaukas.integrations._common import load_config

load_dotenv(ROOT / ".env")


class VoiceUnavailable(Exception):
    """Sarvam could not be reached or refused the request. The message is safe to show."""


class VoiceRefused(Exception):
    """The request itself is not allowed (too big, wrong language, not an assistant reply)."""


def cfg() -> dict:
    return load_config("consumer_voice")


def tts_languages() -> list[str]:
    return list(cfg()["tts"]["languages"])


def tts_language(code: str | None) -> str | None:
    """The Bulbul language code for an app language, or None if there is no voice for it."""
    c = cfg()["tts"]
    code = (c.get("language_aliases") or {}).get(code, code)
    return code if code in c["languages"] else None


def _headers() -> dict:
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        raise VoiceUnavailable("Voice is not set up on this server.")
    return {"api-subscription-key": key}


def _stt(audio: bytes, content_type: str, mode: str) -> dict:
    c = cfg()["stt"]
    try:
        r = httpx.post(c["url"], headers=_headers(), timeout=c["timeout_s"],
                       files={"file": ("voice", audio, content_type or "audio/webm")},
                       data={"model": c["model"], "mode": mode, "language_code": "unknown"})
    except httpx.HTTPError:
        raise VoiceUnavailable("The voice service could not be reached.")
    if r.status_code != 200:
        raise VoiceUnavailable("The voice service could not read that recording.")
    j = r.json()
    return {"text": (j.get("transcript") or "").strip(), "lang": j.get("language_code") or "unknown"}


def transcribe(audio: bytes, content_type: str) -> dict:
    """{text, understood, lang}. `text` is what was said, in that language; `understood` is the English reading."""
    limit = cfg()["stt"]["max_audio_bytes"]
    if not audio:
        raise VoiceRefused("No audio was received.")
    if len(audio) > limit:
        raise VoiceRefused("That recording is too long. Please keep it short.")
    with ThreadPoolExecutor(max_workers=2) as pool:  # the two readings are independent: run them together
        native = pool.submit(_stt, audio, content_type, "transcribe")
        english = pool.submit(_stt, audio, content_type, "translate")
        n, e = native.result(), english.result()
    if not n["text"] and not e["text"]:
        raise VoiceRefused("We could not hear anything. Please try again.")
    return {"text": n["text"] or e["text"], "understood": e["text"] or n["text"], "lang": n["lang"] if n["lang"] != "unknown" else e["lang"]}


def speak(text: str, lang: str) -> bytes:
    """WAV bytes of `text` in `lang`. The caller has already checked that `text` is one of the assistant's own replies."""
    c = cfg()["tts"]
    target = tts_language(lang)
    if not target:
        raise VoiceRefused("There is no voice for this language yet.")
    text = text.strip()
    if not text or len(text) > c["max_chars"]:
        raise VoiceRefused("That reply is too long to read aloud.")
    try:
        r = httpx.post(c["url"], headers=_headers(), timeout=c["timeout_s"],
                       json={"text": text, "target_language_code": target, "model": c["model"]})
    except httpx.HTTPError:
        raise VoiceUnavailable("The voice service could not be reached.")
    if r.status_code != 200:
        raise VoiceUnavailable("The voice service could not read that out.")
    audios = r.json().get("audios") or []
    if not audios:
        raise VoiceUnavailable("The voice service returned no audio.")
    return base64.b64decode(audios[0])
