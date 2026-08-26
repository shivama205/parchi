"""Reconciliation and safety tests — PRD §6, §11, §15.

Test names are keyed to the acceptance criteria (AC-*) and safety invariants
(SR-*) they discharge.
"""

from __future__ import annotations

import pathlib
from datetime import date, timedelta

import pytest

from parchi.drugs import mention_from_reading
from parchi.models import (
    ACTIVE_LIKE,
    ClinicalClaimError,
    Confidence,
    Finding,
    FindingKind,
    LabResult,
    MedStatus,
    MedicationState,
    clinical_claim_phrases_in,
)
from parchi.reconcile import (
    COMPREHENSIVE_REWRITE_THRESHOLD,
    MIN_PRIOR_FOR_REWRITE_TEST,
    STALE_AFTER_DAYS,
    reconcile,
)

from parchi.fixtures import AS_OF, IYER, LAB_RESULTS, MENON, MENTIONS, RAO

SRC = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def result():
    return reconcile(MENTIONS, as_of=AS_OF, lab_results=LAB_RESULTS)


def _m(mid, doc_id, day, brand, prescriber, **kw):
    return mention_from_reading(
        id=mid,
        document_id=doc_id,
        doc_date=day,
        brand_text=brand,
        prescriber=prescriber,
        **kw,
    )


# ==========================================================================
# AC-2 — the medication list
# ==========================================================================

def test_ac2_medication_list_matches_fixture_ground_truth(result):
    """Derived from 9 documents across 3 prescribers."""
    got = {s.molecule: s.status for s in result.states}
    assert got == {
        # Dr Rao's cardiac list, confirmed by his Jun 2026 rewrite
        "telmisartan": MedStatus.ACTIVE,
        "aspirin": MedStatus.ACTIVE,
        "metoprolol": MedStatus.ACTIVE,
        # dropped by that same rewrite
        "torsemide": MedStatus.POSSIBLY_STOPPED,
        # Dr Iyer's diabetes list, confirmed by his Mar 2026 rewrite
        "metformin": MedStatus.ACTIVE,
        "sitagliptin": MedStatus.ACTIVE,
        "calcium carbonate": MedStatus.ACTIVE,
        "cholecalciferol": MedStatus.ACTIVE,
        # added by the Jul 2026 slip, never part of a rewrite
        "glimepiride": MedStatus.LIKELY_ACTIVE,
        # Dr Menon writes no comprehensive rewrites
        "atorvastatin": MedStatus.LIKELY_ACTIVE,
        "amoxicillin": MedStatus.COURSE_COMPLETED,
        "clavulanic acid": MedStatus.COURSE_COMPLETED,
        # handwritten, unconfirmed
        "gabapentin": MedStatus.UNCERTAIN,
        "nortriptyline": MedStatus.UNCERTAIN,
    }


def test_ac2_spans_at_least_six_documents_and_three_prescribers(result):
    docs = {mid for s in result.states for mid in s.evidence_mention_ids}
    assert len(docs) >= 6
    assert {RAO, IYER, MENON} <= {p for s in result.states for p in s.prescribers}


def test_current_strength_and_brand_come_from_the_latest_usable_mention(result):
    metoprolol = result.state_for("metoprolol")
    assert metoprolol.current_strength_mg == 50.0
    assert metoprolol.current_brand_text == "Metolar 50"


# ==========================================================================
# AC-3 — the demo case: a molecule hidden inside a combination
# ==========================================================================

def test_ac3_molecule_in_a_combination_and_prescribed_separately_is_surfaced(result):
    dupes = result.findings_of(FindingKind.DUPLICATE_MOLECULE)
    assert len(dupes) == 1
    f = dupes[0]
    assert f.molecules == ("atorvastatin",)
    # Both products must be named — a finding that says "a duplicate exists"
    # without naming the products is unactionable in a seven-minute consultation.
    assert "Ecosprin AV 75" in f.summary and "Storvas 10" in f.summary
    assert "Ecosprin AV 75" in f.question and "Storvas 10" in f.question
    assert RAO in f.summary and MENON in f.summary


def test_ac3_duplicate_across_prescribers_is_ask_soon(result):
    from parchi.models import Attention

    assert result.findings_of(FindingKind.DUPLICATE_MOLECULE)[0].attention is (
        Attention.ASK_SOON
    )


