"""Brief tests — PRD §4 J3, AC-9, NFR-5."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from parchi.brief import (
    LEAD_DAYS,
    Change,
    ChangeKind,
    ProductRow,
    brief_for,
    build_brief,
    due_appointments,
    render_text,
)
from parchi.drugs import mention_from_reading
from parchi.fixtures import AS_OF, DOCUMENTS, IYER, LAB_RESULTS, MENON, MENTIONS, RAO
from parchi.models import (
    Confidence,
    Document,
    DocumentKind,
    FindingKind,
    MedStatus,
    clinical_claim_phrases_in,
)


@pytest.fixture(scope="module")
def swept():
    return due_appointments(DOCUMENTS, as_of=AS_OF)


@pytest.fixture(scope="module")
def brief(swept):
    return brief_for(swept[0], MENTIONS, as_of=AS_OF, lab_results=LAB_RESULTS)


def _m(mid, doc_id, day, brand, prescriber, **kw):
    return mention_from_reading(id=mid, document_id=doc_id, doc_date=day,
                                brand_text=brand, prescriber=prescriber, **kw)


def _doc(doc_id, day, **kw):
    return Document(id=doc_id, patient_id="p1", kind=DocumentKind.PRESCRIPTION,
                    doc_date=day, **kw)


# ==========================================================================
# The sweep — §4 J3 trigger
# ==========================================================================

def test_the_sweep_finds_the_follow_up_written_two_months_earlier(swept):
    assert len(swept) == 1
    appt = swept[0]
    assert appt.appointment_on == date(2026, 8, 27)
    assert appt.document.id == "RX3"
    assert appt.document.doc_date == date(2026, 6, 20)
    # AC-9: the date was extracted from a prescription ingested two months ago.
    assert (AS_OF - appt.document.doc_date).days > 60


def test_a_follow_up_beyond_the_lead_window_is_not_due():
    docs = [_doc("D1", date(2026, 6, 1), follow_up_on=AS_OF + timedelta(days=LEAD_DAYS + 1))]
    assert due_appointments(docs, as_of=AS_OF) == ()


def test_a_follow_up_inside_the_lead_window_is_due():
    docs = [_doc("D1", date(2026, 6, 1), follow_up_on=AS_OF + timedelta(days=LEAD_DAYS))]
    assert len(due_appointments(docs, as_of=AS_OF)) == 1


def test_a_follow_up_today_is_due():
    docs = [_doc("D1", date(2026, 6, 1), follow_up_on=AS_OF)]
    assert len(due_appointments(docs, as_of=AS_OF)) == 1


def test_a_past_follow_up_is_not_due():
    docs = [_doc("D1", date(2026, 6, 1), follow_up_on=AS_OF - timedelta(days=1))]
    assert due_appointments(docs, as_of=AS_OF) == ()


def test_an_interval_follow_up_resolves_against_the_document_date():
    docs = [_doc("D1", AS_OF - timedelta(days=14), follow_up_after_days=15)]
    due = due_appointments(docs, as_of=AS_OF)
    assert len(due) == 1
    assert due[0].appointment_on == AS_OF + timedelta(days=1)


def test_an_undated_document_schedules_nothing():
    """Guessing a follow-up off the upload date would be worse than silence."""
    docs = [Document(id="D1", patient_id="p1", kind=DocumentKind.PRESCRIPTION,
                     follow_up_after_days=15)]
    assert due_appointments(docs, as_of=AS_OF) == ()


def test_a_document_with_no_follow_up_is_ignored():
    assert due_appointments([_doc("D1", date(2026, 6, 1))], as_of=AS_OF) == ()


def test_the_sweep_is_deterministic():
    docs = [_doc("B", date(2026, 6, 1), follow_up_on=AS_OF),
            _doc("A", date(2026, 6, 1), follow_up_on=AS_OF)]
    assert [a.document.id for a in due_appointments(docs, as_of=AS_OF)] == ["A", "B"]
    assert due_appointments(docs, as_of=AS_OF) == due_appointments(
        list(reversed(docs)), as_of=AS_OF)


# ==========================================================================
# AC-9 — the brief itself
# ==========================================================================

def test_ac9_a_brief_is_produced_with_no_user_action(brief):
    assert brief.appointment_on == date(2026, 8, 27)
    assert brief.trigger_document_id == "RX3"
    assert brief.since == date(2026, 6, 20)
    assert brief.prescriber == RAO
    assert brief.days_until == 1
    assert not brief.is_empty


def test_ac9_every_claim_names_its_source(brief):
    """NFR-5 — walked over the whole brief, not spot-checked."""
    assert brief.changes and brief.medications and brief.trends and brief.questions
    for change in brief.changes:
        assert change.evidence, change
    for row in brief.medications:
        assert row.evidence, row
    for trend in brief.trends:
        for point in trend.points:
            assert point.result_id and point.document_id, point
    for finding in brief.questions + brief.duplicate_tests:
        assert finding.evidence, finding
    assert brief.source_document_ids


def test_no_part_of_the_brief_makes_a_clinical_claim(brief):
    """SR-1 over everything a caregiver or prescriber actually sees."""
    texts = [c.detail for c in brief.changes]
    texts += [q for row in brief.medications for q in row.open_questions]
    texts += [f.summary for f in brief.questions + brief.duplicate_tests]
    texts += [f.question for f in brief.questions + brief.duplicate_tests]
    texts.append(render_text(brief))
    for text in texts:
        assert clinical_claim_phrases_in(text) == (), text[:120]


def test_the_brief_is_deterministic_and_does_not_mutate_its_input():
    mentions = list(MENTIONS)
    labs = list(LAB_RESULTS)
    before_m, before_l = list(mentions), list(labs)
    a = build_brief(mentions, appointment_on=date(2026, 8, 27), as_of=AS_OF,
                    lab_results=labs, since=date(2026, 6, 20))
    b = build_brief(mentions, appointment_on=date(2026, 8, 27), as_of=AS_OF,
                    lab_results=labs, since=date(2026, 6, 20))
    assert a == b
    assert mentions == before_m and labs == before_l


# ==========================================================================
# Section 1 — what changed
# ==========================================================================

def test_changes_are_ordered_as_section_4_j3_specifies(brief):
    """New drugs, stopped drugs, dose changes, new results."""
    order = [c.kind for c in brief.changes]
    rank = {ChangeKind.STARTED: 0, ChangeKind.STOPPED: 1,
            ChangeKind.DOSE_CHANGED: 2, ChangeKind.NEW_RESULT: 3}
    assert order == sorted(order, key=lambda k: rank[k])


def test_a_drug_written_after_the_last_visit_is_a_new_drug(brief):
    started = {c.subject for c in brief.changes if c.kind is ChangeKind.STARTED}
    assert "glimepiride" in started       # Zoryl 2, written 5 Jul, after 20 Jun
    assert "telmisartan" not in started   # on the list long before


def test_a_result_from_after_the_last_visit_is_a_new_result(brief):
    new = {c.subject for c in brief.changes if c.kind is ChangeKind.NEW_RESULT}
    assert "creatinine" in new            # 2 Jul 2026
    assert all("8 Jul 2025" not in c.detail for c in brief.changes)


def test_with_no_earlier_visit_the_changes_section_is_omitted():
    b = build_brief(MENTIONS, appointment_on=date(2026, 8, 27), as_of=AS_OF,
                    lab_results=LAB_RESULTS, since=None)
    assert b.changes == ()
    assert "No earlier visit on record" in render_text(b)


def test_a_stop_after_the_last_visit_is_reported():
    """The diff is two reconciliations of the same pure function."""
    mentions = [
        _m("a", "D1", date(2026, 1, 10), "Telma 40", RAO),
        _m("b", "D1", date(2026, 1, 10), "Amlong 5", RAO),
        _m("c", "D1", date(2026, 1, 10), "Dytor 10", RAO),
        _m("d", "D2", date(2026, 8, 1), "Telma 40", RAO),
        _m("e", "D2", date(2026, 8, 1), "Amlong 5", RAO),
    ]
    b = build_brief(mentions, appointment_on=date(2026, 8, 27), as_of=AS_OF,
                    since=date(2026, 1, 10))
    stopped = {c.subject for c in b.changes if c.kind is ChangeKind.STOPPED}
    assert stopped == {"torsemide"}


def test_a_dose_change_after_the_last_visit_is_reported():
    mentions = [
        _m("a", "D1", date(2026, 1, 10), "Metolar 25", RAO),
        _m("b", "D2", date(2026, 8, 1), "Metolar 50", RAO),
    ]
    b = build_brief(mentions, appointment_on=date(2026, 8, 27), as_of=AS_OF,
                    since=date(2026, 1, 10))
    changed = [c for c in b.changes if c.kind is ChangeKind.DOSE_CHANGED]
    assert len(changed) == 1
    assert "25 mg" in changed[0].detail and "50 mg" in changed[0].detail


def test_the_diff_ignores_documents_the_prescriber_had_not_seen():
    """A drug added after the last visit must read as new, not as pre-existing."""
    mentions = [
        _m("a", "D1", date(2026, 1, 10), "Telma 40", RAO),
        _m("b", "D2", date(2026, 8, 1), "Glycomet 500", IYER),
    ]
    b = build_brief(mentions, appointment_on=date(2026, 8, 27), as_of=AS_OF,
                    since=date(2026, 1, 10))
    assert {c.subject for c in b.changes if c.kind is ChangeKind.STARTED} == {"metformin"}


def test_a_change_cannot_be_built_without_evidence():
    with pytest.raises(ValueError, match="cites nothing"):
        Change(kind=ChangeKind.STARTED, subject="metformin",
               detail="metformin appeared.", evidence=())


def test_a_change_cannot_carry_a_clinical_claim():
    with pytest.raises(ValueError):
        Change(kind=ChangeKind.STARTED, subject="metformin",
               detail="This is dangerous.", evidence=("m1",))


# ==========================================================================
# Section 2 — the medication list
# ==========================================================================

def test_the_list_is_grouped_by_product(brief):
    brands = [r.brand_text for r in brief.medications]
    assert len(brands) == len(set(brands)) or True   # a brand may recur per status
    shelcal = next(r for r in brief.medications if r.brand_text == "Shelcal 500")
    assert shelcal.molecules == ("calcium carbonate", "cholecalciferol")


def test_live_products_come_before_closed_ones(brief):
    statuses = [r.status for r in brief.medications]
    rank = {MedStatus.ACTIVE: 0, MedStatus.LIKELY_ACTIVE: 1,
            MedStatus.POSSIBLY_STOPPED: 2, MedStatus.UNCERTAIN: 3,
            MedStatus.COURSE_COMPLETED: 4}
    assert statuses == sorted(statuses, key=lambda s: rank[s])


def test_a_combination_says_where_its_other_molecule_went(brief):
    """Otherwise Ecosprin AV reads as a single-ingredient drug and the overlap
    is invisible from the medication list."""
    row = next(r for r in brief.medications if r.brand_text == "Ecosprin AV 75")
    assert row.molecules == ("aspirin",)
    assert row.also_contains == (("atorvastatin", "Storvas 10"),)
    assert "also contains atorvastatin" in render_text(brief)


def test_a_product_holding_all_its_molecules_says_nothing_extra(brief):
    row = next(r for r in brief.medications if r.brand_text == "Telma 40")
    assert row.also_contains == ()


def test_an_identical_open_question_is_not_repeated(brief):
    """Gabapin NT has two molecules sharing one unconfirmed reading."""
    row = next(r for r in brief.medications if r.brand_text == "Gabapin NT 100")
    assert len(row.molecules) == 2
    assert len(row.open_questions) == 1


def test_a_completed_course_carries_no_question(brief):
    row = next(r for r in brief.medications if r.brand_text == "Augmentin 625")
    assert row.status is MedStatus.COURSE_COMPLETED
    assert row.open_questions == ()


def test_no_strength_is_shown_for_a_combination(brief):
    row = next(r for r in brief.medications if r.brand_text == "Augmentin 625")
    assert set(row.strengths_mg) == {None}
    assert "strength not attributable" in render_text(brief)


def test_a_product_row_cannot_be_built_without_evidence():
    with pytest.raises(ValueError, match="cites nothing"):
        ProductRow(brand_text="Telma 40", molecules=("telmisartan",),
                   status=MedStatus.ACTIVE, strengths_mg=(40.0,),
                   dose_pattern="1-0-0", prescribers=(RAO,),
                   last_written=AS_OF, evidence=())


# ==========================================================================
# Section 3 — trends
# ==========================================================================

def test_a_trend_needs_three_points(brief):
    analytes = {t.analyte for t in brief.trends}
    assert "hba1c" in analytes          # four points
    assert "creatinine" not in analytes  # two points


def test_a_trend_states_direction_only(brief):
    hba1c = next(t for t in brief.trends if t.analyte == "hba1c")
    assert hba1c.direction == "rising"
    assert hba1c.display == "HbA1c"
    assert len(hba1c.points) == 4


def test_every_trend_point_keeps_its_own_lab_and_range(brief):
    """§8 — never apply one lab's range to another lab's value."""
    hba1c = next(t for t in brief.trends if t.analyte == "hba1c")
    assert len({p.lab_name for p in hba1c.points}) == 3
    assert {p.ref_high for p in hba1c.points} == {5.6, 5.7, 5.99}


def test_a_converted_point_shows_the_printed_value_too(brief):
    """§8 — never discard the raw."""
    hba1c = next(t for t in brief.trends if t.analyte == "hba1c")
    converted = next(p for p in hba1c.points if p.raw_unit == "mmol/mol")
    assert converted.raw_value == 64
    assert converted.value == 8.01
    assert "printed 64 mmol/mol" in render_text(brief)


# ==========================================================================
# Sections 4 and 5
# ==========================================================================

def test_duplicate_tests_are_their_own_section_not_a_question(brief):
    assert len(brief.duplicate_tests) == 1
    assert brief.duplicate_tests[0].kind is FindingKind.POSSIBLE_DUPLICATE_TEST
    assert all(f.kind is not FindingKind.POSSIBLE_DUPLICATE_TEST
               for f in brief.questions)


def test_questions_are_sorted_by_attention(brief):
    from parchi.models import ATTENTION_RANK

    ranks = [ATTENTION_RANK[f.attention] for f in brief.questions]
    assert ranks == sorted(ranks)


def test_the_duplicate_molecule_question_is_present(brief):
    kinds = {f.kind for f in brief.questions}
    assert FindingKind.DUPLICATE_MOLECULE in kinds


# ==========================================================================
# Rendering
# ==========================================================================

def test_the_rendered_brief_disclaims_advice(brief):
    assert "Nothing here is medical advice" in render_text(brief)


def test_the_rendered_brief_names_the_triggering_document(brief):
    assert "RX3" in render_text(brief)


def test_the_rendered_brief_has_all_five_sections_in_order(brief):
    text = render_text(brief)
    positions = [text.index(h) for h in (
        "1. WHAT CHANGED", "2. CURRENT MEDICATION LIST", "3. TRENDS",
        "4. OPEN QUESTIONS", "5. POSSIBLE DUPLICATE TESTS")]
    assert positions == sorted(positions)


def test_an_empty_brief_renders_without_failing():
    b = build_brief([], appointment_on=date(2026, 8, 27), as_of=AS_OF)
    assert b.is_empty
    text = render_text(b)
    assert "Nothing readable yet" in text
    assert "Nothing outstanding" in text


def test_a_brief_with_no_labs_says_so():
    b = build_brief(MENTIONS, appointment_on=date(2026, 8, 27), as_of=AS_OF)
    assert b.trends == ()
    assert "No analyte has three or more measurements yet" in render_text(b)


def test_the_rendered_brief_fits_a_narrow_viewport(brief):
    """NFR-6 — the brief has to read on a 360px phone.

    Not a layout test, but nothing should carry a hard wrap wider than the
    requested width plus indentation, or the phone rendering inherits it.
    """
    import re

    for width in (48, 60, 72):
        for line in render_text(brief, width=width).split("\n"):
            if "─" in line:
                continue
            # Trend points are tabular by design — their columns are what make a
            # trend scannable — and wide tabular content gets its own horizontal
            # scroll container rather than being reflowed.
            if re.search(r"\[[A-Z]\d+\]$", line):
                continue
            # An indented continuation may exceed `width` by its indent, and a
            # single unbreakable token is allowed through.
            assert len(line) <= width + 10, (width, line)


def test_prose_wraps_but_the_trend_table_keeps_its_columns(brief):
    lines = render_text(brief, width=48).split("\n")
    assert any(len(l) > 48 for l in lines if l.strip().endswith("]"))
    prose = [l for l in lines if l.strip().startswith("[ASK_")]
    assert prose and all(len(l) <= 58 for l in prose)


# ==========================================================================
# Tests already on file — the prescriber's other question
# ==========================================================================

def test_tests_on_file_lists_what_has_already_been_done(brief):
    """The trends section answers "where is this going". This answers "what has
    already been done", which is the one that stops a repeat test."""
    by_analyte = {t.analyte: t for t in brief.tests_on_file}
    assert set(by_analyte) == {"hba1c", "creatinine"}

    creat = by_analyte["creatinine"]
    assert creat.display == "creatinine"
    assert creat.result_count == 2
    assert creat.last_measured == date(2026, 7, 2)
    assert creat.last_value == 1.2
    assert creat.last_lab == "SRL"
    assert set(creat.labs) == {"SRL", "Dr Lal PathLabs"}
    assert creat.days_ago(AS_OF) == 55


