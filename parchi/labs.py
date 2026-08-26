"""Lab normalisation — PRD §8.

Two jobs: map a printed analyte label to a canonical key, and convert a value to
one canonical unit per analyte so a trend can be drawn on a single axis.

EVERY CONVERSION FACTOR BELOW IS CITED. §8 requires it, and a silently wrong
factor would bend a trend line — the kind of error that looks like data rather
than like a bug. Where no verified factor exists for a unit we have seen, the
reading is REFUSED rather than converted, exactly as an unresolvable brand name
is refused in drugs.py. Silence is correct.

Reference ranges are per report and are converted alongside the value they
belong to. §8 is explicit: never apply one lab's range to another lab's value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from .models import Confidence, LabResult

# --------------------------------------------------------------------------
# Canonical units — one per analyte
# --------------------------------------------------------------------------

CANONICAL_UNITS: dict[str, str] = {
    "hemoglobin": "g/dL",
    "creatinine": "mg/dL",
    "glucose_fasting": "mg/dL",
    "glucose_pp": "mg/dL",
    "hba1c": "%",
    "total_cholesterol": "mg/dL",
    "hdl": "mg/dL",
    "ldl": "mg/dL",
    "triglycerides": "mg/dL",
    "vitamin_d": "ng/mL",
    "tsh": "µIU/mL",
}

# --------------------------------------------------------------------------
# Analyte label synonyms
# --------------------------------------------------------------------------
# Indian lab reports print the same analyte a dozen ways. Matching is on the
# whole normalised label, never on a substring: "HDL Cholesterol" must not be
# caught by the "cholesterol" entry. An unrecognised label yields no canonical
# analyte and the reading is refused rather than guessed at.

ANALYTE_SYNONYMS: dict[str, str] = {}


def _register(canonical: str, *labels: str) -> None:
    for label in labels:
        ANALYTE_SYNONYMS[_norm_label(label)] = canonical


def _norm_label(text: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return " ".join(cleaned.split())


def _norm_unit(text: str) -> str:
    """Lowercase, spaces removed, micro sign folded to 'u'."""
    return (
        (text or "")
        .strip()
        .lower()
        .replace("µ", "u")      # MICRO SIGN
        .replace("μ", "u")      # GREEK SMALL LETTER MU
        .replace(" ", "")
    )


_register("hemoglobin", "Hb", "Hgb", "Haemoglobin", "Hemoglobin",
          "HAEMOGLOBIN (Hb)", "Hemoglobin (Hb)", "Haemoglobin Hb",
          "Blood Haemoglobin")
_register("hba1c", "HbA1c", "HbA1C", "Hb A1c", "A1c", "HBA1C (Glycosylated Hb)",
          "Glycosylated Haemoglobin", "Glycated Haemoglobin",
          "Glycosylated Hemoglobin", "Glycated Hemoglobin",
          "Glycosylated Haemoglobin (HbA1c)", "Hemoglobin A1c")
_register("creatinine", "Creatinine", "S. Creatinine", "Serum Creatinine",
          "Creatinine, Serum", "Creatinine Serum", "Sr. Creatinine")
_register("glucose_fasting", "FBS", "FPG", "Fasting Blood Sugar",
          "Fasting Glucose", "Glucose Fasting", "Glucose, Fasting",
          "Blood Sugar Fasting", "Fasting Plasma Glucose", "Sugar Fasting")
_register("glucose_pp", "PPBS", "Post Prandial Blood Sugar", "PP Glucose",
          "Postprandial Glucose", "Glucose PP", "Glucose, Post Prandial",
          "Blood Sugar Post Prandial", "2 Hour Post Prandial Glucose")
_register("total_cholesterol", "Total Cholesterol", "Cholesterol Total",
          "Cholesterol", "T. Cholesterol", "S. Cholesterol",
          "Cholesterol - Total", "Serum Cholesterol")
_register("hdl", "HDL", "HDL Cholesterol", "HDL-C", "Cholesterol HDL",
          "HDL Cholesterol - Direct")
_register("ldl", "LDL", "LDL Cholesterol", "LDL-C", "Cholesterol LDL",
          "LDL Cholesterol - Direct")
_register("triglycerides", "Triglycerides", "Triglyceride", "TG",
          "S. Triglycerides", "Serum Triglycerides")
_register("vitamin_d", "Vitamin D", "Vit D", "Vitamin D3",
          "25 OH Vitamin D", "25-Hydroxy Vitamin D", "25(OH)D",
          "25-OH Vitamin D", "Calcidiol", "Vitamin D (25-OH)",
          "Vitamin D Total")
_register("tsh", "TSH", "S. TSH", "Thyroid Stimulating Hormone",
          "TSH - Ultrasensitive", "Serum TSH")


def canonical_analyte(label: str) -> str | None:
    """Canonical key for a printed analyte label, or None if unrecognised."""
    return ANALYTE_SYNONYMS.get(_norm_label(label))


# --------------------------------------------------------------------------
# Unit conversions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UnitConversion:
    """A conversion into an analyte's canonical unit, with its provenance."""

    to_canonical: Callable[[float], float]
    #: Where the factor comes from. §8 requires this to be present in the code.
    source: str


