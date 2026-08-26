"""Extraction — PRD §9.

Turns a document image into observations. Everything here is measured rather
than assumed, because the measurements contradicted the PRD in two places.

WHAT THE MEASUREMENTS SAID (26 Aug 2026, 85 handwritten Indian prescriptions,
gemini-3.5-flash, asia-south1):

  1. The model's self-reported confidence is USELESS AS A GATE. It returned
     HIGH on 342 of 382 medication lines — including 88 that matched no
     annotated drug — and LOW eleven times, none of which were correct
     readings. PRD §9.3 asks for a per-field confidence and §5.2/SR-3 gate on
     it. Asking the model how sure it is does not produce a usable signal, so
     this module does not ask. Confidence is DERIVED from agreement between
     independent reads, and the brand table provides the second safety net:
     an unresolvable reading never becomes state (SR-5) whatever the agreement.

  2. Thinking is occasionally decisive and usually waste. Recall 74.3% without
     it, 82.9% with, at 22x the cost — but the entire gain came from one image
     in ten (2/6 -> 5/6 drugs); the other nine were identical. Thinking spend
     is bimodal: median 1,840 tokens, spikes to 62,910. So it is not a quality
     dial to leave on. Cheap reads first, thinking only when the cheap reads
     disagree.

INDEPENDENCE IS BY PROMPT, NOT TEMPERATURE. Two reads at temperature 0 would be
the same read twice and the agreement signal would be vacuous. The two cheap
reads use materially different framings — one asks for a list, the other asks
for a line-by-line transcription — so they fail differently. That is what makes
their agreement mean something.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol, Sequence

from .drugs import match_tokens, mention_from_reading, resolve
from .models import Confidence, DocumentKind, MedicationMention

# --------------------------------------------------------------------------
# Model configuration
# --------------------------------------------------------------------------

#: asia-south1 (Mumbai) carries gemini-3.5-flash and gemini-2.5-flash only —
#: verified against the account, not the docs. 3.5 Flash is the minimum version
#: the hackathon rules accept ("Gemini 3.5 or newer"), so this is both the
#: cheapest and the only qualifying choice in-region. Keeping health data in an
#: Indian region is the BR-19 posture.
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_LOCATION = "asia-south1"

#: USD per million tokens. Thinking tokens bill as OUTPUT. Sourced from pricing
#: aggregators on 26 Aug 2026 because the official Google pricing page would not
#: render; re-verify before quoting these to anybody.
PRICE_IN_PER_MTOK = 1.50
PRICE_OUT_PER_MTOK = 9.00


@dataclass(frozen=True)
class Usage:
    """Token and cost accounting. Additive so a batch can report one total."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            thinking_tokens=self.thinking_tokens + other.thinking_tokens,
        )

    @property
    def billed_output_tokens(self) -> int:
        """Thinking bills as output. Costing on candidates alone understates
        the bill by orders of magnitude when thinking is on."""
        return self.output_tokens + self.thinking_tokens

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1e6 * PRICE_IN_PER_MTOK
            + self.billed_output_tokens / 1e6 * PRICE_OUT_PER_MTOK
        )


# --------------------------------------------------------------------------
# Transport — injectable so the agreement logic is testable without a network
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RawRead:
    """One model response, already parsed."""

    payload: dict
    usage: Usage
    thinking: bool = False


class Transport(Protocol):
    """Anything that can turn an image plus a prompt into structured JSON."""

    def read(
        self, image: bytes, mime_type: str, prompt: str, *, thinking: bool
    ) -> RawRead: ...


