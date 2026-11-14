"""The demo documents: invented policies, hospital bills, discharge summaries and claim letters, as real PDFs, scans and a photo.

    python -m data.sample_docs                  # writes web/samples/<case>/... and web/samples/manifest.json
    python -m data.sample_docs --out some/dir

Needs `pip install -r requirements-dev.txt` (reportlab for the PDFs, Pillow for the scans). The app does not need either.

Why they exist: the document reader, the claim estimator and the verification must be shown on files that look like real
ones (a letterhead, tables with columns, bulleted lists, a second page, a tilted and slightly blurred scan), and the
verification must be shown catching a document that was altered. Each CASE is one short story with the verdict it must
get. tests/test_sample_docs.py reads every text case through the real reader and the real verification and fails if a
story no longer ends the way the manifest says.

EVERYTHING HERE IS INVENTED: names, policy and claim numbers, hospitals, amounts. Every page says so in its footer. The
claim references and amounts of the cases are the ones in config/insurer_registry_sim.yaml (the SIMULATED insurer's record),
except where a case is meant to be a claim the insurer does not know.

How the text is made: a document is a list of rows of cells placed at x positions. The same layout draws the PDF (text layer,
standard Helvetica, no compression) and the scan (Pillow draws the same rows, then tilts, blurs and speckles them), and
`golden_text` is what a PDF reader gives back for it (cells of a row joined by three spaces), so a test can read the
documents without a PDF library. The money is written "Rs. 2,00,000", the way many Indian documents write it.

Owner: Member 1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "web" / "samples"

W, H = 595.0, 842.0                  # A4 in points
LM, RM, BOTTOM = 40.0, 555.0, 62.0
FOOTER = "DEMO DOCUMENT. Invented data for a product demo. Not valid for any real purpose."
COLORS = {"ink": (0.10, 0.10, 0.12), "white": (1, 1, 1), "grey": (0.38, 0.40, 0.45), "band": (0.05, 0.25, 0.47), "rule": (0.75, 0.78, 0.82)}


def rs(n: int) -> str:
    """Rs. 2,00,000 (Indian grouping)."""
    s = str(int(n))
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + parts + [tail])
    return "Rs. " + s


def num(n: int) -> str:
    return rs(n)[4:]


# ------------------------------------------------------------------ the page model
@dataclass
class Cell:
    x: float
    text: str
    size: float = 9.5
    bold: bool = False
    align: str = "l"                 # "l": x is the left edge, "r": x is the right edge
    color: str = "ink"


@dataclass
class Item:
    kind: str                        # band | row | rule | space | break
    cells: list[Cell] = field(default_factory=list)
    h: float = 0.0


class Sheet:
    """A document built row by row. `layout()` decides the pages; the renderers and `golden_text` all use that layout."""

    def __init__(self) -> None:
        self.items: list[Item] = []

    # -- building
    def band(self, name: str, sub: str = "") -> "Sheet":
        self.items.append(Item("band", [Cell(LM, name, 15, True, color="white"), Cell(LM, sub, 8.5, False, color="white")], 64))
        self.items.append(Item("space", h=14))
        return self

    def row(self, *cells: Cell, size: float = 9.5) -> "Sheet":
        self.items.append(Item("row", list(cells), size * 1.65))
        return self

    def title(self, text: str) -> "Sheet":
        self.space(4)
        self.items.append(Item("row", [Cell(LM, text, 13, True)], 13 * 1.7))
        return self.space(4)

    def h2(self, text: str) -> "Sheet":
        self.space(8)
        self.items.append(Item("row", [Cell(LM, text, 10.5, True)], 10.5 * 1.7))
        return self

    def kv(self, label: str, value: str) -> "Sheet":
        return self.row(Cell(LM, f"{label}: {value}"))

    def kv2(self, left: tuple[str, str], right: tuple[str, str], x2: float = 320) -> "Sheet":
        return self.row(Cell(LM, f"{left[0]}: {left[1]}"), Cell(x2, f"{right[0]}: {right[1]}"))

    def para(self, text: str, size: float = 9.5, x: float = LM, width: float = 470, bold: bool = False) -> "Sheet":
        for line in _wrap(text, size, width, bold):
            self.row(Cell(x, line, size, bold), size=size)
        return self

    def bullets(self, items: list[str], size: float = 9.5) -> "Sheet":
        for t in items:
            lines = _wrap(t, size, 455)
            self.row(Cell(LM + 6, "•", size), Cell(LM + 18, lines[0], size), size=size)
            for cont in lines[1:]:
                self.row(Cell(LM + 18, cont, size), size=size)
        return self

    def space(self, h: float) -> "Sheet":
        self.items.append(Item("space", h=h))
        return self

    def rule(self) -> "Sheet":
        self.items.append(Item("rule", h=8))
        return self

    def page_break(self) -> "Sheet":
        self.items.append(Item("break"))
        return self

    # -- layout: pages of (top y, item)
    def layout(self) -> list[list[tuple[float, Item]]]:
        pages: list[list[tuple[float, Item]]] = [[]]
        y = H - 36
        for it in self.items:
            if it.kind == "break":
                pages.append([])
                y = H - 48
                continue
            if y - it.h < BOTTOM and it.kind in ("row", "rule"):
                pages.append([])
                y = H - 48
            if it.kind == "space" and not pages[-1]:
                continue
            pages[-1].append((y, it))
            y -= it.h if it.kind != "band" else 64
        return pages

    # -- what a PDF reader gives back, page by page
    def golden_pages(self) -> list[str]:
        pages = self.layout()
        out = []
        for n, page in enumerate(pages, 1):
            lines: list[str] = []
            for _, it in page:
                if it.kind == "band":
                    lines += [c.text for c in it.cells if c.text]
                elif it.kind == "row":
                    cells = sorted((c for c in it.cells if c.text), key=_left_edge)
                    lines.append("   ".join(c.text for c in cells))
            lines.append(f"{FOOTER}   Page {n} of {len(pages)}")
            out.append("\n".join(lines))
        return out


def _left_edge(c: "Cell") -> float:
    """Where a cell starts. A PDF reader orders the pieces of a line by this, so a right-aligned amount comes after its label."""
    if c.align == "l":
        return c.x
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return c.x - stringWidth(c.text, "Helvetica-Bold" if c.bold else "Helvetica", c.size)


def _wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    font = "Helvetica-Bold" if bold else "Helvetica"
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if stringWidth(trial, font, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


# ------------------------------------------------------------------ renderers
def render_pdf(sheet: Sheet, path: Path, title: str) -> None:
    from reportlab.pdfgen import canvas

    pages = sheet.layout()
    c = canvas.Canvas(str(path), pagesize=(W, H), pageCompression=0, invariant=1)
    c.setTitle(title)
    c.setAuthor("Kaamezy demo (invented data)")
    c.setCreator("data/sample_docs.py")
    for n, page in enumerate(pages, 1):
        for y, it in page:
            _draw_item(c, y, it)
        c.setFont("Helvetica", 7.5)
        c.setFillColorRGB(*COLORS["grey"])
        c.drawString(LM, 28, FOOTER)
        c.drawRightString(RM, 28, f"Page {n} of {len(pages)}")
        c.showPage()
    c.save()


def _draw_item(c, y: float, it: Item) -> None:
    if it.kind == "band":
        c.setFillColorRGB(*COLORS["band"])
        c.rect(0, y - 64, W, 64, fill=1, stroke=0)
        name, sub = it.cells
        c.setFillColorRGB(*COLORS["white"])
        c.setFont("Helvetica-Bold", name.size)
        c.drawString(name.x, y - 28, name.text)
        if sub.text:
            c.setFont("Helvetica", sub.size)
            c.drawString(sub.x, y - 46, sub.text)
    elif it.kind == "rule":
        c.setStrokeColorRGB(*COLORS["rule"])
        c.line(LM, y - 3, RM, y - 3)
    elif it.kind == "row":
        for cell in it.cells:
            if not cell.text:
                continue
            c.setFillColorRGB(*COLORS[cell.color])
            c.setFont("Helvetica-Bold" if cell.bold else "Helvetica", cell.size)
            (c.drawRightString if cell.align == "r" else c.drawString)(cell.x, y - cell.size, cell.text)


def render_scan(sheet: Sheet, page: int, seed: int, tilt: float, blur: float, specks: int):
    """The page as a grayscale image: the same rows, drawn with Pillow, then tilted, blurred, speckled and unevenly lit."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
    import reportlab

    s = 2.5
    fonts = Path(reportlab.__file__).parent / "fonts"
    cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}

    def font(bold: bool, size: float):
        k = (bold, round(size * s))
        if k not in cache:
            cache[k] = ImageFont.truetype(str(fonts / ("VeraBd.ttf" if bold else "Vera.ttf")), round(size * s * 0.93))
        return cache[k]

    pages = sheet.layout()
    img = Image.new("RGB", (round(W * s), round(H * s)), (252, 252, 250))
    d = ImageDraw.Draw(img)

    def text(x, y, t, size, bold, align, color):
        f = font(bold, size)
        px = x * s - (d.textlength(t, font=f) if align == "r" else 0)
        d.text((px, (H - y) * s), t, font=f, fill=tuple(round(v * 255) for v in COLORS[color]), anchor="ls")

    for y, it in pages[page]:
        if it.kind == "band":
            d.rectangle((0, (H - y) * s, W * s, (H - y + 64) * s), fill=tuple(round(v * 255) for v in COLORS["band"]))
            name, sub = it.cells
            text(name.x, y - 28, name.text, name.size, True, "l", "white")
            if sub.text:
                text(sub.x, y - 46, sub.text, sub.size, False, "l", "white")
        elif it.kind == "rule":
            d.line((LM * s, (H - y + 3) * s, RM * s, (H - y + 3) * s), fill=(190, 196, 204), width=2)
        elif it.kind == "row":
            for cell in it.cells:
                if cell.text:
                    text(cell.x, y - cell.size, cell.text, cell.size, cell.bold, cell.align, cell.color)
    text(LM, 28, FOOTER, 7.5, False, "l", "grey")
    text(RM, 28, f"Page {page + 1} of {len(pages)}", 7.5, False, "r", "grey")

    rng = random.Random(seed)
    g = img.convert("L").rotate(tilt, resample=Image.BICUBIC, expand=True, fillcolor=250).filter(ImageFilter.GaussianBlur(blur))
    dd = ImageDraw.Draw(g)
    for _ in range(specks):
        x, y, r = rng.randrange(g.width), rng.randrange(g.height), rng.choice([1, 1, 2])
        dd.ellipse((x, y, x + r, y + r), fill=rng.randrange(130, 205))
    shade = Image.linear_gradient("L").resize(g.size).point(lambda v: 255 - v // 9)     # a little uneven light, brighter at the top
    return ImageChops.multiply(g, shade)


def render_scan_pdf(sheet: Sheet, path: Path, title: str, seed: int, tilt: float, blur: float, specks: int) -> None:
    """A PDF that holds only pictures of the pages: no text layer, so the reader has to OCR it."""
    from reportlab.pdfgen import canvas
    import io

    c = canvas.Canvas(str(path), pagesize=(W, H), pageCompression=0, invariant=1)
    c.setTitle(title)
    c.setAuthor("Kaamezy demo (invented data)")
    from reportlab.lib.utils import ImageReader

    for n in range(len(sheet.layout())):
        buf = io.BytesIO()
        render_scan(sheet, n, seed + n, tilt, blur, specks).save(buf, "JPEG", quality=70, optimize=True)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, W, H)
        c.showPage()
    c.save()