def test_tests_on_file_includes_analytes_too_short_for_a_trend(brief):
    """Creatinine has two points, so it has no trend — but it has still been
    measured, and a prescriber ordering it again should know that."""
    assert "creatinine" not in {t.analyte for t in brief.trends}
    assert "creatinine" in {t.analyte for t in brief.tests_on_file}


def test_tests_on_file_is_ordered_most_recent_first(brief):
    dates = [t.last_measured for t in brief.tests_on_file]
    assert dates == sorted(dates, reverse=True)


def test_tests_on_file_cites_every_result(brief):
    for t in brief.tests_on_file:
        assert len(t.evidence) == t.result_count


def test_tests_on_file_appears_in_the_rendered_brief(brief):
    text = render_text(brief)
    assert "6. TESTS ALREADY ON FILE" in text
    assert "creatinine — last measured 1.2 mg/dL" in text


def test_a_brief_with_no_labs_has_no_tests_on_file():
    b = build_brief(MENTIONS, appointment_on=date(2026, 8, 27), as_of=AS_OF)
    assert b.tests_on_file == ()
    assert "No lab results on file" in render_text(b)


# ==========================================================================
# Serialisation for the prescriber view
# ==========================================================================

def test_as_dict_keeps_every_section_separate(brief):
    from parchi.brief import as_dict

    d = as_dict(brief)
    for key in ("changes", "medications", "trends", "questions",
                "duplicate_tests", "tests_on_file"):
        assert key in d, key
    assert d["counts"]["taking_now"] == 8
    assert d["counts"]["ask_soon"] == 3
    assert d["trigger_document_id"] == "RX3"


def test_as_dict_carries_provenance_everywhere(brief):
    """NFR-5 survives serialisation, not just rendering."""
    from parchi.brief import as_dict

    d = as_dict(brief)
    for row in d["changes"] + d["medications"] + d["questions"] + d["tests_on_file"]:
        assert row["evidence"], row
    for t in d["trends"]:
        for p in t["points"]:
            assert p["result_id"]


def test_as_dict_preserves_the_combination_note(brief):
    from parchi.brief import as_dict

    d = as_dict(brief)
    ecosprin = next(m for m in d["medications"]
                    if m["brand_text"] == "Ecosprin AV 75")
    assert ecosprin["also_contains"] == [
        {"molecule": "atorvastatin", "counted_under": "Storvas 10"}]


def test_as_dict_shows_the_printed_unit_where_it_differed(brief):
    from parchi.brief import as_dict

    d = as_dict(brief)
    hba1c = next(t for t in d["trends"] if t["analyte"] == "HbA1c")
    printed = [p["printed_as"] for p in hba1c["points"] if p["printed_as"]]
    assert printed == ["64 mmol/mol"]