def test_duplicate_within_one_prescriber_is_ask_next_visit():
    from parchi.models import Attention

    day = AS_OF - timedelta(days=20)
    r = reconcile(
        [
            _m("a", "D1", day, "Ecosprin AV 75", RAO),
            _m("b", "D1", day, "Storvas 10", RAO),
        ],
        as_of=AS_OF,
    )
    dupes = r.findings_of(FindingKind.DUPLICATE_MOLECULE)
    assert len(dupes) == 1
    assert dupes[0].attention is Attention.ASK_NEXT_VISIT


def test_a_product_replaced_long_ago_does_not_create_a_false_duplicate():
    """Metformin under two tuples across a brand switch is not two products.

    §6.5 says bias conservative: a caregiver who learns to distrust these stops
    reading them.
    """
    r = reconcile(
        [
            _m("a", "D1", AS_OF - timedelta(days=400), "Glycomet 500", IYER),
            _m("b", "D2", AS_OF - timedelta(days=20), "Janumet 50", IYER),
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.DUPLICATE_MOLECULE) == ()


# ==========================================================================
# AC-4 / AC-5 — the central judgement (§6.1)
# ==========================================================================

def test_ac4_omission_by_a_different_prescriber_is_not_flagged_as_stopped(result):
    """Dr Rao's Jun 2026 rewrite lists no metformin. He never managed it.

    §6.1: a cardiologist's script omitting the diabetologist's metformin tells
    us nothing.
    """
    assert result.state_for("metformin").status is MedStatus.ACTIVE
    dropped = {
        m for f in result.findings_of(FindingKind.DROPPED_WITHOUT_STOP)
        for m in f.molecules
    }
    assert "metformin" not in dropped
    assert "sitagliptin" not in dropped


def test_ac4_an_add_on_slip_drops_nothing():
    """A script listing only the new drug is not a comprehensive rewrite."""
    r = reconcile(
        [
            _m("a", "D1", date(2026, 1, 10), "Glycomet 500", IYER),
            _m("b", "D1", date(2026, 1, 10), "Januvia 100", IYER),
            _m("c", "D1", date(2026, 1, 10), "Shelcal 500", IYER),
            _m("d", "D2", date(2026, 7, 5), "Zoryl 2", IYER),   # add-on only
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.DROPPED_WITHOUT_STOP) == ()
    assert all(s.status is not MedStatus.POSSIBLY_STOPPED for s in r.states)


def test_ac5_omission_by_the_same_prescriber_in_a_rewrite_is_flagged(result):
    assert result.state_for("torsemide").status is MedStatus.POSSIBLY_STOPPED
    findings = result.findings_of(FindingKind.DROPPED_WITHOUT_STOP)
    assert len(findings) == 1
    assert findings[0].molecules == ("torsemide",)


def test_ac5_the_flag_is_a_question_not_a_conclusion(result):
    """§6.1: even then it produces a question, never a conclusion."""
    f = result.findings_of(FindingKind.DROPPED_WITHOUT_STOP)[0]
    assert f.question.endswith("?")
    assert "Was torsemide discontinued" in f.question
    # The summary states what is on paper and shows the arithmetic behind it.
    assert "re-lists 4 of the 5 molecules" in f.summary
    state = result.state_for("torsemide")
    assert state.open_question and state.open_question.endswith("?")


# ==========================================================================
# §6.2 — comprehensive rewrite detection
# ==========================================================================

def _rewrite_probe(prior_brands, later_brands, *, as_of=AS_OF):
    mentions = [
        _m(f"p{i}", "D1", date(2026, 1, 10), b, RAO)
        for i, b in enumerate(prior_brands)
    ] + [
        _m(f"l{i}", "D2", date(2026, 6, 10), b, RAO)
        for i, b in enumerate(later_brands)
    ]
    return reconcile(mentions, as_of=as_of)


def test_rewrite_requires_a_minimum_number_of_prior_molecules():
    """With one prior molecule there is nothing to judge against. §6.2."""
    assert MIN_PRIOR_FOR_REWRITE_TEST == 2
    r = _rewrite_probe(["Telma 40"], ["Amlong 5"])
    assert r.findings_of(FindingKind.DROPPED_WITHOUT_STOP) == ()
    assert r.state_for("telmisartan").status is MedStatus.LIKELY_ACTIVE


def test_rewrite_threshold_boundary_is_inclusive():
    """Exactly 0.6 counts as a comprehensive rewrite. §6.2."""
    assert COMPREHENSIVE_REWRITE_THRESHOLD == 0.6
    prior = ["Telma 40", "Amlong 5", "Metolar 25", "Ecosprin 75", "Dytor 10"]
    # 3 of 5 re-listed == 0.60
    r = _rewrite_probe(prior, ["Telma 40", "Amlong 5", "Metolar 25"])
    dropped = {m for f in r.findings_of(FindingKind.DROPPED_WITHOUT_STOP)
               for m in f.molecules}
    assert dropped == {"aspirin", "torsemide"}


def test_just_below_the_threshold_is_not_a_rewrite():
    prior = ["Telma 40", "Amlong 5", "Metolar 25", "Ecosprin 75", "Dytor 10"]
    # 2 of 5 re-listed == 0.40
    r = _rewrite_probe(prior, ["Telma 40", "Amlong 5"])
    assert r.findings_of(FindingKind.DROPPED_WITHOUT_STOP) == ()


def test_an_unattributed_document_cannot_establish_same_prescriber():
    """"Same prescriber" requires identity. §6.1."""
    r = reconcile(
        [
            _m("a", "D1", date(2026, 1, 10), "Telma 40", None),
            _m("b", "D1", date(2026, 1, 10), "Amlong 5", None),
            _m("c", "D1", date(2026, 1, 10), "Dytor 10", None),
            _m("d", "D2", date(2026, 6, 10), "Telma 40", None),
            _m("e", "D2", date(2026, 6, 10), "Amlong 5", None),
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.DROPPED_WITHOUT_STOP) == ()


# ==========================================================================
# §6.3 — status derivation
# ==========================================================================

def test_a_completed_course_closes_without_a_question(result):
    """The one case we can close. §6.3.2."""
    for molecule in ("amoxicillin", "clavulanic acid"):
        state = result.state_for(molecule)
        assert state.status is MedStatus.COURSE_COMPLETED
        assert state.open_question is None


def test_a_course_still_running_is_not_completed():
    r = reconcile(
        [_m("a", "D1", AS_OF - timedelta(days=2), "Augmentin 625", MENON,
            duration_days=5)],
        as_of=AS_OF,
    )
    assert r.state_for("amoxicillin").status is MedStatus.LIKELY_ACTIVE


def test_a_stale_open_ended_prescription_attaches_a_question():
    r = reconcile(
        [_m("a", "D1", AS_OF - timedelta(days=STALE_AFTER_DAYS + 1), "Telma 40", RAO)],
        as_of=AS_OF,
    )
    state = r.state_for("telmisartan")
    assert state.status is MedStatus.LIKELY_ACTIVE
    assert state.open_question and "still being taken" in state.open_question


def test_a_recent_prescription_has_no_staleness_question():
    r = reconcile(
        [_m("a", "D1", AS_OF - timedelta(days=30), "Telma 40", RAO)], as_of=AS_OF
    )
    assert r.state_for("telmisartan").open_question is None


def test_a_stated_course_is_not_treated_as_stale():
    """An end date was written, so there is nothing to ask about."""
    r = reconcile(
        [_m("a", "D1", AS_OF - timedelta(days=STALE_AFTER_DAYS + 1), "Azee 500",
            MENON, duration_days=3)],
        as_of=AS_OF,
    )
    assert r.state_for("azithromycin").status is MedStatus.COURSE_COMPLETED


# ==========================================================================
# AC-6 / AC-7 — the confirmation loop (§4 J2)
# ==========================================================================

def _handwritten(confirmed: bool):
    return reconcile(
        [
            _m("h1", "RX-HW", AS_OF - timedelta(days=5), "Telma 40", MENON,
               confidence=Confidence.LOW, user_confirmed=confirmed)
        ],
        as_of=AS_OF,
    )


def test_ac6_a_low_confidence_reading_is_not_an_active_medication():
    """PRD §4 J2 acceptance, verbatim: a handwritten "Telma 40" read at low
    confidence shows telmisartan as an unconfirmed reading, not as active."""
    r = _handwritten(confirmed=False)
    state = r.state_for("telmisartan")
    assert state.status is MedStatus.UNCERTAIN
    assert state.status not in ACTIVE_LIKE
    assert state.open_question and "has not been confirmed" in state.open_question
    assert len(r.findings_of(FindingKind.NEEDS_CONFIRMATION)) == 1


def test_ac6_after_confirmation_it_becomes_active():
    r = _handwritten(confirmed=True)
    assert r.state_for("telmisartan").status in ACTIVE_LIKE


def test_ac7_a_confirmed_reading_stops_being_asked_about():
    """The reconcile-side half of AC-7. Carrying the correction forward to the
    next document from the same prescriber is the memory layer's job."""
    assert _handwritten(confirmed=True).findings_of(
        FindingKind.NEEDS_CONFIRMATION
    ) == ()


def test_an_unresolved_brand_is_asked_about_and_never_becomes_state(result):
    findings = [
        f for f in result.findings_of(FindingKind.NEEDS_CONFIRMATION)
        if "Ltrsn 5" in f.summary
    ]
    assert len(findings) == 1
    assert findings[0].molecules == ()


# ==========================================================================
# §6.5 — parallel prescribing
# ==========================================================================

def test_parallel_prescribing_names_the_specific_disjoint_pairs(result):
    """§6.5 is emphatic: claiming "no overlap" across all prescribers is false
    whenever any two of them share a drug."""
    findings = result.findings_of(FindingKind.PARALLEL_PRESCRIBING)
    assert len(findings) == 1
    summary = findings[0].summary
    # Rao and Menon share atorvastatin, so that pair must NOT be claimed.
    assert f"{IYER} (" in summary
    assert "2 pairs" in summary
    assert f"{RAO} (aspirin, atorvastatin, metoprolol and telmisartan)" in summary
    assert f"{MENON} (atorvastatin)" in summary


def test_parallel_prescribing_is_silent_when_every_pair_overlaps():
    r = reconcile(
        [
            _m("a", "D1", AS_OF - timedelta(days=10), "Telma 40", RAO),
            _m("b", "D2", AS_OF - timedelta(days=10), "Telma 40", IYER),
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.PARALLEL_PRESCRIBING) == ()


def test_parallel_prescribing_needs_two_named_prescribers():
    r = reconcile(
        [
            _m("a", "D1", AS_OF - timedelta(days=10), "Telma 40", RAO),
            _m("b", "D2", AS_OF - timedelta(days=10), "Glycomet 500", None),
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.PARALLEL_PRESCRIBING) == ()


# ==========================================================================
# §6.5 — dose changes
# ==========================================================================

def test_dose_change_reports_the_latest_change_only(result):
    findings = result.findings_of(FindingKind.DOSE_CHANGED)
    assert len(findings) == 1
    assert findings[0].molecules == ("metoprolol",)
    assert "25 mg" in findings[0].summary and "50 mg" in findings[0].summary
    assert "Is 50 mg the current strength" in findings[0].question


def test_dose_change_needs_the_same_prescriber():
    r = reconcile(
        [
            _m("a", "D1", date(2026, 1, 10), "Metolar 25", RAO),
            _m("b", "D2", date(2026, 6, 10), "Metolar 50", MENON),
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.DOSE_CHANGED) == ()


def test_no_dose_change_reported_for_a_combination_product():
    """Strengths are never attributed inside a combination (§6.4), so there is
    nothing to compare and nothing to claim."""
    r = reconcile(
        [
            _m("a", "D1", date(2026, 1, 10), "Ecosprin AV 75", RAO),
            _m("b", "D2", date(2026, 6, 10), "Ecosprin AV 150", RAO),
        ],
        as_of=AS_OF,
    )
    assert r.findings_of(FindingKind.DOSE_CHANGED) == ()


# ==========================================================================
# Labs — §6.5, §8
# ==========================================================================

def test_lab_trend_states_direction_across_the_series(result):
    findings = result.findings_of(FindingKind.LAB_TREND)
    assert len(findings) == 1
    assert "HbA1c has been rising across 4 measurements" in findings[0].summary
    # Provenance per point (NFR-5): every lab named, every point cited.
    assert "SRL" in findings[0].summary
    assert len(findings[0].evidence) == 4
    assert len(findings[0].evidence_dates) == 4


def test_lab_trend_requires_three_points():
    two = LAB_RESULTS[:2]
    r = reconcile([], as_of=AS_OF, lab_results=two)
    assert r.findings_of(FindingKind.LAB_TREND) == ()


def test_lab_trend_requires_a_monotonic_series():
    points = list(LAB_RESULTS[:4])
    from dataclasses import replace

    points[2] = replace(points[2], canonical_value=6.9)   # breaks the run
    r = reconcile([], as_of=AS_OF, lab_results=points)
    assert r.findings_of(FindingKind.LAB_TREND) == ()


def test_possible_duplicate_test_needs_two_different_labs(result):
    findings = result.findings_of(FindingKind.POSSIBLE_DUPLICATE_TEST)
    assert len(findings) == 1
    assert "creatinine" in findings[0].summary
    assert "14 days apart" in findings[0].summary


def test_repeat_at_the_same_lab_is_not_flagged():
    from dataclasses import replace

    pair = (LAB_RESULTS[4], replace(LAB_RESULTS[5], lab_name="Dr Lal PathLabs"))
    r = reconcile([], as_of=AS_OF, lab_results=pair)
    assert r.findings_of(FindingKind.POSSIBLE_DUPLICATE_TEST) == ()


def test_repeat_outside_the_window_is_not_flagged():
    from dataclasses import replace

    later = replace(LAB_RESULTS[5], doc_date=date(2026, 10, 1))
    r = reconcile([], as_of=AS_OF, lab_results=(LAB_RESULTS[4], later))
    assert r.findings_of(FindingKind.POSSIBLE_DUPLICATE_TEST) == ()


def test_reference_ranges_are_kept_per_report():
    """§8 — never apply one lab's range to another lab's value."""
    hba1c = next(s for s in reconcile(
        [], as_of=AS_OF, lab_results=LAB_RESULTS).series if s.analyte == "hba1c")
    assert {p.ref_high for p in hba1c.points} == {5.6, 5.7}


# ==========================================================================
# §11 — safety invariants
# ==========================================================================

def test_no_finding_makes_a_clinical_claim(result):
    """SR-1. If this fails the product has drifted into practising medicine."""
    for f in result.findings:
        assert clinical_claim_phrases_in(f.summary) == (), f
        assert clinical_claim_phrases_in(f.question) == (), f
    for s in result.states:
        assert clinical_claim_phrases_in(s.open_question or "") == (), s


def test_sr1_is_enforced_at_construction():
    with pytest.raises(ClinicalClaimError):
        Finding(
            kind=FindingKind.DUPLICATE_MOLECULE,
            attention=__import__("parchi.models", fromlist=["Attention"]).Attention.FYI,
            summary="This combination is dangerous.",
            question="Should this be changed?",
        )


def test_sr1_survives_hostile_verbatim_text_in_a_reading():
    """OCR output is arbitrary text and reaches NEEDS_CONFIRMATION copy.

    It must not be able to smuggle a forbidden phrase into a finding, and it
    must not crash the brief either.
    """
    r = reconcile(
        [
            _m("a", "D1", AS_OF, "stop taking immediately", MENON,
               confidence=Confidence.LOW)
        ],
        as_of=AS_OF,
    )
    findings = r.findings_of(FindingKind.NEEDS_CONFIRMATION)
    assert len(findings) == 1
    assert clinical_claim_phrases_in(findings[0].summary) == ()
    assert clinical_claim_phrases_in(findings[0].question) == ()


def test_sr2_every_finding_question_ends_with_a_question_mark(result):
    assert result.findings
    for f in result.findings:
        assert f.question.endswith("?"), f


def test_sr2_is_enforced_at_construction():
    from parchi.models import Attention

    with pytest.raises(ValueError, match="does not end in"):
        Finding(
            kind=FindingKind.DOSE_CHANGED,
            attention=Attention.FYI,
            summary="A strength changed.",
            question="Please check the strength.",
        )


def test_sr3_an_unconfirmed_low_reading_never_leaves_uncertain(result):
    """A mention needing confirmation contributes no status but UNCERTAIN."""
    unconfirmed = {
        molecule
        for m in MENTIONS
        if m.needs_confirmation
        for molecule in m.molecules
    }
    assert unconfirmed == {"gabapentin", "nortriptyline"}
    for molecule in unconfirmed:
        assert result.state_for(molecule).status is MedStatus.UNCERTAIN


def test_sr3_holds_when_a_usable_mention_exists_elsewhere():
    """A LOW reading must not upgrade itself by riding along with a good one,
    and must not drag a confirmed molecule down either."""
    r = reconcile(
        [
            _m("good", "D1", AS_OF - timedelta(days=10), "Telma 40", RAO),
            _m("bad", "D2", AS_OF - timedelta(days=1), "Telma 40", MENON,
               confidence=Confidence.LOW),
        ],
        as_of=AS_OF,
    )
    state = r.state_for("telmisartan")
    assert state.status in ACTIVE_LIKE
    # The good mention is the evidence; the LOW one is only a question.
    assert state.evidence_mention_ids == ("good",)
    assert len(r.findings_of(FindingKind.NEEDS_CONFIRMATION)) == 1


def test_sr4_every_state_carries_evidence(result):
    assert result.states
    for s in result.states:
        assert s.evidence_mention_ids, s


def test_sr4_is_enforced_at_construction():
    with pytest.raises(ValueError, match="SR-4"):
        MedicationState(
            molecule="metformin",
            status=MedStatus.ACTIVE,
            first_seen=AS_OF,
            last_mentioned=AS_OF,
            evidence_mention_ids=(),
        )


def test_sr5_an_unresolvable_brand_never_becomes_state(result):
    """"Ltrsn 5" resolved to nothing, so it has no molecule and no state."""
    unresolved = [m for m in MENTIONS if not m.is_resolved]
    assert [m.id for m in unresolved] == ["m20"]
    assert all(s.molecule for s in result.states)
    assert len(result.states) == 14


def test_sr6_no_strength_where_counts_mismatch(result):
    for m in MENTIONS:
        assert not m.strengths_mg or len(m.strengths_mg) == len(m.molecules)
    # Every combination product on the list reports no strength.
    for molecule in ("aspirin", "atorvastatin", "calcium carbonate",
                     "cholecalciferol", "amoxicillin", "clavulanic acid"):
        state = result.state_for(molecule)
        if state.current_brand_text in ("Ecosprin AV 75", "Shelcal 500",
                                        "Augmentin 625"):
            assert state.current_strength_mg is None, molecule


def test_sr7_reconcile_does_not_mutate_its_input():
    mentions = list(MENTIONS)
    labs = list(LAB_RESULTS)
    before_m, before_l = list(mentions), list(labs)
    reconcile(mentions, as_of=AS_OF, lab_results=labs)
    assert mentions == before_m
    assert labs == before_l


def test_sr7_reconcile_is_deterministic():
    a = reconcile(MENTIONS, as_of=AS_OF, lab_results=LAB_RESULTS)
    b = reconcile(MENTIONS, as_of=AS_OF, lab_results=LAB_RESULTS)
    assert a == b


def test_sr7_input_order_does_not_change_the_result():
    """J1 accepts an unordered bulk upload, so ordering must not matter."""
    shuffled = tuple(reversed(MENTIONS[7:] + MENTIONS[:7]))
    assert reconcile(shuffled, as_of=AS_OF, lab_results=tuple(
        reversed(LAB_RESULTS))) == reconcile(
        MENTIONS, as_of=AS_OF, lab_results=LAB_RESULTS)


def test_sr8_no_drug_interaction_is_asserted_anywhere():
    """SR-8 — not in code, not in prompts. A hallucinated reassurance about an
    interaction is worse than no tool (BRD §5.3)."""
    targets = list((SRC / "parchi").rglob("*.py"))
    prompts = SRC / "prompts"
    if prompts.exists():
        targets += list(prompts.rglob("*"))
    assert targets
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for banned in ("interaction", "interacts", "contraindicat"):
            assert banned not in text, f"{path.name} mentions {banned!r}"


def test_sr9_no_real_patient_data_and_fixtures_declared_constructed():
    fixtures = (SRC / "parchi" / "fixtures.py").read_text()
    assert "EVERY VALUE HERE IS INVENTED" in fixtures
    readme = SRC / "README.md"
    assert readme.exists(), "SR-9 requires the README to declare fixtures constructed"
    text = readme.read_text().lower()
    assert "constructed" in text
    assert "no real patient data" in text


# ==========================================================================
# Empty and degenerate input
# ==========================================================================

def test_no_documents_yields_nothing_rather_than_failing():
    r = reconcile([], as_of=AS_OF)
    assert r.states == () and r.findings == () and r.series == ()


def test_a_single_unreadable_document_yields_only_a_question():
    r = reconcile(
        [_m("a", "D1", AS_OF, "Zzqx", MENON, confidence=Confidence.LOW)],
        as_of=AS_OF,
    )
    assert r.states == ()
    assert len(r.findings) == 1
    assert r.findings[0].kind is FindingKind.NEEDS_CONFIRMATION
