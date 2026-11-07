"""Build the translation files for the app and the assistant, using Sarvam Translate.

    python scripts/build_i18n.py                 # every language, only strings not translated yet
    python scripts/build_i18n.py --lang ta-IN    # one language
    python scripts/build_i18n.py --redo          # translate everything again

Why files and not a live call (CLAUDE.md rules 1, 5, 7, 11):
  * the screens show the person's own data (name, email, PAN, amounts). A live "translate this page" call would send
    that to a third party. Here only the app's own fixed wording and the knowledge-base answers are ever sent, once,
    at build time, and the result is reviewed and committed;
  * the assistant's facts stay auditable: its reply is still the knowledge-base text, in another language;
  * the app works with no extra network call and no key in the browser.

Two outputs:
  web/i18n/<code>.json              {"English text": "translated text"} for every fixed string in web/index.html and
                                    web/flows.js. The browser swaps a text node only when it equals a key exactly,
                                    so anything built at run time (names, amounts, quotes) is never touched.
  config/consumer_kb_i18n.yaml      the assistant's disclosure line, intro and every knowledge-base answer.

A translation is kept ONLY if it passes code checks: the same digits and rupee signs as the English, no line count
change in a batch, and (knowledge base) no number that is not already in the knowledge base. A string that fails stays
English, which is always safe, and is listed in the report.

Needs SARVAM_API_KEY in .env. Owner: Member 1
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# code -> (native name, English name). Sarvam Translate (sarvam-translate:v1) covers all of these.
LANGUAGES = {
    "hi-IN": ("हिन्दी", "Hindi"), "mr-IN": ("मराठी", "Marathi"), "bn-IN": ("বাংলা", "Bengali"),
    "ta-IN": ("தமிழ்", "Tamil"), "te-IN": ("తెలుగు", "Telugu"), "kn-IN": ("ಕನ್ನಡ", "Kannada"),
    "ml-IN": ("മലയാളം", "Malayalam"), "gu-IN": ("ગુજરાતી", "Gujarati"), "pa-IN": ("ਪੰਜਾਬੀ", "Punjabi"),
    "od-IN": ("ଓଡ଼ିଆ", "Odia"), "as-IN": ("অসমীয়া", "Assamese"), "ur-IN": ("اردو", "Urdu"),
}
ENDPOINT = "https://api.sarvam.ai/translate"
MODEL = "sarvam-translate:v1"
BATCH_CHARS = 1500  # the API accepts up to 2000 characters per request; leave room
SEP = "\n"


# ------------------------------------------------------------------ which strings are fixed UI wording
class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        for k, v in attrs:
            if k in ("placeholder", "aria-label", "title", "alt") and v:
                self.out.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_texts(src: str) -> list[str]:
    p = _Text()
    p.feed(src)
    return p.out


_LIT = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
_ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\(.)")


def _unescape(s: str) -> str:
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1) else {"n": "\n", "t": " "}.get(m.group(2), m.group(2)), s)


def norm(s: str) -> str:
    """The key the browser looks up: whitespace collapsed, ends trimmed."""
    return re.sub(r"\s+", " ", s).strip()


# brand names, sample data and code-ish strings that happen to look like text: never translated
NEVER = {"Bearer", "Escape", "Authorization", "Noto Sans Devanagari", "Demo Sharma", "Demo Sharma Jr", "Nawazi Labs", "Paytm",
         "Chaukas", "XXXX XXXX XXXX", "— next read —"}


def looks_like_ui(s: str) -> bool:
    if s in NEVER or re.match(r"^[MLCZHVSQTAmlczhvsqta][\d.,\- ]+[\d]", s):  # a brand, or an SVG path
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters or ord(letters[0]) > 127:
        return False  # already Hindi (or another script): the source language here is English
    if not (2 <= len(s) <= 400) or "${" in s or "=>" in s or "function" in s or s.startswith(("http", "/", "#", ".", "data:")):
        return False
    if not re.search(r"[^\W\d_]", s):
        return False
    first = s[0]
    if not (first.isupper() or ord(first) > 127 or first in "(“‘\"'"):
        return False  # class names, ids and fragments start in lower case
    if " " not in s and not re.fullmatch(r"[A-Z][a-z]+[?.!]?", s):
        return False  # a single token is only text if it is a plain capitalised word
    return not re.search(r"[A-Za-z]+_[A-Za-z]+|\.[a-z]+\(", s)


def collect() -> list[str]:
    """Every fixed English string the person can see, in first-seen order."""
    found: dict[str, None] = {}

    def add(s: str):
        s = norm(html.unescape(s))
        if looks_like_ui(s):
            found.setdefault(s)

    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    flows = (ROOT / "web" / "flows.js").read_text(encoding="utf-8")
    for t in _html_texts(index):
        add(t)
    for src in (index, flows):
        for m in _LIT.finditer(src):
            lit = _unescape(m.group(1) if m.group(1) is not None else m.group(2))
            if "<" in lit:
                for t in _html_texts(lit):
                    add(t)
            else:
                add(lit)
    # the HTML template string in flows.js is a backtick literal: its text sits between tags
    for chunk in re.findall(r"`([^`]*)`", flows, re.S):
        for t in _html_texts(re.sub(r"\$\{[^}]*\}", " ", chunk)):
            add(t)
    return list(found)


# ------------------------------------------------------------------ Sarvam
def _key() -> str:
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        envp = ROOT / ".env"
        if envp.exists():
            for line in envp.read_text(encoding="utf-8").splitlines():
                if line.startswith("SARVAM_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
    if not key:
        sys.exit("SARVAM_API_KEY is not set (.env)")
    return key


def translate(text: str, target: str, key: str, tries: int = 4) -> str | None:
    body = json.dumps({"input": text, "source_language_code": "en-IN", "target_language_code": target, "model": MODEL,
                       "numerals_format": "international"}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(ENDPOINT, body, {"Content-Type": "application/json", "api-subscription-key": key})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=60).read())["translated_text"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(2 + attempt * 3)
                continue
            print(f"   HTTP {e.code}: {e.read().decode()[:120]}")
            return None
        except Exception as e:  # network blip
            print(f"   {type(e).__name__}: {e}")
            time.sleep(2)
    return None


_DIGITS = re.compile(r"\d")


def _signature(s: str) -> tuple:
    """What a translation must keep: the digits (in order) and the rupee signs."""
    from chaukas.consumer.assistant import _ascii_digits
    s = _ascii_digits(s)
    return ("".join(_DIGITS.findall(s)), s.count("₹"))


def same_figures(en: str, tr: str) -> bool:
    return _signature(en) == _signature(tr)


def batches(items: list[str]):
    cur, size = [], 0
    for s in items:
        if cur and size + len(s) + 1 > BATCH_CHARS:
            yield cur
            cur, size = [], 0
        cur.append(s)
        size += len(s) + 1
    if cur:
        yield cur


def translate_many(items: list[str], target: str, key: str) -> tuple[dict[str, str], list[str]]:
    """{english: translated} for the items that pass the checks, plus the ones that did not."""
    done: dict[str, str] = {}
    bad: list[str] = []
    for chunk in batches(items):
        out = translate(SEP.join(chunk), target, key)
        parts = out.split(SEP) if out else []
        if len(parts) != len(chunk):  # lines moved: ask one at a time
            parts = [translate(s, target, key) or "" for s in chunk]
        for en, tr in zip(chunk, parts):
            tr = (tr or "").strip()
            if tr and "\n" not in tr and same_figures(en, tr):
                done[en] = tr
            else:
                bad.append(en)
    return done, bad


# ------------------------------------------------------------------ assistant knowledge base
def kb_items() -> dict[str, str]:
    """key -> English text for every assistant sentence: disclosure, intro, and each knowledge-base topic."""
    kb = yaml.safe_load((ROOT / "config" / "consumer_kb.yaml").read_text(encoding="utf-8"))
    disc = yaml.safe_load((ROOT / "config" / "disclosure.yaml").read_text(encoding="utf-8"))
    items = {"disclosure": disc["messages"]["en-IN"]["ai_disclosure"], "session_intro": kb["session_intro"]["en"]}
    for name, t in kb["topics"].items():
        items["topic." + name] = t["en"]
    return items


def kb_ok(en: str, tr: str, allowed: set[str]) -> bool:
    from chaukas.consumer.assistant import _numbers
    return same_figures(en, tr) and not (_numbers(tr) - allowed) and "₹" not in tr


def build(langs: list[str], redo: bool):
    key = _key()
    ui = collect()
    kb = kb_items()
    print(f"{len(ui)} fixed UI strings, {len(kb)} assistant sentences, {len(langs)} languages")
    (ROOT / "web" / "i18n").mkdir(exist_ok=True)
    kb_path = ROOT / "config" / "consumer_kb_i18n.yaml"
    kb_all = (yaml.safe_load(kb_path.read_text(encoding="utf-8")) if kb_path.exists() else None) or {"languages": {}}
    from chaukas.consumer.assistant import allowed_numbers
    allowed = allowed_numbers()
    report = []
    for code in langs:
        print(f"== {code}")
        p = ROOT / "web" / "i18n" / f"{code}.json"
        have = {} if redo or not p.exists() else json.loads(p.read_text(encoding="utf-8"))
        have = {k: v for k, v in have.items() if k in set(ui)}  # strings that left the app are dropped
        todo = [s for s in ui if s not in have]
        got, bad = translate_many(todo, code, key)
        have.update(got)
        ordered = {s: have[s] for s in ui if s in have}
        p.write_text(json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

        cur = {} if redo else dict(kb_all["languages"].get(code, {}))
        cur_flat = {k: v for k, v in cur.items() if k in kb}
        for k, en in kb.items():
            if k in cur_flat:
                continue
            tr = translate(en, code, key)
            if tr and kb_ok(en, tr.strip(), allowed):
                cur_flat[k] = tr.strip()
            else:
                bad.append("KB:" + k)
        kb_all["languages"][code] = {k: cur_flat[k] for k in kb if k in cur_flat}
        report.append((code, len(ordered), len(ui), len(cur_flat), len(kb), bad))
        print(f"   ui {len(ordered)}/{len(ui)}  assistant {len(cur_flat)}/{len(kb)}  kept in English: {len(bad)}")
    head = ("# Assistant sentences translated with Sarvam Translate (scripts/build_i18n.py). Generated: do not edit by hand,\n"
            "# re-run the script. Every entry passed the figures check (same digits, no new number, no rupee amount).\n"
            "# NOT reviewed by a native speaker. A missing entry means the assistant answers in English for that sentence.\n")
    kb_path.write_text(head + yaml.safe_dump(kb_all, allow_unicode=True, sort_keys=True, width=1000), encoding="utf-8")
    manifest = [{"code": "en-IN", "native": "English", "english": "English"}] + [
        {"code": c, "native": n, "english": e} for c, (n, e) in LANGUAGES.items() if c in {r[0] for r in report} or (ROOT / "web" / "i18n" / f"{c}.json").exists()]
    (ROOT / "web" / "i18n" / "languages.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for code, a, b, c, d, bad in report:
        if bad:
            print(f"{code}: kept in English ->", "; ".join(x[:50] for x in bad[:6]), "..." if len(bad) > 6 else "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", action="append", help="language code (repeatable); default all")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the strings that would be translated and exit")
    a = ap.parse_args()
    if a.list:
        print("\n".join(collect()))
        sys.exit(0)
    build(a.lang or list(LANGUAGES), a.redo)
