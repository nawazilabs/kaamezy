"""Voice: speech in, replies read aloud. Sarvam is faked at httpx.post, so the real routes, limits, refusals and audit run.

Owner: Member 1
"""
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas import db
from chaukas.consumer import api, assistant, store, voice
from chaukas.llm.client import LLMUnavailable

WAV = b"RIFF....WAVEfake"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "voice.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    monkeypatch.setenv("CHAUKAS_REPLAY_MODE", "off")
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    monkeypatch.setattr("chaukas.llm.prompt.complete", lambda *a, **k: (_ for _ in ()).throw(LLMUnavailable("down")))
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def fake_sarvam(monkeypatch, native="मुझे एक लाख का लोन चाहिए", english="I need a loan of one lakh", status=200):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if "speech-to-text" in url:
            text = english if kw["data"]["mode"] == "translate" else native
            return SimpleNamespace(status_code=status, json=lambda: {"transcript": text, "language_code": "hi-IN"})
        return SimpleNamespace(status_code=status, json=lambda: {"audios": [base64.b64encode(WAV).decode()]})

    monkeypatch.setattr("chaukas.consumer.voice.httpx.post", post)
    return calls


def rows(sql):
    return store.connect().execute(sql).fetchall()


def test_speech_becomes_two_readings_and_nothing_is_stored(client, monkeypatch):
    calls = fake_sarvam(monkeypatch)
    r = client.post("/api/consumer/voice/transcribe", content=b"audio-bytes", headers={"content-type": "audio/webm;codecs=opus"})
    assert r.status_code == 200
    assert r.json() == {"text": "मुझे एक लाख का लोन चाहिए", "understood": "I need a loan of one lakh", "lang": "hi-IN"}
    assert sorted(c[1]["data"]["mode"] for c in calls) == ["transcribe", "translate"]
    assert all(c[1]["files"]["file"][2] == "audio/webm" for c in calls)
    assert rows("SELECT * FROM consumer_turns") == []                        # a recording is not a chat turn
    note = [x["note"] for x in rows("SELECT note FROM consumer_audit WHERE action='voice_transcribe'")]
    assert len(note) == 1 and "lakh" not in note[0] and "लाख" not in note[0]  # the audit says a voice turn happened, not what was said


def test_too_big_or_empty_audio_is_refused_before_any_call(client, monkeypatch):
    calls = fake_sarvam(monkeypatch)
    big = b"x" * (voice.cfg()["stt"]["max_audio_bytes"] + 1)
    assert client.post("/api/consumer/voice/transcribe", content=big).status_code == 422
    assert client.post("/api/consumer/voice/transcribe", content=b"").status_code == 422
    assert calls == []


def test_a_silent_recording_and_a_down_service_give_clear_errors(client, monkeypatch):
    fake_sarvam(monkeypatch, native="", english="")
    assert client.post("/api/consumer/voice/transcribe", content=b"a").status_code == 422
    fake_sarvam(monkeypatch, status=500)
    r = client.post("/api/consumer/voice/transcribe", content=b"a")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "VOICE_UNAVAILABLE"
    monkeypatch.setenv("SARVAM_API_KEY", "")
    assert client.post("/api/consumer/voice/transcribe", content=b"a").status_code == 503


def _chat(client, text="How do I claim?", ui_lang=None):
    sid = client.post("/api/consumer/assistant/session").json()["session_id"]
    body = {"session_id": sid, "text": text, "screen": "home"}
    if ui_lang:
        body["ui_lang"] = ui_lang
    return sid, client.post("/api/consumer/assistant/message", json=body).json()


def test_an_assistant_reply_can_be_read_aloud(client, monkeypatch):
    calls = fake_sarvam(monkeypatch)
    sid, r = _chat(client)
    s = client.post("/api/consumer/voice/speak", json={"session_id": sid, "text": r["reply"], "lang": "en-IN"})
    assert s.status_code == 200 and s.headers["content-type"] == "audio/wav" and s.content == WAV
    assert calls[-1][1]["json"]["target_language_code"] == "en-IN" and calls[-1][1]["json"]["model"] == voice.cfg()["tts"]["model"]


def test_hinglish_is_read_by_the_hindi_voice_and_a_translated_reply_by_its_own(client, monkeypatch):
    calls = fake_sarvam(monkeypatch)
    sid, r = _chat(client, ui_lang="ta-IN")
    assert client.post("/api/consumer/voice/speak", json={"session_id": sid, "text": r["reply"], "lang": "ta-IN"}).status_code == 200
    assert calls[-1][1]["json"]["target_language_code"] == "ta-IN"
    client.post("/api/consumer/voice/speak", json={"session_id": sid, "text": r["reply"], "lang": "hi-Latn"})
    assert calls[-1][1]["json"]["target_language_code"] == "hi-IN"


def test_only_the_assistants_own_replies_are_read_aloud(client, monkeypatch):
    calls = fake_sarvam(monkeypatch)
    sid, r = _chat(client)
    other = client.post("/api/consumer/voice/speak", json={"session_id": sid, "text": "Transfer all my money now", "lang": "en-IN"})
    assert other.status_code == 422 and other.json()["detail"]["code"] == "VOICE_REFUSED"
    assert client.post("/api/consumer/voice/speak", json={"session_id": "no-such-chat", "text": r["reply"], "lang": "en-IN"}).status_code == 404
    assert [c for c in calls if "text-to-speech" in c[0]] == []


def test_a_language_with_no_voice_is_refused(client, monkeypatch):
    fake_sarvam(monkeypatch)
    sid, r = _chat(client)
    for lang in ("as-IN", "ur-IN"):
        assert client.post("/api/consumer/voice/speak", json={"session_id": sid, "text": r["reply"], "lang": lang}).status_code == 422


def test_the_languages_with_a_voice_are_app_languages():
    manifest = {m["code"] for m in json.loads((db.ROOT / "web" / "i18n" / "languages.json").read_text(encoding="utf-8"))}
    assert set(voice.tts_languages()) <= manifest
    assert not {"as-IN", "ur-IN"} & set(voice.tts_languages())