def render_photo(sheet: Sheet, path: Path, seed: int, tilt: float, blur: float, specks: int) -> None:
    render_scan(sheet, 0, seed, tilt, blur, specks).save(path, "JPEG", quality=72, optimize=True)


# ------------------------------------------------------------------ the people and their documents (all invented)
BASE = dict(
    insurer="DEMO SURAKSHA HEALTH INSURANCE CO. LTD.", insurer_sub="Demo Tower, Bandra Kurla Complex, Mumbai 400051 (invented address)",
    policy_no="SURK/H/2026/00045678", plan="Suraksha Family Floater Plus", holder="Mr. Rahul Sharma", period=("01/04/2025", "31/03/2026"), continuous=None,
    sum_insured=500000, premium=14755, room_rent="Up to Rs. 5,000 per day", copay="10% on every admissible claim", deductible="Rs. 5,000 per claim",
    initial_wait="30 days from the policy start date", ped_wait="36 months", spec_wait="24 months", cashless="Available at network hospitals",
    exclusions=["Cosmetic or plastic surgery unless needed after an accident.", "Dental treatment, unless it needs hospitalisation.",
                "Injury or disease caused by war, nuclear risk or hazardous sports.", "Treatment taken outside India.",
                "Any treatment where hospitalisation is not needed for more than 24 hours."],
    sub_limits=["Cataract surgery: Rs. 40,000 per eye", "Hernia, hysterectomy and piles: Rs. 60,000 per illness", "Knee replacement: 25% of the sum insured"],
    hospital="CITY CARE HOSPITAL", hospital_sub="12 MG Road, Pune 411001 (invented address)", patient="Rahul Sharma", age_sex="34/M", uhid="458213",
    bill_no="CCH/IP/26/009812", estimate_no="CCH/EST/26/0412", admit="12/03/2026", discharge="15/03/2026", doctor="Dr. Anil Mehta",
    diagnosis="Acute Appendicitis", procedure="Laparoscopic Appendectomy", condition="Stable, afebrile", advice="Review after 7 days. Avoid heavy lifting for 4 weeks.",
    items=[("Room Rent (Deluxe Room)", "3 days", 8000, 24000), ("ICU Nursing Charges", "", None, 9000), ("Consultant Visit Charges", "", None, 12000),
           ("Surgeon Fees", "", None, 40000), ("Operation Theatre Charges", "", None, 25000), ("Pharmacy and Medicines", "", None, 31500),
           ("Investigations (Lab, Ultrasound, CT scan)", "", None, 38500), ("Anaesthesia Charges", "", None, 10000), ("Miscellaneous Charges", "", None, 10000)],
    advance=20000, claim_ref="CLM/2026/00012345", claim_date="18/03/2026", claimed=200000, approved=132500, deductions=67500, letter_date="21/03/2026",
    status_line="Approved, payment pending", pay_date="28/03/2026", pay_mode="NEFT to your registered bank account",
    reasons=["Room rent above the policy limit of Rs. 5,000 per day: proportionate deduction.", "Deductible of Rs. 5,000 as per policy.",
             "Co-payment of 10 percent.", "Non-payable consumables."])


