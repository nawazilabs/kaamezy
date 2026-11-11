"""Voice / chat -> a SUGGESTION for the bill and recharge form. It never pays and never creates a payment request.

Modelled on prefill.py, and deterministic (keywords from config/consumer_fintech.yaml; no model is needed):
  * the biller is found by its keywords; an unknown biller returns nothing and the screen asks which one
  * for a recharge, a plan amount is kept ONLY if a figure in the person's own words equals a listed plan of that operator;
    any other figure is dropped, never rounded to a plan
  * a bill amount is never returned: it comes from the biller's (mock) bill on the server
  * the account / mobile number is NEVER taken from what was said; the screen asks the person to type it

Contract: extract_payment(text) -> {"biller_id", "plan_amount_inr", "fields", "missing", "dropped"} | None
"""
from __future__ import annotations

import re

from chaukas.consumer import fintech, prefill


def _figures(text: str) -> set[int]:
    """Whole-number figures in the text (spoken numbers turned into digits). A long digit run (a phone or account number) is not a figure."""
    t = prefill.spoken_numbers(prefill._ascii_digits(text))
    return {int(m.replace(",", "")) for m in re.findall(r"(?<![\d.])\d{1,3}(?:,\d{2,3})+(?![\d,])|(?<![\d.,])\d{1,6}(?![\d.,])", t)}


def extract_payment(text: str) -> dict | None:
    cfg = fintech.cfg()
    biller = next((b for b in cfg["billers"] if any(re.search(k, text, re.I) for k in b.get("keywords") or [])), None)
    if not biller:
        return None
    fields, dropped, plan = ["biller_id"], [], None
    missing = ["account"]  # always typed on the screen
    if biller["kind"] == "recharge":
        plans = {int(p["amount_inr"]) for p in cfg["recharge_plans"].get(biller["id"], [])}
        figs = _figures(text)
        hit = sorted(f for f in figs if f in plans)
        if len(hit) == 1:
            plan = hit[0]
            fields.append("plan_amount_inr")
        else:
            missing.append("plan_amount_inr")
            if figs - plans:
                dropped.append("plan_amount_inr")  # a figure was said but it is not one of this operator's plans
    return {"biller_id": biller["id"], "plan_amount_inr": plan, "fields": fields, "missing": missing, "dropped": dropped}