#: PRD §9.3 as a schema rather than a request. The malformed-JSON risk is real:
#: response_mime_type alone produced an unparseable array on the first live
#: call, so the shape is enforced by the API instead of hoped for.
#:
#: There is deliberately no confidence field. See the module docstring.
_SCHEMA = {
    "type": "object",
    "properties": {
        "document_kind": {
            "type": "string",
            "enum": ["prescription", "lab_report", "discharge_summary", "unknown"],
        },
        "date_on_document": {"type": "string", "nullable": True},
        "prescriber": {"type": "string", "nullable": True},
        "facility": {"type": "string", "nullable": True},
        "follow_up": {"type": "string", "nullable": True},
        "lab_name": {"type": "string", "nullable": True},
        "lab_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "analyte_raw": {"type": "string"},
                    "value": {"type": "number"},
                    "unit_raw": {"type": "string"},
                    "ref_low": {"type": "number", "nullable": True},
                    "ref_high": {"type": "number", "nullable": True},
                },
                "required": ["analyte_raw", "value", "unit_raw"],
            },
        },
        "medications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "brand_text": {"type": "string"},
                    "dose_pattern": {"type": "string", "nullable": True},
                    "duration_days": {"type": "integer", "nullable": True},
                    "instruction": {"type": "string", "nullable": True},
                    "box_2d": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["brand_text"],
            },
        },
    },
    "required": ["document_kind", "medications"],
}

_CONTRACT = (
    "Rules you must follow exactly:\n"
    "- Give the drug name VERBATIM as written in brand_text. Do not expand an "
    "abbreviation, correct a spelling, or normalise a brand to its generic. "
    "Transcribe what is on the paper.\n"
    "- Return null for any field that is not present. Never infer a date that "
    "is not written on the document.\n"
    "- box_2d is [ymin, xmin, ymax, xmax] as integers normalised to 0-1000, "
    "tightly around that one medication line.\n"
    "- Include every medication order, including ones you find hard to read.\n"
    "- follow_up: copy any instruction to come back, verbatim — a date, or an "
    "interval like 'review after 2 weeks'. Null if none is written.\n"
    "- If this is a LABORATORY REPORT, leave medications empty and transcribe "
    "the results table into lab_results instead: the test name exactly as "
    "printed, the number, the unit exactly as printed, and the reference range "
    "printed on THIS report. Do not convert units and do not substitute a "
    "range from memory — a range belongs to the report it is printed on.\n"
    "- lab_name: the laboratory's name from the header, if this is a report."
)

#: Two framings, not two samples. Independence has to come from the prompt —
#: see the module docstring.
PROMPT_LIST = (
    "This is a medical document from an Indian clinic. Identify what kind of "
    "document it is, then list everything on it: every medication order if it "
    "is a prescription, every row of the results table if it is a laboratory "
    "report.\n\n" + _CONTRACT
)
PROMPT_LINEWISE = (
    "This is a medical document from an Indian clinic. Work down the page one "
    "line at a time. For each numbered, bulleted or separately written drug "
    "order — or each row of a results table — transcribe that single line. Do "
    "not summarise or group lines together, and do not skip a line because it "
    "is untidy.\n\n" + _CONTRACT
)
PROMPT_PHARMACIST = (
    "You are a pharmacist about to dispense against this Indian prescription. "
    "Write down exactly what you would take off the shelf, one entry per item "
    "the doctor has ordered. If you could not safely dispense a line because "
    "you cannot read it, still write down your best reading of it. If the "
    "document is a laboratory report rather than a prescription, switch roles: "
    "you are the technician checking the printout against the analyser before "
    "it is released, so transcribe every result exactly as printed.\n\n"
    + _CONTRACT
)

#: THREE reads, not two, and no thinking. Measured over the same ten images:
#:
#:   1 cheap read                    74.3% recall   $0.0033/image
#:   2 reads + thinking escalation    80.0% recall   $0.0680/image   219s
#:   always thinking                  82.9% recall   $0.0729/image   284s
#:   3 cheap reads (this)             82.9% recall   $0.0162/image    20s
#:
#: Three cheap reads match the recall of always-thinking at a quarter of the
#: cost and a fourteenth of the wall time, and — because thinking spend is
#: bimodal — without the tail that made one image in ten cost $0.58 on its own.
#: That tail mattered: a single escalation consumed the whole NFR-2 monthly
#: budget for a patient.
#:
#: It also calibrates far better. Escalation flagged 1 line in 35; three reads
#: flag 15 in 42 as MEDIUM or LOW, which is the difference between a gate that
#: fires and a gate that does not.
CHEAP_PROMPTS = (PROMPT_LIST, PROMPT_LINEWISE, PROMPT_PHARMACIST)