def _identity(source: str) -> UnitConversion:
    return UnitConversion(lambda v: v, source)


def _scale(factor: float, source: str) -> UnitConversion:
    return UnitConversion(lambda v, f=factor: v * f, source)


# Citations used below:
#
# [UKKA] UK Kidney Association, "Appendix I — Laboratory Conversion Factors".
#        https://www.ukkidney.org/sites/renal.org/files/Appen-I.pdf
#        Prints: Cholesterol mg/dl = mmol/L x 38.6; Creatinine mg/dl =
#        umol/L x 0.011; Glucose mg/dl = mmol/L x 18; Albumin g/dl = g/L x 0.1.
# [NGSP] NGSP, "IFCC Standardization" — the HbA1c master equation
#        NGSP = [0.09148 x IFCC] + 2.152.  https://ngsp.org/ifcc.asp
# [MOLAR] Derived from the compound's molar mass. Given for the factors where
#        [UKKA] rounds, and for the two analytes it does not list. Stated so the
#        arithmetic can be checked rather than trusted.

_C: dict[tuple[str, str], UnitConversion] = {
    # -- haemoglobin: g/L -> g/dL is a decimal shift, same basis as [UKKA]'s
    #    albumin line.
    ("hemoglobin", "g/dl"): _identity("canonical unit"),
    ("hemoglobin", "g/l"): _scale(0.1, "[UKKA] g/dl = g/L x 0.1"),

    # -- creatinine: [UKKA] rounds to 0.011. Molar mass 113.12 g/mol gives
    #    1 umol/L = 0.011312 mg/dL, i.e. the standard divisor 88.4. The precise
    #    value is used because a 2.8% error would visibly tilt a trend line.
    ("creatinine", "mg/dl"): _identity("canonical unit"),
    ("creatinine", "umol/l"): _scale(
        1 / 88.4, "[MOLAR] creatinine 113.12 g/mol -> /88.4; [UKKA] rounds to x0.011"
    ),

    # -- glucose: [UKKA] gives x18. Molar mass 180.156 g/mol gives x18.016.
    ("glucose_fasting", "mg/dl"): _identity("canonical unit"),
    ("glucose_fasting", "mmol/l"): _scale(
        18.016, "[MOLAR] glucose 180.156 g/mol; [UKKA] rounds to x18"
    ),
    ("glucose_pp", "mg/dl"): _identity("canonical unit"),
    ("glucose_pp", "mmol/l"): _scale(
        18.016, "[MOLAR] glucose 180.156 g/mol; [UKKA] rounds to x18"
    ),

    # -- HbA1c: affine, not a scale factor. This is the one conversion where a
    #    naive multiplication would be badly wrong.
    ("hba1c", "%"): _identity("canonical unit (NGSP %)"),
    ("hba1c", "mmol/mol"): UnitConversion(
        lambda v: 0.09148 * v + 2.152,
        "[NGSP] master equation NGSP% = 0.09148 x IFCC + 2.152",
    ),

    # -- lipids: cholesterol 386.65 g/mol -> x38.67; [UKKA] rounds to 38.6.
    ("total_cholesterol", "mg/dl"): _identity("canonical unit"),
    ("total_cholesterol", "mmol/l"): _scale(
        38.67, "[MOLAR] cholesterol 386.65 g/mol; [UKKA] rounds to x38.6"
    ),
    ("hdl", "mg/dl"): _identity("canonical unit"),
    ("hdl", "mmol/l"): _scale(
        38.67, "[MOLAR] cholesterol 386.65 g/mol; [UKKA] rounds to x38.6"
    ),
    ("ldl", "mg/dl"): _identity("canonical unit"),
    ("ldl", "mmol/l"): _scale(
        38.67, "[MOLAR] cholesterol 386.65 g/mol; [UKKA] rounds to x38.6"
    ),

    # -- triglycerides: not in [UKKA]. Mixed triglycerides average 885.7 g/mol,
    #    giving the standard x88.57.
    ("triglycerides", "mg/dl"): _identity("canonical unit"),
    ("triglycerides", "mmol/l"): _scale(
        88.57, "[MOLAR] mixed triglycerides 885.7 g/mol -> x88.57"
    ),

    # -- 25-OH vitamin D: not in [UKKA]. Calcidiol 400.65 g/mol, giving the
    #    standard divisor 2.496.
    ("vitamin_d", "ng/ml"): _identity("canonical unit"),
    ("vitamin_d", "nmol/l"): _scale(
        1 / 2.496, "[MOLAR] calcidiol 400.65 g/mol -> /2.496"
    ),

    # -- TSH: µIU/mL and mIU/L are the same quantity, not a conversion.
    #    1 µIU/mL = 1 mIU/L exactly.
    ("tsh", "uiu/ml"): _identity("canonical unit"),
    ("tsh", "miu/l"): _identity("dimensional identity: 1 uIU/mL = 1 mIU/L"),
    ("tsh", "uu/ml"): _identity("dimensional identity: 1 uU/mL = 1 mIU/L"),
}