def person(**over) -> dict:
    p = deepcopy(BASE)
    p.update(over)
    return p


ANITA = person(
    policy_no="SURK/H/2026/00051122", plan="Suraksha Gold", holder="Mrs. Anita Verma", continuous="01/04/2022", sum_insured=1000000, premium=21400,
    room_rent="1% of sum insured per day", copay="Nil", deductible="Nil", sub_limits=["Cataract surgery: Rs. 40,000 per eye", "Knee replacement: 25% of the sum insured"],
    hospital="LIFELINE MULTISPECIALITY HOSPITAL", hospital_sub="45 Station Road, Indore 452001 (invented address)", patient="Anita Verma", age_sex="41/F", uhid="771204",
    bill_no="LMH/IP/26/004417", estimate_no="LMH/EST/26/0233", admit="18/03/2026", discharge="21/03/2026", doctor="Dr. Meena Joshi",
    diagnosis="Cholelithiasis (gall stones)", procedure="Laparoscopic Cholecystectomy", advice="Light diet for 2 weeks. Review after 10 days.",
    items=[("Room Rent (Single Room)", "3 days", 6000, 18000), ("Consultant Visit Charges", "", None, 12000), ("Surgeon Fees", "", None, 30000),
           ("Operation Theatre Charges", "", None, 20000), ("Pharmacy and Medicines", "", None, 24000), ("Investigations (Lab, Ultrasound)", "", None, 16000)],
    advance=15000, claim_ref="CLM/2026/00022222", claim_date="24/03/2026", claimed=120000, approved=120000, deductions=0, letter_date="27/03/2026",
    pay_date="05/04/2026", reasons=[])

