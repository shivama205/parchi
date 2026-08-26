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

from parchi.drugs import mention_from_reading
from parchi.models import Confidence, Document, DocumentKind, LabResult

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


LAB_RESULTS = (
    # HbA1c rising across four measurements at two labs — AC-8 shape.
    LabResult(id="L1", document_id="LR1", doc_date=date(2025, 7, 8),
              analyte_raw="HbA1c", analyte="hba1c", value=7.1, unit_raw="%",
              canonical_value=7.1, canonical_unit="%", lab_name="SRL",
              ref_low=4.0, ref_high=5.7),
    LabResult(id="L2", document_id="LR2", doc_date=date(2026, 1, 12),
              analyte_raw="HBA1C (Glycosylated Hb)", analyte="hba1c", value=7.6,
              unit_raw="%", canonical_value=7.6, canonical_unit="%",
              lab_name="Dr Lal PathLabs", ref_low=4.0, ref_high=5.6),
    LabResult(id="L3", document_id="LR3", doc_date=date(2026, 3, 8),
              analyte_raw="HbA1c", analyte="hba1c", value=8.0, unit_raw="%",
              canonical_value=8.0, canonical_unit="%", lab_name="SRL",
              ref_low=4.0, ref_high=5.7),
    LabResult(id="L4", document_id="LR4", doc_date=date(2026, 6, 18),
              analyte_raw="HbA1c", analyte="hba1c", value=8.4, unit_raw="%",
              canonical_value=8.4, canonical_unit="%",
              lab_name="Dr Lal PathLabs", ref_low=4.0, ref_high=5.6),

    # Creatinine measured twice inside the window at different labs.
    LabResult(id="L5", document_id="LR4", doc_date=date(2026, 6, 18),
              analyte_raw="Creatinine, Serum", analyte="creatinine", value=1.1,
              unit_raw="mg/dL", canonical_value=1.1, canonical_unit="mg/dL",
              lab_name="Dr Lal PathLabs", ref_low=0.7, ref_high=1.3),
    LabResult(id="L6", document_id="LR5", doc_date=date(2026, 7, 2),
              analyte_raw="S. Creatinine", analyte="creatinine", value=1.2,
              unit_raw="mg/dL", canonical_value=1.2, canonical_unit="mg/dL",
              lab_name="SRL", ref_low=0.6, ref_high=1.2),
)
