"""The unprompted appointment brief — PRD §4 J3.

Triggered by a follow-up date extracted from a document ingested months
earlier, not by anything the user does. Sections run in the order §4 J3
specifies, because that is the order a prescriber with seven minutes reads in:
what changed, what they are on now, where the numbers are going, what nobody
has answered, and what was tested twice.

ASSEMBLED IN CODE, NOT BY A MODEL. PRD §9.2 lists brief assembly as a Flash
task. It is deterministic Python here instead, for one reason: SR-1 forbids
clinical-claim vocabulary in anything the caregiver sees, and an invariant you
can only check after generation is not an invariant. Every sentence in a brief
is built from a template that is itself covered by the SR-1 test, so the
guarantee holds by construction rather than by inspection. Translation to Hindi
(BR-13) IS a model task — it is a language problem, not a judgement one, and it
runs after the English has already passed the invariant.

"WHAT CHANGED" IS A DIFF OF TWO RECONCILIATIONS. reconcile() is pure and takes
`as_of` as an argument, so the state the prescriber last saw is simply
reconcile() over the documents that existed then. No second code path, no
snapshot to drift.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from .models import (
    ACTIVE_LIKE,
    ATTENTION_RANK,
    Document,
    Finding,
    FindingKind,
    LabResult,
    MedStatus,
    MedicationMention,
    MedicationState,
    analyte_display,
    clinical_claim_phrases_in,
)
from .reconcile import reconcile

#: How far ahead of an appointment the brief is sent. §4 J3 triggers on a
#: follow-up date "falling within the next 48 hours".
LEAD_DAYS = 2

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _d(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def _num(value: float) -> str:
    return f"{value:g}"


def _join(items) -> str:
    """"A", "A and B", "A, B and C"."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


# --------------------------------------------------------------------------
# The trigger
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DueAppointment:
    """A follow-up coming up, and the document that scheduled it."""

    appointment_on: date
    document: Document

    @property
    def days_away(self) -> int:
        return -1  # placeholder, never used; see days_from

    def days_from(self, as_of: date) -> int:
        return (self.appointment_on - as_of).days


def due_appointments(
    documents, *, as_of: date, lead_days: int = LEAD_DAYS
) -> tuple[DueAppointment, ...]:
    """Follow-ups falling within the lead window. The J3 sweep.

    Deterministic and clock-free: `as_of` is passed in so a scheduled run is
    reproducible and testable. An undated document schedules nothing.
    """
    out = []
    for doc in documents:
        when = doc.follow_up_date
        if when is None:
            continue
        if as_of <= when <= as_of + timedelta(days=lead_days):
            out.append(DueAppointment(appointment_on=when, document=doc))
    return tuple(sorted(out, key=lambda a: (a.appointment_on, a.document.id)))


# --------------------------------------------------------------------------
# Section 1 — what changed
# --------------------------------------------------------------------------

class ChangeKind(str, Enum):
    STARTED = "STARTED"
    STOPPED = "STOPPED"
    DOSE_CHANGED = "DOSE_CHANGED"
    NEW_RESULT = "NEW_RESULT"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    subject: str
    detail: str
    evidence: tuple[str, ...]
    evidence_dates: tuple[date, ...] = ()
    #: Proper nouns copied out of a document — a laboratory, a prescriber, a
    #: brand. Masked before the SR-1 scan; naming SRL Diagnostics is not
    #: making a diagnosis. See models.clinical_claim_phrases_in.
    quoted_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence:
            # NFR-5 made structural: no claim without a source.
            raise ValueError(f"change {self.kind.value} for {self.subject!r} cites nothing")
        hits = clinical_claim_phrases_in(self.detail, quoted=self.quoted_names)
        if hits:
            raise ValueError(f"change detail contains {hits}: {self.detail!r}")


