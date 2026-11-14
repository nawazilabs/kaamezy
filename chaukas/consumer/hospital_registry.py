"""The SIMULATED partner-hospital registry: where a pre-authorisation advance may be paid.

The payee of a hospital-direct advance is always a hospital on this list, matched by name from the verified documents. The
person never types a payee or a bank account. A hospital that is not here, or is not a partner, is simply not paid to
directly. Nothing is stored or cached: every lookup reads the config again.

Owner: Member 1 · config: config/hospital_registry_sim.yaml
"""
from __future__ import annotations

import yaml

from chaukas.db import ROOT


def lookup(hospital_name: str | None) -> dict | None:
    """The partner hospital with this name (a partner only), or None."""
    from chaukas.consumer import verify          # org_match lives with the other document checks
    if not hospital_name:
        return None
    reg = yaml.safe_load((ROOT / "config" / "hospital_registry_sim.yaml").read_text(encoding="utf-8")) or {}
    for h in reg.get("hospitals", []):
        if h.get("network") and verify.org_match(hospital_name, h["name"]):
            return dict(h)
    return None


def by_id(hospital_id: str | None) -> dict | None:
    """The partner hospital with this id (a partner only), or None."""
    reg = yaml.safe_load((ROOT / "config" / "hospital_registry_sim.yaml").read_text(encoding="utf-8")) or {}
    return next((dict(h) for h in reg.get("hospitals", []) if h["id"] == hospital_id and h.get("network")), None)
