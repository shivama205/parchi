"""Lab normalisation tests — PRD §8.

The conversion tests are pinned to well-known clinical threshold pairs
(7.0 mmol/L glucose = 126 mg/dL, 5.2 mmol/L cholesterol = 200 mg/dL,
64 mmol/mol HbA1c = 8.0%, 75 nmol/L vitamin D = 30 ng/mL). A wrong factor
would not land on those numbers, so they function as an independent check on
the arithmetic rather than a restatement of it.
"""

from __future__ import annotations

from datetime import date

import pytest

from parchi.labs import (
    CANONICAL_UNITS,
    _C,
    canonical_analyte,
    conversion_for,
    normalise_reading,
)

DAY = date(2026, 3, 8)


def _norm(label, value, unit, **kw):
    return normalise_reading(
        id="X", document_id="D", doc_date=DAY, analyte_raw=label,
        value=value, unit_raw=unit, **kw,
    )


# -- analyte labels --------------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Hb", "hemoglobin"),
        ("HAEMOGLOBIN (Hb)", "hemoglobin"),
        ("Haemoglobin", "hemoglobin"),
        ("HbA1c", "hba1c"),
        ("HBA1C (Glycosylated Hb)", "hba1c"),
        ("Glycated Haemoglobin", "hba1c"),
        ("S. Creatinine", "creatinine"),
        ("Creatinine, Serum", "creatinine"),
        ("FBS", "glucose_fasting"),
        ("Blood Sugar Post Prandial", "glucose_pp"),
        ("25(OH)D", "vitamin_d"),
        ("Vitamin D (25-OH)", "vitamin_d"),
        ("TSH - Ultrasensitive", "tsh"),
    ],
)
def test_printed_labels_map_to_canonical_keys(label, expected):
    """§8 — case- and punctuation-insensitive, via a synonym table."""
    assert canonical_analyte(label) == expected


def test_matching_is_on_the_whole_label_not_a_substring():
    """"HDL Cholesterol" must not be caught by the "Cholesterol" entry.

    Same failure mode as the brand prefix bug in drugs.py: a substring match
    silently returns the wrong analyte, and a trend would then mix HDL into
    total cholesterol.
    """
    assert canonical_analyte("HDL Cholesterol") == "hdl"
    assert canonical_analyte("LDL Cholesterol") == "ldl"
    assert canonical_analyte("Total Cholesterol") == "total_cholesterol"
    assert canonical_analyte("Cholesterol") == "total_cholesterol"


def test_an_unrecognised_label_yields_no_analyte():
    assert canonical_analyte("Zorblax Index") is None
    assert canonical_analyte("") is None


# -- conversions pinned to known clinical pairs ---------------------------

@pytest.mark.parametrize(
    "analyte,value,unit,expected",
    [
        ("hemoglobin", 135, "g/L", 13.5),
        ("creatinine", 97, "µmol/L", 1.1),
        ("creatinine", 88.4, "µmol/L", 1.0),
        ("glucose_fasting", 7.0, "mmol/L", 126.11),      # diabetes threshold
        # The 200 mg/dL convention uses the rounded x18; the precise molar
        # factor 18.016 gives 199.98. The arithmetic is the reference here.
        ("glucose_pp", 11.1, "mmol/L", 199.98),
        ("hba1c", 64, "mmol/mol", 8.01),                 # standard table pair
        ("hba1c", 48, "mmol/mol", 6.54),                 # 48 = 6.5%
        ("total_cholesterol", 5.2, "mmol/L", 201.08),    # ~200 mg/dL
        ("triglycerides", 1.7, "mmol/L", 150.57),        # 150 mg/dL threshold
        ("vitamin_d", 75, "nmol/L", 30.05),              # 30 ng/mL sufficiency
        ("tsh", 2.1, "mIU/L", 2.1),                      # dimensional identity
    ],
)
def test_conversion_lands_on_the_known_pair(analyte, value, unit, expected):
    conversion = conversion_for(analyte, unit)
    assert conversion is not None, f"no conversion for {analyte} {unit}"
    assert round(conversion.to_canonical(value), 2) == pytest.approx(expected, abs=0.02)