# --------------------------------------------------------------------------
# Section 2 — current medication list, grouped by product
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductRow:
    """One product and the molecules it currently accounts for.

    Grouped by the product each molecule is *currently* reaching the patient
    through, so a molecule that moved from one brand to another appears under
    the new one. Where two products deliver the same molecule, that overlap is
    a DUPLICATE_MOLECULE question in section 4 rather than a merged row here.
    """

    brand_text: str
    molecules: tuple[str, ...]
    status: MedStatus
    strengths_mg: tuple[float | None, ...]
    dose_pattern: str | None
    prescribers: tuple[str, ...]
    last_written: date
    evidence: tuple[str, ...]
    open_questions: tuple[str, ...] = ()
    #: Molecules this product contains that are currently attributed to a
    #: different product, as (molecule, where) pairs.
    #:
    #: Without this a combination reads as if it were a single-ingredient drug:
    #: Ecosprin AV shows aspirin only, because its atorvastatin is now credited
    #: to a later Storvas prescription. A prescriber scanning the list in a
    #: seven-minute consultation would not see the overlap at all. The
    #: DUPLICATE_MOLECULE question says it too, but it belongs where the drug is.
    also_contains: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError(f"product row {self.brand_text!r} cites nothing")


# --------------------------------------------------------------------------
# Section 3 — trends
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TrendPoint:
    on: date
    value: float
    lab_name: str | None
    ref_low: float | None
    ref_high: float | None
    document_id: str
    result_id: str
    #: The value exactly as printed, kept because §8 forbids discarding the raw.
    raw_value: float | None = None
    raw_unit: str | None = None


@dataclass(frozen=True)
class TrendRow:
    analyte: str
    display: str
    unit: str
    #: "rising", "falling", or None. Direction only — §8 forbids interpreting
    #: what a trend means.
    direction: str | None
    points: tuple[TrendPoint, ...]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError(f"trend {self.analyte!r} has no points")


@dataclass(frozen=True)
class TestOnFile:
    """One analyte we hold results for, so nobody re-orders it blind.

    The trend section answers "where is this going". This answers the
    prescriber's other question — "what has already been done" — which is the
    one that stops a repeat test being ordered from the consulting room. Facts
    only: what, when, where, how many. No interpretation (§8).
    """

    analyte: str
    display: str
    unit: str
    last_measured: date
    last_value: float
    last_lab: str | None
    result_count: int
    labs: tuple[str, ...]
    evidence: tuple[str, ...]

    def days_ago(self, as_of: date) -> int:
        return (as_of - self.last_measured).days


def _tests_on_file(series) -> tuple[TestOnFile, ...]:
    rows = []
    for s in series:
        latest = max(s.points, key=lambda p: (p.doc_date, p.id))
        rows.append(TestOnFile(
            analyte=s.analyte,
            display=analyte_display(s.analyte),
            unit=s.canonical_unit,
            last_measured=latest.doc_date,
            last_value=latest.canonical_value,
            last_lab=latest.lab_name,
            result_count=len(s.points),
            labs=tuple(sorted({p.lab_name for p in s.points if p.lab_name})),
            evidence=tuple(p.id for p in s.points),
        ))
    # Most recently measured first: that is the order in which a repeat test is
    # least defensible.
    return tuple(sorted(rows, key=lambda r: (-r.last_measured.toordinal(), r.analyte)))


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Brief:
    appointment_on: date
    generated_on: date
    #: The last time the triggering prescriber saw the patient. Changes are
    #: measured from here.
    since: date | None
    trigger_document_id: str | None
    prescriber: str | None
    changes: tuple[Change, ...] = ()
    medications: tuple[ProductRow, ...] = ()
    trends: tuple[TrendRow, ...] = ()
    questions: tuple[Finding, ...] = ()
    duplicate_tests: tuple[Finding, ...] = ()
    tests_on_file: tuple[TestOnFile, ...] = ()
    #: Every document the brief draws on, for the provenance footer.
    source_document_ids: tuple[str, ...] = ()

    @property
    def days_until(self) -> int:
        return (self.appointment_on - self.generated_on).days

    @property
    def is_empty(self) -> bool:
        return not (self.changes or self.medications or self.trends
                    or self.questions or self.duplicate_tests
                    or self.tests_on_file)


