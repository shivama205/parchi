"""The agent fleet — PRD §10, built on Google ADK.

WHAT THE AGENTS DO AND DO NOT DO. Parchi's judgement lives in reconcile.py and
is deterministic Python with no model call in it. The agents do not make the
judgement; they decide *when* to look, they carry the conversation, and they
call tools that compute the answer exactly. §10 asks for three registered
agents rather than a monolith, and that is honoured here because each one owns a
distinct responsibility, a distinct instruction and a distinct tool set — not
because three sounded better than one. Wrapping reconcile() in an LlmAgent would
have been theatre, and §10 says plainly: do not fake an integration.

So the fleet is:

    ingest   — owns getting paper into the record. Its one model-driven tool is
               the three-read extraction harness in extract.py.
    records  — owns the derived view: medications, questions, trends, and the
               confirmation loop that resolves an uncertain reading.
    brief    — owns the unprompted brief and the follow-up sweep.

A coordinator routes between them. The sweep that actually triggers J3 needs no
agent at all — see sweep_once() — because nothing about it requires language.

A NOTE ON SAFETY AT THIS LAYER. SR-1 is structural for Findings: a Finding
carrying clinical-claim vocabulary cannot be constructed. That guarantee does
NOT extend to an agent's free prose, which no invariant can constrain from the
inside. The instructions below forbid it, and safe_reply() in server.py screens
what leaves the process. Both are mitigations, not proofs, and the difference is
worth being honest about.
"""

from __future__ import annotations

from dataclasses import dataclass, replace as dc_replace
from datetime import date
from typing import Callable

from .brief import brief_for, build_brief, due_appointments, render_text
from .drugs import mention_from_reading, resolve
from .extract import Transport, VertexTransport, extract, to_mentions
from .labs import normalise_reading as normalise_lab_reading
from .models import ACTIVE_LIKE, Confidence, MedStatus, analyte_display
from .reconcile import reconcile
from .blobs import BlobStore
from .store import Correction, MemoryStore, Store, apply_corrections

MODEL = "gemini-3.5-flash"

#: Carried by every agent in the fleet. This is the product's ethic stated to
#: the model; it is not a substitute for the structural invariants.
ETHIC = """
You are Parchi. You help an adult child coordinate care for an ageing parent by
reading the family's medical paperwork.

Absolute rules, no exceptions:
- You do NOT diagnose, triage, or interpret symptoms.
- You do NOT recommend, change, start or stop any medication or dose.
- You NEVER assert that two drugs interact, or that any drug is safe or unsafe.
- You state observations and pair them with questions for the doctor. The human
  decides. Findings end in a question mark.
- Every claim you make names the document and date it came from. If you cannot
  cite it, do not say it.
- When a reading is uncertain, say so plainly. Never present a guess as fact.
- If asked for medical advice, say that is for the prescriber, and offer to put
  the question on the list for the next appointment instead.

Your tools compute exact answers from the paperwork. Never estimate a
medication list, a strength or a lab value yourself — call the tool and report
what it returns.
""".strip()


@dataclass
class Deps:
    """What the tools need. Injected so tests run without cloud or clock."""

    store: Store
    transport: Transport | None = None
    today: Callable[[], date] = date.today
    blobs: "BlobStore | None" = None

    def read_transport(self) -> Transport:
        if self.transport is None:
            self.transport = VertexTransport()
        return self.transport


# --------------------------------------------------------------------------
# Tool implementations — plain functions, individually testable
# --------------------------------------------------------------------------

def _record(deps: Deps, patient_id: str):
    """Every derived answer starts here: evidence in, corrections applied."""
    mentions = apply_corrections(
        deps.store, patient_id, deps.store.list_mentions(patient_id)
    )
    labs = deps.store.list_lab_results(patient_id)
    return mentions, labs


def tool_list_documents(deps: Deps, patient_id: str) -> dict:
    """List the documents held for a patient, oldest first."""
    docs = deps.store.list_documents(patient_id)
    return {
        "count": len(docs),
        "documents": [
            {
                "document_id": d.id,
                "kind": d.kind.value,
                "date_on_document": d.doc_date.isoformat() if d.doc_date else None,
                "undated": d.doc_date is None,
                "prescriber": d.prescriber,
                "follow_up": d.follow_up_date.isoformat() if d.follow_up_date else None,
            }
            for d in docs
        ],
        "undated_count": sum(1 for d in docs if d.doc_date is None),
    }


