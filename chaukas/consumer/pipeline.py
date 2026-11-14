"""The claim-financing pipeline: ONE typed, plain-Python path from documents to an offer, with an agent log of every stage.

Why this exists (CLAUDE.md rules 1, 2, 3, 5, 6, 8): Workflow A (an advance before the claim) and Workflow B (an advance against an
approved claim) used to repeat the same steps inside two endpoints. They now share this module, so there is one place where the
documents are read, cross-checked, estimated, risk-checked and gated, and one log that shows it. No LangChain or LangGraph: it is
a list of stages run in order, each returning a typed `PipelineStep`.

  READ -> VERIFY -> ESTIMATE -> RISK -> GATE -> OFFER

  * READ / VERIFY / ESTIMATE / RISK are the existing deterministic modules (docs, verify, estimate, risk). Nothing here calls a model
    except the optional clause finder inside docs, which only reads; it can never decide.
  * GATE is the deterministic eligibility decision for the workflow (the reasons are codes). It is the only thing that can let an
    offer be prepared, and an offer is only a stored row: money still needs the guardrail (check_bridge_accept /
    check_preauth_accept) and the person's own consent, KYC and one-time code.
  * OFFER writes the verified claim (B) or the pre-authorisation offer (A) when the gate passed, nothing otherwise.

The AGENT LOG (`consumer_pipeline_runs`, append-only) keeps, per run, who did what and the outcome: stage names, statuses, headlines,
codes and whole-rupee figures. It never holds document text, names, policy or claim numbers (the headlines are written from codes and
numbers, and tests/test_pipeline.py checks that no value read from a document appears in a log). A person can read it to see exactly
why an offer was or was not made.

Owner: Member 1 · config: config/consumer.yaml (`bridge`, `preauth`), config/consumer_docs.yaml (`verify`), config/risk.yaml
"""
from __future__ import annotations

from datetime import datetime, timezone

from chaukas.consumer import engine, guardrail, hospital_registry, insurer_registry, store, verify
from chaukas.consumer.models import PipelineStep

STAGES = ("READ", "VERIFY", "ESTIMATE", "RISK", "GATE", "OFFER")
ACTORS = {"READ": "document reader", "VERIFY": "verifier", "ESTIMATE": "estimator", "RISK": "risk check", "GATE": "eligibility rule", "OFFER": "offer"}
WORKFLOW_OF = {"approved_claim": "B", "preapproval": "A"}


def _step(name: str, status: str, headline: str, **facts) -> PipelineStep:
    return PipelineStep(name=name, actor=ACTORS[name], status=status, headline=headline, facts=facts)


def _inr(n: int) -> str:
    return guardrail.inr(n)


# ------------------------------------------------------------------ the gates (deterministic, no model)
def _gate_bridge(conn, user: str, app_id: str, report: dict, fps: list[dict]) -> tuple[dict | None, list[str]]:
    """Workflow B: a verified claim is created only for a `consistent` set the insurer's own record confirms, with a future payout."""
    claim_fp = next((f["fingerprint"] for f in fps if f["kind"] == "claim"), None)
    mine = store.verified_claim_by_hash(conn, claim_fp) if claim_fp else None
    if mine and mine["user_id"] == user:
        return mine, []                                    # the same person asking again: a replay, nothing new is created
    if mine:
        return None, ["CLAIM_ALREADY_USED"]
    reg, facts = report.get("registry") or {}, report["facts"]
    if report["verdict"] != "consistent":
        return None, ["DOCUMENTS_" + report["verdict"].upper()]
    if not (reg.get("found") and reg.get("status") == "approved" and isinstance(reg.get("approved_inr"), int) and claim_fp):
        return None, ["INSURER_RECORD_NOT_CONFIRMED"]
    reasons: list[str] = []
    ref = (facts.get("claim_reference") or {}).get("value")
    due = insurer_registry.payout_date(insurer_registry.lookup(ref))
    holder = (facts.get("holder_name") or {}).get("value")
    if not due or due <= datetime.now(timezone.utc).date():
        reasons.append("PAYOUT_DATE_UNKNOWN_OR_PAST")
    if not holder:
        reasons.append("HOLDER_NAME_MISSING")
    if reg["approved_inr"] <= 0 or not reg.get("claimed_inr"):
        reasons.append("NOTHING_APPROVED")
    if reasons:
        return None, reasons
    claim = store.create_verified_claim(conn, user, app_id, claim_fp, holder, reg["claimed_inr"], reg["approved_inr"], due.isoformat())
    store.record_fingerprints(conn, user, app_id, fps)
    return claim, []


