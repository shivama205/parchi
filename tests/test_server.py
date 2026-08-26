"""HTTP surface tests — the route contract.

These exist because two bugs of the same family reached Cloud Run: `from
__future__ import annotations` made FastAPI unable to resolve types declared
inside create_app(), so every POST body degraded into a query parameter (422)
and schema generation failed (500 on /openapi.json). Neither showed up in any
unit test, and neither was visible until a real request was made.

The agent is never invoked here: /api/ask needs a model, so it is exercised
against the fleet separately. Everything else is deterministic and offline.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("PARCHI_STORE", "memory")
os.environ.setdefault("PARCHI_TODAY", "2026-08-26")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from parchi.server import create_app

    return TestClient(create_app())


def test_the_openapi_schema_can_be_generated(client):
    """A 500 here means an annotation somewhere cannot be resolved.

    This is the check that would have caught the create_app() closure bug before
    it was deployed.
    """
    r = client.get("/openapi.json")
    assert r.status_code == 200, r.text[:400]
    assert r.json()["paths"]


def test_every_post_route_takes_a_body_not_query_parameters(client):
    """The exact shape of the shipped bug: a Pydantic body silently becoming a
    query parameter, so every request failed validation."""
    paths = client.get("/openapi.json").json()["paths"]
    for path, ops in paths.items():
        for verb, op in ops.items():
            if verb != "post" or not path.startswith("/api"):
                continue
            if path == "/api/ask":
                assert "requestBody" in op, f"{path} lost its body"
            for param in op.get("parameters", []) or []:
                assert param.get("name") != "body", f"{path} takes body as a query param"


def test_health_reports_the_fleet(client):
    for path in ("/api/health", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["ok"] is True
        assert body["agents"] == ["parchi", "ingest", "records", "brief"]


def test_the_interface_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Parchi" in r.text
    # SR-9 has to be visible to whoever opens it, not only in the README.
    assert "no real patient data" in r.text.lower()
    assert "not medical advice" in r.text.lower()


def test_seed_then_read_the_derived_record(client):
    seeded = client.post("/api/seed").json()
    assert seeded["documents"] == 9
    assert seeded["mentions"] == 20

    record = client.get(f"/api/record/{seeded['patient_id']}").json()
    meds = record["medications"]["medications"]
    assert len(meds) == 14
    assert sum(1 for m in meds if m["taking_now"]) == 9
    assert record["questions"]["count"] == 8
    # NFR-5 — nothing user-visible without a source.
    for m in meds:
        assert m["evidence"]
    for q in record["questions"]["questions"]:
        assert q["evidence"]
        assert q["ask"].endswith("?")


def test_the_sweep_requires_its_token(client, monkeypatch):
    monkeypatch.setenv("PARCHI_SWEEP_TOKEN", "s3cret")
    assert client.post("/api/sweep").status_code == 403
    assert client.post("/api/sweep", headers={"x-parchi-token": "wrong"}).status_code == 403
    ok = client.post("/api/sweep", headers={"x-parchi-token": "s3cret"})
    assert ok.status_code == 200
    assert ok.json()["briefs"] >= 1


def test_the_sweep_builds_the_unprompted_brief(client):
    client.post("/api/seed")
    body = client.post("/api/sweep").json()
    assert body["briefs"] == 1
    result = body["results"][0]
    assert result["appointment_on"] == "2026-08-27"
    assert result["trigger_document"] == "RX3"
    assert "WHAT CHANGED" in result["rendered"]


def test_deleting_a_patient_removes_everything(client):
    client.post("/api/seed")
    gone = client.delete("/api/patient/p-fixture-1").json()
    assert gone["records_removed"] > 0
    after = client.get("/api/record/p-fixture-1").json()
    assert after["medications"]["medications"] == []
    client.post("/api/seed")


def test_safe_reply_withholds_clinical_prose():
    from parchi.server import WITHHELD, safe_reply

    clean, hits = safe_reply("Torsemide is not on the June script. Was it discontinued?")
    assert hits == () and clean.startswith("Torsemide")
    withheld, hits = safe_reply("You should stop taking it immediately.")
    assert withheld == WITHHELD
    assert set(hits) >= {"you should", "stop taking", "immediately"}


# ==========================================================================
# The prescriber view — BO-5's distribution channel
# ==========================================================================

def test_the_structured_brief_is_served(client):
    client.post("/api/seed")
    r = client.get("/api/brief/p-fixture-1")
    assert r.status_code == 200
    d = r.json()
    assert d["appointment_on"] == "2026-08-27"
    assert d["trigger_document_id"] == "RX3"
    assert d["counts"]["tests_on_file"] == 2


def test_the_structured_brief_accepts_an_explicit_date(client):
    client.post("/api/seed")
    r = client.get("/api/brief/p-fixture-1", params={"on": "2026-09-30"})
    assert r.status_code == 200
    assert r.json()["appointment_on"] == "2026-09-30"


def test_a_bad_date_is_rejected_rather_than_guessed(client):
    r = client.get("/api/brief/p-fixture-1", params={"on": "next tuesday"})
    assert r.status_code == 400


def test_no_follow_up_on_file_is_a_404_not_an_invented_appointment(client):
    client.post("/api/seed")
    client.delete("/api/patient/p-fixture-1")
    r = client.get("/api/brief/p-fixture-1")
    assert r.status_code == 404
    client.post("/api/seed")


def test_the_prescriber_page_is_served(client):
    r = client.get("/d/p-fixture-1")
    assert r.status_code == 200
    assert "Case history" in r.text
    # The boundary has to be visible to the prescriber, not only to the family.
    assert "nothing here is a diagnosis" in r.text.lower()
    assert "no real patient data" in r.text.lower()


# ==========================================================================
# Ingestion — AC-1
# ==========================================================================

def _png(name="scan.png", body=b"\x89PNG\r\n\x1a\n" + b"x" * 64):
    return ("files", (name, body, "image/png"))


def test_an_upload_returns_before_anything_is_read(client, monkeypatch):
    """J1: the interface never blocks on reading (NFR-1)."""
    monkeypatch.setenv("PARCHI_STORE", "memory")
    r = client.post("/api/upload", data={"patient_id": "up1"},
                    files=[_png("a.png"), _png("b.png", b"different bytes")])
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == 2
    assert [a["document_id"] for a in body["accepted"]] == [
        "UP20260826-001", "UP20260826-002"]


def test_identical_bytes_in_one_batch_are_not_read_twice(client):
    same = b"\x89PNG\r\n\x1a\n" + b"identical"
    first = client.post("/api/upload", data={"patient_id": "dup"},
                        files=[("files", ("a.png", same, "image/png"))]).json()
    second = client.post("/api/upload", data={"patient_id": "dup"},
                         files=[("files", ("b.png", same, "image/png"))]).json()
    assert second["accepted"] == []
    assert second["duplicates"][0]["same_as"] == first["accepted"][0]["document_id"]


def test_document_ids_stay_contiguous_across_batches(client):
    a = client.post("/api/upload", data={"patient_id": "seq"},
                    files=[_png("1.png", b"one")]).json()
    b = client.post("/api/upload", data={"patient_id": "seq"},
                    files=[_png("2.png", b"two")]).json()
    assert a["accepted"][0]["document_id"].endswith("-001")
    assert b["accepted"][0]["document_id"].endswith("-002")


def test_ac1_the_timeline_is_ordered_by_the_date_on_the_document(client):
    """Not by upload order, not by id."""
    client.post("/api/seed")
    t = client.get("/api/timeline/p-fixture-1").json()
    dates = [r["date_on_document"] for r in t["timeline"]]
    assert dates == sorted(dates)
    assert len(dates) == 9
    assert t["counts"] == {"ready": 9}
    assert t["settled"] is True


def test_an_undated_document_is_listed_apart_from_the_timeline(client):
    """§4 J1.4 — flagged as undated rather than given the upload date.

    A document with no legible date never enters the timeline, whatever stage
    of reading it is at: queued, unreadable, or read and genuinely undated.
    """
    client.post("/api/seed")
    t = client.get("/api/timeline/p-fixture-1").json()
    assert t["undated"] == []

    client.post("/api/upload", data={"patient_id": "p-fixture-1"},
                files=[_png("late.png", b"unread bytes")])
    t2 = client.get("/api/timeline/p-fixture-1").json()
    assert len(t2["undated"]) == 1
    assert t2["undated"][0]["status"] in ("queued", "reading", "failed", "undated")
    # It must not have been slotted into the timeline under today's date.
    assert [r["date_on_document"] for r in t2["timeline"]] == \
        [r["date_on_document"] for r in t["timeline"]]


def test_deleting_a_patient_also_removes_the_images(client):
    client.post("/api/upload", data={"patient_id": "wipe"},
                files=[_png("x.png", b"some image bytes")])
    gone = client.delete("/api/patient/wipe").json()
    assert gone["records_removed"] >= 1
    assert gone["images_removed"] >= 1