def tool_read_document(deps: Deps, patient_id: str, document_id: str) -> dict:
    """Read one stored document image and record what it says.

    Runs the three-read extraction harness. Confidence comes from agreement
    between the reads, never from the model's opinion of itself.
    """
    doc = deps.store.get_document(patient_id, document_id)
    if doc is None:
        return {"error": f"no document {document_id} for this patient"}
    if not doc.source_file:
        return {"error": f"document {document_id} has no image on file"}
    if deps.blobs is None:
        return {"error": "no image store configured"}
    image = deps.blobs.get(doc.source_file)
    mime = "image/png" if doc.source_file.lower().endswith(".png") else "image/jpeg"
    result = extract(image, transport=deps.read_transport(), mime_type=mime)

    doc_date = doc.doc_date or result.doc_date
    if doc_date is None:
        return {
            "document_id": document_id,
            "read": len(result.lines),
            "stored": 0,
            "note": ("no date is legible on this document, so nothing was placed "
                     "on the timeline. Ask the caregiver for the date."),
            "cost_usd": round(result.usage.cost_usd, 5),
        }
    # Write back everything the page told us about itself: the date, who wrote
    # it, where, and when to come back. Without this the timeline shows no
    # prescriber and the J3 sweep has no follow-up to find, even though both
    # were read off the paper.
    deps.store.put_document(dc_replace(
        doc,
        doc_date=doc_date,
        doc_date_inferred=False,
        prescriber=doc.prescriber or result.prescriber,
        facility=doc.facility or result.facility,
        follow_up_on=doc.follow_up_on or result.follow_up_on,
        follow_up_after_days=(doc.follow_up_after_days
                              or result.follow_up_after_days),
    ))

    mentions = to_mentions(
        result,
        document_id=document_id,
        doc_date=doc_date,
        prescriber=doc.prescriber or result.prescriber,
        id_prefix=f"{document_id}-",
    )
    mentions = apply_corrections(deps.store, patient_id, mentions)
    deps.store.put_mentions(patient_id, mentions)
    return {
        "document_id": document_id,
        "kind": result.kind.value,
        "date_on_document": doc_date.isoformat(),
        "read": len(result.lines),
        "stored": len(mentions),
        "prescriber": doc.prescriber or result.prescriber,
        "follow_up": (result.follow_up_text or None),
        "needs_confirmation": [
            {"mention_id": m.id, "we_read": m.brand_text}
            for m in mentions if m.needs_confirmation or not m.is_resolved
        ],
        "cost_usd": round(result.usage.cost_usd, 5),
        "notes": list(result.notes),
    }


def tool_current_medications(deps: Deps, patient_id: str) -> dict:
    """The current medication list, derived from every document on file."""
    mentions, labs = _record(deps, patient_id)
    r = reconcile(mentions, as_of=deps.today(), lab_results=labs)
    return {
        "as_of": r.as_of.isoformat(),
        "medications": [
            {
                "molecule": s.molecule,
                "status": s.status.value,
                "taking_now": s.status in ACTIVE_LIKE,
                "strength_mg": s.current_strength_mg,
                "dose_pattern": s.current_dose_pattern,
                "written_as": s.current_brand_text,
                "prescribers": list(s.prescribers),
                "last_written": s.last_mentioned.isoformat(),
                "evidence": list(s.evidence_mention_ids),
                "open_question": s.open_question,
            }
            for s in r.states
        ],
        "unconfirmed_count": sum(
            1 for s in r.states if s.status is MedStatus.UNCERTAIN),
    }


def tool_open_questions(deps: Deps, patient_id: str) -> dict:
    """Questions worth asking the doctor, most pressing first."""
    mentions, labs = _record(deps, patient_id)
    r = reconcile(mentions, as_of=deps.today(), lab_results=labs)
    return {
        "count": len(r.findings),
        "questions": [
            {
                "kind": f.kind.value,
                "attention": f.attention.value,
                "observed": f.summary,
                "ask": f.question,
                "molecules": list(f.molecules),
                "evidence": list(f.evidence),
            }
            for f in r.findings
        ],
    }


