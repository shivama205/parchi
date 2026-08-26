"""Data model — PRD §5.

Observations are immutable; state is derived (PRD §1.1). A MedicationMention is
never edited once created: a user correction appends a new mention carrying
`original_reading`. Nothing here stores a derived claim as truth — MedicationState
and Finding are recomputed from mentions on every call to reconcile().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class DocumentKind(str, Enum):
    PRESCRIPTION = "prescription"
    LAB_REPORT = "lab_report"
    DISCHARGE_SUMMARY = "discharge_summary"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


class MedStatus(str, Enum):
    ACTIVE = "ACTIVE"
    LIKELY_ACTIVE = "LIKELY_ACTIVE"
    COURSE_COMPLETED = "COURSE_COMPLETED"
    POSSIBLY_STOPPED = "POSSIBLY_STOPPED"
    UNCERTAIN = "UNCERTAIN"


#: Statuses that mean "the patient is plausibly taking this right now". Findings
#: about duplication and parallel prescribing are restricted to these — raising a
#: duplicate-molecule question about a drug we think was stopped is a false
#: positive, and PRD §6.5 says bias conservative.
ACTIVE_LIKE = frozenset({MedStatus.ACTIVE, MedStatus.LIKELY_ACTIVE})


class Attention(str, Enum):
    """How much of the caregiver's attention this deserves.

    Deliberately *not* clinical severity — PRD §5.4. Parchi does not rank
    medical risk.
    """
    ASK_SOON = "ASK_SOON"
    ASK_NEXT_VISIT = "ASK_NEXT_VISIT"
    FYI = "FYI"


ATTENTION_RANK = {Attention.ASK_SOON: 0, Attention.ASK_NEXT_VISIT: 1, Attention.FYI: 2}


class FindingKind(str, Enum):
    DUPLICATE_MOLECULE = "DUPLICATE_MOLECULE"
    PARALLEL_PRESCRIBING = "PARALLEL_PRESCRIBING"
    DROPPED_WITHOUT_STOP = "DROPPED_WITHOUT_STOP"
    DOSE_CHANGED = "DOSE_CHANGED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    POSSIBLE_DUPLICATE_TEST = "POSSIBLE_DUPLICATE_TEST"
    LAB_TREND = "LAB_TREND"


# --------------------------------------------------------------------------
# Safety — SR-1 / SR-2 (PRD §11)
# --------------------------------------------------------------------------

#: SR-1. Verbatim from PRD §11. Matched case-insensitively as substrings, so
#: "diagnos" catches diagnosis/diagnosed/diagnostic.
CLINICAL_CLAIM_PHRASES = (
    "you should",
    "we recommend",
    "dangerous",
    "harmful",
    "overdose",
    "toxic",
    "stop taking",
    "reduce the dose",
    "increase the dose",
    "diagnos",
    "you must",
    "immediately",
)


class ClinicalClaimError(ValueError):
    """Raised when finding copy drifts into practising medicine.

    Failing loud is deliberate. If this fires, the product has crossed the line
    PRD §1.1 principle 3 draws, and that is not something to degrade gracefully
    around.
    """


def clinical_claim_phrases_in(text: str) -> tuple[str, ...]:
    """Return the SR-1 phrases present in `text`, if any."""
    if not text:
        return ()
    low = text.lower()
    return tuple(p for p in CLINICAL_CLAIM_PHRASES if p in low)


def safe_quote(verbatim: str, *, limit: int = 60) -> str:
    """Quote OCR text inside finding copy without letting it breach SR-1.

    Finding copy interpolates only structured fields — molecule names, dates,
    prescribers, strengths — with one exception: NEEDS_CONFIRMATION has to show
    the caregiver what we read. That text is arbitrary model output, so a raw
    interpolation could smuggle a forbidden phrase into a finding and trip the
    invariant on legitimate evidence.

    Forbidden vocabulary is redacted to "[…]" here. The full verbatim reading is
    never lost — it stays on the mention, reachable through the finding's
    evidence ids (SR-4, NFR-5). A finding is a summary, not the evidence itself.
    """
    text = " ".join((verbatim or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    for phrase in clinical_claim_phrases_in(text):
        idx = text.lower().find(phrase)
        while idx != -1:
            text = text[:idx] + "[…]" + text[idx + len(phrase):]
            idx = text.lower().find(phrase)
    return text


# --------------------------------------------------------------------------
# Analyte display names
# --------------------------------------------------------------------------
# Canonical analyte keys are lowercase slugs; user-visible copy needs the
# conventional rendering. Full label canonicalisation and unit conversion is
# PRD §8 and lands in labs.py.

ANALYTE_DISPLAY = {
    "hba1c": "HbA1c",
    "hemoglobin": "haemoglobin",
    "creatinine": "creatinine",
    "glucose_fasting": "fasting glucose",
    "glucose_pp": "post-prandial glucose",
    "total_cholesterol": "total cholesterol",
    "hdl": "HDL",
    "ldl": "LDL",
    "triglycerides": "triglycerides",
    "vitamin_d": "vitamin D (25-OH)",
    "tsh": "TSH",
}


def analyte_display(key: str) -> str:
    """Conventional rendering of a canonical analyte key."""
    return ANALYTE_DISPLAY.get(key, key)


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Document:
    """A source document. PRD §5.1."""

    id: str
    patient_id: str
    kind: DocumentKind
    #: The date printed ON the document — not the upload date. Where no date is
    #: legible this stays None and `doc_date_inferred` explains why; PRD §4 J1.4
    #: forbids silently substituting the upload date.
    doc_date: date | None = None
    doc_date_inferred: bool = False
    captured_at: datetime | None = None
    prescriber: str | None = None
    facility: str | None = None
    source_file: str | None = None
    confidence: Confidence = Confidence.HIGH

    @property
    def is_undated(self) -> bool:
        return self.doc_date is None


# --------------------------------------------------------------------------
# MedicationMention — the immutable observation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MedicationMention:
    """One drug as written on one document. PRD §5.2.

    Append-only. `brand_text` is the evidence and is stored exactly as written;
    normalisation lives in drugs.py where it is testable.
    """

    id: str
    document_id: str
    doc_date: date
    brand_text: str
    prescriber: str | None = None
    #: Resolved molecules, after normalisation. Empty means the brand did not
    #: resolve — SR-5 keeps such a mention out of medication state entirely.
    molecules: tuple[str, ...] = ()
    #: Parallel to `molecules`, or empty. Never partially populated: PRD §6.4
    #: and SR-6 require silence rather than a guess at attribution.
    strengths_mg: tuple[float, ...] = ()
    form: str | None = None
    dose_pattern: str | None = None
    duration_days: int | None = None
    instruction: str | None = None
    confidence: Confidence = Confidence.HIGH
    user_confirmed: bool = False
    original_reading: str | None = None

    def __post_init__(self) -> None:
        if self.strengths_mg and len(self.strengths_mg) != len(self.molecules):
            # SR-6 made structural. A partially-attributed strength is the
            # confidently-wrong claim PRD §6.4 exists to prevent.
            raise ValueError(
                f"strengths_mg has {len(self.strengths_mg)} entries against "
                f"{len(self.molecules)} molecules for {self.brand_text!r}; "
                "SR-6 requires an exact match or silence"
            )

    @property
    def is_resolved(self) -> bool:
        return bool(self.molecules)

    @property
    def needs_confirmation(self) -> bool:
        """LOW confidence and not yet confirmed by a human. PRD §5.2."""
        return self.confidence is Confidence.LOW and not self.user_confirmed

    @property
    def is_usable(self) -> bool:
        """May this observation inform derived medication state?

        SR-3: an unconfirmed LOW reading may not. SR-5: an unresolved brand may
        not. Both appear as open questions instead.
        """
        return self.is_resolved and not self.needs_confirmation

    def course_ends_on(self) -> date | None:
        """Last day of an explicitly stated course, or None if open-ended."""
        if self.duration_days is None:
            return None
        return self.doc_date + timedelta(days=self.duration_days)

    def strength_of(self, molecule: str) -> float | None:
        """Strength for one molecule, or None where attribution is unsafe."""
        if not self.strengths_mg:
            return None
        try:
            return self.strengths_mg[self.molecules.index(molecule)]
        except ValueError:
            return None


# --------------------------------------------------------------------------
# MedicationState — derived, recomputed every call
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MedicationState:
    """What we believe about one molecule right now. PRD §5.3.

    Never persisted as truth. Rebuilt from mentions by reconcile() on demand so
    it cannot drift from its evidence (PRD §1.1 principle 1).
    """

    molecule: str
    status: MedStatus
    first_seen: date
    last_mentioned: date
    #: SR-4 — never empty.
    evidence_mention_ids: tuple[str, ...]
    prescribers: tuple[str, ...] = ()
    current_strength_mg: float | None = None
    current_dose_pattern: str | None = None
    current_brand_text: str | None = None
    #: Populated only where we genuinely cannot resolve the question ourselves.
    #: COURSE_COMPLETED is the one status that closes without asking (PRD §6.3).
    open_question: str | None = None

    def __post_init__(self) -> None:
        if not self.evidence_mention_ids:
            # SR-4 made structural.
            raise ValueError(
                f"MedicationState for {self.molecule!r} carries no evidence; "
                "SR-4 requires at least one mention id"
            )
        if self.open_question and not self.open_question.endswith("?"):
            raise ValueError("open_question must end with '?'")
        if self.open_question:
            hits = clinical_claim_phrases_in(self.open_question)
            if hits:
                raise ClinicalClaimError(
                    f"open_question for {self.molecule!r} contains {hits}"
                )


# --------------------------------------------------------------------------
# Finding
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """An observation paired with a question. PRD §5.4.

    Enforces SR-1 and SR-2 at construction. There is no way to build a Finding
    that asserts a clinical conclusion or that fails to end in a question.
    """

    kind: FindingKind
    attention: Attention
    #: What we observed, in plain language. Process observation only — never a
    #: clinical claim (PRD §1.1 principle 3).
    summary: str
    #: What to ask the doctor. The human decides (principle 4).
    question: str
    evidence: tuple[str, ...] = ()
    evidence_dates: tuple[date, ...] = ()
    molecules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.question.endswith("?"):
            # SR-2.
            raise ValueError(
                f"{self.kind.value} question does not end in '?': {self.question!r}"
            )
        for label, text in (("summary", self.summary), ("question", self.question)):
            hits = clinical_claim_phrases_in(text)
            if hits:
                # SR-1.
                raise ClinicalClaimError(
                    f"{self.kind.value} {label} contains forbidden phrase(s) "
                    f"{hits}: {text!r}"
                )


# --------------------------------------------------------------------------
# Labs — PRD §5.5
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LabResult:
    id: str
    document_id: str
    doc_date: date
    analyte_raw: str
    analyte: str
    value: float
    unit_raw: str
    canonical_value: float
    canonical_unit: str
    lab_name: str | None = None
    #: The range printed on THIS report. PRD §8 — never apply one lab's range to
    #: another lab's value.
    ref_low: float | None = None
    ref_high: float | None = None
    confidence: Confidence = Confidence.HIGH


@dataclass(frozen=True)
class LabSeries:
    analyte: str
    canonical_unit: str
    points: tuple[LabResult, ...] = ()

    @property
    def direction(self) -> str | None:
        """"rising" / "falling" / None. Direction only — PRD §8 forbids
        interpreting what a trend means."""
        vals = [p.canonical_value for p in self.points]
        if len(vals) < 3:
            return None
        if all(b > a for a, b in zip(vals, vals[1:])):
            return "rising"
        if all(b < a for a, b in zip(vals, vals[1:])):
            return "falling"
        return None


@dataclass(frozen=True)
class ReconciliationResult:
    """Everything reconcile() derives. Immutable; safe to hand to the brief."""

    as_of: date
    states: tuple[MedicationState, ...] = ()
    findings: tuple[Finding, ...] = ()
    series: tuple[LabSeries, ...] = ()

    def state_for(self, molecule: str) -> MedicationState | None:
        for s in self.states:
            if s.molecule == molecule:
                return s
        return None

    def findings_of(self, kind: FindingKind) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind is kind)
