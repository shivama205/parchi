"""HTTP surface — Cloud Run entry point.

Three jobs: talk to the caregiver, accept a document, and answer the scheduled
sweep that makes J3 unprompted.

WHY safe_reply EXISTS. SR-1 is structural for Findings — one carrying
clinical-claim vocabulary cannot be constructed. That guarantee stops at the
agent's own prose, and a live run showed exactly why it matters: given a finding
reading "Was torsemide discontinued, or was it left off the prescription of
20 Jun 2026?", the model reported "Was Torsemide intended to be stopped
permanently?". Nothing in the evidence supports "permanently".

So everything leaving this process is screened. A reply carrying forbidden
vocabulary is withheld rather than sent, and the caregiver gets the cited
findings instead. This is a mitigation, not a proof: no invariant can constrain
a language model from the inside, and pretending otherwise would be the kind of
overclaim this product exists to avoid.
"""

# NO `from __future__ import annotations` IN THIS FILE, DELIBERATELY.
#
# It turns every annotation into a string, and FastAPI resolves those against
# module globals. Anything declared or imported inside create_app() then cannot
# be found: a request body silently degrades into a query parameter (every POST
# returns 422) and schema generation fails outright (/openapi.json returns 500).
# Both of those shipped to Cloud Run before a route test caught them.

import asyncio
import dataclasses
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from .agent import Deps, build_fleet, sweep_once, tool_read_document
from .blobs import GcsBlobStore, LocalBlobStore, blob_key, content_digest
from .models import Document, DocumentKind, clinical_claim_phrases_in
from .reconcile import reconcile
from .store import FirestoreStore, MemoryStore, Store

APP_NAME = "parchi"


class Ask(BaseModel):
    """One caregiver turn.

    Defined at module level deliberately. `from __future__ import annotations`
    turns every annotation into a string, and FastAPI resolves those against
    module globals — a Pydantic model declared inside create_app() cannot be
    found, so the body silently degrades into a query parameter and every
    request fails validation with a 422.
    """

    message: str
    patient_id: str | None = None
    session_id: str | None = None
    user_id: str = "caregiver"
STATIC = Path(__file__).parent / "static"

WITHHELD = (
    "I have written something I am not willing to send, because it strayed into "
    "language that sounds like medical advice. Here is what the paperwork "
    "actually says instead — every line names the document it came from."
)


def safe_reply(text: str) -> tuple[str, tuple[str, ...]]:
    """Screen model prose before it reaches a caregiver.

    Returns the text to send and the phrases that caused a withholding.
    """
    hits = clinical_claim_phrases_in(text or "")
    if hits:
        return WITHHELD, hits
    return text, ()


def _fallback(deps: Deps, patient_id: str) -> str:
    """What the caregiver sees when a reply is withheld: the cited findings."""
    from .store import apply_corrections

    mentions = apply_corrections(deps.store, patient_id,
                                 deps.store.list_mentions(patient_id))
    r = reconcile(mentions, as_of=deps.today(),
                  lab_results=deps.store.list_lab_results(patient_id))
    lines = []
    for f in r.findings:
        lines.append(f"• {f.summary}")
        lines.append(f"  {f.question}")
        lines.append(f"  source: {', '.join(f.evidence)}")
    return "\n".join(lines) or "Nothing outstanding on the paperwork."


#: How many documents are read at once. Extraction is three Vertex calls per
#: document and entirely I/O bound, so concurrency is cheap; the cap exists to
#: keep a 40-file dump from opening 120 sockets at once.
INGEST_CONCURRENCY = 6


def make_blobs():
    """Cloud Storage in Cloud Run, the filesystem locally."""
    bucket = os.getenv("PARCHI_BUCKET")
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if bucket and project and os.getenv("PARCHI_STORE", "").lower() != "memory":
        return GcsBlobStore(bucket, project=project)
    return LocalBlobStore(os.getenv("PARCHI_BLOB_DIR", "/tmp/parchi-blobs"))