def tool_lab_trends(deps: Deps, patient_id: str) -> dict:
    """Lab values over time, normalised to one unit per analyte."""
    mentions, labs = _record(deps, patient_id)
    r = reconcile(mentions, as_of=deps.today(), lab_results=labs)
    return {
        "trends": [
            {
                "analyte": analyte_display(s.analyte),
                "unit": s.canonical_unit,
                "direction": s.direction,
                "points": [
                    {
                        "on": p.doc_date.isoformat(),
                        "value": p.canonical_value,
                        "lab": p.lab_name,
                        "printed_as": f"{p.value} {p.unit_raw}",
                        "reference_on_that_report": (
                            [p.ref_low, p.ref_high]
                            if p.ref_low is not None and p.ref_high is not None
                            else None
                        ),
                        "result_id": p.id,
                    }
                    for p in s.points
                ],
            }
            for s in r.series
        ]
    }


def tool_confirm_reading(
    deps: Deps, patient_id: str, mention_id: str, corrected_text: str
) -> dict:
    """Record the caregiver's reading of an uncertain entry.

    Pass the text exactly as the caregiver says it appears. If they confirm our
    reading was right, pass that same text back. The correction is retained and
    applied to later documents from the same prescriber without asking again.
    """
    existing = {m.id: m for m in deps.store.list_mentions(patient_id)}
    m = existing.get(mention_id)
    if m is None:
        return {"error": f"no reading {mention_id} on file for this patient"}
    corrected = (corrected_text or "").strip()
    if not corrected:
        return {"error": "corrected_text was empty; nothing recorded"}

    deps.store.put_correction(Correction(
        patient_id=patient_id, prescriber=m.prescriber or "",
        misread=m.brand_text, corrected=corrected, mention_id=mention_id,
    ))
    updated = mention_from_reading(
        id=m.id, document_id=m.document_id, doc_date=m.doc_date,
        brand_text=corrected, prescriber=m.prescriber,
        confidence=Confidence.HIGH, form=m.form, dose_pattern=m.dose_pattern,
        duration_days=m.duration_days, instruction=m.instruction,
        user_confirmed=True,
        original_reading=m.original_reading or m.brand_text,
    )
    deps.store.replace_mention(patient_id, updated)
    res = resolve(corrected)
    return {
        "mention_id": mention_id,
        "was_read_as": m.brand_text,
        "now_recorded_as": corrected,
        "molecules": list(res.molecules),
        "resolved": res.resolved,
        "counts_towards_medication_list": updated.is_usable,
        "note": (
            "recorded, and it will be applied to later documents from "
            f"{m.prescriber or 'this prescriber'} without asking again"
            if res.resolved else
            "recorded, but this product is not in our table, so it still will "
            "not appear on the medication list"
        ),
    }


def tool_upcoming_appointments(deps: Deps, patient_id: str) -> dict:
    """Follow-ups coming up, taken from dates written on earlier prescriptions."""
    today = deps.today()
    due = due_appointments(deps.store.list_documents(patient_id), as_of=today)
    return {
        "as_of": today.isoformat(),
        "appointments": [
            {
                "on": a.appointment_on.isoformat(),
                "days_away": a.days_from(today),
                "written_on_document": a.document.id,
                "written_on": a.document.doc_date.isoformat() if a.document.doc_date else None,
                "prescriber": a.document.prescriber,
            }
            for a in due
        ],
    }


def tool_appointment_brief(deps: Deps, patient_id: str, appointment_date: str) -> dict:
    """Build the one-page brief for an appointment. Date as YYYY-MM-DD."""
    try:
        when = date.fromisoformat(appointment_date)
    except ValueError:
        return {"error": f"could not read {appointment_date!r} as a YYYY-MM-DD date"}
    mentions, labs = _record(deps, patient_id)
    today = deps.today()
    docs = {d.id: d for d in deps.store.list_documents(patient_id)}
    trigger = next(
        (d for d in docs.values() if d.follow_up_date == when), None
    )
    b = build_brief(
        mentions, appointment_on=when, as_of=today, lab_results=labs,
        since=trigger.doc_date if trigger else None,
        trigger_document_id=trigger.id if trigger else None,
        prescriber=trigger.prescriber if trigger else None,
    )
    return {
        "appointment_on": when.isoformat(),
        "days_until": b.days_until,
        "prescriber": b.prescriber,
        "changes_since_last_visit": len(b.changes),
        "medications": len(b.medications),
        "questions": len(b.questions),
        "possible_duplicate_tests": len(b.duplicate_tests),
        "rendered": render_text(b, width=60),
    }


# --------------------------------------------------------------------------
# The sweep — J3, and it needs no agent
# --------------------------------------------------------------------------