def conversion_for(analyte: str, unit_raw: str) -> UnitConversion | None:
    """The verified conversion for this analyte and unit, or None."""
    return _C.get((analyte, _norm_unit(unit_raw)))


#: How many decimals a canonical value keeps. Labs print one or two; carrying
#: the full float would put "8.00688" on a caregiver's screen.
_DECIMALS = 2


@dataclass(frozen=True)
class LabNormalisation:
    """Outcome of normalising one printed lab line."""

    result: LabResult | None = None
    #: Why the reading was refused, in plain language. None on success.
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


def normalise_reading(
    *,
    id: str,
    document_id: str,
    doc_date: date,
    analyte_raw: str,
    value: float,
    unit_raw: str,
    lab_name: str | None = None,
    ref_low: float | None = None,
    ref_high: float | None = None,
    confidence: Confidence = Confidence.HIGH,
) -> LabNormalisation:
    """Normalise one printed lab line into a LabResult.

    Refuses rather than guesses. An unrecognised analyte label or an unverified
    unit yields a problem string for the caller to surface as a question — the
    same posture drugs.py takes toward an unlisted brand.

    The printed reference range is converted with the same function as the
    value, so the range still means something against the canonical number. It
    stays attached to this reading only (§8).
    """
    analyte = canonical_analyte(analyte_raw)
    if analyte is None:
        return LabNormalisation(
            problem=f'analyte label "{analyte_raw}" is not recognised'
        )

    conversion = conversion_for(analyte, unit_raw)
    if conversion is None:
        return LabNormalisation(
            problem=(
                f'no verified conversion from "{unit_raw}" to '
                f"{CANONICAL_UNITS[analyte]} for {analyte}"
            )
        )

    def _conv(v: float | None) -> float | None:
        return None if v is None else round(conversion.to_canonical(v), _DECIMALS)

    return LabNormalisation(
        result=LabResult(
            id=id,
            document_id=document_id,
            doc_date=doc_date,
            analyte_raw=analyte_raw,
            analyte=analyte,
            value=value,
            unit_raw=unit_raw,
            canonical_value=round(conversion.to_canonical(value), _DECIMALS),
            canonical_unit=CANONICAL_UNITS[analyte],
            lab_name=lab_name,
            # Converted with the same function, so the range and the value stay
            # comparable. Never reused across reports.
            ref_low=_conv(ref_low),
            ref_high=_conv(ref_high),
            confidence=confidence,
        )
    )
