"""Persistence — PRD §10.

WHAT IS STORED AND WHAT IS NOT. §1.1 principle 1 is load-bearing here:
observations are immutable, state is derived, nothing is stored as truth. So
this module persists documents, medication mentions, lab results and user
corrections — the evidence — and never persists a MedicationState, a Finding or
a Brief. Those are recomputed from the evidence on every request, which costs
microseconds and makes drift structurally impossible.

RESOLVING BR-7 AGAINST BR-20. BR-7 asks that corrections be retained and applied
to subsequent documents from the same prescriber, calling them "the core data
asset" — wording that implies one shared handwriting model per prescriber across
every patient. BR-20 requires deleting a patient to remove all derived state,
and BR-24 scopes consent to the patient. Those cannot all hold: a cross-patient
prescriber model survives the deletion of any one patient, and no patient
consented to their corrections training a model used for someone else.

Corrections are therefore keyed (patient_id, prescriber, misread_text).

That satisfies BR-20 and BR-24 exactly, and still delivers AC-7 — which asks
that a subsequent document from the same prescriber not re-ask — because AC-7 is
about the same patient's next document. The cross-patient asset is a product
decision that needs a consent model first, and it is deliberately not built.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from typing import Iterable, Protocol

from .models import (
    Confidence,
    Document,
    DocumentKind,
    LabResult,
    MedicationMention,
)


def normalise_reading(text: str) -> str:
    """Key form for a misread reading: case- and punctuation-insensitive."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


@dataclass(frozen=True)
class Correction:
    """A human's correction of one reading, retained per patient and prescriber."""

    patient_id: str
    prescriber: str
    misread: str
    corrected: str
    corrected_at: datetime | None = None
    #: The mention the correction was made against, so the trail stays walkable.
    mention_id: str | None = None

    @property
    def key(self) -> str:
        return correction_key(self.patient_id, self.prescriber, self.misread)


def correction_key(patient_id: str, prescriber: str | None, misread: str) -> str:
    raw = f"{patient_id}\x1f{(prescriber or '').strip().lower()}\x1f{normalise_reading(misread)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Store(Protocol):
    """Everything the agent tools need from persistence."""

    def put_document(self, doc: Document) -> None: ...
    def get_document(self, patient_id: str, document_id: str) -> Document | None: ...
    def list_documents(self, patient_id: str) -> tuple[Document, ...]: ...
    def put_mentions(self, patient_id: str, mentions: Iterable[MedicationMention]) -> None: ...
    def list_mentions(self, patient_id: str) -> tuple[MedicationMention, ...]: ...
    def replace_mention(self, patient_id: str, mention: MedicationMention) -> None: ...
    def put_lab_results(self, patient_id: str, results: Iterable[LabResult]) -> None: ...
    def list_lab_results(self, patient_id: str) -> tuple[LabResult, ...]: ...
    def put_correction(self, correction: Correction) -> None: ...
    def find_correction(
        self, patient_id: str, prescriber: str | None, misread: str
    ) -> Correction | None: ...
    def list_patients(self) -> tuple[str, ...]: ...
    def delete_patient(self, patient_id: str) -> int: ...


# --------------------------------------------------------------------------
# In-memory — used by tests and by the local demo
# --------------------------------------------------------------------------

