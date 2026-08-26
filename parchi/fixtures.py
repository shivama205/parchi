"""Constructed scenario — PRD §13.

EVERY VALUE HERE IS INVENTED. No real patient data (SR-9). The prescriber names,
facilities, dates and readings were written for this test suite.

One patient, 68, diabetes + hypertension, three prescribers, 14 months. Contains
each case §13 requires:

  * a fixed-dose combination hiding a duplicate molecule — Ecosprin AV
    (aspirin + atorvastatin) from the cardiologist while the physician writes
    Storvas (atorvastatin). This is the demo case, AC-3.
  * a comprehensive rewrite that drops a drug — Dr Rao's Jun 2026 script
    re-lists 4 of the 5 molecules he had written and omits torsemide (AC-5).
  * an add-on slip that drops nothing — Dr Iyer's Jul 2026 script lists only
    the new glimepiride and must not imply anything was stopped (AC-4).
  * a completed antibiotic course — Augmentin, 5 days, May 2026.
  * a low-confidence handwritten entry — Dr Menon's Aug 2026 slip (AC-6).
"""

from __future__ import annotations

from datetime import date

from .drugs import mention_from_reading
from .labs import normalise_reading
from .models import Confidence, Document, DocumentKind

AS_OF = date(2026, 8, 26)

RAO = "Dr Rao"            # cardiologist
IYER = "Dr Iyer"          # diabetologist
MENON = "Dr Menon"        # general physician


def _rx(doc_id: str, doc_date: date, prescriber: str, **kw) -> Document:
    return Document(
        id=doc_id,
        patient_id="p-fixture-1",
        kind=DocumentKind.PRESCRIPTION,
        doc_date=doc_date,
        prescriber=prescriber,
        **kw,
    )


DOCUMENTS = (
    _rx("RX1", date(2025, 7, 10), RAO, facility="City Heart Clinic"),
    _rx("RX2", date(2026, 1, 15), RAO, facility="City Heart Clinic"),
    _rx("RX3", date(2026, 6, 20), RAO, facility="City Heart Clinic"),
    _rx("RX4", date(2025, 8, 5), IYER, facility="Nagpur Diabetes Centre"),
    _rx("RX5", date(2026, 3, 10), IYER, facility="Nagpur Diabetes Centre"),
    _rx("RX6", date(2026, 7, 5), IYER, facility="Nagpur Diabetes Centre"),
    _rx("RX7", date(2026, 5, 2), MENON, facility="Sitabuldi Polyclinic"),
    _rx("RX8", date(2026, 7, 20), MENON, facility="Sitabuldi Polyclinic"),
    _rx("RX9", date(2026, 8, 1), MENON, facility="Sitabuldi Polyclinic",
        confidence=Confidence.LOW),
)

DOCUMENT_DATES = {d.id: d.doc_date for d in DOCUMENTS}


def _m(mid: str, doc_id: str, brand: str, prescriber: str, **kw):
    return mention_from_reading(
        id=mid,
        document_id=doc_id,
        doc_date=DOCUMENT_DATES[doc_id],
        brand_text=brand,
        prescriber=prescriber,
        **kw,
    )