def test_hba1c_conversion_is_affine_not_a_scale_factor():
    """The one conversion where multiplying by a factor would be badly wrong.

    NGSP% = 0.09148 x IFCC + 2.152, so zero does not map to zero.
    """
    conversion = conversion_for("hba1c", "mmol/mol")
    assert round(conversion.to_canonical(0), 3) == 2.152
    # A pure scale factor would satisfy f(2x) == 2*f(x). This must not.
    assert conversion.to_canonical(80) != pytest.approx(
        2 * conversion.to_canonical(40)
    )


@pytest.mark.parametrize("unit", ["µmol/L", "μmol/L", "umol/L", "uMol/L", " umol/l "])
def test_micro_sign_variants_all_resolve(unit):
    """MICRO SIGN, GREEK MU and a plain 'u' all appear on Indian reports."""
    assert conversion_for("creatinine", unit) is not None


# -- refusal rather than guessing -----------------------------------------

def test_an_unrecognised_analyte_is_refused():
    out = _norm("Zorblax Index", 1.0, "mg/dL")
    assert not out.ok
    assert "not recognised" in out.problem


def test_an_unverified_unit_is_refused():
    """No factor, no conversion. Silence is correct (§8)."""
    out = _norm("HbA1c", 8.0, "furlongs")
    assert not out.ok
    assert "no verified conversion" in out.problem


def test_a_plausible_but_unlisted_unit_is_still_refused():
    """g/dL for creatinine is a real unit, just not one we have a factor for."""
    assert not _norm("S. Creatinine", 1.1, "g/L").ok


# -- reference ranges ------------------------------------------------------

def test_reference_range_is_converted_with_the_same_function():
    """Otherwise the printed range means nothing against the canonical value."""
    out = _norm("Glycosylated Haemoglobin", 64, "mmol/mol",
                lab_name="Metropolis", ref_low=20, ref_high=42)
    assert out.ok
    assert out.result.canonical_value == 8.01
    assert out.result.ref_low == 3.98      # 0.09148*20 + 2.152
    assert out.result.ref_high == 5.99     # 0.09148*42 + 2.152


def test_a_missing_reference_range_stays_missing():
    out = _norm("HbA1c", 7.4, "%")
    assert out.ok
    assert out.result.ref_low is None and out.result.ref_high is None


def test_the_raw_value_and_unit_are_never_discarded():
    """§8 — store both the raw and canonical value; never discard the raw."""
    out = _norm("Creatinine, Serum", 97, "µmol/L")
    assert out.result.value == 97
    assert out.result.unit_raw == "µmol/L"
    assert out.result.analyte_raw == "Creatinine, Serum"
    assert out.result.canonical_value == 1.1
    assert out.result.canonical_unit == "mg/dL"


# -- structural: §8's citation requirement --------------------------------

def test_every_conversion_cites_a_source():
    """§8 requires every factor verified against a reference and cited in the
    code. This makes that an automated check rather than a good intention."""
    assert _C
    for (analyte, unit), conversion in _C.items():
        assert conversion.source.strip(), f"{analyte}/{unit} has no source"
        assert len(conversion.source) > 12, f"{analyte}/{unit} source is too thin"


def test_every_analyte_has_an_identity_conversion_for_its_canonical_unit():
    from parchi.labs import _norm_unit

    for analyte, canonical in CANONICAL_UNITS.items():
        conversion = conversion_for(analyte, canonical)
        assert conversion is not None, f"{analyte} cannot accept {canonical}"
        assert conversion.to_canonical(7.77) == 7.77


def test_every_registered_analyte_appears_in_canonical_units():
    for analyte, _unit in _C:
        assert analyte in CANONICAL_UNITS


def test_the_prd_section_8_table_is_fully_covered():
    """Every analyte and alternate unit listed in PRD §8."""
    required = [
        ("hemoglobin", "g/L"),
        ("creatinine", "µmol/L"),
        ("glucose_fasting", "mmol/L"),
        ("glucose_pp", "mmol/L"),
        ("hba1c", "mmol/mol"),
        ("total_cholesterol", "mmol/L"),
        ("hdl", "mmol/L"),
        ("ldl", "mmol/L"),
        ("triglycerides", "mmol/L"),
        ("vitamin_d", "nmol/L"),
        ("tsh", "mIU/L"),
    ]
    missing = [pair for pair in required if conversion_for(*pair) is None]
    assert missing == []
