"""Every screen the frontend injects must be listed in index.html's SCREENS, or showScreen() never un-hides it (a screen that
is missing there simply stays blank). This caught the 'Read a document' screen, which had been left out."""
import re

from chaukas import db

WEB = db.ROOT / "web"


def test_every_flow_section_is_a_known_screen():
    flows = (WEB / "flows.js").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")
    screens = set(re.findall(r'"([a-z0-9-]+)"', re.search(r"const SCREENS = \[(.*?)\];", index).group(1)))
    sections = set(re.findall(r'<section class="flow" id="([a-z0-9-]+)"', flows))
    assert sections and {"hdr", "hdc", "hrl"} <= sections
    assert sections <= screens, sorted(sections - screens)


def test_the_claim_documents_screen_only_calls_endpoints_that_exist():
    flows = (WEB / "flows.js").read_text(encoding="utf-8")
    start = flows.index("claim documents: check a set")
    block = flows[start:flows.index("agent log: what the claim-financing agent did", start)]
    for path in set(re.findall(r'capi\("(?:GET|POST)", "(/[a-z/-]+)"', block)):
        assert path in ("/docs/config", "/docs/verify", "/bridge/from-documents", "/preauth/from-documents"), path
    assert "approved_inr" not in block.split("askAdvance")[1]       # the screen never sends an amount


def test_the_agent_log_screen_is_read_only():
    flows = (WEB / "flows.js").read_text(encoding="utf-8")
    start = flows.index("agent log: what the claim-financing agent did")
    block = flows[start:flows.index("/* ---------------- lists", start)]
    calls = re.findall(r'capi\("([A-Z]+)", "(/[a-z/?=0-9]*)', block)
    assert calls and all(method == "GET" and path.startswith("/pipeline/runs") for method, path in calls), calls