def _product_rows(states) -> tuple[ProductRow, ...]:
    from .drugs import resolve

    grouped: dict[tuple[str, MedStatus], list[MedicationState]] = {}
    for st in states:
        key = (st.current_brand_text or "unnamed product", st.status)
        grouped.setdefault(key, []).append(st)

    # Where each molecule is currently accounted for, so an unattributed
    # constituent can say where it went.
    attributed_to = {s.molecule: s.current_brand_text for s in states}

    rows = []
    for (brand, status), members in grouped.items():
        members = sorted(members, key=lambda s: s.molecule)
        accounted = {s.molecule for s in members}
        composition = resolve(brand).molecules
        elsewhere = tuple(
            (mol, attributed_to.get(mol) or "not on the current list")
            for mol in composition
            if mol not in accounted
        )
        seen: list[str] = []
        for s in members:
            if s.open_question and s.open_question not in seen:
                seen.append(s.open_question)
        rows.append(
            ProductRow(
                brand_text=brand,
                molecules=tuple(s.molecule for s in members),
                status=status,
                strengths_mg=tuple(s.current_strength_mg for s in members),
                dose_pattern=next(
                    (s.current_dose_pattern for s in members if s.current_dose_pattern),
                    None,
                ),
                prescribers=tuple(sorted({p for s in members for p in s.prescribers})),
                last_written=max(s.last_mentioned for s in members),
                evidence=tuple(sorted({e for s in members
                                       for e in s.evidence_mention_ids})),
                open_questions=tuple(seen),
                also_contains=elsewhere,
            )
        )
    # Live products first, then the ones we are unsure about, then closed ones.
    order = {
        MedStatus.ACTIVE: 0,
        MedStatus.LIKELY_ACTIVE: 1,
        MedStatus.POSSIBLY_STOPPED: 2,
        MedStatus.UNCERTAIN: 3,
        MedStatus.COURSE_COMPLETED: 4,
    }
    return tuple(sorted(rows, key=lambda r: (order[r.status], r.brand_text)))


def _trend_rows(series, results) -> tuple[TrendRow, ...]:
    by_id = {r.id: r for r in results}
    rows = []
    for s in series:
        # §4 J3: any analyte with three or more points.
        if len(s.points) < 3:
            continue
        rows.append(
            TrendRow(
                analyte=s.analyte,
                display=analyte_display(s.analyte),
                unit=s.canonical_unit,
                direction=s.direction,
                points=tuple(
                    TrendPoint(
                        on=p.doc_date,
                        value=p.canonical_value,
                        lab_name=p.lab_name,
                        ref_low=p.ref_low,
                        ref_high=p.ref_high,
                        document_id=p.document_id,
                        result_id=p.id,
                        raw_value=by_id[p.id].value if p.id in by_id else None,
                        raw_unit=by_id[p.id].unit_raw if p.id in by_id else None,
                    )
                    for p in s.points
                ),
            )
        )
    return tuple(rows)


