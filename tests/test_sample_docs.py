"""The demo documents in web/samples: real PDFs, scans and a photo of invented policies, bills and claim letters.

Each CASE is a short story with the verdict it must get. The text cases are read through the REAL reader and the REAL
verification (the golden text is what a PDF reader gives back for the file: `python -m data.sample_docs` draws the PDF and
the golden text from the same layout) and must end exactly as the manifest says. The scan and photo cases need an OCR engine, so
pytest cannot read them; the manifest marks them `ocr: true`, and they were checked in a real PDF reader and OCR.

Owner: Member 1
"""
import json
import re
from pathlib import Path

import pytest

from chaukas.consumer import docs, insurer_registry, verify
from data import sample_docs as sd

SAMPLES = sd.OUT_DIR
CASES = sd.build_cases()
TEXT_CASES = [c for c in CASES if not c.expect.get("ocr")]


def readings(case):
    out = []
    for f in case.files:
        pages = sd.golden_text(f)
        out.append(docs.read_document(pages, ["text"] * len(pages), use_ai=False))
    return out


def run(case):
    return verify.verify(readings(case), case.workflow, declared_pre_existing=False, already_claimed_inr=0)


# ------------------------------------------------------------------ the committed files are the ones the generator makes
def test_the_committed_manifest_is_what_the_generator_makes():
    committed = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    assert committed == sd.manifest_of(CASES), "the samples are out of date: run `python -m data.sample_docs`"


def test_the_generator_is_deterministic():
    assert sd.manifest_of(sd.build_cases()) == sd.manifest_of(sd.build_cases())
    assert [sd.golden_text(f) for f in CASES[0].files] == [sd.golden_text(f) for f in sd.build_cases()[0].files]


def test_every_file_exists_and_nothing_else_is_there():
    manifest = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    on_disk = {str(p.relative_to(SAMPLES)).replace("\\", "/") for p in SAMPLES.rglob("*") if p.is_file()}
    listed = {f"{c['id']}/{f['name']}" for c in manifest["cases"] for f in c["files"]} | {"manifest.json"}
    assert on_disk == listed
    assert len({c["id"] for c in manifest["cases"]}) == len(manifest["cases"]) == 10


def test_text_pdfs_carry_their_text_and_scans_carry_none():
    for c in CASES:
        for f in c.files:
            data = (SAMPLES / c.id / f.name).read_bytes()
            assert len(data) > 1000
            if f.kind == "photo":
                assert data[:2] == b"\xff\xd8"
                continue
            assert data[:5] == b"%PDF-"
            cells = [x.text for p in f.sheet.layout() for _, it in p for x in it.cells
                     if x.text and x.text.isascii() and not set(x.text) & set("()\\")]
            found = [t for t in cells if t.encode("ascii") in data]
            if f.kind == "text":
                assert len(found) >= 0.95 * len(cells), (c.id, f.name, len(found), len(cells))      # a real text layer, the same words as the model
            else:
                assert b"/Image" in data and not [t for t in cells if len(t) > 12 and t.encode("ascii") in data], (c.id, f.name)   # pictures only


# ------------------------------------------------------------------ every story ends the way it says
@pytest.mark.parametrize("case", TEXT_CASES, ids=lambda c: c.id)
def test_each_story_ends_with_its_verdict(case):
    rep = run(case)
    assert rep["verdict"] == case.expect["verdict"], [(c["code"], c["status"]) for c in rep["checks"] if c["status"] != "pass"]
    assert set(case.expect.get("failed", [])) <= set(rep["failed"])
    warned = {c["code"] for c in rep["checks"] if c["status"] == "warn"}
    assert set(case.expect.get("warned", [])) <= warned
    if "estimate_inr" in case.expect:
        assert rep["estimate"]["estimated_claim_inr"] == case.expect["estimate_inr"]