def make_store() -> Store:
    """Firestore in Cloud Run, in-memory locally so the demo needs no project."""
    if os.getenv("PARCHI_STORE", "").lower() == "memory":
        return MemoryStore()
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        return MemoryStore()
    return FirestoreStore(project=project)


def seed_fixture(store: Store) -> str:
    """Load the constructed scenario. Every value is invented (SR-9)."""
    from .fixtures import DOCUMENTS, LAB_RESULTS, MENTIONS

    patient_id = DOCUMENTS[0].patient_id
    for d in DOCUMENTS:
        store.put_document(d)
    store.put_mentions(patient_id, MENTIONS)
    store.put_lab_results(patient_id, LAB_RESULTS)
    return patient_id


def create_app():
    from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import FileResponse, JSONResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    store = make_store()
    blobs = make_blobs()
    # A fixed clock keeps the demo reproducible; unset it for real time.
    pinned = os.getenv("PARCHI_TODAY")
    today = (lambda: date.fromisoformat(pinned)) if pinned else date.today
    deps = Deps(store=store, today=today, blobs=blobs)
    coordinator, fleet = build_fleet(deps)

    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=coordinator, session_service=sessions)

    app = FastAPI(title="Parchi", version="0.1.0")

    def _health():
        return {"ok": True, "agents": ["parchi", *fleet],
                "today": today().isoformat(),
                "store": type(store).__name__}

    # Health lives under /api because a bare /healthz is answered upstream of the
    # container on Cloud Run — the 404 arrives with no Google Frontend header, so
    # the request never reaches this process. /healthz is kept as an alias for
    # local use.
    app.get("/api/health")(_health)
    app.get("/healthz")(_health)

    @app.get("/")
    def index():
        page = STATIC / "index.html"
        if page.exists():
            return FileResponse(page)
        return JSONResponse({"service": "parchi", "see": "/healthz"})

    @app.get("/architecture.svg")
    def architecture():
        """The architecture diagram, served by the thing it describes."""
        page = STATIC / "architecture.svg"
        if page.exists():
            return FileResponse(page, media_type="image/svg+xml")
        raise HTTPException(404, "diagram not bundled")

    @app.post("/api/seed")
    def seed():
        """Load the constructed fixture. Idempotent."""
        patient_id = seed_fixture(store)
        return {"patient_id": patient_id,
                "documents": len(store.list_documents(patient_id)),
                "mentions": len(store.list_mentions(patient_id))}

    @app.get("/api/record/{patient_id}")
    def record(patient_id: str):
        from .agent import tool_current_medications, tool_open_questions

        return {"medications": tool_current_medications(deps, patient_id),
                "questions": tool_open_questions(deps, patient_id)}

    @app.post("/api/ask")
    async def ask(body: Ask):
        session_id = body.session_id
        if not session_id:
            created = await sessions.create_session(
                app_name=APP_NAME, user_id=body.user_id)
            session_id = created.id
        prompt = body.message
        if body.patient_id:
            prompt = f"[patient_id: {body.patient_id}]\n{prompt}"

        parts, tools_used = [], []
        async for ev in runner.run_async(
            user_id=body.user_id, session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if not (ev.content and ev.content.parts):
                continue
            for p in ev.content.parts:
                if getattr(p, "function_call", None):
                    tools_used.append(p.function_call.name)
                if getattr(p, "text", None) and ev.author == coordinator.name:
                    parts.append(p.text)

        raw = "".join(parts).strip()
        sent, withheld = safe_reply(raw)
        payload = {"session_id": session_id, "reply": sent,
                   "tools_used": tools_used}
        if withheld:
            payload["withheld_phrases"] = list(withheld)
            payload["findings"] = _fallback(
                deps, body.patient_id or "p-fixture-1")
        return payload

    @app.post("/api/upload")
    async def upload(
        request: Request,
        patient_id: str = Form("p-fixture-1"),
        files: list[UploadFile] = File(...),
    ):
        """Accept an unordered batch of documents. Returns before reading them.

        J1: the caregiver forwards photographs. Nothing is sorted, labelled or
        renamed first, because any flow that requires that will not be used.
        Reading happens after the response, so a 40-file dump never blocks
        (NFR-1). Cloud Tasks is the production answer; a background task plus
        --no-cpu-throttling is the hackathon one, and CPU throttling would
        silently freeze this work without that flag.
        """
        existing = deps.store.list_documents(patient_id)
        seen: dict[str, str] = {
            doc.content_digest: doc.id for doc in existing if doc.content_digest
        }
        # Numbering continues from what is already on file, and increments only
        # for documents actually accepted, so ids stay contiguous.
        next_number = len(existing) + 1

        accepted, duplicates = [], []
        for index, upload_file in enumerate(files):
            data = await upload_file.read()
            if not data:
                continue
            digest = content_digest(data)
            if digest in seen:
                duplicates.append({"filename": upload_file.filename,
                                   "same_as": seen[digest]})
                continue
            document_id = f"UP{today():%Y%m%d}-{next_number:03d}"
            next_number += 1
            key = blob_key(patient_id, document_id, upload_file.filename or f"f{index}")
            blobs.put(key, data, upload_file.content_type or "image/jpeg")
            seen[digest] = document_id
            deps.store.put_document(Document(
                id=document_id,
                patient_id=patient_id,
                kind=DocumentKind.UNKNOWN,
                source_file=key,
                ingest_status="queued",
                content_digest=digest,
            ))
            accepted.append({"document_id": document_id,
                             "filename": upload_file.filename,
                             "bytes": len(data)})

        if accepted:
            queued_ids = [a["document_id"] for a in accepted]
            asyncio.get_running_loop().run_in_executor(
                None, _ingest_batch, patient_id, queued_ids)

        return {"patient_id": patient_id, "accepted": accepted,
                "duplicates": duplicates, "queued": len(accepted)}

    def _ingest_batch(patient_id: str, document_ids: list[str]) -> None:
        with ThreadPoolExecutor(max_workers=INGEST_CONCURRENCY) as pool:
            list(pool.map(lambda d: _ingest_one(patient_id, d), document_ids))

    def _ingest_one(patient_id: str, document_id: str) -> None:
        doc = deps.store.get_document(patient_id, document_id)
        if doc is None:
            return
        deps.store.put_document(dataclasses.replace(doc, ingest_status="reading"))
        try:
            result = tool_read_document(deps, patient_id, document_id)
        except Exception as exc:  # a bad scan must not take the batch down
            deps.store.put_document(dataclasses.replace(
                doc, ingest_status="failed", ingest_note=str(exc)[:200]))
            return
        fresh = deps.store.get_document(patient_id, document_id) or doc
        if "error" in result:
            deps.store.put_document(dataclasses.replace(
                fresh, ingest_status="failed", ingest_note=result["error"]))
        elif result.get("note"):
            # No legible date. Flagged, never given the upload date (§4 J1.4).
            deps.store.put_document(dataclasses.replace(
                fresh, ingest_status="undated", ingest_note=result["note"]))
        else:
            deps.store.put_document(dataclasses.replace(
                fresh, ingest_status="ready",
                kind=DocumentKind(result.get("kind", "unknown")),
                ingest_note=f"{result['stored']} entries recorded"))

    @app.post("/api/ingest/{patient_id}")
    def ingest_now(patient_id: str):
        """Read anything still queued, synchronously.

        The background path is the normal one; this exists so a demo, a test, or
        a retry after a cold start can drive ingestion without waiting on it.
        """
        queued = [d.id for d in deps.store.list_documents(patient_id)
                  if d.ingest_status in ("queued", "failed")]
        _ingest_batch(patient_id, queued)
        return {"processed": queued}

    @app.get("/api/timeline/{patient_id}")
    def timeline(patient_id: str):
        """Documents in the order they were written, not the order they arrived.

        AC-1. Undated documents are listed separately rather than being slotted
        in under their upload date.
        """
        docs = deps.store.list_documents(patient_id)
        mentions = deps.store.list_mentions(patient_id)
        per_doc: dict[str, int] = {}
        for m in mentions:
            per_doc[m.document_id] = per_doc.get(m.document_id, 0) + 1

        def row(d):
            return {"document_id": d.id, "kind": d.kind.value,
                    "date_on_document": d.doc_date.isoformat() if d.doc_date else None,
                    "prescriber": d.prescriber, "facility": d.facility,
                    "status": d.ingest_status, "note": d.ingest_note,
                    "entries_recorded": per_doc.get(d.id, 0),
                    "follow_up": d.follow_up_date.isoformat() if d.follow_up_date else None}

        dated = [row(d) for d in docs if d.doc_date is not None]
        undated = [row(d) for d in docs if d.doc_date is None]
        counts: dict[str, int] = {}
        for d in docs:
            counts[d.ingest_status] = counts.get(d.ingest_status, 0) + 1
        return {"patient_id": patient_id, "total": len(docs), "counts": counts,
                "timeline": dated, "undated": undated,
                "settled": all(d.ingest_status not in ("queued", "reading")
                               for d in docs)}

    @app.get("/api/brief/{patient_id}")
    def brief_json(patient_id: str, on: str | None = None):
        """The brief as structured data, for the prescriber view.

        Without `on`, uses the next follow-up the sweep would find. The
        prescriber never asks for this; the caregiver hands over the link.
        """
        from .brief import as_dict, brief_for, build_brief, due_appointments
        from .store import apply_corrections

        mentions = apply_corrections(deps.store, patient_id,
                                     deps.store.list_mentions(patient_id))
        labs = deps.store.list_lab_results(patient_id)
        docs = deps.store.list_documents(patient_id)

        if on:
            try:
                when = date.fromisoformat(on)
            except ValueError:
                raise HTTPException(400, f"could not read {on!r} as YYYY-MM-DD")
            trigger = next((d for d in docs if d.follow_up_date == when), None)
            b = build_brief(
                mentions, appointment_on=when, as_of=today(), lab_results=labs,
                since=trigger.doc_date if trigger else None,
                trigger_document_id=trigger.id if trigger else None,
                prescriber=trigger.prescriber if trigger else None)
        else:
            due = due_appointments(docs, as_of=today())
            if not due:
                raise HTTPException(
                    404, "no follow-up on file; pass ?on=YYYY-MM-DD")
            b = brief_for(due[0], mentions, as_of=today(), lab_results=labs)
        return as_dict(b)

    @app.get("/d/{patient_id}")
    def doctor_view(patient_id: str):
        """The prescriber's one-screen history. BO-5's distribution channel."""
        page = STATIC / "doctor.html"
        if page.exists():
            return FileResponse(page)
        return JSONResponse({"see": f"/api/brief/{patient_id}"})

    @app.post("/api/sweep")
    def sweep(request: Request):
        """The J3 trigger. Called by Cloud Scheduler, not by a person.

        No model call: finding a date in a window and assembling a cited brief
        is arithmetic, and paying a model to do arithmetic on a timer is how a
        per-patient budget disappears.
        """
        expected = os.getenv("PARCHI_SWEEP_TOKEN")
        if expected and request.headers.get("x-parchi-token") != expected:
            raise HTTPException(status_code=403, detail="sweep token mismatch")
        briefs = sweep_once(deps)
        return {"as_of": today().isoformat(), "briefs": len(briefs),
                "results": briefs}

    @app.delete("/api/patient/{patient_id}")
    def forget(patient_id: str):
        """BR-20 — removes source documents, observations and corrections."""
        removed = store.delete_patient(patient_id)
        images = blobs.delete_prefix(f"patients/{patient_id}")
        return {"patient_id": patient_id, "records_removed": removed,
                "images_removed": images}

    return app


app = create_app() if os.getenv("PARCHI_EAGER_APP", "1") == "1" else None
