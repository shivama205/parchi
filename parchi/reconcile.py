"""Reconciliation — PRD §6. This module is the product.

The central judgement (§6.1):

    Absence of a drug from a prescription usually means nothing, and
    occasionally means everything.

A cardiologist's script omitting the diabetologist's metformin tells us nothing
— he was never managing it. The *same* cardiologist, having previously listed
six drugs, writing a fresh script listing five, has probably stopped one. Even
then the output is a question, never a conclusion.

reconcile() is pure and deterministic (§6.6, SR-7). It takes no clock: `as_of`
is a required argument.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .models import (
    ACTIVE_LIKE,
    ATTENTION_RANK,
    Attention,
    Finding,
    FindingKind,
    LabResult,
    LabSeries,
    MedStatus,
    MedicationMention,
    MedicationState,
    ReconciliationResult,
    analyte_display,
    safe_quote,
)

# --------------------------------------------------------------------------
# Tunable judgements — NOT derived constants (PRD §6.2)
# --------------------------------------------------------------------------
# Changing these changes product behaviour. Too low a rewrite threshold and every
# add-on slip triggers a false "did the doctor stop this?" question, which
# teaches the caregiver to ignore us. Too high and real stops go unnoticed. Tune
# against real prescriptions; do not reason about it. Change the test
# deliberately or not at all.

COMPREHENSIVE_REWRITE_THRESHOLD = 0.6
MIN_PRIOR_FOR_REWRITE_TEST = 2
STALE_AFTER_DAYS = 180
DUPLICATE_TEST_WINDOW_DAYS = 45

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _d(value: date) -> str:
    """Locale-independent date rendering, so output is deterministic (SR-7)."""
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def _num(value: float) -> str:
    return f"{value:g}"


def _join(items) -> str:
    """"A", "A and B", "A, B and C"."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _order(m: MedicationMention) -> tuple[date, str]:
    """Total order over mentions. Ties on doc_date break on id for determinism."""
    return (m.doc_date, m.id)


# --------------------------------------------------------------------------
# §6.2 — comprehensive rewrite detection
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PrescriberDoc:
    """One prescriber's molecules on one document, with the rewrite verdict."""

    prescriber: str
    document_id: str
    doc_date: date
    molecules: frozenset[str]
    is_comprehensive_rewrite: bool
    prior_count: int
    relisted_count: int
    ratio: float | None


def _prescriber_docs(usable: tuple[MedicationMention, ...]) -> tuple[PrescriberDoc, ...]:
    """Classify each prescriber-document as a comprehensive rewrite or not.

    §6.2: a prescription qualifies as a comprehensive rewrite by its prescriber
    when it re-lists at least COMPREHENSIVE_REWRITE_THRESHOLD of the molecules
    that prescriber had previously put the patient on, given at least
    MIN_PRIOR_FOR_REWRITE_TEST prior molecules.

    Only named prescribers are classified. "Same prescriber" (§6.1) requires
    identity, and an unattributed document cannot establish it.
    """
    by_prescriber: dict[str, dict[str, list]] = defaultdict(dict)
    for m in usable:
        if not m.prescriber:
            continue
        docs = by_prescriber[m.prescriber]
        entry = docs.setdefault(m.document_id, [m.doc_date, set()])
        entry[1].update(m.molecules)

    out: list[PrescriberDoc] = []
    for prescriber in sorted(by_prescriber):
        ordered = sorted(
            by_prescriber[prescriber].items(), key=lambda kv: (kv[1][0], kv[0])
        )
        for doc_id, (doc_date, molecules) in ordered:
            prior: set[str] = set()
            for other_id, (other_date, other_mols) in ordered:
                if other_date < doc_date:          # strictly earlier documents
                    prior |= other_mols
            ratio: float | None = None
            is_rewrite = False
            relisted = len(prior & molecules)
            if len(prior) >= MIN_PRIOR_FOR_REWRITE_TEST:
                ratio = relisted / len(prior)
                is_rewrite = ratio >= COMPREHENSIVE_REWRITE_THRESHOLD
            out.append(
                PrescriberDoc(
                    prescriber=prescriber,
                    document_id=doc_id,
                    doc_date=doc_date,
                    molecules=frozenset(molecules),
                    is_comprehensive_rewrite=is_rewrite,
                    prior_count=len(prior),
                    relisted_count=relisted,
                    ratio=ratio,
                )
            )
    return tuple(out)


