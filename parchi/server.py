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

import os
import uuid
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from .agent import Deps, build_fleet, sweep_once
from .models import clinical_claim_phrases_in
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
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    store = make_store()
    # A fixed clock keeps the demo reproducible; unset it for real time.
    pinned = os.getenv("PARCHI_TODAY")
    today = (lambda: date.fromisoformat(pinned)) if pinned else date.today
    deps = Deps(store=store, today=today)
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
        return {"patient_id": patient_id, "records_removed": removed}

    return app


app = create_app() if os.getenv("PARCHI_EAGER_APP", "1") == "1" else None