NEHA = person(
    insurer="DEMO NAVJEEVAN CARE INSURANCE CO. LTD.", insurer_sub="Demo Plaza, Hitech City, Hyderabad 500081 (invented address)",
    policy_no="NAVJ/H/2026/00088550", plan="Navjeevan Care Basic", holder="Ms. Neha Singh", period=("01/06/2025", "31/05/2026"), continuous="01/06/2024",
    premium=12900, room_rent="Single private room", copay="Nil", deductible="Nil", initial_wait="15 days from the policy start date", ped_wait="12 months", spec_wait="12 months",
    sub_limits=[], hospital="GREEN VALLEY HOSPITAL", hospital_sub="7 Lake View Road, Bhopal 462001 (invented address)", patient="Neha Singh", age_sex="29/F", uhid="330918",
    bill_no="GVH/IP/26/002250", estimate_no="GVH/EST/26/0118", admit="10/02/2026", discharge="12/02/2026", doctor="Dr. Rakesh Nair",
    diagnosis="Dengue fever with thrombocytopenia", procedure="Medical management", condition="Recovered, platelets normal", advice="Rest for one week. Plenty of fluids.",
    items=[("Room Rent (Single Private Room)", "2 days", 7500, 15000), ("Consultant Visit Charges", "", None, 9000), ("Pharmacy and Medicines", "", None, 21000),
           ("Investigations (Blood tests, Platelet count)", "", None, 25000), ("Nursing Charges", "", None, 10000)],
    advance=10000, claim_ref="CLM/2026/00055555", claim_date="20/02/2026", claimed=80000, approved=80000, deductions=0, letter_date="03/03/2026",
    status_line="Settled, payment made", pay_date="10/03/2026", pay_mode="NEFT, already credited to your registered bank account", reasons=[])