def _rewrites_by(docs: tuple[PrescriberDoc, ...], prescriber: str) -> tuple[PrescriberDoc, ...]:
    return tuple(
        d for d in docs if d.prescriber == prescriber and d.is_comprehensive_rewrite
    )


def _superseding_rewrite(
    docs: tuple[PrescriberDoc, ...], molecule: str, last: MedicationMention
) -> PrescriberDoc | None:
    """The rewrite that suggests `molecule` was dropped, if there is one. §6.3.3.

    A later comprehensive rewrite by the prescriber who last wrote the drug, on
    which the drug does not appear. Returns the earliest such rewrite — that is
    the point at which the omission first became evidence.
    """
    if not last.prescriber:
        return None
    candidates = [
        d
        for d in _rewrites_by(docs, last.prescriber)
        if d.doc_date > last.doc_date and molecule not in d.molecules
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: (d.doc_date, d.document_id))


def _latest_rewrite(
    docs: tuple[PrescriberDoc, ...], prescriber: str | None
) -> PrescriberDoc | None:
    if not prescriber:
        return None
    rewrites = _rewrites_by(docs, prescriber)
    if not rewrites:
        return None
    return max(rewrites, key=lambda d: (d.doc_date, d.document_id))


# --------------------------------------------------------------------------
# §6.3 — status derivation
# --------------------------------------------------------------------------

def _derive_state(
    molecule: str,
    mentions: tuple[MedicationMention, ...],
    docs: tuple[PrescriberDoc, ...],
    as_of: date,
) -> MedicationState:
    """Status for one molecule, evaluating §6.3's rules in order."""
    usable = tuple(m for m in mentions if m.is_usable)
    evidence = usable or mentions
    first_seen = min(m.doc_date for m in evidence)
    last_mentioned = max(m.doc_date for m in evidence)
    prescribers = tuple(sorted({m.prescriber for m in evidence if m.prescriber}))
    evidence_ids = tuple(m.id for m in sorted(evidence, key=_order))

    common = dict(
        molecule=molecule,
        first_seen=first_seen,
        last_mentioned=last_mentioned,
        evidence_mention_ids=evidence_ids,
        prescribers=prescribers,
    )

    # 1. All mentions unusable -> UNCERTAIN. Name the document date and our
    #    proposed reading, and do not let it inform anything else (SR-3).
    if not usable:
        latest = max(mentions, key=_order)
        return MedicationState(
            status=MedStatus.UNCERTAIN,
            current_brand_text=latest.brand_text,
            open_question=(
                f'The entry read as "{safe_quote(latest.brand_text)}" on the '
                f"document of {_d(latest.doc_date)} has not been confirmed. "
                "Is that reading correct?"
            ),
            **common,
        )

    last = max(usable, key=_order)
    current = dict(
        current_strength_mg=last.strength_of(molecule),
        current_dose_pattern=last.dose_pattern,
        current_brand_text=last.brand_text,
    )

    # 2. Explicit course elapsed -> COURSE_COMPLETED. The one case we can close
    #    without asking anybody.
    ends_on = last.course_ends_on()
    if ends_on is not None and ends_on < as_of:
        return MedicationState(
            status=MedStatus.COURSE_COMPLETED, open_question=None, **common, **current
        )

    # 3. The last-writing prescriber later produced a comprehensive rewrite that
    #    omits it -> POSSIBLY_STOPPED. A question, never a conclusion (§6.1).
    superseding = _superseding_rewrite(docs, molecule, last)
    if superseding is not None:
        return MedicationState(
            status=MedStatus.POSSIBLY_STOPPED,
            open_question=(
                f"Was {molecule} discontinued by {last.prescriber}, or left off "
                f"the prescription of {_d(superseding.doc_date)}?"
            ),
            **common,
            **current,
        )

    # 4. Present in that prescriber's most recent comprehensive rewrite -> ACTIVE.
    latest_rewrite = _latest_rewrite(docs, last.prescriber)
    if latest_rewrite is not None and molecule in latest_rewrite.molecules:
        return MedicationState(
            status=MedStatus.ACTIVE, open_question=None, **common, **current
        )

    # 5. Otherwise LIKELY_ACTIVE, with a staleness question where the trail has
    #    gone cold and no end date was ever written.
    question = None
    age = (as_of - last.doc_date).days
    if age > STALE_AFTER_DAYS and last.duration_days is None:
        question = (
            f"{molecule} was last written on {_d(last.doc_date)}, more than "
            f"{STALE_AFTER_DAYS} days ago, with no end date on the prescription. "
            "Is it still being taken?"
        )
    return MedicationState(
        status=MedStatus.LIKELY_ACTIVE, open_question=question, **common, **current
    )


