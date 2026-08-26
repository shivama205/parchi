"""Brand normalisation tests — PRD §7, §6.4, SR-5, SR-6."""

from __future__ import annotations

from datetime import date

import pytest

from parchi.drugs import (
    BRAND_TABLE,
    RELEASE_MODIFIERS,
    effective_confidence,
    mention_from_reading,
    resolve,
)
from parchi.models import Confidence, MedicationMention


# -- longest match first ---------------------------------------------------

def test_longest_match_beats_shorter_prefix():
    """"Glycomet GP" must never silently resolve to plain metformin. §7."""
    r = resolve("Glycomet GP 2")
    assert r.molecules == ("metformin", "glimepiride")
    assert r.matched_brand == "Glycomet GP"


def test_plain_brand_still_resolves_to_its_own_composition():
    assert resolve("Glycomet 500").molecules == ("metformin",)


@pytest.mark.parametrize(
    "written,expected",
    [
        ("Telma H", ("telmisartan", "hydrochlorothiazide")),
        ("Telma AM 40", ("telmisartan", "amlodipine")),
        ("Ecosprin AV 75", ("aspirin", "atorvastatin")),
        ("Pan-D", ("pantoprazole", "domperidone")),
        ("Gabapin NT", ("gabapentin", "nortriptyline")),
        ("Urimax D", ("tamsulosin", "dutasteride")),
        ("Zoryl M 2", ("glimepiride", "metformin")),
        ("Galvus Met 50", ("vildagliptin", "metformin")),
        ("Glycomet Trio 2", ("metformin", "glimepiride", "pioglitazone")),
        ("Rosuvas F 10", ("rosuvastatin", "fenofibrate")),
    ],
)
def test_composition_changing_suffix_is_never_dropped(written, expected):
    """A suffix that adds a molecule must survive matching.

    Dropping one of these is the silent under-read §7 forbids — it would put a
    shorter, wrong list on the medication sheet.
    """
    r = resolve(written)
    assert r.molecules == expected
    assert len(r.molecules) > 1
    assert not r.modifier_dropped


def test_release_modifier_may_be_dropped_because_it_changes_nothing():
    r = resolve("Glycomet SR 500")
    assert r.molecules == ("metformin",)
    assert r.modifier_dropped is True


def test_release_modifier_set_excludes_composition_changing_suffixes():
    for suffix in ("gp", "av", "h", "am", "d", "m", "nt", "trio", "met", "f"):
        assert suffix not in RELEASE_MODIFIERS


# -- tail stripping --------------------------------------------------------

@pytest.mark.parametrize(
    "written", ["Telma 40", "Telma 40mg", "Telma 40 mg", "Telma 40 OD", "Telma 40mg BD"]
)
def test_trailing_strength_and_frequency_tokens_stripped(written):
    assert resolve(written).molecules == ("telmisartan",)


def test_generic_molecule_names_are_recognised():
    """Teaching-hospital prescribers write generics. §7."""
    assert resolve("metformin 500").molecules == ("metformin",)
    assert resolve("Telmisartan 40 mg").molecules == ("telmisartan",)


def test_unresolvable_brand_yields_no_molecules():
    """Precursor to SR-5 — an empty tuple cannot become medication state."""
    r = resolve("Ltrsn 5")
    assert r.molecules == ()
    assert r.matched_brand is None
    assert not r.resolved


def test_empty_input_is_handled():
    assert resolve("").molecules == ()
    assert resolve("   ").molecules == ()


# -- confusion sets --------------------------------------------------------

def test_molecularly_divergent_confusion_demotes_confidence():
    """Glycomet vs Glycomet GP differ by a sulfonylurea. §7."""
    r = resolve("Glycomet 500")
    assert r.demote_confidence is True
    assert "Glycomet GP" in r.confusable_with
    assert effective_confidence(Confidence.HIGH, r) is Confidence.MEDIUM
    assert effective_confidence(Confidence.MEDIUM, r) is Confidence.LOW


def test_molecularly_identical_confusion_does_not_demote():
    """Telma and Telmikind are both plain telmisartan, so confusing them cannot
    produce a wrong drug and carries no penalty. See CONFUSION_SETS."""
    r = resolve("Telma 40")
    assert r.confusable_with == ("Telmikind",)
    assert r.demote_confidence is False
    assert effective_confidence(Confidence.HIGH, r) is Confidence.HIGH


def test_demotion_never_promotes():
    r = resolve("Glycomet 500")
    assert effective_confidence(Confidence.LOW, r) is Confidence.LOW


# -- strength assignment: §6.4 / SR-6 -------------------------------------

def test_single_molecule_strength_is_assigned():
    assert resolve("Telma 40").strengths_mg == (40.0,)


def test_combination_with_one_number_yields_no_strength():
    """"Augmentin 625" is amoxicillin 500 + clavulanate 125. One number against
    two molecules tells us nothing about either. Silence is correct. §6.4."""
    r = resolve("Augmentin 625")
    assert r.molecules == ("amoxicillin", "clavulanic acid")
    assert r.strengths_mg == ()