IMRAN = person(
    policy_no="SURK/H/2026/00061234", holder="Mr. Imran Khan", hospital="SUNRISE HOSPITAL", hospital_sub="88 Ring Road, Nagpur 440001 (invented address)",
    patient="Imran Khan", age_sex="45/M", uhid="902341", bill_no="SRH/IP/26/001905", estimate_no="SRH/EST/26/0071", admit="02/03/2026", discharge="05/03/2026",
    doctor="Dr. S. Kulkarni", diagnosis="Lumbar disc prolapse", procedure="Microdiscectomy", condition="Stable, walking with support",
    items=[("Room Rent (Semi-private Room)", "3 days", 4000, 12000), ("Consultant Visit Charges", "", None, 8000), ("Surgeon Fees", "", None, 24000),
           ("Operation Theatre Charges", "", None, 16000), ("Pharmacy and Medicines", "", None, 18000), ("Investigations (MRI, X-ray)", "", None, 12000)],
    advance=10000, claim_ref="CLM/2026/00033333", claim_date="10/03/2026", claimed=90000, approved=90000, deductions=0, letter_date="18/03/2026",
    pay_date="26/03/2026", reasons=[])

SURESH = person(
    insurer="DEMO AROGYA PLUS INSURANCE CO. LTD.", insurer_sub="Demo House, Ashram Road, Ahmedabad 380009 (invented address)",
    policy_no="AROG/H/2026/00077001", plan="Arogya Plus Premier", holder="Mr. Suresh Patel", sum_insured=1000000, premium=33100, room_rent="1% of sum insured per day",
    ped_wait="36 months", hospital="PATEL MEMORIAL HOSPITAL", hospital_sub="3 Gandhi Path, Surat 395003 (invented address)", patient="Suresh Patel", age_sex="58/M",
    uhid="118802", bill_no="PMH/IP/26/003120", estimate_no="PMH/EST/26/0150", admit="25/02/2026", discharge="01/03/2026", doctor="Dr. Hardik Shah",
    diagnosis="Unstable angina", procedure="Coronary angioplasty with stent", condition="Stable",
    items=[("Room Rent (ICU and Private Room)", "5 days", 7000, 35000), ("Consultant Visit Charges", "", None, 10000), ("Surgeon Fees", "", None, 30000),
           ("Operation Theatre Charges", "", None, 15000), ("Pharmacy and Medicines (includes stent)", "", None, 40000), ("Investigations (Angiography, ECG)", "", None, 20000)],
    advance=15000, claim_ref="CLM/2026/00044444", claim_date="05/03/2026", claimed=150000, approved=150000, deductions=0, letter_date="12/03/2026",
    pay_date="22/03/2026", reasons=[])


# ------------------------------------------------------------------ the six kinds of document
def _letterhead(s: Sheet, p: dict, who: str) -> Sheet:
    return s.band(p[who], p[who + "_sub"])


def policy_sheet(p: dict) -> Sheet:
    s = Sheet().band(p["insurer"], p["insurer_sub"]).title("Policy Schedule and Certificate of Insurance")
    s.para("(Demo document with invented data.)", size=8.5)
    s.kv("Policy Number", p["policy_no"]).kv("Plan Name", p["plan"]).kv("Policy Holder", p["holder"])
    s.row(Cell(LM, f"Policy Period From: {p['period'][0]}"), Cell(320, f"Policy Period To: {p['period'][1]}"))
    if p["continuous"]:
        s.kv("Continuous Cover Since", p["continuous"])
    s.kv("Sum Insured", rs(p["sum_insured"])).kv("Premium", f"{rs(p['premium'])} (inclusive of GST)")
    s.h2("Key Coverage Terms")
    s.kv("Room Rent", p["room_rent"]).kv("Co-payment", p["copay"]).kv("Deductible", p["deductible"])
    s.kv("Initial Waiting Period", p["initial_wait"]).kv("Pre-existing Diseases Waiting Period", p["ped_wait"])
    s.kv("Specific Disease Waiting Period", p["spec_wait"]).kv("Cashless Facility", p["cashless"])
    s.page_break()
    s.h2("Exclusions").bullets(p["exclusions"])
    if p["sub_limits"]:
        s.h2("Sub-limits").bullets(p["sub_limits"])
    s.h2("Terms and Conditions")
    s.para("This document is a demo. The insurer alone decides a claim, and the amount it pays can be different from an estimate.")
    return s