def _changes(before, now, results, since: date) -> tuple[Change, ...]:
    out: list[Change] = []
    was = {s.molecule: s for s in before.states}
    is_ = {s.molecule: s for s in now.states}

    for molecule in sorted(is_):
        new_state = is_[molecule]
        old_state = was.get(molecule)
        if new_state.status not in ACTIVE_LIKE and new_state.status is not MedStatus.UNCERTAIN:
            continue
        if old_state is None:
            out.append(Change(
                kind=ChangeKind.STARTED,
                subject=molecule,
                detail=(
                    f"{molecule} first appears on the prescription of "
                    f"{_d(new_state.first_seen)}"
                    + (f", written by {new_state.prescribers[0]}"
                       if new_state.prescribers else "")
                    + (f", as {new_state.current_brand_text}"
                       if new_state.current_brand_text else "")
                    + "."
                ),
                evidence=new_state.evidence_mention_ids,
                evidence_dates=(new_state.first_seen,),
                quoted_names=tuple(new_state.prescribers) + tuple(
                    n for n in (new_state.current_brand_text,) if n),
            ))
        elif (old_state.current_strength_mg is not None
              and new_state.current_strength_mg is not None
              and old_state.current_strength_mg != new_state.current_strength_mg):
            out.append(Change(
                kind=ChangeKind.DOSE_CHANGED,
                subject=molecule,
                detail=(
                    f"{molecule} was written at "
                    f"{_num(old_state.current_strength_mg)} mg on "
                    f"{_d(old_state.last_mentioned)} and at "
                    f"{_num(new_state.current_strength_mg)} mg on "
                    f"{_d(new_state.last_mentioned)}."
                ),
                evidence=new_state.evidence_mention_ids,
                evidence_dates=(old_state.last_mentioned, new_state.last_mentioned),
                quoted_names=tuple(new_state.prescribers),
            ))

    for molecule in sorted(was):
        old_state = was[molecule]
        new_state = is_.get(molecule)
        if old_state.status not in ACTIVE_LIKE:
            continue
        if new_state is None or new_state.status in ACTIVE_LIKE:
            continue
        if new_state.status is MedStatus.COURSE_COMPLETED:
            detail = (
                f"{molecule} was a stated course and its last day has passed "
                f"(written {_d(new_state.last_mentioned)})."
            )
        elif new_state.status is MedStatus.POSSIBLY_STOPPED:
            detail = (
                f"{molecule} was on the list on {_d(old_state.last_mentioned)} "
                "and does not appear on the most recent prescription from the "
                "same prescriber."
            )
        else:
            detail = (
                f"{molecule} no longer has a confirmed reading behind it "
                f"(last written {_d(new_state.last_mentioned)})."
            )
        out.append(Change(
            kind=ChangeKind.STOPPED,
            subject=molecule,
            detail=detail,
            evidence=new_state.evidence_mention_ids,
            evidence_dates=(new_state.last_mentioned,),
            quoted_names=tuple(new_state.prescribers),
        ))

    for r in sorted(results, key=lambda r: (r.doc_date, r.id)):
        if r.doc_date <= since:
            continue
        name = analyte_display(r.analyte)
        out.append(Change(
            kind=ChangeKind.NEW_RESULT,
            subject=r.analyte,
            detail=(
                f"{name} measured {_num(r.canonical_value)} {r.canonical_unit} on "
                f"{_d(r.doc_date)}"
                + (f" at {r.lab_name}" if r.lab_name else "")
                + (f" (range printed on that report: {_num(r.ref_low)}"
                   f"–{_num(r.ref_high)})"
                   if r.ref_low is not None and r.ref_high is not None else "")
                + "."
            ),
            evidence=(r.id,),
            evidence_dates=(r.doc_date,),
            quoted_names=tuple(n for n in (r.lab_name,) if n),
        ))

    # §4 J3 gives the order explicitly: new drugs, stopped drugs, dose changes,
    # new results.
    section_order = {
        ChangeKind.STARTED: 0,
        ChangeKind.STOPPED: 1,
        ChangeKind.DOSE_CHANGED: 2,
        ChangeKind.NEW_RESULT: 3,
    }
    return tuple(sorted(out, key=lambda c: (section_order[c.kind], c.subject)))