class MemoryStore:
    """A Store that keeps everything in dicts. No I/O, fully deterministic."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Document]] = {}
        self._mentions: dict[str, dict[str, MedicationMention]] = {}
        self._labs: dict[str, dict[str, LabResult]] = {}
        self._corrections: dict[str, Correction] = {}

    def put_document(self, doc: Document) -> None:
        self._docs.setdefault(doc.patient_id, {})[doc.id] = doc

    def get_document(self, patient_id: str, document_id: str) -> Document | None:
        return self._docs.get(patient_id, {}).get(document_id)

    def list_documents(self, patient_id: str) -> tuple[Document, ...]:
        docs = self._docs.get(patient_id, {}).values()
        # Undated documents sort last rather than being assigned a date (§4 J1.4).
        return tuple(sorted(docs, key=lambda d: (d.doc_date is None, d.doc_date or date.min, d.id)))

    def put_mentions(self, patient_id: str, mentions) -> None:
        bucket = self._mentions.setdefault(patient_id, {})
        for m in mentions:
            bucket[m.id] = m

    def list_mentions(self, patient_id: str) -> tuple[MedicationMention, ...]:
        return tuple(sorted(self._mentions.get(patient_id, {}).values(),
                            key=lambda m: (m.doc_date, m.id)))

    def replace_mention(self, patient_id: str, mention: MedicationMention) -> None:
        self._mentions.setdefault(patient_id, {})[mention.id] = mention

    def put_lab_results(self, patient_id: str, results) -> None:
        bucket = self._labs.setdefault(patient_id, {})
        for r in results:
            bucket[r.id] = r

    def list_lab_results(self, patient_id: str) -> tuple[LabResult, ...]:
        return tuple(sorted(self._labs.get(patient_id, {}).values(),
                            key=lambda r: (r.doc_date, r.id)))

    def put_correction(self, correction: Correction) -> None:
        self._corrections[correction.key] = correction

    def find_correction(self, patient_id, prescriber, misread) -> Correction | None:
        return self._corrections.get(correction_key(patient_id, prescriber, misread))

    def list_patients(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._docs) | set(self._mentions) | set(self._labs)))

    def delete_patient(self, patient_id: str) -> int:
        """BR-20 — removes source documents, observations AND corrections."""
        removed = 0
        for bucket in (self._docs, self._mentions, self._labs):
            removed += len(bucket.pop(patient_id, {}) or {})
        for key in [k for k, c in self._corrections.items()
                    if c.patient_id == patient_id]:
            del self._corrections[key]
            removed += 1
        return removed


# --------------------------------------------------------------------------
# Serialisation — shared by the Firestore store
# --------------------------------------------------------------------------

_DATE_FIELDS = {"doc_date", "captured_at", "follow_up_on", "corrected_at"}


def to_doc(obj) -> dict:
    """Dataclass -> a Firestore-safe dict. Enums flattened, dates isoformatted."""
    out = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if isinstance(value, (DocumentKind, Confidence)):
            value = value.value
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif isinstance(value, tuple):
            value = list(value)
        out[f.name] = value
    return out


def _revive(raw, cls):
    kwargs = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        value = raw[f.name]
        if value is None:
            kwargs[f.name] = None
            continue
        if f.name == "kind":
            value = DocumentKind(value)
        elif f.name == "confidence":
            value = Confidence(value)
        elif f.name in _DATE_FIELDS and isinstance(value, str):
            value = (datetime.fromisoformat(value) if f.name == "captured_at"
                     or f.name == "corrected_at" else date.fromisoformat(value))
        elif isinstance(value, list):
            value = tuple(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def from_document(raw: dict) -> Document:
    return _revive(raw, Document)


def from_mention(raw: dict) -> MedicationMention:
    return _revive(raw, MedicationMention)


def from_lab_result(raw: dict) -> LabResult:
    return _revive(raw, LabResult)


def from_correction(raw: dict) -> Correction:
    return _revive(raw, Correction)


# --------------------------------------------------------------------------
# Firestore
# --------------------------------------------------------------------------

class FirestoreStore:
    """Store backed by Firestore in native mode.

    Layout keeps everything for one patient under one document, so BR-20's
    "remove all derived state and source documents" is a subtree delete rather
    than a scan:

        patients/{patient_id}/documents/{document_id}
        patients/{patient_id}/mentions/{mention_id}
        patients/{patient_id}/labs/{result_id}
        patients/{patient_id}/corrections/{key}

    Corrections live under the patient deliberately — see the module docstring.
    """

    def __init__(self, *, project: str | None = None, database: str = "(default)"):
        from google.cloud import firestore  # lazily: tests use MemoryStore

        self._db = firestore.Client(project=project, database=database)

    def _patient(self, patient_id: str):
        return self._db.collection("patients").document(patient_id)

    def put_document(self, doc: Document) -> None:
        self._patient(doc.patient_id).collection("documents").document(
            doc.id).set(to_doc(doc))

    def get_document(self, patient_id: str, document_id: str) -> Document | None:
        snap = self._patient(patient_id).collection("documents").document(
            document_id).get()
        return from_document(snap.to_dict()) if snap.exists else None

    def list_documents(self, patient_id: str) -> tuple[Document, ...]:
        docs = [from_document(s.to_dict())
                for s in self._patient(patient_id).collection("documents").stream()]
        return tuple(sorted(docs, key=lambda d: (d.doc_date is None,
                                                 d.doc_date or date.min, d.id)))

    def put_mentions(self, patient_id: str, mentions) -> None:
        batch = self._db.batch()
        col = self._patient(patient_id).collection("mentions")
        for m in mentions:
            batch.set(col.document(m.id), to_doc(m))
        batch.commit()

    def list_mentions(self, patient_id: str) -> tuple[MedicationMention, ...]:
        out = [from_mention(s.to_dict())
               for s in self._patient(patient_id).collection("mentions").stream()]
        return tuple(sorted(out, key=lambda m: (m.doc_date, m.id)))

    def replace_mention(self, patient_id: str, mention: MedicationMention) -> None:
        self._patient(patient_id).collection("mentions").document(
            mention.id).set(to_doc(mention))

    def put_lab_results(self, patient_id: str, results) -> None:
        batch = self._db.batch()
        col = self._patient(patient_id).collection("labs")
        for r in results:
            batch.set(col.document(r.id), to_doc(r))
        batch.commit()

    def list_lab_results(self, patient_id: str) -> tuple[LabResult, ...]:
        out = [from_lab_result(s.to_dict())
               for s in self._patient(patient_id).collection("labs").stream()]
        return tuple(sorted(out, key=lambda r: (r.doc_date, r.id)))

    def put_correction(self, correction: Correction) -> None:
        self._patient(correction.patient_id).collection("corrections").document(
            correction.key).set(to_doc(correction))

    def find_correction(self, patient_id, prescriber, misread) -> Correction | None:
        key = correction_key(patient_id, prescriber, misread)
        snap = self._patient(patient_id).collection("corrections").document(key).get()
        return from_correction(snap.to_dict()) if snap.exists else None

    def list_patients(self) -> tuple[str, ...]:
        return tuple(sorted(d.id for d in
                            self._db.collection("patients").list_documents()))

    def delete_patient(self, patient_id: str) -> int:
        removed = 0
        patient = self._patient(patient_id)
        for name in ("documents", "mentions", "labs", "corrections"):
            for snap in patient.collection(name).stream():
                snap.reference.delete()
                removed += 1
        patient.delete()
        return removed


# --------------------------------------------------------------------------
# Applying retained corrections — the other half of AC-7
# --------------------------------------------------------------------------

def apply_corrections(
    store: Store, patient_id: str, mentions: Iterable[MedicationMention]
) -> tuple[MedicationMention, ...]:
    """Rewrite readings this patient's caregiver has already corrected.

    The second half of AC-7: a later document from the same prescriber carrying
    the same misreading is corrected without asking again. The corrected mention
    keeps `original_reading`, so the trail from paper to list stays walkable
    (NFR-5), and it is marked user_confirmed so SR-3 lets it inform state.
    """
    from .drugs import mention_from_reading

    out = []
    for m in mentions:
        found = store.find_correction(patient_id, m.prescriber, m.brand_text)
        if found is None or normalise_reading(found.corrected) == normalise_reading(m.brand_text):
            out.append(m)
            continue
        out.append(
            mention_from_reading(
                id=m.id,
                document_id=m.document_id,
                doc_date=m.doc_date,
                brand_text=found.corrected,
                prescriber=m.prescriber,
                confidence=Confidence.HIGH,
                form=m.form,
                dose_pattern=m.dose_pattern,
                duration_days=m.duration_days,
                instruction=m.instruction,
                user_confirmed=True,
                original_reading=m.brand_text,
            )
        )
    return tuple(out)