def _item_rows(s: Sheet, p: dict) -> None:
    s.row(Cell(LM, "Sr", bold=True), Cell(70, "Description", bold=True), Cell(330, "Qty/Days", bold=True), Cell(470, "Rate (Rs.)", bold=True, align="r"), Cell(RM, "Amount (Rs.)", bold=True, align="r"))
    s.rule()
    for n, (desc, qty, rate, amount) in enumerate(p["items"], 1):
        cells = [Cell(LM, str(n)), Cell(70, desc)]
        if qty:
            cells.append(Cell(330, qty))
        if rate:
            cells.append(Cell(470, num(rate), align="r"))
        cells.append(Cell(RM, num(amount), align="r"))
        s.row(*cells)
    s.rule()


def bill_sheet(p: dict) -> Sheet:
    s = _letterhead(Sheet(), p, "hospital").title("FINAL BILL / TAX INVOICE")
    s.row(Cell(LM, f"Bill No: {p['bill_no']}"), Cell(380, f"UHID: {p['uhid']}"))
    s.row(Cell(LM, f"Patient Name: {p['patient']}"), Cell(380, f"Age/Sex: {p['age_sex']}"))
    s.row(Cell(LM, f"Date of Admission: {p['admit']}"), Cell(320, f"Date of Discharge: {p['discharge']}"))
    s.kv("Diagnosis", p["diagnosis"]).space(8)
    _item_rows(s, p)
    total = sum(i[3] for i in p["items"])
    s.row(Cell(380, "Grand Total", bold=True), Cell(RM, num(total), bold=True, align="r"))
    s.row(Cell(380, "Advance Paid"), Cell(RM, num(p["advance"]), align="r"))
    s.row(Cell(380, "Net Payable", bold=True), Cell(RM, num(total - p["advance"]), bold=True, align="r"))
    s.space(24).row(Cell(380, "Authorised Signatory"), size=8.5)
    return s


def estimate_sheet(p: dict) -> Sheet:
    s = _letterhead(Sheet(), p, "hospital").title("COST ESTIMATE FOR HOSPITALISATION (Pre-authorisation request)")
    s.row(Cell(LM, f"Estimate No: {p['estimate_no']}"), Cell(380, f"UHID: {p['uhid']}"))
    s.row(Cell(LM, f"Patient Name: {p['patient']}"), Cell(380, f"Age/Sex: {p['age_sex']}"))
    s.row(Cell(LM, f"Expected Date of Admission: {p['admit']}"), Cell(320, f"Expected Date of Discharge: {p['discharge']}"))
    s.kv("Provisional Diagnosis", p["diagnosis"]).kv("Procedure Planned", p["procedure"]).space(8)
    _item_rows(s, p)
    s.row(Cell(380, "Estimated Total", bold=True), Cell(RM, num(sum(i[3] for i in p["items"])), bold=True, align="r"))
    s.space(10).para("This is an estimate of the likely charges, not a bill. The final bill can be different.", size=8.5)
    return s


def discharge_sheet(p: dict) -> Sheet:
    s = _letterhead(Sheet(), p, "hospital").title("DISCHARGE SUMMARY")
    s.kv("Patient Name", p["patient"]).kv("UHID", p["uhid"]).kv("Date of Admission", p["admit"]).kv("Date of Discharge", p["discharge"]).kv("Treating Doctor", p["doctor"])
    s.h2("Clinical Summary").kv("Final Diagnosis", p["diagnosis"]).kv("Procedure Performed", p["procedure"]).kv("Condition at Discharge", p["condition"])
    s.h2("Advice on Discharge").para(p["advice"])
    s.space(30).row(Cell(380, "Treating Consultant"), size=8.5)
    return s


def ack_sheet(p: dict) -> Sheet:
    s = Sheet().band(p["insurer"], p["insurer_sub"]).title("Claim Acknowledgement")
    s.para("We have received your claim. Your claim has been registered.").space(6)
    s.kv("Claim Reference Number", p["claim_ref"]).kv("Policy Number", p["policy_no"]).kv("Patient Name", p["patient"])
    s.kv("Hospital", p["hospital"].title().replace("Multispeciality", "Multispeciality")).kv("Claimed Amount", rs(p["claimed"])).kv("Date of Claim", p["claim_date"])
    s.space(8).para("Your claim is under process. Documents received: bill, discharge summary.")
    return s