# --------------------------------------------------------------------------
# Currency of individual products
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _LiveProduct:
    molecules: tuple[str, ...]
    mention: MedicationMention


def _live_products(
    molecule: str,
    usable: tuple[MedicationMention, ...],
    docs: tuple[PrescriberDoc, ...],
    as_of: date,
) -> tuple[_LiveProduct, ...]:
    """The distinct products currently delivering `molecule`.

    §6.5 defines a duplicate as one active molecule present in two or more
    distinct products (distinct = different resolved molecule tuple). Taken
    literally over all history that misfires: a patient moved from Glycomet to
    Janumet a year ago has metformin under two tuples and is not taking two
    products.

    So a product counts only while it is plausibly still being taken — its
    course has not elapsed, no later rewrite by its own prescriber dropped it,
    and the trail has not gone cold past STALE_AFTER_DAYS. §6.5 says bias
    conservative: better to miss a subtle finding than to produce a false one.
    """
    by_tuple: dict[tuple[str, ...], list[MedicationMention]] = defaultdict(list)
    for m in usable:
        by_tuple[m.molecules].append(m)

    live: list[_LiveProduct] = []
    for tup in sorted(by_tuple):
        latest = max(by_tuple[tup], key=_order)
        ends_on = latest.course_ends_on()
        if ends_on is not None and ends_on < as_of:
            continue
        if _superseding_rewrite(docs, molecule, latest) is not None:
            continue
        if (as_of - latest.doc_date).days > STALE_AFTER_DAYS:
            continue
        live.append(_LiveProduct(molecules=tup, mention=latest))
    return tuple(live)


def _describe(product: _LiveProduct) -> str:
    who = product.mention.prescriber or "an unattributed prescription"
    return f"{product.mention.brand_text} ({who}, {_d(product.mention.doc_date)})"


# --------------------------------------------------------------------------
# §6.5 — findings
# --------------------------------------------------------------------------

def _duplicate_molecule_findings(
    states: tuple[MedicationState, ...],
    live: dict[str, tuple[_LiveProduct, ...]],
) -> list[Finding]:
    out: list[Finding] = []
    for state in states:
        if state.status not in ACTIVE_LIKE:
            continue
        products = live.get(state.molecule, ())
        if len(products) < 2:
            continue
        prescribers = {p.mention.prescriber for p in products if p.mention.prescriber}
        attention = (
            Attention.ASK_SOON if len(prescribers) > 1 else Attention.ASK_NEXT_VISIT
        )
        described = _join(_describe(p) for p in products)
        names = _join(p.mention.brand_text for p in products)
        out.append(
            Finding(
                kind=FindingKind.DUPLICATE_MOLECULE,
                attention=attention,
                summary=(
                    f"{state.molecule} reaches the patient through "
                    f"{len(products)} separate products: {described}."
                ),
                question=(
                    f"Is {state.molecule} intended twice over, through both "
                    f"{names}?"
                ),
                evidence=tuple(p.mention.id for p in products),
                evidence_dates=tuple(
                    sorted(p.mention.doc_date for p in products)
                ),
                molecules=(state.molecule,),
            )
        )
    return out


