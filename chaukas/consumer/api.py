"""FastAPI router for the consumer flows, mounted at /api/consumer.

Every money step follows the same shape as the merchant side:
  1. the server finds the stored quote (the ONLY source of amounts) and the caller's own facts (KYC, policies, loans)
  2. the deterministic guardrail decides ALLOW / NEEDS_CONFIRMATION / BLOCK
  3. the decision is written to the append-only audit log
  4. only an ALLOW creates the policy or loan, and the DB refuses an execution row without an ALLOW audit row

A one-time code is checked here, on the server. A human approval is a row written by a staff call and is bound to the
exact quote and insurer, so the browser cannot approve itself.

Owner: Member 1 · see config/consumer.yaml and data/consumer_schema.sql
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from chaukas import db
from chaukas.consumer import assistant, engine, guardrail, store, voice
from chaukas.consumer.auth import current_user
from chaukas.consumer.models import AssistantMessage, HandoffResolve, InsuranceProposal, LoanAccept, LoanApplication, PolicyPurchase, VoiceSpeak
from chaukas.policy.engine import load_mock_otp
from chaukas.core import m2_shim
from chaukas.privacy.pii import mask

router = APIRouter(prefix="/api/consumer", tags=["consumer"])


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _otp_ok(supplied: str | None) -> bool:
    real = load_mock_otp()
    return bool(real) and supplied is not None and secrets.compare_digest(str(supplied), str(real))


def _public_decision(d: dict) -> dict:
    return {"verdict": d["verdict"], "kind": d.get("kind"), "reason_code": d["reason_code"], "reason": d["reason"],
            "checks": d["checks"]}


def _policy_view(p: dict) -> dict:
    return {"id": p["id"], "insurer_id": p["insurer_id"], "insurer_name": p["insurer_name"],
            "members": json.loads(p["members_json"]), "cover_inr": p["cover_inr"], "premium_inr": p["premium_inr"],
            "holder": p["holder_masked"], "pan": p["pan_masked"], "nominee": p["nominee_masked"],
            "start_date": p["start_date"], "end_date": p["end_date"], "status": p["status"]}


def _loan_view(x: dict) -> dict:
    return {k: x[k] for k in ("id", "lender_name", "amount_inr", "tenure_months", "apr_pct", "emi_inr", "total_inr",
                              "fee_inr", "fee_gst_inr", "net_inr", "purpose", "pan_masked", "first_emi_date",
                              "cooling_off_end", "status", "created_at")}


# ------------------------------------------------------------------ public config
@router.get("/config")
def get_config() -> dict:
    c = engine.cfg()
    hi, pl = c["health_insurance"], c["personal_loan"]
    return {"insurance": {"covers_inr": hi["covers_inr"], "conditions": {k: v["name"] for k, v in hi["conditions"].items()},
                          "auto_limit_inr": hi["guardrail"]["auto_limit_inr"],
                          "proposer_age": [hi["guardrail"]["min_proposer_age"], hi["guardrail"]["max_proposer_age"]]},
            "loan": {"tenures_months": pl["tenures_months"], "min_amount_inr": pl["lender"]["min_amount_inr"],
                     "cap_inr": pl["lender"]["cap_inr"], "lender_name": pl["lender"]["name"],
                     "auto_limit_inr": pl["guardrail"]["auto_limit_inr"],
                     "cooling_off_days": pl["lender"]["cooling_off_days"]}}


# ------------------------------------------------------------------ identity (DigiLocker is a mock)
@router.post("/kyc/digilocker-demo")
def kyc_digilocker_demo(user: str = Depends(current_user)) -> dict:
    """MOCK of DigiLocker: marks this user verified. Only a verified flag and 4 digits are kept, never an Aadhaar number."""
    conn = store.connect()
    ref = str(int(hashlib.sha1(user.encode()).hexdigest(), 16) % 9000 + 1000)
    k = store.set_kyc(conn, user, ref, "digilocker-demo")
    store.audit(conn, user, "user", "kyc_digilocker", "ALLOW", "KYC_VERIFIED", note="ref ••••" + ref + ", no Aadhaar number received")
    conn.commit()
    return {"verified": True, "ref_last4": k["ref_last4"], "verified_at": k["verified_at"]}


@router.get("/kyc")
def kyc_status(user: str = Depends(current_user)) -> dict:
    k = store.get_kyc(store.connect(), user)
    return {"verified": bool(k), "ref_last4": k["ref_last4"] if k else None}


# ------------------------------------------------------------------ health insurance
@router.post("/insurance/quote")
def insurance_quote(body: InsuranceProposal, user: str = Depends(current_user)) -> dict:
    try:
        qs = engine.quotes(body)
    except ValueError as e:
        raise _err(422, "BAD_PROPOSAL", str(e))
    rec = engine.recommend(qs, body)
    # Keep only the 3-digit area prefix (all the city tier needs); the full pincode is dropped, not stored.
    kept = {**body.model_dump(), "pincode": body.pincode[:3] + "000"}
    tier = engine.city_tier(body.pincode)
    conn = store.connect()
    qid = store.save_quote(conn, user, "health", kept, {"quotes": qs, "recommended": rec}, engine.params_hash(kept))
    store.audit(conn, user, "agent", "quote_plans", "ALLOW", "READ_ONLY", note=f"{len(qs)} plans priced", quote_id=qid)
    conn.commit()
    return {"quote_id": qid, "city_tier": tier, "quotes": qs, "recommended": rec}


@router.post("/insurance/purchase")
def insurance_purchase(body: PolicyPurchase, user: str = Depends(current_user)) -> dict:
    g = engine.cfg()["health_insurance"]["guardrail"]
    conn = store.connect()
    q = store.get_quote(conn, user, body.quote_id, "health")
    if not q:
        raise _err(404, "QUOTE_NOT_FOUND", "That quote does not exist. Get a new quote.")
    if store.quote_expired(q, g["quote_ttl_minutes"]):
        raise _err(410, "QUOTE_EXPIRED", "That quote has expired. Get a new quote.")
    proposal = InsuranceProposal(**q["params"])
    chosen = next((x for x in q["result"]["quotes"] if x["insurer_id"] == body.insurer_id), None)
    if not chosen:
        raise _err(422, "UNKNOWN_INSURER", "That insurer is not part of this quote.")

    key = f"policy:{user}:{q['id']}:{body.insurer_id}"
    done = store.execution_by_key(conn, key)
    if done:  # a retried request replays the old result, nothing is created twice
        pol = next(p for p in store.active_policies(conn, user) if p["id"] == done["ref_id"])
        return {"decision": {"verdict": "ALLOW", "kind": None, "reason_code": "WITHIN_POLICY",
                             "reason": "This purchase already went through.", "checks": []},
                "audit_id": done["audit_id"], "handoff": None, "policy": _policy_view(pol), "replayed": True}

    phash = engine.params_hash({"quote": q["id"], "insurer": body.insurer_id})
    d = guardrail.check_policy_purchase(
        proposal=proposal, stored_quote=q["result"], insurer_id=body.insurer_id, pan=body.pan, mobile=body.mobile,
        holder_name=body.holder_name, nominee_name=body.nominee_name, consent=body.consent_share_health,
        kyc_verified=store.get_kyc(conn, user) is not None, active_policies=store.active_policies(conn, user),
        otp_ok=_otp_ok(body.otp), human_ok=store.human_approved(conn, user, body.handoff_id, q["id"], phash))
    note = "otp rejected" if (body.otp and d["reason_code"] == "OVER_AUTO_LIMIT") else None
    audit_id = store.audit(conn, user, "guardrail", "issue_policy", d["verdict"], d["reason_code"],
                           amount=chosen["premium_inr"], note=note, quote_id=q["id"])
    handoff = None
    if d["verdict"] == guardrail.CONFIRM and d["kind"] == "human":
        names = [g_["name"] for k, g_ in engine.cfg()["health_insurance"]["conditions"].items() if k in proposal.conditions]
        brief = (f"{len(proposal.members)} member(s), cover {guardrail.inr(chosen['cover_inr'])}, conditions: "
                 f"{', '.join(names) or 'none'}, premium {guardrail.inr(chosen['premium_inr'])}.")
        h = store.open_handoff(conn, user, q["id"], phash, d["reason_code"], brief)
        store.audit(conn, user, "agent", "handoff_to_human", guardrail.CONFIRM, d["reason_code"], note=h["id"], quote_id=q["id"])
        handoff = {"id": h["id"], "status": h["status"], "brief": h["brief"]}
    policy = None
    if d["verdict"] == guardrail.ALLOW:
        pid = store.new_policy_id()
        today = datetime.now(timezone.utc).date()
        end = today.replace(year=today.year + 1) - timedelta(days=1)
        conn.execute("INSERT INTO consumer_policies(id,user_id,quote_id,insurer_id,insurer_name,member_sig,members_json,cover_inr,"
                     "premium_inr,holder_masked,pan_masked,nominee_masked,start_date,end_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, user, q["id"], body.insurer_id, chosen["name"], engine.member_signature(proposal),
                      json.dumps([f"{m.rel} {m.age}" for m in proposal.members]), chosen["cover_inr"],
                      chosen["premium_inr"], mask(body.holder_name, "name"), mask(body.pan or "", "pan"),
                      mask(body.nominee_name, "name") + f" ({body.nominee_relation or 'nominee'})",
                      today.isoformat(), end.isoformat(), db.now_iso()))
        store.record_execution(conn, user, "issue_policy", pid, audit_id, key)
        store.consent(conn, user, "share_health_data", q["id"])
        store.audit(conn, user, "agent", "issue_policy", guardrail.ALLOW, d["reason_code"], amount=chosen["premium_inr"],
                    note=pid + " (mock issue)", quote_id=q["id"])
        policy = _policy_view(dict(conn.execute("SELECT * FROM consumer_policies WHERE id=?", (pid,)).fetchone()))
    conn.commit()
    return {"decision": _public_decision(d), "audit_id": audit_id, "handoff": handoff, "policy": policy}


@router.get("/policies")
def list_policies(user: str = Depends(current_user)) -> list[dict]:
    return [_policy_view(p) for p in store.active_policies(store.connect(), user)]


# ------------------------------------------------------------------ personal loan
@router.post("/loan/decide")
def loan_decide(body: LoanApplication, user: str = Depends(current_user)) -> dict:
    try:
        d = engine.lender_decide(body)
    except ValueError as e:
        raise _err(422, "BAD_APPLICATION", str(e))
    kfs = engine.build_kfs(d) if d["eligible"] else None
    conn = store.connect()
    qid = store.save_quote(conn, user, "loan", body.model_dump(), {"decision": d, "kfs": kfs}, engine.params_hash(body.model_dump()))
    store.audit(conn, user, "lender", "credit_decision", guardrail.ALLOW if d["eligible"] else guardrail.BLOCK,
                "ELIGIBLE" if d["eligible"] else d["reasons"][0], amount=d.get("amount_inr"),
                note="uses age, income and EMIs only", quote_id=qid)
    conn.commit()
    return {"quote_id": qid, "decision": d, "kfs": kfs}


@router.post("/loan/quotes/{quote_id}/kfs-shown")
def loan_kfs_shown(quote_id: str, user: str = Depends(current_user)) -> dict:
    """The screen calls this when the Key Facts Statement is actually on display. The server records the time."""
    conn = store.connect()
    q = store.get_quote(conn, user, quote_id, "loan")
    if not q or not q["result"]["decision"].get("eligible"):
        raise _err(404, "QUOTE_NOT_FOUND", "There is no offer to show Key Facts for.")
    store.mark_kfs_shown(conn, user, quote_id)
    store.audit(conn, user, "agent", "kfs_shown", guardrail.ALLOW, "KFS_SHOWN", amount=q["result"]["decision"]["amount_inr"], quote_id=quote_id)
    conn.commit()
    return {"kfs_shown": True}


@router.post("/loan/accept")
def loan_accept(body: LoanAccept, user: str = Depends(current_user)) -> dict:
    g = engine.cfg()["personal_loan"]["guardrail"]
    conn = store.connect()
    q = store.get_quote(conn, user, body.quote_id, "loan")
    if not q:
        raise _err(404, "QUOTE_NOT_FOUND", "That offer does not exist. Check your offer again.")
    if store.quote_expired(q, g["quote_ttl_minutes"]):
        raise _err(410, "QUOTE_EXPIRED", "That offer has expired. Check your offer again.")
    stored = q["result"]["decision"]
    if not stored.get("eligible"):
        raise _err(409, "NOT_ELIGIBLE", "The lender did not make an offer on this quote.")
    app = LoanApplication(**q["params"])

    key = f"loan:{user}:{q['id']}"
    done = store.execution_by_key(conn, key)
    if done:
        ln = next(x for x in store.active_loans(conn, user) if x["id"] == done["ref_id"])
        return {"decision": {"verdict": "ALLOW", "kind": None, "reason_code": "WITHIN_POLICY",
                             "reason": "This loan was already created.", "checks": []},
                "audit_id": done["audit_id"], "handoff": None, "loan": _loan_view(ln), "replayed": True}

    phash = engine.params_hash({"quote": q["id"]})
    d = guardrail.check_loan_accept(
        application=app, stored_quote=q["result"], pan=body.pan, mobile=body.mobile, holder_name=body.holder_name,
        pincode=body.pincode, consent_offer=body.consent_offer, consent_credit=body.consent_credit_check,
        kyc_verified=store.get_kyc(conn, user) is not None, kfs_shown=q["kfs_shown_at"] is not None,
        active_loans=store.active_loans(conn, user), otp_ok=_otp_ok(body.otp),
        human_ok=store.human_approved(conn, user, body.handoff_id, q["id"], phash))
    note = "otp rejected" if (body.otp and d["reason_code"] == "OVER_AUTO_LIMIT") else None
    audit_id = store.audit(conn, user, "guardrail", "disburse_loan", d["verdict"], d["reason_code"],
                           amount=stored["amount_inr"], note=note, quote_id=q["id"])
    handoff = None
    if d["verdict"] == guardrail.CONFIRM and d["kind"] == "human":
        brief = (f"self-employed, monthly income {guardrail.inr(app.monthly_income_inr)}, requested "
                 f"{guardrail.inr(stored['amount_inr'])} over {stored['tenure_months']} months. Income proof is needed.")
        h = store.open_handoff(conn, user, q["id"], phash, d["reason_code"], brief)
        store.audit(conn, user, "agent", "handoff_to_human", guardrail.CONFIRM, d["reason_code"], note=h["id"], quote_id=q["id"])
        handoff = {"id": h["id"], "status": h["status"], "brief": h["brief"]}
    loan = None
    if d["verdict"] == guardrail.ALLOW:
        lid = store.new_loan_id()
        L = engine.cfg()["personal_loan"]["lender"]
        today = datetime.now(timezone.utc).date()
        now = db.now_iso()
        conn.execute("INSERT INTO consumer_loans(id,user_id,quote_id,lender_name,amount_inr,tenure_months,apr_pct,emi_inr,total_inr,fee_inr,"
                     "fee_gst_inr,net_inr,purpose,employment,pan_masked,kfs_shown_at,consent_at,first_emi_date,cooling_off_end,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (lid, user, q["id"], L["name"], stored["amount_inr"], stored["tenure_months"], stored["apr_pct"],
                      stored["emi_inr"], stored["total_inr"], stored["fee_inr"], stored["fee_gst_inr"], stored["net_inr"],
                      app.purpose, app.employment, mask(body.pan or "", "pan"), q["kfs_shown_at"], now,
                      (today + timedelta(days=30)).isoformat(), (today + timedelta(days=L["cooling_off_days"])).isoformat(), now))
        store.record_execution(conn, user, "disburse_loan", lid, audit_id, key)
        store.consent(conn, user, "loan_offer", q["id"])
        store.consent(conn, user, "credit_check", q["id"])
        store.audit(conn, user, "agent", "disburse_loan", guardrail.ALLOW, d["reason_code"], amount=stored["amount_inr"],
                    note=lid + " (mock disbursal)", quote_id=q["id"])
        loan = _loan_view(dict(conn.execute("SELECT * FROM consumer_loans WHERE id=?", (lid,)).fetchone()))
    conn.commit()
    return {"decision": _public_decision(d), "audit_id": audit_id, "handoff": handoff, "loan": loan}


@router.get("/loans")
def list_loans(user: str = Depends(current_user)) -> list[dict]:
    return [_loan_view(x) for x in store.active_loans(store.connect(), user)]


# ------------------------------------------------------------------ human review + activity
@router.get("/handoffs/{handoff_id}")
def get_handoff(handoff_id: str, user: str = Depends(current_user)) -> dict:
    h = store.get_handoff(store.connect(), handoff_id)
    if not h or h["user_id"] != user:
        raise _err(404, "HANDOFF_NOT_FOUND", "No such review.")
    return {"id": h["id"], "status": h["status"], "reason_code": h["reason_code"], "brief": h["brief"]}


@router.post("/handoffs/{handoff_id}/resolve")
def resolve_handoff(handoff_id: str, body: HandoffResolve, x_staff_key: str | None = Header(default=None)) -> dict:
    """Staff decision. In the demo the key is 'demo-staff' unless CHAUKAS_STAFF_KEY is set; a real deployment must set it."""
    real = os.environ.get("CHAUKAS_STAFF_KEY", "demo-staff")
    if not x_staff_key or not secrets.compare_digest(x_staff_key, real):
        raise _err(403, "STAFF_ONLY", "Only agent-desk staff can resolve a review.")
    conn = store.connect()
    h = store.resolve_handoff(conn, handoff_id, body.decision, body.staff_id)
    if not h:
        raise _err(409, "ALREADY_RESOLVED", "That review is missing or already resolved.")
    store.audit(conn, h["user_id"], "human", "underwriter_review", guardrail.ALLOW if body.decision == "approved" else guardrail.BLOCK,
                "HUMAN_APPROVED" if body.decision == "approved" else "HUMAN_REJECTED", note=handoff_id, quote_id=h["quote_id"])
    conn.commit()
    return {"id": h["id"], "status": h["status"]}


@router.get("/activity")
def activity(limit: int = 200, user: str = Depends(current_user)) -> list[dict]:
    return store.activity(store.connect(), user, max(1, min(limit, 500)))


# ------------------------------------------------------------------ assistant (LLM agent with a deterministic fallback)
MAX_ASSISTANT_TURNS = 60


@router.post("/assistant/session")
def assistant_session(lang: str | None = None, user: str = Depends(current_user)) -> dict:
    conn = store.connect()
    sid = store.create_chat_session(conn, user)
    return {"session_id": sid, **assistant.disclosure(lang)}


@router.post("/assistant/message")
def assistant_message(body: AssistantMessage, user: str = Depends(current_user)) -> dict:
    conn = store.connect()
    if not store.get_chat_session(conn, user, body.session_id):
        raise _err(404, "SESSION_NOT_FOUND", "That chat does not exist. Start a new chat.")
    if store.turn_count(conn, body.session_id) >= MAX_ASSISTANT_TURNS:
        raise _err(429, "CHAT_TOO_LONG", "This chat is very long. Please start a new chat.")
    return assistant.answer(conn, user, body.session_id, body.text.strip(), body.screen, body.ui_lang)


# ------------------------------------------------------------------ voice (speech in, replies read aloud)
@router.get("/voice/config")
def voice_config(user: str = Depends(current_user)) -> dict:
    return {"tts_languages": voice.tts_languages(), "language_aliases": voice.cfg()["tts"].get("language_aliases") or {},
            "max_seconds": voice.cfg()["stt"]["max_seconds"]}


@router.post("/voice/transcribe")
async def voice_transcribe(request: Request, user: str = Depends(current_user)) -> dict:
    """Raw audio in the body (Content-Type: the recorder's type). Returns {text, understood, lang}. Nothing is stored."""
    audio = await request.body()
    ctype = (request.headers.get("content-type") or "audio/webm").split(";")[0].strip()
    try:
        out = await run_in_threadpool(voice.transcribe, audio, ctype)
    except voice.VoiceRefused as e:
        raise _err(422, "VOICE_REFUSED", str(e))
    except voice.VoiceUnavailable as e:
        raise _err(503, "VOICE_UNAVAILABLE", str(e))
    conn = store.connect()
    store.audit(conn, user, "agent", "voice_transcribe", "ALLOW", "READ_ONLY", note=f"lang={out['lang']}; bytes={len(audio)}; words never logged")
    conn.commit()
    return out


@router.post("/voice/speak")
async def voice_speak(body: VoiceSpeak, user: str = Depends(current_user)) -> Response:
    """Reads aloud one of the assistant's own recent replies in this chat. Any other text is refused."""
    conn = store.connect()
    if not store.get_chat_session(conn, user, body.session_id):
        raise _err(404, "SESSION_NOT_FOUND", "That chat does not exist.")
    replies = {t["text_masked"] for t in store.recent_turns(conn, user, body.session_id, 40) if t["role"] == "assistant"}
    if m2_shim.scrub(body.text) not in replies:
        raise _err(422, "VOICE_REFUSED", "Only the assistant's own replies can be read aloud.")
    try:
        wav = await run_in_threadpool(voice.speak, body.text, body.lang)
    except voice.VoiceRefused as e:
        raise _err(422, "VOICE_REFUSED", str(e))
    except voice.VoiceUnavailable as e:
        raise _err(503, "VOICE_UNAVAILABLE", str(e))
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})