def approval_sheet(p: dict) -> Sheet:
    s = Sheet().band(p["insurer"], p["insurer_sub"]).title("Claim Approval Letter")
    s.row(Cell(RM, f"Date: {p['letter_date']}", align="r")).space(4)
    s.para(f"Dear {p['patient']},")
    if p["status_line"].startswith("Settled"):
        s.para("We are writing about your claim. The claim has been settled and the amount has been paid.")
    else:
        s.para("We are pleased to inform you that your claim has been approved. The details are below.")
    s.space(6).kv("Claim Reference Number", p["claim_ref"]).kv("Policy Number", p["policy_no"]).kv("Patient Name", p["patient"])
    s.kv("Hospital", p["hospital"].title()).kv("Claimed Amount", rs(p["claimed"]))
    s.kv("Total Deductions", rs(p["deductions"]) if p["deductions"] else "Nil").kv("Approved Amount", rs(p["approved"]))
    s.kv("Claim Status", p["status_line"])
    s.kv("Payment Date" if p["status_line"].startswith("Settled") else "Expected Payment Date", p["pay_date"]).kv("Mode of Payment", p["pay_mode"])
    if p["reasons"]:
        s.h2("Reasons for Deduction").bullets(p["reasons"])
    s.h2("Payment Details").para("Payment is made to the bank account registered with the policy, after the discharge voucher is received.")
    s.space(14).para("Yours sincerely,").para("Claims Department")
    return s


# ------------------------------------------------------------------ the cases
@dataclass
class DocFile:
    name: str                        # file name, e.g. "bill.pdf"
    doc_type: str                    # policy | hospital_bill | discharge_summary | claim_ack | claim_approval
    sheet: Sheet
    kind: str = "text"               # text (a PDF with a text layer) | scan (a PDF of pictures) | photo (a JPEG)
    seed: int = 0
    tilt: float = 0.5
    blur: float = 0.6
    specks: int = 600


@dataclass
class Case:
    id: str
    title: str
    story: str
    workflow: str
    files: list[DocFile]
    expect: dict                     # verdict, failed (codes that must fail), warned (codes that must warn), estimate_inr


def _set(p: dict, kinds: dict[str, str] | None = None) -> list[DocFile]:
    kinds = kinds or {}
    spec = [("policy", "policy", policy_sheet), ("bill", "hospital_bill", bill_sheet), ("discharge_summary", "discharge_summary", discharge_sheet),
            ("claim_acknowledgement", "claim_ack", ack_sheet), ("approval_letter", "claim_approval", approval_sheet)]
    files = []
    for n, (stem, dtype, make) in enumerate(spec):
        kind = kinds.get(stem, "text")
        ext = {"text": ".pdf", "scan": ".pdf", "photo": ".jpg"}[kind]
        name = stem + ("_scan" if kind == "scan" else "_photo" if kind == "photo" else "") + ext
        files.append(DocFile(name, dtype, make(p), kind, seed=100 + n, tilt=(0.9 if kind == "photo" else 0.4), blur=(0.75 if kind == "photo" else 0.55),
                             specks=(1400 if kind == "photo" else 500)))
    return files