def sweep_once(deps: Deps) -> list[dict]:
    """Find follow-ups due across all patients and build their briefs.

    Called by Cloud Scheduler. Deliberately has no LLM in it: finding a date in
    a window and assembling a cited brief is arithmetic, and paying a model to
    do arithmetic on a timer is how a per-patient budget disappears.
    """
    today = deps.today()
    out = []
    for patient_id in deps.store.list_patients():
        due = due_appointments(deps.store.list_documents(patient_id), as_of=today)
        if not due:
            continue
        mentions, labs = _record(deps, patient_id)
        for appt in due:
            b = brief_for(appt, mentions, as_of=today, lab_results=labs)
            out.append({
                "patient_id": patient_id,
                "appointment_on": b.appointment_on.isoformat(),
                "trigger_document": b.trigger_document_id,
                "questions": len(b.questions),
                "rendered": render_text(b, width=60),
            })
    return out


# --------------------------------------------------------------------------
# ADK wiring
# --------------------------------------------------------------------------

def _bind(fn, deps: Deps):
    """Bind deps out of the signature so the model only sees real arguments."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(deps, *args, **kwargs)

    wrapper.__name__ = fn.__name__.removeprefix("tool_")
    doc = fn.__doc__ or ""
    wrapper.__doc__ = doc
    # Drop the leading `deps` parameter from the exposed signature.
    import inspect
    sig = inspect.signature(fn)
    wrapper.__signature__ = sig.replace(
        parameters=[p for name, p in sig.parameters.items() if name != "deps"]
    )
    return wrapper


def build_fleet(deps: Deps):
    """Construct the three agents and the coordinator that routes between them.

    Returns the coordinator. Import of ADK is deferred so the tool functions
    above stay testable without it.
    """
    from google.adk.agents import LlmAgent
    from google.adk.tools import AgentTool, FunctionTool

    ingest = LlmAgent(
        name="ingest",
        model=MODEL,
        description=(
            "Gets paper into the record: lists documents on file and reads an "
            "image to record what it says."
        ),
        instruction=ETHIC + "\n\n" + (
            "You own ingestion. Read a document when asked, then report how many "
            "entries were recorded and which need the caregiver to confirm them. "
            "Never invent a date: if none is legible say the document stays "
            "undated and ask for it."
        ),
        tools=[FunctionTool(_bind(tool_list_documents, deps)),
               FunctionTool(_bind(tool_read_document, deps))],
    )

    records = LlmAgent(
        name="records",
        model=MODEL,
        description=(
            "Owns the derived view: the current medication list, the open "
            "questions, lab trends, and confirming an uncertain reading."
        ),
        instruction=ETHIC + "\n\n" + (
            "You own the record. Report exactly what the tools return, with the "
            "document and date behind each item. Say plainly when something is "
            "unconfirmed and therefore not on the list. When a reading needs "
            "confirming, quote what we read and ask the caregiver what it says — "
            "then record their answer with confirm_reading."
        ),
        tools=[FunctionTool(_bind(tool_current_medications, deps)),
               FunctionTool(_bind(tool_open_questions, deps)),
               FunctionTool(_bind(tool_lab_trends, deps)),
               FunctionTool(_bind(tool_confirm_reading, deps))],
    )

    briefing = LlmAgent(
        name="brief",
        model=MODEL,
        description="Owns upcoming appointments and the one-page brief for them.",
        instruction=ETHIC + "\n\n" + (
            "You own the appointment brief. The brief is already assembled and "
            "cited when the tool returns it — present it, do not rewrite its "
            "claims or add any of your own."
        ),
        tools=[FunctionTool(_bind(tool_upcoming_appointments, deps)),
               FunctionTool(_bind(tool_appointment_brief, deps))],
    )

    return LlmAgent(
        name="parchi",
        model=MODEL,
        description="Coordinates Parchi's ingest, records and brief agents.",
        instruction=ETHIC + "\n\n" + (
            "Route to the right specialist and answer in plain language a "
            "worried adult child can act on. Keep it short. One question at a "
            "time. Use ingest for reading paperwork, records for what the "
            "parent is taking and what to ask, brief for an appointment."
        ),
        tools=[AgentTool(agent=ingest), AgentTool(agent=records),
               AgentTool(agent=briefing)],
    ), {"ingest": ingest, "records": records, "brief": briefing}