def _gate_preauth(conn, user: str, app_id: str, report: dict, fps: list[dict]) -> tuple[dict | None, list[str]]:
    """Workflow A: an offer only for a `consistent` set whose estimate is payable and confident, at a partner hospital."""
    P = engine.cfg()["preauth"]
    facts, est = report["facts"], report.get("estimate")
    key = verify.preauth_key(facts)
    mine = store.preauth_by_key(conn, key) if key else None
    if mine and mine["user_id"] == user:
        return mine, []
    if mine:
        return None, ["ESTIMATE_ALREADY_USED"]
    hospital = hospital_registry.lookup((facts.get("hospital_name") or {}).get("value"))
    holder = (facts.get("holder_name") or {}).get("value")
    if report["verdict"] != "consistent":
        return None, ["DOCUMENTS_" + report["verdict"].upper()]
    if not est:
        return None, ["ESTIMATE_NOT_POSSIBLE"]
    if est["status"] != "likely_eligible":
        return None, ["CLAIM_LIKELY_NOT_PAYABLE"]
    if est["confidence"]["level"] not in P["min_confidence"]:
        return None, ["ESTIMATE_NOT_CONFIDENT_ENOUGH"]
    if not hospital:
        return None, ["HOSPITAL_NOT_A_PARTNER"]
    if not (holder and key):
        return None, ["ESTIMATE_NOT_IDENTIFIABLE"]
    if est["conservative_inr"] <= 0:
        return None, ["NOTHING_TO_FINANCE"]
    days = report["risk"]["settlement_outlook"]["could_take_up_to_days"] + P["claim_buffer_days"]
    pre = store.create_preauth(conn, user, app_id, key, holder, hospital["id"], hospital["name"], est["bill_total_inr"],
                               est["estimated_claim_inr"], est["conservative_inr"], est["confidence"]["level"], days)
    store.record_fingerprints(conn, user, app_id, fps)
    return pre, []