def build_cases() -> list[Case]:
    rahul = person()
    tampered = person(approved=190000, deductions=10000)
    other_patient = person(patient="Rohit Verma")
    unknown = person(claim_ref="CLM/2026/00099999")
    settled_letter = deepcopy(NEHA)
    return [
        Case("rahul_approved", "A claim approved: the clean story",
             "Rahul Sharma was operated on for appendicitis. The bill is Rs. 2,00,000, the insurer approved Rs. 1,32,500 and the payout is still pending. Every document agrees and the insurer's record confirms the letter.",
             "approved_claim", _set(rahul), dict(verdict="consistent", estimate_inr=129037)),
        Case("anita_full_approval", "A renewed policy, approved in full",
             "Anita Verma's gall bladder was removed. Her policy is renewed (cover since 2022, so the waiting periods are over), the room is within its limit, and the insurer approved the whole Rs. 1,20,000.",
             "approved_claim", _set(ANITA), dict(verdict="consistent", estimate_inr=120000)),
        Case("rahul_scanned", "The same claim, from scans and a photo",
             "The policy and the acknowledgement are PDFs, but the bill and the discharge summary are scanned pages and the approval letter is a tilted, slightly blurred photo. The reader has to OCR them; the story must still end the same way.",
             "approved_claim", _set(rahul, {"bill": "scan", "discharge_summary": "scan", "approval_letter": "photo"}), dict(verdict="consistent", estimate_inr=129037, ocr=True)),
        Case("tampered_amount", "An approval letter with the amount changed",
             "The letter says Rs. 1,90,000 was approved (and was edited so its own sums still add up), but the insurer's record says Rs. 1,32,500. The letter does not match the insurer, so no advance can follow.",
             "approved_claim", _set(rahul)[:4] + [_set(tampered)[4]], dict(verdict="inconsistent", failed=["INSURER_RECORD_DIFFERS"], warned=["APPROVAL_ABOVE_ESTIMATE"])),
        Case("different_patient", "A bill that belongs to someone else",
             "The hospital bill is in another person's name. The documents contradict each other, so the set is refused until a person looks at it.",
             "approved_claim", [_set(rahul)[0], _set(other_patient)[1]] + _set(rahul)[2:], dict(verdict="inconsistent", failed=["PATIENT_NAME_DIFFERS"])),
        Case("unknown_claim", "A claim the insurer does not know",
             "Everything agrees, but the claim reference is not in the insurer's record, so the letter cannot be confirmed. It is not called false: a person must check it before any financing.",
             "approved_claim", _set(rahul)[:3] + [_set(unknown)[3], _set(unknown)[4]], dict(verdict="needs_review", warned=["INSURER_RECORD_NOT_FOUND"])),
        Case("settled_claim", "A claim that is already paid",
             "Neha Singh's insurer has already paid this claim (the letter and the insurer's record both say so). There is nothing left to advance against.",
             "approved_claim", _set(settled_letter), dict(verdict="inconsistent", failed=["LETTER_SAYS_SETTLED"])),
        Case("forged_after_rejection", "An approval letter for a rejected claim",
             "The insurer rejected Imran Khan's claim. A letter that says Rs. 90,000 was approved does not match the insurer's record.",
             "approved_claim", _set(IMRAN), dict(verdict="inconsistent", failed=["INSURER_RECORD_DIFFERS"])),
        Case("approval_not_issued", "An approval that the insurer has not issued yet",
             "Suresh Patel's claim is still under review at the insurer, but a letter says it was approved. The letter does not match the insurer's record.",
             "approved_claim", _set(SURESH), dict(verdict="inconsistent", failed=["INSURER_RECORD_DIFFERS"])),
        Case("preapproval_estimate", "Before the bill: a policy and a hospital's cost estimate",
             "Rahul Sharma is about to be admitted. The hospital has sent an estimate for the pre-authorisation. With the policy this is enough to work out what the policy would probably pay.",
             "preapproval", [_set(rahul)[0], DocFile("cost_estimate.pdf", "hospital_bill", estimate_sheet(rahul))], dict(verdict="consistent", estimate_inr=129037)),
    ]


# ------------------------------------------------------------------ writing
def golden_text(f: DocFile) -> list[str]:
    """What a PDF reader returns for this file's pages (a scan has no text layer: its text is what the OCR must find)."""
    return f.sheet.golden_pages()


def manifest_of(cases: list[Case]) -> dict:
    """The manifest. Depends only on the content (no file sizes or hashes of binaries), so it is the same on every machine."""
    out = []
    for c in cases:
        out.append({"id": c.id, "title": c.title, "story": c.story, "workflow": c.workflow, "expect": c.expect,
                    "files": [{"name": f.name, "doc_type": f.doc_type, "kind": f.kind, "pages": len(f.sheet.layout()),
                               "text_sha256": hashlib.sha256("\n\f\n".join(golden_text(f)).encode("utf-8")).hexdigest()} for f in c.files]})
    return {"note": "DEMO DATA. Every name, number and amount in these files is invented. The insurer's record is config/insurer_registry_sim.yaml.", "cases": out}


def write_all(out: Path = OUT_DIR) -> dict:
    cases = build_cases()
    for c in cases:
        d = out / c.id
        d.mkdir(parents=True, exist_ok=True)
        for f in c.files:
            path = d / f.name
            if f.kind == "text":
                render_pdf(f.sheet, path, f"{f.doc_type} (demo)")
            elif f.kind == "scan":
                render_scan_pdf(f.sheet, path, f"{f.doc_type} scan (demo)", f.seed, f.tilt, f.blur, f.specks)
            else:
                render_photo(f.sheet, path, f.seed, f.tilt, f.blur, f.specks)
    m = manifest_of(cases)
    (out / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    m = write_all(args.out)
    n = sum(len(c["files"]) for c in m["cases"])
    size = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file())
    print(f"wrote {n} files for {len(m['cases'])} cases to {args.out} ({size // 1024} KB)")


if __name__ == "__main__":
    main()
