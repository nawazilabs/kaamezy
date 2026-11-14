"""The SIMULATED insurer registry: the insurer's own record of a claim, looked up by claim reference.

An approval letter is a document, and a document can be altered, so it is never trusted on its own. `lookup` is the one
call a real insurer's claim-status API would answer; here a mock reads config/insurer_registry_sim.yaml (invented demo
data). Nothing is stored, nothing is cached: every lookup reads the config again.

Owner: Member 1 · config: config/insurer_registry_sim.yaml
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from chaukas.integrations._common import load_config


def normalise_reference(ref: str | None) -> str:
    """'clm/2026/00012345', 'CLM 2026 00012345' and 'CLM-2026-00012345' are the same reference."""
    return re.sub(r"[^A-Z0-9]", "", str(ref or "").upper())


def lookup(claim_reference: str | None) -> dict | None:
    """The insurer's record of this claim, or None if it has no such claim (the letter then cannot be confirmed)."""
    want = normalise_reference(claim_reference)
    if not want:
        return None
    for rec in load_config("insurer_registry_sim").get("claims", []):
        if normalise_reference(rec["claim_reference"]) == want:
            return dict(rec)
    return None


def payout_date(rec: dict | None, today: date | None = None) -> date | None:
    """When this (simulated) insurer will pay. A real insurer's API gives a date; the demo records carry `payment_in_days` so the
    payout is always in the future whenever the demo is run, and fall back to their fixed `payment_expected`."""
    if not rec:
        return None
    today = today or date.today()
    if isinstance(rec.get("payment_in_days"), int):
        return today + timedelta(days=rec["payment_in_days"])
    try:
        return date.fromisoformat(str(rec.get("payment_expected")))
    except ValueError:
        return None