class VertexTransport:
    """Default transport, via the Google GenAI SDK against Vertex AI."""

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str = DEFAULT_LOCATION,
        model: str = DEFAULT_MODEL,
    ) -> None:
        from google import genai  # imported lazily: tests never need it

        self._types = __import__("google.genai.types", fromlist=["types"])
        self._model = model
        kwargs = {"vertexai": True, "location": location}
        if project:
            kwargs["project"] = project
        self._client = genai.Client(**kwargs)

    def read(
        self, image: bytes, mime_type: str, prompt: str, *, thinking: bool
    ) -> RawRead:
        t = self._types
        config = t.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_SCHEMA,
            # thinking_budget is effectively a boolean: nonzero caps are
            # silently ignored (256 and 1024 both produced ~62,911 tokens on
            # the same image), so only 0 actually disables it.
            thinking_config=None if thinking else t.ThinkingConfig(thinking_budget=0),
        )
        resp = self._client.models.generate_content(
            model=self._model,
            contents=[t.Part.from_bytes(data=image, mime_type=mime_type), prompt],
            config=config,
        )
        u = resp.usage_metadata
        usage = Usage(
            calls=1,
            input_tokens=u.prompt_token_count or 0,
            output_tokens=u.candidates_token_count or 0,
            thinking_tokens=getattr(u, "thoughts_token_count", None) or 0,
        )
        try:
            payload = json.loads(resp.text or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return RawRead(payload=payload, usage=usage, thinking=thinking)


# --------------------------------------------------------------------------
# Extracted lines
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractedLine:
    """One medication line as read, before it becomes an observation."""

    brand_text: str
    dose_pattern: str | None = None
    duration_days: int | None = None
    instruction: str | None = None
    #: (left, top, right, bottom) as fractions of the image, for J2's highlight
    #: overlay. A box, not a crop: the corpus licence forbids derivatives, so
    #: the caregiver is shown the full image with this region marked.
    box: tuple[float, float, float, float] | None = None
    reads_agreeing: int = 0
    reads_total: int = 0

    @property
    def agreement(self) -> float:
        return self.reads_agreeing / self.reads_total if self.reads_total else 0.0

    @property
    def confidence(self) -> Confidence:
        """Derived from agreement between independent reads (see module docs).

        Unanimous is HIGH; a majority is MEDIUM; a single dissenting read is
        LOW and therefore gated by SR-3 until a human confirms it.
        """
        if self.reads_total == 0:
            return Confidence.LOW
        if self.reads_agreeing == self.reads_total:
            return Confidence.HIGH
        if self.reads_agreeing >= 2:
            return Confidence.MEDIUM
        return Confidence.LOW


@dataclass(frozen=True)
class ExtractedLabValue:
    """One row of a results table, as printed, before normalisation."""

    analyte_raw: str
    value: float
    unit_raw: str
    ref_low: float | None = None
    ref_high: float | None = None
    reads_agreeing: int = 0
    reads_total: int = 0
    #: True when the reads returned different numbers for the same analyte.
    #: A disagreement about a value is more serious than one about a name.
    value_disputed: bool = False

    @property
    def confidence(self) -> Confidence:
        if self.value_disputed or self.reads_total == 0:
            return Confidence.LOW
        if self.reads_agreeing == self.reads_total:
            return Confidence.HIGH
        if self.reads_agreeing >= 2:
            return Confidence.MEDIUM
        return Confidence.LOW


@dataclass(frozen=True)
class ExtractionResult:
    kind: DocumentKind = DocumentKind.UNKNOWN
    date_on_document: str | None = None
    doc_date: date | None = None
    prescriber: str | None = None
    facility: str | None = None
    lines: tuple[ExtractedLine, ...] = ()
    lab_values: tuple[ExtractedLabValue, ...] = ()
    lab_name: str | None = None
    #: The follow-up instruction as written, and what it resolves to. This is
    #: what makes J3 unprompted: the appointment date comes off a page read
    #: months earlier, not from anything the caregiver does.
    follow_up_text: str | None = None
    follow_up_on: date | None = None
    follow_up_after_days: int | None = None
    usage: Usage = field(default_factory=Usage)
    #: True when the cheap reads disagreed and a thinking read was spent.
    escalated: bool = False
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Line identity across reads
# --------------------------------------------------------------------------

def line_key(brand_text: str) -> tuple[str, ...]:
    """How two reads are judged to have found the same drug.

    Resolved molecules where the brand is known, so "Tab Telma 40" and
    "TELMA 40MG TAB" count as agreement rather than as two findings. Falling
    back to the normalised words means an unknown brand still matches itself
    without pretending we know what it is.
    """
    res = resolve(brand_text)
    if res.resolved:
        return res.molecules
    # Unknown brand: key on the tokens matching would have used, not the raw
    # words. Otherwise one read writing "T Cepodem 200g" and another
    # "T Cepodem 200mg" invents a second medication out of a unit disagreement.
    return match_tokens(brand_text)


def _parse_box(raw) -> tuple[float, float, float, float] | None:
    """Gemini returns [ymin, xmin, ymax, xmax] on a 0-1000 scale."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (float(v) / 1000.0 for v in raw)
    except (TypeError, ValueError):
        return None
    box = (min(xmin, xmax), min(ymin, ymax), max(xmin, xmax), max(ymin, ymax))
    if not all(0.0 <= v <= 1.0 for v in box):
        return None
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


_INTERVAL_RE = re.compile(
    r"\b(?:after|in|review\s+in|repeat\s+after)\s+(\d{1,3})\s*"
    r"(day|days|week|weeks|month|months|wk|wks|mo)\b",
    re.IGNORECASE,
)
_INTERVAL_UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "wk": 7,
                   "wks": 7, "month": 30, "months": 30, "mo": 30}


def parse_follow_up(text: str | None) -> tuple[date | None, int | None]:
    """Read a follow-up instruction into a date or an interval.

    Prescriptions write both — "Review after 2 weeks", "F/U 12/09/2026", or
    both together. A date is preferred when present because it needs no
    arithmetic; an interval is resolved against the document date later, never
    against the upload date.
    """
    if not text:
        return None, None
    explicit = parse_document_date(text)
    if explicit is not None:
        return explicit, None
    m = _INTERVAL_RE.search(text)
    if m:
        return None, int(m.group(1)) * _INTERVAL_UNITS[m.group(2).lower()]
    return None, None


_DATE_PATTERNS = (
    # Indian convention is day first. An ambiguous numeric date is read
    # DD/MM/YYYY; where the first field exceeds 12 it is unambiguous anyway.
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})\b"), "dmy2"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b"), "dMy"),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "Mdy"),
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_document_date(text: str | None) -> date | None:
    """Parse a date printed on a document, or return None.

    Never guesses. An unparseable or impossible date leaves the document
    undated rather than silently taking the upload date (PRD §4 J1.4).
    """
    if not text:
        return None
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if kind == "dmy":
                d, mo, y = int(m[1]), int(m[2]), int(m[3])
            elif kind == "dmy2":
                d, mo, y = int(m[1]), int(m[2]), 2000 + int(m[3])
            elif kind == "dMy":
                d, y = int(m[1]), int(m[3])
                mo = _MONTHS.get(m[2][:3].lower(), 0)
            else:
                mo = _MONTHS.get(m[1][:3].lower(), 0)
                d, y = int(m[2]), int(m[3])
            return date(y, mo, d)
        except (ValueError, KeyError):
            continue
    return None


def _lines_from(payload: dict) -> dict[tuple[str, ...], ExtractedLine]:
    out: dict[tuple[str, ...], ExtractedLine] = {}
    for item in payload.get("medications") or []:
        if not isinstance(item, dict):
            continue
        brand = (item.get("brand_text") or "").strip()
        if not brand:
            continue
        key = line_key(brand)
        if not key or key in out:
            continue
        duration = item.get("duration_days")
        out[key] = ExtractedLine(
            brand_text=brand,
            dose_pattern=(item.get("dose_pattern") or None),
            duration_days=duration if isinstance(duration, int) else None,
            instruction=(item.get("instruction") or None),
            box=_parse_box(item.get("box_2d")),
        )
    return out


def lab_key(analyte_raw: str) -> str:
    """How two reads are judged to have found the same test.

    The canonical analyte where the label is recognised, so "HbA1c" and
    "HBA1C (Glycosylated Hb)" count as agreement. Otherwise the normalised
    label, so an unknown test still matches itself without pretending we know
    what it is.
    """
    from .labs import _norm_label, canonical_analyte

    return canonical_analyte(analyte_raw) or _norm_label(analyte_raw)


def _lab_rows_from(payload: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in payload.get("lab_results") or []:
        if not isinstance(item, dict):
            continue
        label = (item.get("analyte_raw") or "").strip()
        value = item.get("value")
        unit = (item.get("unit_raw") or "").strip()
        if not label or not unit or not isinstance(value, (int, float)):
            continue
        key = lab_key(label)
        if not key or key in out:
            continue
        low, high = item.get("ref_low"), item.get("ref_high")
        out[key] = {
            "analyte_raw": label,
            "value": float(value),
            "unit_raw": unit,
            "ref_low": float(low) if isinstance(low, (int, float)) else None,
            "ref_high": float(high) if isinstance(high, (int, float)) else None,
        }
    return out


def _merge_lab_values(per_read, total: int) -> tuple[ExtractedLabValue, ...]:
    merged = []
    for key in sorted({k for rows in per_read for k in rows}):
        candidates = [rows[key] for rows in per_read if key in rows]
        values = {round(c["value"], 6) for c in candidates}
        chosen = candidates[0]
        merged.append(ExtractedLabValue(
            analyte_raw=chosen["analyte_raw"],
            value=chosen["value"],
            unit_raw=chosen["unit_raw"],
            ref_low=chosen["ref_low"],
            ref_high=chosen["ref_high"],
            reads_agreeing=len(candidates),
            reads_total=total,
            value_disputed=len(values) > 1,
        ))
    return tuple(merged)


def _first(reads: Sequence[RawRead], field_name: str) -> str | None:
    """First non-empty value for a header field, preferring a thinking read."""
    ordered = sorted(reads, key=lambda r: not r.thinking)
    for r in ordered:
        value = r.payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def combine(reads: Sequence[RawRead]) -> ExtractionResult:
    """Merge independent reads into one result, deriving confidence.

    Pure: no network, no clock. This is where the agreement rule lives, and it
    is the part worth testing.
    """
    reads = list(reads)
    if not reads:
        return ExtractionResult(notes=("no reads",))

    per_read = [_lines_from(r.payload) for r in reads]
    total = len(reads)
    # Prefer the thinking read's own transcription of a line where it has one:
    # it is the more expensive opinion and was only bought because the cheap
    # reads disagreed.
    preference = sorted(range(total), key=lambda i: not reads[i].thinking)

    merged: list[ExtractedLine] = []
    for key in {k for lines in per_read for k in lines}:
        agreeing = sum(1 for lines in per_read if key in lines)
        chosen = next(
            per_read[i][key] for i in preference if key in per_read[i]
        )
        merged.append(replace(chosen, reads_agreeing=agreeing, reads_total=total))

    merged.sort(key=lambda ln: (ln.box[1] if ln.box else 1.0, ln.brand_text))

    kinds = [r.payload.get("document_kind") for r in reads]
    kind = DocumentKind.UNKNOWN
    for candidate in kinds:
        try:
            kind = DocumentKind(candidate)
            break
        except ValueError:
            continue

    notes: list[str] = []
    if len({k for k in kinds if k}) > 1:
        notes.append(f"reads disagreed on document kind: {sorted({k for k in kinds if k})}")
    disagreed = [ln for ln in merged if ln.reads_agreeing < total]
    if disagreed:
        notes.append(
            f"{len(disagreed)} of {len(merged)} lines were not seen by every read"
        )
    lab_values = _merge_lab_values([_lab_rows_from(r.payload) for r in reads],
                                   total)

    disputed = [v.analyte_raw for v in lab_values if v.value_disputed]
    if disputed:
        notes.append(
            f"reads returned different numbers for: {', '.join(disputed)}")
    date_text = _first(reads, "date_on_document")
    follow_up_text = _first(reads, "follow_up")
    follow_up_on, follow_up_after = parse_follow_up(follow_up_text)
    usage = Usage()
    for r in reads:
        usage = usage + r.usage

    return ExtractionResult(
        kind=kind,
        date_on_document=date_text,
        doc_date=parse_document_date(date_text),
        prescriber=_first(reads, "prescriber"),
        facility=_first(reads, "facility"),
        lines=tuple(merged),
        lab_values=lab_values,
        lab_name=_first(reads, "lab_name"),
        follow_up_text=follow_up_text,
        follow_up_on=follow_up_on,
        follow_up_after_days=follow_up_after,
        usage=usage,
        escalated=any(r.thinking for r in reads),
        notes=tuple(notes),
    )


def reads_disagree(reads: Sequence[RawRead]) -> bool:
    """Do the cheap reads see the same set of drugs?"""
    keysets = [set(_lines_from(r.payload)) for r in reads]
    return any(k != keysets[0] for k in keysets[1:])


def extract(
    image: bytes,
    *,
    transport: Transport,
    mime_type: str = "image/jpeg",
    prompts: Sequence[str] = CHEAP_PROMPTS,
    escalate: bool = False,
) -> ExtractionResult:
    """Read one document with several independently-framed prompts.

    Costs $0.0162 per prescription image at the default three prompts, including
    bounding boxes. See CHEAP_PROMPTS for the measurements behind that number.

    `escalate` adds a thinking read when the cheap reads disagree. It is OFF by
    default because it bought nothing over a third cheap prompt and carried a
    cost tail that a per-patient budget cannot absorb. The path is kept, and
    tested, because it is the right lever if a harder document class turns up.

    A LIMIT WORTH NAMING: agreement detects disagreement, not shared omission.
    A drug that every read misses produces no line at all, so it cannot be
    flagged LOW — it is simply absent, and at 82.9% recall roughly one drug in
    six is. The confirmation loop surfaces what we read badly; it cannot surface
    what we never saw. Nothing in this module fixes that.
    """
    reads = [
        transport.read(image, mime_type, prompt, thinking=False) for prompt in prompts
    ]
    if escalate and len(reads) > 1 and reads_disagree(reads):
        reads.append(transport.read(image, mime_type, prompts[0], thinking=True))
    return combine(reads)


def to_lab_results(
    result: ExtractionResult,
    *,
    document_id: str,
    doc_date: date,
    lab_name: str | None = None,
    id_prefix: str = "L",
):
    """Normalise extracted rows into LabResults, plus whatever was refused.

    Returns (results, problems). labs.py refuses an unrecognised analyte or an
    unverified unit rather than converting it, so a refusal is expected output
    and not an error: it becomes something to ask about.
    """
    from .labs import normalise_reading

    results, problems = [], []
    for i, v in enumerate(result.lab_values, start=1):
        out = normalise_reading(
            id=f"{id_prefix}{i:02d}",
            document_id=document_id,
            doc_date=doc_date,
            analyte_raw=v.analyte_raw,
            value=v.value,
            unit_raw=v.unit_raw,
            lab_name=lab_name if lab_name is not None else result.lab_name,
            ref_low=v.ref_low,
            ref_high=v.ref_high,
            confidence=v.confidence,
        )
        if out.ok:
            results.append(out.result)
        else:
            problems.append({"printed_as": f"{v.analyte_raw} {v.value} {v.unit_raw}",
                             "problem": out.problem})
    return tuple(results), tuple(problems)


def to_mentions(
    result: ExtractionResult,
    *,
    document_id: str,
    doc_date: date,
    prescriber: str | None = None,
    id_prefix: str = "m",
) -> tuple[MedicationMention, ...]:
    """Turn extracted lines into immutable observations.

    Confidence comes from agreement, and drugs.mention_from_reading lowers it
    further where normalisation could not establish the product. An unresolvable
    brand carries no molecules and so never reaches medication state (SR-5).
    """
    out = []
    for i, line in enumerate(result.lines, start=1):
        out.append(
            mention_from_reading(
                id=f"{id_prefix}{i:02d}",
                document_id=document_id,
                doc_date=doc_date,
                brand_text=line.brand_text,
                prescriber=prescriber if prescriber is not None else result.prescriber,
                confidence=line.confidence,
                dose_pattern=line.dose_pattern,
                duration_days=line.duration_days,
                instruction=line.instruction,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------
# CLI — python -m parchi.extract <image> [...]
# --------------------------------------------------------------------------

def _main(argv: Sequence[str]) -> int:
    import pathlib
    import sys

    if not argv:
        print(__doc__.strip().split("\n")[0])
        print("\nusage: python -m parchi.extract <image> [<image> ...]")
        return 2

    transport = VertexTransport()
    total = Usage()
    for path_str in argv:
        path = pathlib.Path(path_str)
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        result = extract(path.read_bytes(), transport=transport, mime_type=mime)
        total = total + result.usage

        print(f"\n{'=' * 74}\n{path.name}  —  {result.kind.value}")
        if result.date_on_document:
            parsed = result.doc_date.isoformat() if result.doc_date else "unparseable"
            print(f"date on document: {result.date_on_document!r} -> {parsed}")
        else:
            print("date on document: none legible (document stays undated)")
        for label, value in (("prescriber", result.prescriber),
                             ("facility", result.facility)):
            if value:
                print(f"{label}: {value}")
        print(f"{'-' * 74}")
        for line in result.lines:
            res = resolve(line.brand_text)
            molecules = ", ".join(res.molecules) if res.resolved else "UNRESOLVED"
            box = (f"[{line.box[0]:.2f},{line.box[1]:.2f}"
                   f"-{line.box[2]:.2f},{line.box[3]:.2f}]" if line.box else "no box")
            print(f"  {line.brand_text:30} {line.confidence.value:6} "
                  f"{line.reads_agreeing}/{line.reads_total} reads  {box}")
            print(f"  {'':30} -> {molecules}")
            if line.dose_pattern:
                print(f"  {'':30}    {line.dose_pattern}")
        for note in result.notes:
            print(f"  note: {note}")
        print(f"{'-' * 74}")
        print(f"  {result.usage.calls} calls · "
              f"in {result.usage.input_tokens:,} · out {result.usage.output_tokens:,} · "
              f"thinking {result.usage.thinking_tokens:,} · "
              f"${result.usage.cost_usd:.5f}"
              + ("  [ESCALATED]" if result.escalated else ""))

    if len(argv) > 1:
        print(f"\n{'=' * 74}\ntotal: {total.calls} calls · ${total.cost_usd:.5f} "
              f"· thinking {total.thinking_tokens:,}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