def build_brief(
    mentions,
    *,
    appointment_on: date,
    as_of: date,
    lab_results=(),
    since: date | None = None,
    trigger_document_id: str | None = None,
    prescriber: str | None = None,
) -> Brief:
    """Assemble the brief. Pure and deterministic.

    `since` is normally the date of the document that scheduled this follow-up —
    the last time this prescriber saw the patient — so "what changed" answers
    the question that prescriber actually has. With no `since`, the changes
    section is omitted rather than guessed at.
    """
    mentions = tuple(mentions)
    lab_results = tuple(lab_results)
    now = reconcile(mentions, as_of=as_of, lab_results=lab_results)

    changes: tuple[Change, ...] = ()
    if since is not None:
        # The state this prescriber last saw: the same function, over the
        # documents that existed then.
        past_mentions = tuple(m for m in mentions if m.doc_date <= since)
        past_labs = tuple(r for r in lab_results if r.doc_date <= since)
        before = reconcile(past_mentions, as_of=since, lab_results=past_labs)
        changes = _changes(before, now, lab_results, since)

    duplicate_tests = now.findings_of(FindingKind.POSSIBLE_DUPLICATE_TEST)
    questions = tuple(
        f for f in now.findings
        if f.kind is not FindingKind.POSSIBLE_DUPLICATE_TEST
    )
    questions = tuple(sorted(
        questions, key=lambda f: (ATTENTION_RANK[f.attention], f.kind.value, f.summary)
    ))

    sources = {m.document_id for m in mentions} | {r.document_id for r in lab_results}

    return Brief(
        appointment_on=appointment_on,
        generated_on=as_of,
        since=since,
        trigger_document_id=trigger_document_id,
        prescriber=prescriber,
        changes=changes,
        medications=_product_rows(now.states),
        trends=_trend_rows(now.series, lab_results),
        questions=questions,
        duplicate_tests=duplicate_tests,
        tests_on_file=_tests_on_file(now.series),
        source_document_ids=tuple(sorted(sources)),
    )


