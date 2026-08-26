# Parchi

An agent that reads a family's medical paperwork and produces a medication list
with provenance, plus the questions worth asking at the next appointment.

It does not diagnose, advise, or assert drug interactions. Ever.

Built for the [All Things Agentic hackathon](https://allthingsagentichackathon.devpost.com/).
Category: **Collaborative Partner**.

---

## No real patient data

**Every document, prescriber, lab and value in this repository is constructed.**
The fixture corpus in `tests/fixtures.py` was written by hand for this test
suite: "Ramesh", "Dr Rao", "Dr Iyer", "Dr Menon", the clinics and the lab
results are all invented. No real patient data appears anywhere in this
repository, in the demo, or in any submission artefact. This is SR-9 in the PRD
and it is enforced by an automated test.

Nobody penalises honest fixtures; everyone notices fake precision.

---

## Setup

Requires Python 3.11 or newer.

```bash
git clone <this-repo> && cd parchi
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
```

Run the test suite:

```bash
./.venv/bin/python -m pytest -q
```

See the reconciliation engine work on the constructed scenario — 9 documents,
3 prescribers, 14 months:

```bash
./.venv/bin/python -m parchi.demo
```

---

## What is built

| Component | File | Status |
|---|---|---|
| Data model (PRD §5) | `parchi/models.py` | Done |
| Brand normalisation (§7) | `parchi/drugs.py` | Done — demo-grade table |
| Reconciliation rules (§6) | `parchi/reconcile.py` | Done |
| Safety invariants SR-1…SR-9 (§11) | enforced in models + `tests/` | Done |
| Lab unit conversion (§8) | `parchi/labs.py` | Not started |
| Extraction (§9) | `parchi/extract.py` | Not started |
| Per-prescriber correction memory (§4 J2.3) | — | Not started |
| Brief assembly (§4 J3) | — | Not started |
| ADK agents + Cloud Run (§10) | — | Not started |

---

## The idea in one function

`reconcile()` takes immutable observations and derives everything else. Nothing
is stored as truth, so derived state cannot drift from its evidence.

```python
from datetime import date
from parchi.reconcile import reconcile

result = reconcile(mentions, as_of=date(2026, 8, 26), lab_results=labs)
```

It is pure and deterministic: it takes no clock, mutates no input, and returns
identical output for identical input regardless of the order documents arrived
in. That last property is what makes the unordered bulk upload in J1 safe.

### The central judgement

> Absence of a drug from a prescription usually means nothing, and occasionally
> means everything.

A cardiologist's script omitting the diabetologist's metformin tells us
**nothing** — he was never managing it. The *same* cardiologist, having
previously listed six drugs, writing a fresh script listing five, has
*probably* stopped one. Even then the output is a question, never a conclusion.

Omission counts as evidence of stopping only when it comes from the same
prescriber, and only when that prescriber's new script re-lists at least 60% of
the molecules they had previously written — a comprehensive rewrite rather than
an add-on slip.

`COMPREHENSIVE_REWRITE_THRESHOLD` and `STALE_AFTER_DAYS` are judgement calls,
not derived constants. Too low a threshold and every add-on slip triggers a
false "did the doctor stop this?" question, which teaches the caregiver to
ignore us. Too high and real stops go unnoticed.

---

## Safety invariants

Each has a passing automated test. `test_no_finding_makes_a_clinical_claim`
implements SR-1; if it fails, the product has drifted into practising medicine.

| ID | Invariant | Where |
|---|---|---|
| SR-1 | No finding contains clinical-claim vocabulary | `Finding.__post_init__` + test |
| SR-2 | Every finding question ends in "?" | `Finding.__post_init__` + test |
| SR-3 | An unconfirmed low-confidence reading never leaves `UNCERTAIN` | `MedicationMention.is_usable` |
| SR-4 | Every medication state carries evidence | `MedicationState.__post_init__` |
| SR-5 | An unresolvable brand never becomes state | `drugs.resolve` returns `()` |
| SR-6 | No strength where parsed count ≠ molecule count | `MedicationMention.__post_init__` |
| SR-7 | `reconcile()` is pure and deterministic | test |
| SR-8 | No drug interaction asserted in code or prompts | test scans the source |
| SR-9 | No real patient data; fixtures declared constructed | test scans this README |

Three of these are structural rather than advisory — `Finding` cannot be
constructed with a clinical claim or without a question mark, and
`MedicationMention` refuses to hold a partially attributed strength. Failing
loud is deliberate.

---

## Deliberate deviations from the PRD

Both are documented at the code that implements them.

**Strength attribution is narrower than §6.4.** §6.4 requires the parsed
strength count to equal the molecule count. That is necessary but not
sufficient: "Glycomet GP 1/500" is glimepiride 1 mg + metformin 500 mg, while
the table lists `(metformin, glimepiride)`. Two numbers against two molecules
passes the count test and would attribute both backwards. Strengths are
therefore attributed only for single-molecule products; combinations stay
silent, and the written strength survives verbatim in `brand_text`.

**Confusion-set demotion is conditional.** §7 forces confidence down on any
match inside a known confusion set. Telma and Telmikind are both plain
telmisartan, so mistaking one for the other cannot produce a wrong drug — the
penalty is applied only where members differ molecularly (Pan vs Pan-D,
Glycomet vs Glycomet GP). Applying it to molecularly identical pairs would make
every printed telmisartan prescription unusable for no safety gain. The
demotion is one step (HIGH → MEDIUM → LOW), which is what "forces confidence
down" says.

---

## The brand table is demo-grade

`BRAND_TABLE` in `parchi/drugs.py` was assembled by hand. Brand compositions
change when manufacturers reformulate, and a wrong mapping produces a
confidently incorrect medication list — the single worst failure this product
has. Before any real use it must be replaced with a table derived from an
authoritative source (NPPA ceiling-price notifications, CDSCO listings) with a
human review step. The machine-readability of those sources has not been
verified.

An unlisted variant resolves to nothing and becomes a question. That is the
intended behaviour: `Telma CT` is telmisartan + chlorthalidone, and resolving it
to plain telmisartan would delete a diuretic from the list without a word.

---

## Out of scope, permanently

Diagnosis, triage and symptom checking. Dose recommendations. Drug interaction
assertions. Medicine ordering, lab booking, teleconsultation. Health scores and
streaks. Diagnostic lab partnerships. Advice of any kind.

The product's value rests on not being another party with an interest.