# ------------------------------------------------------------------ the run
def run(conn, user: str, workflow: str, readings: list[dict], *, declared_pre_existing: bool | None = None,
        already_claimed_inr: int | None = None, scanned_pages: int = 0) -> dict:
    """Run every stage, write the audit row and the agent log, and return {report, row, reasons, run}. `row` is the verified claim (B)
    or the pre-authorisation offer (A) when the gate passed, else None. Documents are never stored; only the hashes, and only when an
    offer is prepared."""
    if workflow not in WORKFLOW_OF:
        raise ValueError("unknown workflow")
    steps: list[PipelineStep] = []
    types = [r["doc_type"] for r in readings]
    steps.append(_step("READ", "done", f"Read {len(readings)} document(s) on the server; {sum(t != 'unknown' for t in types)} recognised.",
                       document_types=sorted(t for t in types if t != "unknown"), not_recognised=sum(t == "unknown" for t in types), scanned_pages=scanned_pages))

    app_id = store.new_application_id()
    report = verify.verify(readings, workflow, declared_pre_existing=declared_pre_existing, already_claimed_inr=already_claimed_inr,
                           used_elsewhere=lambda kind, fp: store.fingerprint_used_elsewhere(conn, user, app_id, kind, fp, own_preauth_ok=True))
    fps = report.pop("fingerprints")
    v = report["verdict"]
    warns = [c["code"] for c in report["checks"] if c["status"] == "warn"]
    steps.append(_step("VERIFY", "done" if v == "consistent" else "flagged" if v == "needs_review" else "stopped",
                       {"consistent": "The documents agree with each other and with the insurer's record.", "needs_review": "Nothing contradicts, but a person must look at something.",
                        "incomplete": "Some documents are still needed.", "inconsistent": "The documents contradict each other or the insurer."}[v],
                       verdict=v, failed=report["failed"], warnings=warns, missing_documents=report["missing_documents"]))
    est = report.get("estimate")
    if est:
        steps.append(_step("ESTIMATE", "done" if est["status"] == "likely_eligible" else "flagged",
                           f"Estimated payout {_inr(est['estimated_claim_inr'])} on a {_inr(est['bill_total_inr'])} bill; cautious figure {_inr(est['conservative_inr'])}. An estimate, not an approval.",
                           payable=est["status"], estimated_inr=est["estimated_claim_inr"], conservative_inr=est["conservative_inr"], confidence=est["confidence"]["level"],
                           deductions=[s["code"] for s in est["steps"] if s["deduction_inr"] > 0], uncertainties=[u["code"] for u in est["uncertainties"]],
                           demo_waivers=[w["code"] for w in est.get("demo_waivers", [])]))
    else:
        steps.append(_step("ESTIMATE", "skipped", "No estimate could be worked out from these documents.", reasons=[c["code"] for c in report["checks"] if c["code"] == "ESTIMATE_NOT_POSSIBLE"]))
    risk = report.get("risk")
    if risk:
        steps.append(_step("RISK", "flagged" if risk["review"] else "done",
                           "Signals that ask a person to look: " + ", ".join(f["code"] for f in risk["flags"]) + "." if risk["review"] else "No unusual signal. These checks only ever ask a person to look.",
                           flags=[f["code"] for f in risk["flags"]], ml_ran=risk["ml"]["ran"], ml_flagged=risk["ml"]["flagged"],
                           payout_likely_days=risk["settlement_outlook"]["likely_days"], payout_up_to_days=risk["settlement_outlook"]["could_take_up_to_days"]))
    else:
        steps.append(_step("RISK", "skipped", "Not run: there was no bill or approval to look at."))

    row, reasons = (_gate_bridge if workflow == "approved_claim" else _gate_preauth)(conn, user, app_id, report, fps)
    steps.append(_step("GATE", "done" if row else "stopped",
                       "Every condition for an offer is met." if row else "No offer: " + ", ".join(reasons) + ".", reasons=reasons))
    kind = "verified claim" if workflow == "approved_claim" else "hospital-advance offer"
    steps.append(_step("OFFER", "done" if row else "skipped",
                       f"A {kind} {row['id']} was prepared. It is only an offer: the person's consent, KYC and one-time code and the guardrail still decide any money." if row
                       else "Nothing was prepared and nothing was paid.", ref=row["id"] if row else None))
    outcome = "offer_prepared" if row else "needs_a_person" if any(r.startswith("DOCUMENTS_NEEDS") or r.startswith("DOCUMENTS_INCOMPLETE") for r in reasons) else "no_offer"

    action = "bridge_from_documents" if workflow == "approved_claim" else "preauth_from_documents"
    figure = (row["approved_inr"] if workflow == "approved_claim" else row["conservative_inr"]) if row else None
    store.audit(conn, user, "agent", action, guardrail.ALLOW if row else guardrail.BLOCK, ("CLAIM_VERIFIED" if workflow == "approved_claim" else "OFFER_PREPARED") if row else reasons[0],
                amount=figure, note=f"verdict={v}; failed={','.join(report['failed']) or 'none'}; "
                + ("sized from the insurer record, not the letter" if workflow == "approved_claim" else "sized from the conservative estimate, an estimate is not an approval") + "; text never logged")
    run_id = store.save_pipeline_run(conn, user, workflow, outcome, [s.model_dump() for s in steps], row["id"] if row else None)
    return {"report": report, "row": row, "reasons": reasons,
            "run": {"id": run_id, "workflow": workflow, "kind": WORKFLOW_OF[workflow], "outcome": outcome, "steps": [s.model_dump() for s in steps]}}