def brief_for(
    appointment: DueAppointment,
    mentions,
    *,
    as_of: date,
    lab_results=(),
) -> Brief:
    """Build the brief for a follow-up the sweep found."""
    doc = appointment.document
    return build_brief(
        mentions,
        appointment_on=appointment.appointment_on,
        as_of=as_of,
        lab_results=lab_results,
        since=doc.doc_date,
        trigger_document_id=doc.id,
        prescriber=doc.prescriber,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _wrap(text: str, *, width: int, indent: str = "", hang: str | None = None):
    """Wrap prose to the target width. NFR-6: this has to read on a 360px phone.

    Findings run long — a DROPPED_WITHOUT_STOP summary shows the rewrite
    arithmetic — and an unwrapped 200-character line inherits a horizontal
    scrollbar on every narrow viewport it reaches.
    """
    body = " ".join((text or "").split())
    if not body:
        return []
    return textwrap.wrap(
        body,
        width=max(width, 24),
        initial_indent=indent,
        subsequent_indent=hang if hang is not None else indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent + body]


def render_text(brief: Brief, *, width: int = 72) -> str:
    """Plain-text brief. Sections in §4 J3 order, every claim carrying a source."""
    rule = "─" * width
    out: list[str] = []

    def para(text, indent="  ", hang=None):
        out.extend(_wrap(text, width=width, indent=indent, hang=hang))
    when = _d(brief.appointment_on)
    days = brief.days_until
    lead = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
    out.append(rule)
    header = f"Appointment {lead} — {when}"
    if brief.prescriber:
        header += f", {brief.prescriber}"
    out.append(header)
    if brief.trigger_document_id and brief.since:
        para(f"Follow-up was written on the prescription of {_d(brief.since)} "
             f"[{brief.trigger_document_id}].", indent="")
    para("Observations and questions only. Nothing here is medical advice.",
         indent="")

    out.append(f"\n1. WHAT CHANGED{'' if brief.since is None else f' SINCE {_d(brief.since).upper()}'}\n{rule}")
    if brief.since is None:
        out.append("  No earlier visit on record, so nothing to compare against.")
    elif not brief.changes:
        out.append("  Nothing changed on paper.")
    else:
        labels = {ChangeKind.STARTED: "started", ChangeKind.STOPPED: "no longer listed",
                  ChangeKind.DOSE_CHANGED: "strength changed",
                  ChangeKind.NEW_RESULT: "new result"}
        for c in brief.changes:
            para(f"[{labels[c.kind]}] {c.detail}", indent="  ", hang="      ")
            para(f"source: {', '.join(c.evidence)}", indent="      ",
                 hang="        ")

    out.append(f"\n2. CURRENT MEDICATION LIST\n{rule}")
    if not brief.medications:
        out.append("  Nothing readable yet.")
    for row in brief.medications:
        strengths = ", ".join(
            f"{m} {_num(s)} mg" if s is not None else f"{m} (strength not attributable)"
            for m, s in zip(row.molecules, row.strengths_mg)
        )
        out.append(f"  {row.brand_text} — {row.status.value}")
        para(strengths, indent="      ", hang="        ")
        line = f"      {', '.join(row.prescribers) or 'unattributed'}"
        if row.dose_pattern:
            line += f" · {row.dose_pattern}"
        out.append(f"{line} · last written {_d(row.last_written)}")
        for mol, where in row.also_contains:
            para(f"also contains {mol} — counted under {where}", indent="      ",
                 hang="        ")
        para(f"source: {', '.join(row.evidence)}", indent="      ",
             hang="        ")
        for q in row.open_questions:
            para(f"? {q}", indent="      ", hang="        ")

    out.append(f"\n3. TRENDS\n{rule}")
    if not brief.trends:
        out.append("  No analyte has three or more measurements yet.")
    for t in brief.trends:
        direction = t.direction or "no consistent direction"
        out.append(f"  {t.display} ({t.unit}) — {direction} across {len(t.points)} measurements")
        for p in t.points:
            ref = (f"ref {_num(p.ref_low)}–{_num(p.ref_high)}"
                   if p.ref_low is not None and p.ref_high is not None
                   else "no range printed")
            raw = ""
            if p.raw_unit and p.raw_unit != t.unit:
                raw = f" (printed {_num(p.raw_value)} {p.raw_unit})"
            # Deliberately tabular and NOT wrapped: the columns are what make a
            # trend readable at a glance. Wide tabular content gets its own
            # horizontal scroll container in the HTML rendering rather than
            # being reflowed (NFR-6).
            out.append(
                f"      {_d(p.on):>14}  {_num(p.value):>7}  "
                f"{p.lab_name or 'lab not named'} · {ref}{raw} [{p.result_id}]"
            )

    out.append(f"\n4. OPEN QUESTIONS\n{rule}")
    if not brief.questions:
        out.append("  Nothing outstanding.")
    for f in brief.questions:
        para(f"[{f.attention.value}] {f.summary}", indent="  ", hang="      ")
        para(f"→ {f.question}", indent="      ", hang="        ")
        para(f"source: {', '.join(f.evidence)}", indent="      ", hang="        ")

    out.append(f"\n5. POSSIBLE DUPLICATE TESTS\n{rule}")
    if not brief.duplicate_tests:
        out.append("  None seen.")
    for f in brief.duplicate_tests:
        para(f.summary, indent="  ", hang="      ")
        para(f"→ {f.question}", indent="      ", hang="        ")
        para(f"source: {', '.join(f.evidence)}", indent="      ", hang="        ")

    out.append(f"\n6. TESTS ALREADY ON FILE\n{rule}")
    if not brief.tests_on_file:
        out.append("  No lab results on file.")
    for t in brief.tests_on_file:
        ago = t.days_ago(brief.generated_on)
        # Prose rather than a table: unlike a trend series, nothing here is
        # compared down a column, so wrapping costs nothing and NFR-6 is served.
        para(f"{t.display} — last measured {_num(t.last_value)} {t.unit} on "
             f"{_d(t.last_measured)}, {ago} days ago. "
             f"{t.result_count} result(s) on file from "
             f"{_join(t.labs) or 'a lab that is not named'}.",
             indent="  ", hang="      ")
        para(f"source: {', '.join(t.evidence)}", indent="      ", hang="        ")

    out.append(f"\n{rule}")
    para(f"Built from {len(brief.source_document_ids)} documents: "
         f"{', '.join(brief.source_document_ids)}", indent="")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI — python -m parchi.brief
# --------------------------------------------------------------------------

def _main() -> int:
    """Run the J3 sweep over the constructed fixture and print what it finds.

    Takes no arguments and no input: that is the point. The appointment date
    comes from a prescription ingested two months earlier (AC-9).
    """
    from .fixtures import AS_OF, DOCUMENTS, LAB_RESULTS, MENTIONS

    due = due_appointments(DOCUMENTS, as_of=AS_OF)
    print(f"Sweep at {_d(AS_OF)} — {len(due)} follow-up(s) inside the "
          f"{LEAD_DAYS}-day window.")
    if not due:
        print("Nothing due. No brief sent.")
        return 0
    for appt in due:
        print(f"  {_d(appt.appointment_on)} "
              f"({appt.days_from(AS_OF)} day(s) away), from {appt.document.id} "
              f"dated {_d(appt.document.doc_date)}")
    for appt in due:
        print()
        print(render_text(brief_for(appt, MENTIONS, as_of=AS_OF,
                                    lab_results=LAB_RESULTS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


# --------------------------------------------------------------------------
# Serialisation for the prescriber view
# --------------------------------------------------------------------------

def as_dict(brief: Brief) -> dict:
    """The brief as structured data, for a view that discloses progressively.

    render_text() gives one block, which is right for a terminal and wrong for a
    prescriber with seven minutes. This keeps the sections separate so the page
    can show the summary and hold the detail behind a tap.
    """
    return {
        "appointment_on": brief.appointment_on.isoformat(),
        "generated_on": brief.generated_on.isoformat(),
        "days_until": brief.days_until,
        "since": brief.since.isoformat() if brief.since else None,
        "trigger_document_id": brief.trigger_document_id,
        "prescriber": brief.prescriber,
        "counts": {
            "changes": len(brief.changes),
            "medications": len(brief.medications),
            "taking_now": sum(1 for r in brief.medications
                              if r.status in ACTIVE_LIKE),
            "questions": len(brief.questions),
            "ask_soon": sum(1 for f in brief.questions
                            if f.attention.value == "ASK_SOON"),
            "duplicate_tests": len(brief.duplicate_tests),
            "tests_on_file": len(brief.tests_on_file),
            "documents": len(brief.source_document_ids),
        },
        "changes": [
            {"kind": c.kind.value, "subject": c.subject, "detail": c.detail,
             "evidence": list(c.evidence)}
            for c in brief.changes
        ],
        "medications": [
            {
                "brand_text": r.brand_text,
                "status": r.status.value,
                "taking_now": r.status in ACTIVE_LIKE,
                "molecules": [
                    {"molecule": m, "strength_mg": s}
                    for m, s in zip(r.molecules, r.strengths_mg)
                ],
                "also_contains": [
                    {"molecule": m, "counted_under": w} for m, w in r.also_contains
                ],
                "dose_pattern": r.dose_pattern,
                "prescribers": list(r.prescribers),
                "last_written": r.last_written.isoformat(),
                "evidence": list(r.evidence),
                "open_questions": list(r.open_questions),
            }
            for r in brief.medications
        ],
        "trends": [
            {
                "analyte": t.display, "unit": t.unit, "direction": t.direction,
                "points": [
                    {"on": p.on.isoformat(), "value": p.value, "lab": p.lab_name,
                     "printed_as": (f"{_num(p.raw_value)} {p.raw_unit}"
                                    if p.raw_unit and p.raw_unit != t.unit else None),
                     "reference": ([p.ref_low, p.ref_high]
                                   if p.ref_low is not None and p.ref_high is not None
                                   else None),
                     "result_id": p.result_id}
                    for p in t.points
                ],
            }
            for t in brief.trends
        ],
        "questions": [
            {"kind": f.kind.value, "attention": f.attention.value,
             "observed": f.summary, "ask": f.question,
             "molecules": list(f.molecules), "evidence": list(f.evidence)}
            for f in brief.questions
        ],
        "duplicate_tests": [
            {"observed": f.summary, "ask": f.question, "evidence": list(f.evidence)}
            for f in brief.duplicate_tests
        ],
        "tests_on_file": [
            {"analyte": t.display, "unit": t.unit,
             "last_measured": t.last_measured.isoformat(),
             "days_ago": t.days_ago(brief.generated_on),
             "last_value": t.last_value, "last_lab": t.last_lab,
             "result_count": t.result_count, "labs": list(t.labs),
             "evidence": list(t.evidence)}
            for t in brief.tests_on_file
        ],
        "source_document_ids": list(brief.source_document_ids),
    }