@pytest.mark.parametrize("case", TEXT_CASES, ids=lambda c: c.id)
def test_each_document_is_read_as_what_it_is_and_nothing_is_missing(case):
    for f, r in zip(case.files, readings(case)):
        assert r["doc_type"] == f.doc_type, (case.id, f.name)
        assert r["missing_required"] == [] and r["needs_review"] == [], (case.id, f.name, r["missing_required"], r["needs_review"])


def test_the_claims_in_the_stories_are_the_insurers_records_except_the_unknown_one():
    for c in CASES:
        if c.workflow != "approved_claim":
            continue
        letter = next(f for f in c.files if f.doc_type == "claim_approval")
        ref = docs.read_document(sd.golden_text(letter), use_ai=False)["fields"]["claim_reference"]["value"]
        assert (insurer_registry.lookup(ref) is None) == (c.id == "unknown_claim"), c.id


def test_the_scan_and_photo_cases_are_marked_for_ocr():
    for c in CASES:
        has_scan = any(f.kind != "text" for f in c.files)
        assert bool(c.expect.get("ocr")) == has_scan, c.id
    assert {f.kind for c in CASES for f in c.files} == {"text", "scan", "photo"}


def test_the_stories_cover_a_clean_claim_a_renewal_a_pre_approval_and_each_way_a_claim_can_be_refused():
    ids = {c.id for c in CASES}
    assert {"rahul_approved", "anita_full_approval", "preapproval_estimate", "tampered_amount", "different_patient", "unknown_claim",
            "settled_claim", "forged_after_rejection", "approval_not_issued", "rahul_scanned"} == ids
    assert {c.expect["verdict"] for c in CASES} == {"consistent", "needs_review", "inconsistent"}


# ------------------------------------------------------------------ what a reader of the sample documents sees
def test_the_clean_policy_has_two_pages_and_its_exclusions_are_cited_on_the_second():
    case = next(c for c in CASES if c.id == "rahul_approved")
    pol = readings(case)[0]
    assert pol["pages"] == 2 and pol["lists"]["exclusions"][0]["page"] == 2 and pol["fields"]["sum_insured_inr"]["page"] == 1
    assert len(pol["lists"]["exclusions"]) == 5 and len(pol["lists"]["sub_limits"]) == 3


def test_a_renewed_policy_shows_continuous_cover_and_is_not_blocked_by_a_waiting_period():
    anita = next(c for c in CASES if c.id == "anita_full_approval")
    pol = readings(anita)[0]
    assert pol["fields"]["continuous_since"]["value"] == "2022-04-01"
    rep = run(anita)
    assert rep["estimate"]["status"] == "likely_eligible" and rep["estimate"]["estimated_claim_inr"] == 120000


def test_the_tampered_letter_is_internally_consistent_so_only_the_insurer_record_catches_it():
    rep = run(next(c for c in CASES if c.id == "tampered_amount"))
    approval = next(r for r in readings(next(c for c in CASES if c.id == "tampered_amount")) if r["doc_type"] == "claim_approval")
    assert approval["warnings"] == [] and approval["fields"]["approved_inr"]["value"] == 190000
    assert "INSURER_RECORD_DIFFERS" in rep["failed"] and "APPROVED_WITHIN_CLAIMED" in [c["code"] for c in rep["checks"] if c["status"] == "pass"]


# ------------------------------------------------------------------ everything is invented, and says so
def test_nothing_looks_like_real_personal_data_and_every_page_says_it_is_a_demo():
    for c in CASES:
        for f in c.files:
            pages = sd.golden_text(f)
            blob = "\n".join(pages)
            assert not re.search(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)", blob), (c.id, f.name)                  # no Aadhaar-like number
            assert not re.search(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])", blob), (c.id, f.name)        # no PAN
            assert not re.search(r"(?<!\d)[6-9]\d{9}(?!\d)", blob) and "@" not in blob, (c.id, f.name)               # no mobile, no e-mail
            assert all(sd.FOOTER in p for p in pages) and "invented data" in blob.lower(), (c.id, f.name)
    assert "invented" in json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))["note"].lower()
