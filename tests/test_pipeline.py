"""Phase 8: the claim-financing pipeline and its agent log (consumer/pipeline.py).

One typed path, READ -> VERIFY -> ESTIMATE -> RISK -> GATE -> OFFER, shared by Workflow A and B. The log it writes is append-only and
holds codes and whole-rupee figures only: never a value read from a document.
"""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaukas.consumer import api, docs, store
from tests.test_doc_verify import NAMES, texts

BASE = "/api/consumer"
STAGES = ["READ", "VERIFY", "ESTIMATE", "RISK", "GATE", "OFFER"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAUKAS_DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setenv("CHAUKAS_CONSUMER_AUTH", "off")
    monkeypatch.setenv("CHAUKAS_REPLAY_MODE", "off")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def run(c, workflow="approved_claim", t=None, names=NAMES, **over):
    t = t or texts()
    b = {"workflow": workflow, "documents": [{"pages": [t[n]], "page_sources": ["text"]} for n in names], "declared_pre_existing": False,
         "already_claimed_inr": 0, "use_ai": False}
    b.update(over)
    r = c.post(f"{BASE}/pipeline/run", json=b)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_clean_claim_runs_every_stage_in_order_and_prepares_an_offer(client):
    r = run(client)
    assert r["eligible"] is True and r["verdict"] == "consistent" and r["offer"]["id"].startswith("VCL-") and r["offer"]["approved_inr"] == 132500
    steps = r["run"]["steps"]
    assert [s["name"] for s in steps] == STAGES and r["run"]["outcome"] == "offer_prepared" and r["run"]["kind"] == "B"
    assert [s["status"] for s in steps] == ["done"] * 6
    assert steps[0]["facts"]["document_types"] == ["claim_ack", "claim_approval", "discharge_summary", "hospital_bill", "policy"]
    assert "Estimated payout" in steps[2]["headline"] and "not an approval" in steps[2]["headline"]
    assert steps[3]["facts"]["flags"] == [] and steps[5]["facts"]["ref"] == r["offer"]["id"]
    assert "only an offer" in steps[5]["headline"]


def test_workflow_a_runs_the_same_stages_and_the_offer_is_a_hospital_advance(client):
    r = run(client, "preapproval", names=("policy", "bill"))
    assert r["eligible"] is True and r["run"]["kind"] == "A" and r["offer"]["id"].startswith("PRE-") and r["offer"]["payee_name"] == "City Care Hospital"
    assert [s["name"] for s in r["run"]["steps"]] == STAGES


def test_a_stopped_set_still_logs_every_stage_and_says_why(client):
    t = texts(claim_approval=("Approved Amount: Rs. 1,32,500", "Approved Amount: Rs. 1,90,000"))
    r = run(client, t=t)
    assert r["eligible"] is False and r["offer"] is None and r["reasons"] == ["DOCUMENTS_INCONSISTENT"] and r["run"]["outcome"] == "no_offer"
    by = {s["name"]: s for s in r["run"]["steps"]}
    assert by["VERIFY"]["status"] == "stopped" and "CLAIMED_AMOUNT_MATCHES" not in by["VERIFY"]["facts"]["failed"] and by["VERIFY"]["facts"]["failed"]
    assert by["GATE"]["status"] == "stopped" and by["GATE"]["facts"]["reasons"] == ["DOCUMENTS_INCONSISTENT"]
    assert by["OFFER"]["status"] == "skipped" and by["OFFER"]["headline"].startswith("Nothing was prepared")


def test_a_set_that_needs_a_person_is_logged_as_such(client):
    t = texts()
    for n in ("claim_ack", "claim_approval"):
        t[n] = t[n].replace("CLM/2026/00012345", "CLM/2026/00099999")
    r = run(client, t=t)
    assert r["run"]["outcome"] == "needs_a_person" and r["reasons"] == ["DOCUMENTS_NEEDS_REVIEW"]
    assert {s["name"]: s["status"] for s in r["run"]["steps"]}["VERIFY"] == "flagged"


def test_the_log_is_kept_listed_and_fetched_by_id(client):
    first, second = run(client), run(client, "preapproval", names=("policy", "bill"))
    runs = client.get(f"{BASE}/pipeline/runs").json()
    assert [x["id"] for x in runs] == [second["run"]["id"], first["run"]["id"]]
    one = client.get(f"{BASE}/pipeline/runs/{first['run']['id']}").json()
    assert one["steps"] == first["run"]["steps"] and one["ref_id"] == first["offer"]["id"] and one["outcome"] == "offer_prepared"
    assert client.get(f"{BASE}/pipeline/runs/RUN-00000000").status_code == 404


def test_the_log_never_holds_a_value_read_from_a_document(client):
    t = texts()
    r = run(client, t=t)
    blob = json.dumps(r["run"]).lower() + json.dumps(client.get(f"{BASE}/pipeline/runs").json()).lower()
    secrets = set()
    for n in NAMES:
        reading = docs.read_document([t[n]], use_ai=False)
        for f in reading["fields"].values():
            v = str(f["value"]).lower()
            if len(v) >= 5 and not v.isdigit() and v not in ("approved", "pending", "settled", "rejected"):
                secrets.add(v)
    assert {"rahul sharma", "city care hospital", "clm/2026/00012345"} & secrets
    leaked = [v for v in secrets if v in blob]
    assert not leaked, leaked[:3]
    assert "fraud" not in blob


def test_the_agent_log_is_append_only(client):
    r = run(client)
    conn = store.connect()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE consumer_pipeline_runs SET outcome='no_offer' WHERE id=?", (r["run"]["id"],))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM consumer_pipeline_runs WHERE id=?", (r["run"]["id"],))


def test_the_pipeline_endpoint_and_the_older_endpoints_reach_the_same_decision(client):
    t = texts()
    body = {"documents": [{"pages": [t[n]], "page_sources": ["text"]} for n in NAMES], "declared_pre_existing": False, "already_claimed_inr": 0, "use_ai": False}
    old = client.post(f"{BASE}/bridge/from-documents", json=body).json()
    new = run(client)
    assert old["eligible"] is new["eligible"] is True and old["claim"]["id"] == new["offer"]["id"] and old["run_id"].startswith("RUN-")
    assert len(client.get(f"{BASE}/pipeline/runs").json()) == 2


def test_a_run_never_moves_money(client):
    run(client)
    run(client, "preapproval", names=("policy", "bill"))
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM consumer_loans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM consumer_executions").fetchone()[0] == 0


def test_an_unknown_workflow_is_refused(client):
    r = client.post(f"{BASE}/pipeline/run", json={"workflow": "anything", "documents": [{"pages": ["x"]}]})
    assert r.status_code == 422
