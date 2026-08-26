"""Extraction tests — PRD §9.

The transport is faked throughout: the agreement rule is the part worth
testing, and it must be testable without a network or a bill.
"""

from __future__ import annotations

from datetime import date

import pytest

from parchi.extract import (
    CHEAP_PROMPTS,
    ExtractedLine,
    RawRead,
    Usage,
    _parse_box,
    combine,
    extract,
    line_key,
    parse_document_date,
    reads_disagree,
    to_mentions,
)
from parchi.models import ACTIVE_LIKE, Confidence, DocumentKind, FindingKind, MedStatus
from parchi.reconcile import reconcile


def med(brand, **kw):
    d = {"brand_text": brand}
    d.update(kw)
    return d


def payload(*meds, kind="prescription", **kw):
    d = {"document_kind": kind, "medications": list(meds)}
    d.update(kw)
    return d


def read(*meds, thinking=False, kind="prescription", **kw):
    return RawRead(payload=payload(*meds, kind=kind, **kw),
                   usage=Usage(calls=1, input_tokens=1000, output_tokens=200,
                               thinking_tokens=5000 if thinking else 0),
                   thinking=thinking)


class FakeTransport:
    """Returns a queued response per call and records how it was asked."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, bool]] = []

    def read(self, image, mime_type, prompt, *, thinking):
        self.calls.append((prompt, thinking))
        r = self.responses.pop(0)
        return RawRead(payload=r.payload, usage=r.usage, thinking=thinking)


# ==========================================================================
# Usage and cost
# ==========================================================================

def test_thinking_tokens_are_billed_as_output():
    """Costing on candidates alone understates the bill badly."""
    u = Usage(calls=1, input_tokens=1160, output_tokens=231, thinking_tokens=62911)
    assert u.billed_output_tokens == 63142
    # 1160/1e6*1.50 + 63142/1e6*9.00 = 0.00174 + 0.568278
    assert round(u.cost_usd, 4) == 0.57


def test_usage_without_thinking_is_the_cheap_case():
    u = Usage(calls=1, input_tokens=1160, output_tokens=231)
    assert round(u.cost_usd, 5) == 0.00382


def test_usage_is_additive():
    a = Usage(calls=1, input_tokens=10, output_tokens=2, thinking_tokens=1)
    assert (a + a) == Usage(calls=2, input_tokens=20, output_tokens=4,
                            thinking_tokens=2)


# ==========================================================================
# Line identity across reads
# ==========================================================================

def test_two_spellings_of_a_known_brand_are_the_same_line():
    """Otherwise agreement would never be detected and everything would be LOW."""
    assert line_key("Tab Telma 40") == line_key("TELMA 40MG TAB")
    assert line_key("Tab Telma 40") == ("telmisartan",)


def test_an_unknown_brand_matches_only_itself():
    assert line_key("Zzqx 5") == line_key("zzqx 5")
    assert line_key("Zzqx 5") != line_key("Zzqy 5")


def test_different_drugs_are_different_lines():
    assert line_key("Telma 40") != line_key("Glycomet 500")


# ==========================================================================
# Bounding boxes — §9.3
# ==========================================================================

def test_box_is_normalised_to_fractions_and_reordered():
    assert _parse_box([445, 364, 587, 994]) == (0.364, 0.445, 0.994, 0.587)


@pytest.mark.parametrize(
    "raw",
    [None, [], [1, 2, 3], [1, 2, 3, 4, 5], "nope", [0, 0, 0, 0],
     [1200, 0, 1400, 100], ["a", "b", "c", "d"], [100, 100, 100, 900]],
)
def test_an_unusable_box_becomes_none_rather_than_nonsense(raw):
    assert _parse_box(raw) is None


def test_reversed_box_coordinates_are_normalised_not_rejected():
    """The model occasionally emits min and max the wrong way round. A box is
    still recoverable from that, so it is corrected rather than discarded."""
    assert _parse_box([500, 100, 400, 200]) == (0.1, 0.4, 0.2, 0.5)


# ==========================================================================
# Document dates — never inferred
# ==========================================================================

@pytest.mark.parametrize(
    "text,expected",
    [
        ("12/03/2026", date(2026, 3, 12)),
        ("Date: 05-11-2025", date(2025, 11, 5)),
        ("1.4.26", date(2026, 4, 1)),
        ("18 Jun 2026", date(2026, 6, 18)),
        ("20 September 2025", date(2025, 9, 20)),
        ("Mar 12, 2026", date(2026, 3, 12)),
        ("25/12/2025", date(2025, 12, 25)),
    ],
)
def test_printed_dates_are_parsed(text, expected):
    assert parse_document_date(text) == expected


def test_ambiguous_numeric_dates_are_read_day_first():
    """Indian convention. 03/04/2026 is 3 April, not 4 March."""
    assert parse_document_date("03/04/2026") == date(2026, 4, 3)


@pytest.mark.parametrize(
    "text", [None, "", "no date here", "32/13/2026", "Smudge", "99/99/9999"]
)
def test_an_unparseable_date_leaves_the_document_undated(text):
    """§4 J1.4 — never silently substitute the upload date."""
    assert parse_document_date(text) is None


# ==========================================================================
# The agreement rule
# ==========================================================================

def test_a_line_both_reads_saw_is_high_confidence():
    result = combine([read(med("Tab Telma 40")), read(med("TELMA 40MG TAB"))])
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.reads_agreeing == 2 and line.reads_total == 2
    assert line.confidence is Confidence.HIGH


def test_a_line_only_one_read_saw_is_low_confidence():
    """The whole point: disagreement, not self-report, drives the gate."""
    result = combine([
        read(med("Tab Telma 40"), med("Glycomet 500")),
        read(med("Tab Telma 40")),
    ])
    by_brand = {ln.brand_text: ln for ln in result.lines}
    assert by_brand["Tab Telma 40"].confidence is Confidence.HIGH
    assert by_brand["Glycomet 500"].confidence is Confidence.LOW


def test_a_majority_of_three_reads_is_medium():
    result = combine([
        read(med("Telma 40"), med("Glycomet 500")),
        read(med("Telma 40")),
        read(med("Telma 40"), med("Glycomet 500"), thinking=True),
    ])
    by_brand = {ln.brand_text: ln for ln in result.lines}
    assert by_brand["Telma 40"].confidence is Confidence.HIGH      # 3 of 3
    assert by_brand["Glycomet 500"].confidence is Confidence.MEDIUM  # 2 of 3


def test_no_reads_yields_nothing_rather_than_failing():
    result = combine([])
    assert result.lines == () and "no reads" in result.notes


def test_a_malformed_payload_is_survivable():
    result = combine([RawRead(payload={}, usage=Usage(calls=1)),
                      RawRead(payload={"medications": "not a list"},
                              usage=Usage(calls=1))])
    assert result.lines == ()
    assert result.kind is DocumentKind.UNKNOWN


def test_blank_and_malformed_medication_entries_are_skipped():
    result = combine([read(med(""), med("Telma 40"), "junk", None)])
    assert [ln.brand_text for ln in result.lines] == ["Telma 40"]


def test_disagreement_is_recorded_in_the_notes():
    result = combine([read(med("Telma 40"), med("Glycomet 500")),
                      read(med("Telma 40"))])
    assert any("not seen by every read" in n for n in result.notes)


def test_lines_are_ordered_down_the_page():
    result = combine([
        read(med("Second", box_2d=[500, 100, 560, 900]),
             med("First", box_2d=[200, 100, 260, 900])),
        read(med("Second", box_2d=[500, 100, 560, 900]),
             med("First", box_2d=[200, 100, 260, 900])),
    ])
    assert [ln.brand_text for ln in result.lines] == ["First", "Second"]


def test_the_thinking_read_supplies_the_transcription_it_paid_for():
    result = combine([
        read(med("Tab Telma 4O")),                    # cheap misread
        read(med("Tab Telma 40")),
        read(med("Tab Telma 40 mg"), thinking=True),
    ])
    assert result.lines[0].brand_text == "Tab Telma 40 mg"
    assert result.escalated is True


def test_document_kind_disagreement_is_surfaced():
    result = combine([read(med("Telma 40"), kind="prescription"),
                      read(med("Telma 40"), kind="discharge_summary")])
    assert any("document kind" in n for n in result.notes)


def test_usage_is_summed_across_reads():
    result = combine([read(med("Telma 40")), read(med("Telma 40"))])
    assert result.usage.calls == 2
    assert result.usage.input_tokens == 2000


# ==========================================================================
# Reads and escalation
# ==========================================================================

def test_three_independently_framed_reads_by_default():
    """Independence comes from the framing. Reads at temperature 0 sharing one
    prompt would be the same read repeated and the signal would be vacuous."""
    assert len(CHEAP_PROMPTS) == 3
    assert len(set(CHEAP_PROMPTS)) == 3
    t = FakeTransport(*[read(med("Telma 40")) for _ in range(3)])
    extract(b"img", transport=t)
    assert len({prompt for prompt, _ in t.calls}) == 3


def test_thinking_is_off_by_default():
    """It bought nothing over a third cheap prompt and carried a cost tail a
    per-patient budget cannot absorb."""
    t = FakeTransport(*[read(med("Telma 40")) for _ in range(3)])
    result = extract(b"img", transport=t)
    assert [thinking for _, thinking in t.calls] == [False, False, False]
    assert result.escalated is False
    assert result.usage.thinking_tokens == 0


def test_disagreement_alone_does_not_spend_a_thinking_call():
    t = FakeTransport(
        read(med("Telma 40")),
        read(med("Telma 40"), med("Pan 40")),
        read(med("Telma 40")),
    )
    result = extract(b"img", transport=t)
    assert len(t.calls) == 3
    assert result.escalated is False


def test_escalation_when_explicitly_requested():
    """The path is kept and tested: it is the right lever if a harder document
    class turns up, even though it is not the default."""
    t = FakeTransport(
        read(med("Telma 40")),
        read(med("Telma 40"), med("Pan 40")),
        read(med("Telma 40")),
        read(med("Telma 40"), med("Pan 40"), thinking=True),
    )
    result = extract(b"img", transport=t, escalate=True)
    assert len(t.calls) == 4
    assert t.calls[3][1] is True
    assert result.escalated is True


def test_agreeing_reads_never_escalate_even_when_asked():
    t = FakeTransport(*[read(med("Telma 40")) for _ in range(3)])
    result = extract(b"img", transport=t, escalate=True)
    assert len(t.calls) == 3
    assert result.escalated is False


def test_a_custom_prompt_set_is_honoured():
    t = FakeTransport(read(med("Telma 40")), read(med("Telma 40")))
    extract(b"img", transport=t, prompts=CHEAP_PROMPTS[:2])
    assert len(t.calls) == 2


def test_reads_disagree_detects_set_differences_only():
    same = [read(med("Tab Telma 40")), read(med("TELMA 40MG TAB"))]
    assert reads_disagree(same) is False
    diff = [read(med("Telma 40")), read(med("Telma 40"), med("Pan 40"))]
    assert reads_disagree(diff) is True


def test_two_of_three_reads_is_medium_and_one_is_low():
    """The calibration that matters: three reads give a gate that fires."""
    t = FakeTransport(
        read(med("Telma 40"), med("Pan 40"), med("Metolar 25")),
        read(med("Telma 40"), med("Pan 40")),
        read(med("Telma 40")),
    )
    result = extract(b"img", transport=t)
    by_brand = {ln.brand_text: ln.confidence for ln in result.lines}
    assert by_brand["Telma 40"] is Confidence.HIGH
    assert by_brand["Pan 40"] is Confidence.MEDIUM
    assert by_brand["Metolar 25"] is Confidence.LOW


# ==========================================================================
# Handover to the data model
# ==========================================================================

def test_agreement_confidence_reaches_the_mention():
    result = combine([read(med("Tab Telma 40")), read(med("Tab Telma 40"))])
    mentions = to_mentions(result, document_id="RX1", doc_date=date(2026, 8, 1),
                          prescriber="Dr Rao")
    assert len(mentions) == 1
    assert mentions[0].molecules == ("telmisartan",)
    assert mentions[0].is_usable is True


def test_a_single_read_line_is_gated_out_of_medication_state():
    """End to end: extraction disagreement -> LOW -> SR-3 -> UNCERTAIN.

    This is the property the confirmation loop rests on, and it now rests on
    read agreement rather than on the model's opinion of itself.
    """
    result = combine([
        read(med("Tab Telma 40"), med("Tab Metolar 25")),
        read(med("Tab Telma 40")),
    ])
    mentions = to_mentions(result, document_id="RX1", doc_date=date(2026, 8, 1),
                          prescriber="Dr Rao")
    r = reconcile(mentions, as_of=date(2026, 8, 20))
    assert r.state_for("metoprolol").status is MedStatus.UNCERTAIN
    assert r.state_for("metoprolol").status not in ACTIVE_LIKE
    assert r.state_for("telmisartan").status in ACTIVE_LIKE
    assert len(r.findings_of(FindingKind.NEEDS_CONFIRMATION)) == 1


def test_an_unresolvable_brand_never_becomes_state_even_when_unanimous():
    """SR-5 is the second net. Both reads agreeing on garbage is still garbage."""
    result = combine([read(med("Zzqx 5")), read(med("Zzqx 5"))])
    assert result.lines[0].confidence is Confidence.HIGH
    mentions = to_mentions(result, document_id="RX1", doc_date=date(2026, 8, 1),
                           prescriber="Dr Rao")
    assert mentions[0].molecules == ()
    r = reconcile(mentions, as_of=date(2026, 8, 20))
    assert r.states == ()
    assert len(r.findings_of(FindingKind.NEEDS_CONFIRMATION)) == 1


def test_a_combination_product_still_reports_no_strength():
    result = combine([read(med("Tab Janumet 50/1000")),
                      read(med("JANUMET 50/1000MG TAB"))])
    mentions = to_mentions(result, document_id="RX1", doc_date=date(2026, 8, 1))
    assert mentions[0].molecules == ("sitagliptin", "metformin")
    assert mentions[0].strengths_mg == ()


def test_dose_pattern_and_duration_survive_to_the_mention():
    result = combine([
        read(med("Augmentin 625", dose_pattern="1-0-1", duration_days=5,
                 instruction="x 5 days")),
        read(med("Augmentin 625", dose_pattern="1-0-1", duration_days=5,
                 instruction="x 5 days")),
    ])
    m = to_mentions(result, document_id="RX1", doc_date=date(2026, 5, 2))[0]
    assert m.dose_pattern == "1-0-1"
    assert m.duration_days == 5
    assert m.course_ends_on() == date(2026, 5, 7)


# ==========================================================================
# Line identity is robust to transcription noise
# ==========================================================================

def test_a_continuation_prefix_does_not_split_one_drug_into_two():
    """Regression from a live run on rx-004.

    One read wrote "Cont. Tab Glimy (4mg)" and the other "Tab Glimy (4mg)".
    Before the fix these keyed differently, so the agreement check saw two
    medications where there was one, escalated needlessly, and reported the
    real line at MEDIUM with a phantom LOW line beside it.
    """
    assert line_key("Cont. Tab Glimy (4mg)") == line_key("Tab Glimy (4mg)")
    result = combine([read(med("Cont. Tab Glimy (4mg)", dose_pattern="1 BD")),
                      read(med("Tab Glimy (4mg)", dose_pattern="1 BD"))])
    assert len(result.lines) == 1
    assert result.lines[0].confidence is Confidence.HIGH


def test_transcription_noise_does_not_split_a_line_across_reads():
    t = FakeTransport(read(med("Cont. Tab Telma 40")),
                      read(med("TELMA 40MG TAB")),
                      read(med("Tab. Telma 40mg OD")))
    result = extract(b"img", transport=t)
    assert len(result.lines) == 1
    assert result.lines[0].confidence is Confidence.HIGH


def test_a_strength_unit_disagreement_does_not_invent_a_medication():
    """Regression from a live run on rx-077.

    One read wrote "T Cepodem 200g" and another "T Cepodem 200mg". Neither
    brand is in the table, and keying on raw words made them two different
    medications — a phantom drug at LOW beside the real one at MEDIUM.
    """
    assert line_key("T Cepodem 200g") == line_key("T Cepodem 200mg")
    assert line_key("T. Domstal (10)") == line_key("T. Domstal")
    result = combine([read(med("T Cepodem 200g", dose_pattern="BD")),
                      read(med("T Cepodem 200mg", dose_pattern="BD")),
                      read(med("T Cepodem 200 mg", dose_pattern="BD"))])
    assert len(result.lines) == 1
    assert result.lines[0].confidence is Confidence.HIGH


def test_an_unknown_brand_is_still_distinguished_from_a_different_one():
    """Convergence must not go so far that two real drugs merge."""
    assert line_key("T Cepodem 200mg") != line_key("T Valavir 1gm")


# ==========================================================================
# Follow-up capture — what makes J3 unprompted
# ==========================================================================

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Review after 68 days  —  27/08/2026", (date(2026, 8, 27), None)),
        ("F/U 12/09/2026", (date(2026, 9, 12), None)),
        ("Review after 2 weeks", (None, 14)),
        ("review in 3 months", (None, 90)),
        ("Repeat after 10 wks", (None, 70)),
        ("come back next time", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_a_follow_up_instruction_is_read_as_a_date_or_an_interval(text, expected):
    from parchi.extract import parse_follow_up

    assert parse_follow_up(text) == expected


def test_an_explicit_date_wins_over_an_interval():
    """Both are often written. A date needs no arithmetic, so prefer it."""
    from parchi.extract import parse_follow_up

    on, after = parse_follow_up("Review after 9 weeks — 27/08/2026")
    assert on == date(2026, 8, 27)
    assert after is None


def test_the_follow_up_reaches_the_extraction_result():
    result = combine([
        read(med("Telma 40"), follow_up="Review after 2 weeks"),
        read(med("Telma 40"), follow_up="Review after 2 weeks"),
    ])
    assert result.follow_up_text == "Review after 2 weeks"
    assert result.follow_up_after_days == 14
    assert result.follow_up_on is None


def test_no_follow_up_written_means_none_invented():
    result = combine([read(med("Telma 40")), read(med("Telma 40"))])
    assert result.follow_up_text is None
    assert result.follow_up_on is None and result.follow_up_after_days is None
