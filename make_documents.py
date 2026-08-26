#!/usr/bin/env python3
"""Generate the printed half of the fixture corpus.

    python make_documents.py

EVERY DOCUMENT PRODUCED BY THIS SCRIPT IS CONSTRUCTED (SR-9). The patient,
prescribers, clinics and laboratories are invented, and the content is drawn
directly from parchi/fixtures.py so the images and the test scenario cannot
drift apart.

WHY THIS EXISTS. AC-1 requires that a dozen mixed documents, uploaded unordered,
produce a correctly date-ordered timeline. The real handwriting corpus cannot
demonstrate that: the images are cropped marketing scans, most carry no legible
date at all, and the one that does reads 2016 — a decade off the scenario. So
the printed half is generated here, with dates on the page where a real
prescription puts them.

The division of labour in the demo follows from that. Printed documents prove
J1 and AC-1 — bulk upload, classification, the timeline. The MIRAGE handwriting
proves J2 and AC-6, which is the harder and more interesting claim.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

from PIL import Image, ImageDraw, ImageFont

from parchi.fixtures import DOCUMENTS, MENTIONS, PRINTED_LABS
OUT = pathlib.Path(__file__).parent / "fixtures" / "printed"

W, H = 1240, 1754          # A4 at 150 dpi
INK = (26, 28, 38)
FAINT = (120, 120, 128)
RULE = (176, 176, 184)
PAPER = (253, 253, 250)

_FONTS = {
    "serif": "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "sans": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "mono": "/System/Library/Fonts/Supplemental/Courier New.ttf",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Invented clinics, one per prescriber, so a document looks like it came from
#: somewhere rather than from a template.
LETTERHEAD = {
    "Dr Rao": ("CITY HEART CLINIC",
               "Dr A. Rao  ·  MD, DM (Cardiology)  ·  Reg. MH-41822",
               "2nd Floor, Dharampeth Plaza, Nagpur 440010  ·  0712 244 8180"),
    "Dr Iyer": ("NAGPUR DIABETES CENTRE",
                "Dr S. Iyer  ·  MD, DNB (Endocrinology)  ·  Reg. MH-33907",
                "Ramdaspeth, Nagpur 440010  ·  0712 253 1147"),
    "Dr Menon": ("SITABULDI POLYCLINIC",
                 "Dr K. Menon  ·  MBBS, MD (Gen. Med.)  ·  Reg. MH-52310",
                 "Central Avenue, Sitabuldi, Nagpur 440012  ·  0712 276 4402"),
}
LAB_HEAD = {
    "SRL": ("SRL DIAGNOSTICS", "NABL accredited  ·  Nagpur collection centre"),
    "Dr Lal PathLabs": ("DR LAL PATHLABS",
                        "NABL accredited  ·  Wardha Road, Nagpur"),
    "Metropolis": ("METROPOLIS HEALTHCARE",
                   "NABL accredited  ·  Reports in IFCC units"),
}


def font(kind: str, size: int):
    try:
        return ImageFont.truetype(_FONTS[kind], size)
    except OSError:
        return ImageFont.load_default()


def d(value: date) -> str:
    return f"{value.day:02d}/{value.month:02d}/{value.year}"


def long_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


class Sheet:
    def __init__(self):
        self.img = Image.new("RGB", (W, H), PAPER)
        self.dr = ImageDraw.Draw(self.img)
        self.y = 0

    def text(self, x, s, *, kind="sans", size=30, fill=INK, dy=0):
        self.y += dy
        self.dr.text((x, self.y), s, font=font(kind, size), fill=fill)
        return self

    def line(self, y=None, x0=70, x1=W - 70, fill=RULE, width=2):
        yy = self.y if y is None else y
        self.dr.line([(x0, yy), (x1, yy)], fill=fill, width=width)
        return self

    def save(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / name
        self.img.save(path, "PNG", optimize=True)
        return path


def letterhead(sheet: Sheet, prescriber: str, doc_date: date, doc_id: str):
    name, creds, addr = LETTERHEAD[prescriber]
    sheet.y = 70
    sheet.text(70, name, kind="serif", size=46)
    sheet.text(70, creds, size=24, fill=FAINT, dy=58)
    sheet.text(70, addr, size=22, fill=FAINT, dy=32)
    sheet.y += 44
    sheet.line()
    sheet.y += 26
    sheet.text(70, f"Date: {d(doc_date)}", kind="mono", size=28)
    sheet.dr.text((W - 300, sheet.y), f"OPD No. {doc_id}", font=font("mono", 24),
                  fill=FAINT)
    sheet.y += 44
    sheet.text(70, "Patient: Ramesh (M / 68)   Wt: 74 kg   BP: 138/84",
               kind="mono", size=26)
    sheet.y += 46
    sheet.line()
    sheet.y += 34


def prescription(doc, mentions) -> pathlib.Path:
    sheet = Sheet()
    letterhead(sheet, doc.prescriber, doc.doc_date, doc.id)
    # "Rx" spelled out: Times New Roman has no U+211E and renders a tofu box.
    sheet.text(70, "Rx", kind="serif", size=48)
    sheet.y += 74

    for i, m in enumerate(mentions, start=1):
        sheet.text(96, f"{i}.", kind="mono", size=28, fill=FAINT)
        sheet.text(150, m.brand_text, kind="serif", size=34)
        bits = []
        if m.dose_pattern:
            bits.append(m.dose_pattern)
        if m.duration_days:
            bits.append(f"x {m.duration_days} days")
        if m.instruction:
            bits.append(m.instruction)
        if bits:
            sheet.dr.text((640, sheet.y + 4), "   ·   ".join(bits),
                          font=font("mono", 26), fill=INK)
        sheet.y += 62

    sheet.y += 40
    if doc.follow_up_after_days:
        sheet.line()
        sheet.y += 26
        days = doc.follow_up_after_days
        # Weeks only when it divides cleanly. "9 weeks" for 68 days would put a
        # wrong number on the page, and the extractor is entitled to trust it.
        interval = (f"{days // 7} weeks" if days % 7 == 0 else f"{days} days")
        sheet.text(70, f"Review after {interval}  —  {d(doc.follow_up_date)}",
                   kind="serif", size=32)
        sheet.y += 56

    sheet.y = H - 210
    sheet.line()
    sheet.y += 24
    sheet.text(W - 420, "Signature", kind="serif", size=30, fill=FAINT)
    sheet.y = H - 96
    sheet.text(70, "CONSTRUCTED DOCUMENT — NOT A REAL PRESCRIPTION",
               kind="mono", size=20, fill=FAINT)
    return sheet.save(f"{doc.id}-prescription.png")


def lab_report(doc_id: str, lab: str, when: date, rows) -> pathlib.Path:
    sheet = Sheet()
    name, sub = LAB_HEAD[lab]
    sheet.y = 70
    sheet.text(70, name, kind="serif", size=46)
    sheet.text(70, sub, size=24, fill=FAINT, dy=58)
    sheet.y += 44
    sheet.line()
    sheet.y += 26
    sheet.text(70, f"Reported: {d(when)}", kind="mono", size=28)
    sheet.dr.text((W - 340, sheet.y), f"Report {doc_id}", font=font("mono", 24),
                  fill=FAINT)
    sheet.y += 44
    sheet.text(70, "Patient: Ramesh (M / 68)   Ref. by: Dr Iyer",
               kind="mono", size=26)
    sheet.y += 50
    sheet.line()
    sheet.y += 30

    headers = ("INVESTIGATION", "RESULT", "UNITS", "REFERENCE")
    for x, head in zip((96, 600, 790, 960), headers):
        sheet.dr.text((x, sheet.y), head, font=font("mono", 22), fill=FAINT)
    sheet.y += 40
    sheet.line()
    sheet.y += 22

    for r in rows:
        sheet.dr.text((96, sheet.y), r["label"], font=font("serif", 32), fill=INK)
        sheet.dr.text((600, sheet.y), r["value"], font=font("mono", 32), fill=INK)
        sheet.dr.text((790, sheet.y), r["unit"], font=font("mono", 28), fill=INK)
        sheet.dr.text((960, sheet.y), r["ref"], font=font("mono", 26), fill=FAINT)
        sheet.y += 58

    sheet.y += 30
    sheet.line()
    sheet.y += 24
    sheet.text(70, "Reference ranges are method- and laboratory-specific.",
               size=22, fill=FAINT)
    sheet.y = H - 96
    sheet.text(70, "CONSTRUCTED DOCUMENT — NOT A REAL LABORATORY REPORT",
               kind="mono", size=20, fill=FAINT)
    return sheet.save(f"{doc_id}-lab.png")


def main() -> int:
    written = []

    by_doc: dict[str, list] = {}
    for m in MENTIONS:
        by_doc.setdefault(m.document_id, []).append(m)

    for doc in DOCUMENTS:
        mentions = by_doc.get(doc.id, [])
        # RX9 is the handwritten slip. It is not generated: real handwriting from
        # the MIRAGE corpus does that job, and a printed imitation of a
        # hard-to-read scrawl would prove nothing.
        if not mentions or doc.id == "RX9":
            continue
        written.append(prescription(doc, mentions))

    lab_docs: dict[str, list] = {}
    for r in PRINTED_LABS:
        lab_docs.setdefault(r["document_id"], []).append(r)

    for doc_id, results in sorted(lab_docs.items()):
        first = results[0]
        # Printed values and printed ranges, in the report's own units. Reading
        # the normalised range off LabResult put a % range on an mmol/mol report.
        rows = [{
            "label": r["label"],
            "value": f"{r['value']:g}",
            "unit": r["unit"],
            "ref": f"{r['low']:g} - {r['high']:g}",
        } for r in results]
        written.append(lab_report(doc_id, first["lab"], first["doc_date"], rows))

    print(f"{len(written)} documents -> {OUT}")
    for path in written:
        kb = path.stat().st_size // 1024
        print(f"  {path.name:34} {kb:>4} KB")
    print("\nAll constructed. No real patient data (SR-9).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