MENTIONS = (
    # -- Dr Rao, cardiologist ------------------------------------------------
    _m("m01", "RX1", "Telma 40", RAO, dose_pattern="1-0-0"),
    _m("m02", "RX1", "Ecosprin AV 75", RAO, dose_pattern="0-0-1"),
    _m("m03", "RX1", "Metolar 25", RAO, dose_pattern="1-0-0"),

    _m("m04", "RX2", "Telma 40", RAO, dose_pattern="1-0-0"),
    _m("m05", "RX2", "Ecosprin AV 75", RAO, dose_pattern="0-0-1"),
    _m("m06", "RX2", "Metolar 25", RAO, dose_pattern="1-0-0"),
    _m("m07", "RX2", "Dytor 10", RAO, dose_pattern="1-0-0"),

    # Comprehensive rewrite: re-lists 4 of the 5 molecules Rao had written,
    # omitting torsemide. Also raises metoprolol from 25 mg to 50 mg.
    _m("m08", "RX3", "Telma 40", RAO, dose_pattern="1-0-0"),
    _m("m09", "RX3", "Ecosprin AV 75", RAO, dose_pattern="0-0-1"),
    _m("m10", "RX3", "Metolar 50", RAO, dose_pattern="1-0-0"),

    # -- Dr Iyer, diabetologist ---------------------------------------------
    _m("m11", "RX4", "Glycomet 500", IYER, dose_pattern="1-0-1"),
    _m("m12", "RX4", "Januvia 100", IYER, dose_pattern="1-0-0"),

    _m("m13", "RX5", "Glycomet 500", IYER, dose_pattern="1-0-1"),
    _m("m14", "RX5", "Januvia 100", IYER, dose_pattern="1-0-0"),
    _m("m15", "RX5", "Shelcal 500", IYER, dose_pattern="0-0-1"),

    # Add-on slip: the new drug only. Nothing here implies anything stopped.
    _m("m16", "RX6", "Zoryl 2", IYER, dose_pattern="1-0-0"),

    # -- Dr Menon, physician ------------------------------------------------
    _m("m17", "RX7", "Augmentin 625", MENON, dose_pattern="1-0-1",
       duration_days=5, instruction="x 5 days"),

    # The other half of the duplicate: plain atorvastatin, while Rao's
    # Ecosprin AV is already delivering it.
    _m("m18", "RX8", "Storvas 10", MENON, dose_pattern="0-0-1"),

    # Handwritten slip. Extraction is LOW per PRD §9.1.
    _m("m19", "RX9", "Gabapin NT 100", MENON, dose_pattern="0-0-1",
       confidence=Confidence.LOW),
    _m("m20", "RX9", "Ltrsn 5", MENON, dose_pattern="1-0-0",
       confidence=Confidence.LOW),
)


def _lab(lid, doc_id, day, label, value, unit, lab, low, high):
    """Build a LabResult through the §8 normaliser, so raw units are exercised."""
    out = normalise_reading(
        id=lid, document_id=doc_id, doc_date=day, analyte_raw=label,
        value=value, unit_raw=unit, lab_name=lab, ref_low=low, ref_high=high,
    )
    assert out.ok, out.problem
    return out.result


# HbA1c rising across four measurements at three labs — and Metropolis reports
# IFCC mmol/mol while the others report NGSP %. AC-8 needs exactly this: two
# units landing on one normalised axis, each point keeping its own printed
# range. 64 mmol/mol normalises to 8.01%.
LAB_RESULTS = (
    _lab("L1", "LR1", date(2025, 7, 8), "HbA1c", 7.1, "%",
         "SRL", 4.0, 5.7),
    _lab("L2", "LR2", date(2026, 1, 12), "HBA1C (Glycosylated Hb)", 7.6, "%",
         "Dr Lal PathLabs", 4.0, 5.6),
    _lab("L3", "LR3", date(2026, 3, 8), "Glycosylated Haemoglobin", 64, "mmol/mol",
         "Metropolis", 20, 42),
    _lab("L4", "LR4", date(2026, 6, 18), "HbA1c", 8.4, "%",
         "Dr Lal PathLabs", 4.0, 5.6),

    # Creatinine measured twice inside the window at different labs, one
    # reporting SI units. 97 µmol/L normalises to 1.1 mg/dL.
    _lab("L5", "LR4", date(2026, 6, 18), "Creatinine, Serum", 97, "µmol/L",
         "Dr Lal PathLabs", 62, 115),
    _lab("L6", "LR5", date(2026, 7, 2), "S. Creatinine", 1.2, "mg/dL",
         "SRL", 0.6, 1.2),
)