def _parallel_prescribing_findings(
    states: tuple[MedicationState, ...],
    live: dict[str, tuple[_LiveProduct, ...]],
) -> list[Finding]:
    """§6.5 — name the specific disjoint pairs.

    Claiming "no overlap" across all prescribers is false the moment any two of
    them share a drug, and a finding that overstates its evidence is worse than
    no finding at all.
    """
    by_prescriber: dict[str, set[str]] = defaultdict(set)
    ids: dict[str, set[str]] = defaultdict(set)
    for state in states:
        if state.status not in ACTIVE_LIKE:
            continue
        for product in live.get(state.molecule, ()):
            if product.mention.prescriber:
                by_prescriber[product.mention.prescriber].add(state.molecule)
                ids[product.mention.prescriber].add(product.mention.id)

    names = sorted(by_prescriber)
    if len(names) < 2:
        return []

    disjoint: list[tuple[str, str]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not (by_prescriber[a] & by_prescriber[b]):
                disjoint.append((a, b))
    if not disjoint:
        return []

    involved = sorted({n for pair in disjoint for n in pair})

    def _mols(name: str) -> str:
        return _join(sorted(by_prescriber[name]))

    if len(disjoint) == 1:
        a, b = disjoint[0]
        summary = (
            f"{a} and {b} each have active prescriptions with no molecule in "
            f"common — {a}: {_mols(a)}; {b}: {_mols(b)}."
        )
    else:
        pairs_text = "; ".join(
            f"{a} ({_mols(a)}) and {b} ({_mols(b)})" for a, b in disjoint
        )
        summary = (
            f"{len(disjoint)} pairs of prescribers have active prescriptions "
            f"with no molecule in common — {pairs_text}."
        )
    return [
        Finding(
            kind=FindingKind.PARALLEL_PRESCRIBING,
            attention=Attention.ASK_NEXT_VISIT,
            summary=summary,
            question=(
                "Does each of these prescribers have the current list written "
                "by the other?"
            ),
            evidence=tuple(sorted({i for n in involved for i in ids[n]})),
            molecules=tuple(sorted({m for n in involved for m in by_prescriber[n]})),
        )
    ]


def _dropped_without_stop_findings(
    states: tuple[MedicationState, ...],
    by_molecule: dict[str, tuple[MedicationMention, ...]],
    docs: tuple[PrescriberDoc, ...],
) -> list[Finding]:
    out: list[Finding] = []
    for state in states:
        if state.status is not MedStatus.POSSIBLY_STOPPED:
            continue
        usable = tuple(m for m in by_molecule[state.molecule] if m.is_usable)
        last = max(usable, key=_order)
        rewrite = _superseding_rewrite(docs, state.molecule, last)
        if rewrite is None:                      # pragma: no cover - status implies it
            continue
        out.append(
            Finding(
                kind=FindingKind.DROPPED_WITHOUT_STOP,
                attention=Attention.ASK_NEXT_VISIT,
                summary=(
                    f"{state.molecule} ({last.brand_text}) was on "
                    f"{rewrite.prescriber}'s list until {_d(last.doc_date)}. "
                    f"Their prescription of {_d(rewrite.doc_date)} re-lists "
                    f"{rewrite.relisted_count} of the {rewrite.prior_count} "
                    "molecules they had previously written, and does not "
                    "include this one."
                ),
                question=(
                    f"Was {state.molecule} discontinued, or was it left off "
                    f"the prescription of {_d(rewrite.doc_date)}?"
                ),
                evidence=(last.id,),
                evidence_dates=(last.doc_date, rewrite.doc_date),
                molecules=(state.molecule,),
            )
        )
    return out


def _dose_changed_findings(
    by_molecule: dict[str, tuple[MedicationMention, ...]],
) -> list[Finding]:
    """Same molecule, same prescriber, strength differs between consecutive
    mentions (§6.5). Only the most recent change per pair is reported — the
    caregiver needs the current position, not a changelog."""
    out: list[Finding] = []
    for molecule in sorted(by_molecule):
        by_prescriber: dict[str, list[MedicationMention]] = defaultdict(list)
        for m in by_molecule[molecule]:
            if m.is_usable and m.prescriber and m.strength_of(molecule) is not None:
                by_prescriber[m.prescriber].append(m)
        for prescriber in sorted(by_prescriber):
            seq = sorted(by_prescriber[prescriber], key=_order)
            change: tuple[MedicationMention, MedicationMention] | None = None
            for earlier, later in zip(seq, seq[1:]):
                if earlier.strength_of(molecule) != later.strength_of(molecule):
                    change = (earlier, later)
            if change is None:
                continue
            earlier, later = change
            old = _num(earlier.strength_of(molecule))
            new = _num(later.strength_of(molecule))
            out.append(
                Finding(
                    kind=FindingKind.DOSE_CHANGED,
                    attention=Attention.FYI,
                    summary=(
                        f"{prescriber} wrote {molecule} at {old} mg on "
                        f"{_d(earlier.doc_date)} and at {new} mg on "
                        f"{_d(later.doc_date)}."
                    ),
                    question=f"Is {new} mg the current strength of {molecule}?",
                    evidence=(earlier.id, later.id),
                    evidence_dates=(earlier.doc_date, later.doc_date),
                    molecules=(molecule,),
                )
            )
    return out


def _needs_confirmation_findings(
    mentions: tuple[MedicationMention, ...],
) -> list[Finding]:
    out: list[Finding] = []
    for m in sorted(mentions, key=_order):
        unresolved = not m.is_resolved
        low = m.needs_confirmation
        if not (unresolved or low):
            continue
        quoted = safe_quote(m.brand_text)
        if unresolved:
            summary = (
                f'The entry written as "{quoted}" on the document of '
                f"{_d(m.doc_date)} does not match any product we know."
            )
            question = f'What does "{quoted}" read as?'
        else:
            summary = (
                f'The entry on the document of {_d(m.doc_date)} reads to us as '
                f'"{quoted}", with low confidence.'
            )
            question = f'Does this entry read as "{quoted}"?'
        out.append(
            Finding(
                kind=FindingKind.NEEDS_CONFIRMATION,
                attention=Attention.ASK_SOON,
                summary=summary,
                question=question,
                evidence=(m.id,),
                evidence_dates=(m.doc_date,),
                molecules=m.molecules,
            )
        )
    return out


# --------------------------------------------------------------------------
# Labs — §6.5, §8
# --------------------------------------------------------------------------

def _lab_series(results: tuple[LabResult, ...]) -> tuple[LabSeries, ...]:
    by_analyte: dict[str, list[LabResult]] = defaultdict(list)
    for r in results:
        by_analyte[r.analyte].append(r)
    out: list[LabSeries] = []
    for analyte in sorted(by_analyte):
        points = tuple(sorted(by_analyte[analyte], key=lambda r: (r.doc_date, r.id)))
        out.append(
            LabSeries(
                analyte=analyte,
                canonical_unit=points[0].canonical_unit,
                points=points,
            )
        )
    return tuple(out)


def _lab_findings(series: tuple[LabSeries, ...]) -> list[Finding]:
    out: list[Finding] = []
    for s in series:
        # POSSIBLE_DUPLICATE_TEST — same analyte twice inside the window at
        # different labs.
        for earlier, later in zip(s.points, s.points[1:]):
            gap = (later.doc_date - earlier.doc_date).days
            if gap > DUPLICATE_TEST_WINDOW_DAYS:
                continue
            if not (earlier.lab_name and later.lab_name):
                continue
            if earlier.lab_name == later.lab_name:
                continue
            name = analyte_display(s.analyte)
            out.append(
                Finding(
                    kind=FindingKind.POSSIBLE_DUPLICATE_TEST,
                    attention=Attention.ASK_SOON,
                    summary=(
                        f"{name} was measured on {_d(earlier.doc_date)} at "
                        f"{earlier.lab_name} and again on {_d(later.doc_date)} "
                        f"at {later.lab_name}, {gap} days apart."
                    ),
                    question=(
                        f"Was the earlier {name} result available when the "
                        "second test was ordered?"
                    ),
                    evidence=(earlier.id, later.id),
                    evidence_dates=(earlier.doc_date, later.doc_date),
                )
            )
        # LAB_TREND — three or more points, monotonic across the series.
        direction = s.direction
        if direction is None:
            continue
        first, last = s.points[0], s.points[-1]
        labs = sorted({p.lab_name for p in s.points if p.lab_name})
        name = analyte_display(s.analyte)
        out.append(
            Finding(
                kind=FindingKind.LAB_TREND,
                attention=Attention.FYI,
                summary=(
                    f"{name} has been {direction} across "
                    f"{len(s.points)} measurements, from "
                    f"{_num(first.canonical_value)} {s.canonical_unit} on "
                    f"{_d(first.doc_date)} to {_num(last.canonical_value)} "
                    f"{s.canonical_unit} on {_d(last.doc_date)}"
                    + (f" ({_join(labs)})." if labs else ".")
                ),
                question=f"Has the {name} trend been reviewed?",
                evidence=tuple(p.id for p in s.points),
                evidence_dates=tuple(p.doc_date for p in s.points),
            )
        )
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _finding_sort_key(f: Finding) -> tuple:
    return (
        ATTENTION_RANK[f.attention],
        f.kind.value,
        f.molecules,
        f.evidence,
        f.summary,
    )


def reconcile(
    mentions,
    *,
    as_of: date,
    lab_results=(),
) -> ReconciliationResult:
    """Derive medication state, findings and lab series from observations.

    Pure and deterministic (§6.6, SR-7): inputs are never mutated, no clock is
    read, and every collection is explicitly ordered before it is returned.
    """
    all_mentions = tuple(sorted(mentions, key=_order))
    all_labs = tuple(sorted(lab_results, key=lambda r: (r.doc_date, r.id)))

    usable = tuple(m for m in all_mentions if m.is_usable)
    docs = _prescriber_docs(usable)

    # Mentions that resolved to at least one molecule can be keyed by molecule.
    # Unresolved brands have no key and stay out of state entirely (SR-5); they
    # surface as NEEDS_CONFIRMATION instead.
    by_molecule: dict[str, list[MedicationMention]] = defaultdict(list)
    for m in all_mentions:
        for molecule in m.molecules:
            by_molecule[molecule].append(m)
    frozen_by_molecule = {k: tuple(v) for k, v in by_molecule.items()}

    states = tuple(
        _derive_state(molecule, frozen_by_molecule[molecule], docs, as_of)
        for molecule in sorted(frozen_by_molecule)
    )

    live = {
        molecule: _live_products(
            molecule,
            tuple(m for m in frozen_by_molecule[molecule] if m.is_usable),
            docs,
            as_of,
        )
        for molecule in sorted(frozen_by_molecule)
    }

    series = _lab_series(all_labs)

    findings: list[Finding] = []
    findings += _duplicate_molecule_findings(states, live)
    findings += _parallel_prescribing_findings(states, live)
    findings += _dropped_without_stop_findings(states, frozen_by_molecule, docs)
    findings += _dose_changed_findings(frozen_by_molecule)
    findings += _needs_confirmation_findings(all_mentions)
    findings += _lab_findings(series)

    return ReconciliationResult(
        as_of=as_of,
        states=states,
        findings=tuple(sorted(findings, key=_finding_sort_key)),
        series=series,
    )