def test_combination_with_matching_count_still_yields_no_strength():
    """"Glycomet GP 1/500" is glimepiride 1 + metformin 500, while the table
    lists (metformin, glimepiride). Two numbers against two molecules passes
    §6.4's count test and would attribute both backwards."""
    r = resolve("Glycomet GP 1/500")
    assert len(r.molecules) == 2
    assert r.strengths_mg == ()


def test_gram_strengths_convert_to_mg():
    assert resolve("Augmentin 1g").strengths_mg == ()          # still a combination
    assert resolve("paracetamol 1g").strengths_mg == (1000.0,)


def test_non_mg_units_yield_no_strength():
    assert resolve("Telma 40 IU").strengths_mg == ()


# -- the one supported construction path ----------------------------------

def test_mention_from_reading_applies_normalisation_and_confidence():
    m = mention_from_reading(
        id="m1",
        document_id="RX1",
        doc_date=date(2026, 8, 1),
        brand_text="Glycomet GP 2",
        prescriber="Dr Iyer",
        confidence=Confidence.HIGH,
    )
    assert m.molecules == ("metformin", "glimepiride")
    assert m.strengths_mg == ()
    assert m.confidence is Confidence.MEDIUM      # divergent confusion demotes
    assert m.is_usable is True


def test_mention_rejects_mismatched_strength_count():
    """SR-6 made structural — the model refuses to hold a partial attribution."""
    with pytest.raises(ValueError, match="SR-6"):
        MedicationMention(
            id="m1",
            document_id="RX1",
            doc_date=date(2026, 8, 1),
            brand_text="Augmentin 625",
            molecules=("amoxicillin", "clavulanic acid"),
            strengths_mg=(625.0,),
        )


def test_every_table_entry_has_at_least_one_molecule():
    for brand, molecules in BRAND_TABLE.items():
        assert molecules, f"{brand} maps to no molecule"
        assert all(m == m.lower() for m in molecules), brand


# -- regressions -----------------------------------------------------------

@pytest.mark.parametrize("written", ["Telma CT 40", "Ecosprin XY", "Glycomet QR 500"])
def test_unlisted_combination_variant_does_not_resolve_to_its_base(written):
    """A variant we do not know must become a question, not a shorter list.

    Regression: prefix matching consumed "Telma" out of "Telma CT" and dropped
    the rest in silence. Telma CT is telmisartan + chlorthalidone; resolving it
    to plain telmisartan would delete a diuretic from the medication list
    without a word to anybody.
    """
    r = resolve(written)
    assert r.molecules == ()
    assert not r.resolved


@pytest.mark.parametrize(
    "written", ["Tab Telma 40", "Tab. Telma 40", "Cap Pan-D", "Syp Crocin 250"]
)
def test_dosage_form_prefix_is_stripped(written):
    assert resolve(written).resolved


# -- real-world annotation formats ----------------------------------------
# Every string below is a verbatim ground-truth annotation from the MIRAGE
# subset (see fetch_fixtures.py). They caught a bug the constructed fixtures
# missed entirely: form words appear in front, in the middle and at the end,
# and stripping only a leading one left every such reading unresolvable.

@pytest.mark.parametrize(
    "written,expected",
    [
        ("DOLO TAB 650MG", ("paracetamol",)),                    # form in middle
        ("AUGMENTIN 625MG TAB", ("amoxicillin", "clavulanic acid")),  # form last
        ("Tab. Telma 40 mg OD", ("telmisartan",)),               # form first
        ("PAN 40MG TAB", ("pantoprazole",)),
        ("TELMA 40MG TAB", ("telmisartan",)),
        ("TELMA-AM TAB", ("telmisartan", "amlodipine")),
        ("JANUMET 50/1000MG TAB", ("sitagliptin", "metformin")),
        ("AZEE 500MG TAB", ("azithromycin",)),
        ("THYRONORM 62.5MCG TAB", ("levothyroxine",)),
        ("SHELCAL 500MG TAB", ("calcium carbonate", "cholecalciferol")),
        ("RANTAC SYP", ("ranitidine",)),
        ("CROCIN SUSP", ("paracetamol",)),
        ("LASIX TAB", ("furosemide",)),
        ("ECOSPRIN AV 75MG TAB", ("aspirin", "atorvastatin")),
    ],
)
def test_real_annotation_formats_resolve(written, expected):
    assert resolve(written).molecules == expected


@pytest.mark.parametrize(
    "written", ["TELMA 40MG TAB", "DOLO TAB 650MG", "Tab Telma 40", "TELMA TAB 40MG"]
)
def test_form_word_stripped_from_any_position(written):
    assert resolve(written).resolved


def test_relaxation_never_buys_a_match_by_dropping_a_molecule():
    """The progressive relaxation added for form words must not weaken §7.

    MONTAIR FX is montelukast + fexofenadine and Telma CT is telmisartan +
    chlorthalidone. Neither is in the table, and neither may fall back to a
    base product we do happen to know.
    """
    for written in ("MONTAIR FX TAB", "Telma CT 40", "GLYCOMET GP TRIO XR"):
        assert resolve(written).molecules == (), written


def test_strength_still_suppressed_for_a_real_combination():
    """JANUMET 50/1000MG: two numbers, two molecules, written order unknown."""
    r = resolve("JANUMET 50/1000MG TAB")
    assert len(r.molecules) == 2
    assert r.strengths_mg == ()
