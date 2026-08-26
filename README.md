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

Run the test suite (332 tests):

```bash
./.venv/bin/python -m pytest -q
```

See the reconciliation engine work on the constructed scenario — 9 documents,
3 prescribers, 14 months:

```bash
./.venv/bin/python -m parchi.demo
```

Fetch the external handwriting corpus (needed for extraction work, not for the
tests):

```bash
./.venv/bin/python -m pip install -e '.[fixtures]'
./.venv/bin/python fetch_fixtures.py
```

## External fixture corpus

Handwritten prescriptions come from a 100-record subset of the MIRAGE corpus,
released by its authors on HuggingFace:
[chaithanyakota/100-handwritten-medical-records](https://huggingface.co/datasets/chaithanyakota/100-handwritten-medical-records).
Licence **CC BY-ND 4.0**. Attribution: *MIRAGE: Multimodal Identification and
Recognition of Annotations in Indian General Prescriptions*
([arXiv:2410.09729](https://arxiv.org/abs/2410.09729)); the full 743,118-image
corpus is proprietary to Medyug Technology Pvt. Ltd. and is not public.

The NoDerivatives term shapes two design decisions:

- **Images are fetched, never redistributed.** `fetch_fixtures.py` makes the
  corpus reproducible without this repository carrying it, and
  `fixtures/external/` is gitignored. Image bytes are written exactly as stored,
  with no re-encoding.
- **J2 draws a highlight box over the full image rather than showing a crop.**
  §9.3 requires the caregiver to see what the model was looking at; an overlay
  achieves that without producing a modified copy.

The dataset card does not state whether these prescriptions are real or
simulated, and this repository does not assert either beyond what each source
says. The MIRAGE paper describes its own corpus as simulated records.

Against the current brand table, 70 of 344 annotated drug lines resolve
(20.3%). The rest become `NEEDS_CONFIRMATION` findings, which is the designed
behaviour for an unknown product rather than a failure — but it is also a direct
measure of how demo-grade the table is.

---

## What is built

| Component | File | Status |
|---|---|---|
| Data model (PRD §5) | `parchi/models.py` | Done |
| Brand normalisation (§7) | `parchi/drugs.py` | Done — demo-grade table |
| Reconciliation rules (§6) | `parchi/reconcile.py` | Done |
| Safety invariants SR-1…SR-9 (§11) | enforced in models + `tests/` | Done |
| Lab unit conversion (§8) | `parchi/labs.py` | Done — every factor cited |
| Extraction (§9) | `parchi/extract.py` | Done — agreement-based confidence |
| Bulk ingestion + timeline (AC-1) | `parchi/server.py` | Done |
| Printed fixture generator | `make_documents.py` | Done — 13 documents |
| Document images | `parchi/blobs.py` | Done — Cloud Storage, asia-south1 |
| Per-prescriber correction memory (§4 J2.3) | — | Not started |
| Unprompted brief + sweep (§4 J3) | `parchi/brief.py` | Done |
| Brief assembly (§4 J3) | `parchi/brief.py` | Done — deterministic |
| Prescriber view (BO-5) | `parchi/static/doctor.html` | Done |
| ADK agent fleet (§10) | `parchi/agent.py` | Done — 3 agents + coordinator |
| Firestore persistence | `parchi/store.py` | Done — asia-south1 |
| HTTP service + chat UI | `parchi/server.py` | Done — live on Cloud Run |
| ADK agents + Cloud Run (§10) | — | Not started |

---

## Extraction: confidence is measured, not asked for

PRD §9.3 asks the model for a per-field confidence, and §5.2/SR-3 gate on it.
Measured against 85 handwritten Indian prescriptions, that signal does not
exist: `gemini-3.5-flash` returned HIGH on **342 of 382** medication lines,
including 88 that matched no annotated drug, and returned LOW **eleven** times
— none of them correct readings. It essentially never expresses doubt.

So `extract.py` does not ask. Confidence is **derived from agreement between
independent reads**: unanimous is HIGH, a majority is MEDIUM, a single
dissenting read is LOW and therefore gated by SR-3 until a human confirms it.
Independence comes from the *prompt*, not from temperature — two reads at
temperature 0 with one prompt would be the same read twice, so the two cheap
reads use materially different framings (a list, and a line-by-line
transcription) and fail differently.

The brand table is the second net. Both reads agreeing on garbage is still
garbage, and an unresolvable reading never becomes medication state (SR-5)
whatever its agreement.

### Three cheap reads beat thinking

Measured over the same ten prescriptions:

| configuration | recall | cost/image | wall (10 images) | lines flagged |
|---|---|---|---|---|
| 1 cheap read | 74.3% | $0.0033 | — | none possible |
| 2 reads + thinking escalation | 80.0% | $0.0680 | 219s | 1 of 35 |
| always thinking | 82.9% | $0.0729 | 284s | unusable |
| **3 cheap reads (shipped)** | **82.9%** | **$0.0162** | **20s** | **15 of 42** |

Three independently framed cheap reads match the recall of always-thinking at a
quarter of the cost and a fourteenth of the wall time — and calibrate far
better, flagging 15 lines of 42 rather than 1 of 35.

They also remove a cost tail that a per-patient budget cannot absorb. Thinking
spend is **bimodal**: median 1,840 tokens, spiking to 62,910. One escalation in
ten images cost $0.58 on its own, which is the entire NFR-2 monthly allowance
for a patient. Nonzero `thinkingBudget` values are silently ignored (256 and
1024 both produced ~62,911 tokens on one image), so it is effectively a boolean
and the spend cannot be capped. The escalation path is kept and tested, but off.

### A limit worth naming

Agreement detects disagreement, not shared omission. A drug that every read
misses produces no line at all, so it cannot be flagged — it is simply absent,
and at 82.9% recall roughly one drug in six is. The confirmation loop surfaces
what was read badly; it cannot surface what was never seen. Nothing in this
codebase fixes that, and a caregiver should not be told the list is complete.

## Two audiences, one derivation

**The family** gets the chat interface at `/` — the current list, the open
questions, and an agent that answers from the paperwork.

**The prescriber** gets `/d/{patient_id}` — a case history the caregiver hands
over. Seven minutes is the design constraint, so it opens on six counts and two
visible sections (what changed since your last visit, and what the family is
asking), with the medication list, lab trends and tests-on-file held behind a
tap. Native `<details>`, so it works with no JavaScript and reads on a printed
page.

Both are the same `reconcile()` call. Nothing is computed twice and nothing is
stored, so the two views cannot disagree.

### What was deliberately not built

**Adherence.** We only ever see paper. A prescription records what was
*prescribed*, never what was *taken*, and a gap between scripts could be a
switched pharmacy, a sample pack, a hospital admission, or a strip bought
without one. Inferring skipped doses from documents would be the
confidently-wrong claim this codebase is arranged to prevent.

What exists instead is the honest version: `STALE_AFTER_DAYS` surfaces
*"metformin was last written 200 days ago with no end date — is it still being
taken?"* — an observation about paper, paired with a question. A
caregiver-reported log ("he stopped the Zoryl last week", cited to the person who
said it) is the right next step, and it needs a conversation layer rather than a
new inference.

## The unprompted brief

```bash
./.venv/bin/python -m parchi.brief
```

Takes no arguments and no input, which is the point. A scheduled sweep finds a
follow-up date extracted from a prescription two months old and builds the brief
without anybody asking (AC-9). Sections run in the order §4 J3 specifies,
because that is the order a prescriber with seven minutes reads in.

**Assembled in code, not by a model.** §9.2 lists brief assembly as a Flash
task. It is deterministic Python here for one reason: SR-1 forbids
clinical-claim vocabulary in anything a caregiver sees, and an invariant you can
only check *after* generation is not an invariant. Every sentence comes from a
template that the SR-1 test itself covers. Translation to Hindi (BR-13) is a
model task — that is a language problem, not a judgement one, and it runs after
the English has already passed.

**"What changed" is a diff of two reconciliations.** `reconcile()` is pure and
takes `as_of` as an argument, so the state the prescriber last saw is just
`reconcile()` over the documents that existed then. No second code path and no
snapshot to drift out of date.

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

Lab conversions carry a further structural check: `test_every_conversion_cites_a_source`
fails if any factor is added without provenance, which is §8's citation
requirement expressed as a test rather than a good intention.

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

## Lab conversions and their sources

Factors are verified, not remembered. Primary sources are cited at each entry in
`parchi/labs.py`:

- **[UKKA]** UK Kidney Association, [Appendix I — Laboratory Conversion Factors](https://www.ukkidney.org/sites/renal.org/files/Appen-I.pdf)
  — cholesterol, creatinine, glucose, and the g/L → g/dL basis.
- **[NGSP]** NGSP, [IFCC Standardization](https://ngsp.org/ifcc.asp) — the HbA1c
  master equation, `NGSP% = 0.09148 × IFCC + 2.152`. This is the one conversion
  that is affine rather than a scale factor; multiplying by a factor would be
  badly wrong.
- **[MOLAR]** Derived from molar mass, stated so the arithmetic can be checked
  rather than trusted — used where [UKKA] rounds (creatinine, glucose,
  cholesterol) and for the two analytes it does not list (triglycerides at
  885.7 g/mol, calcidiol at 400.65 g/mol).

An unrecognised analyte label or an unverified unit is **refused**, not
converted — the same posture `drugs.py` takes toward an unlisted brand. The raw
value and unit are never discarded, and a printed reference range is converted
with the same function as its own value and never reused across reports.

## The brand table is demo-grade

`BRAND_TABLE` in `parchi/drugs.py` was assembled by hand. Brand compositions
change when manufacturers reformulate, and a wrong mapping produces a
confidently incorrect medication list — the single worst failure this product
has. Before any real use it must be replaced with a table derived from an
authoritative source (NPPA ceiling-price notifications, CDSCO listings) with a
human review step. The machine-readability of those sources has not been
verified.

Nineteen brands were added from the corpus's most frequent unresolved names,
each composition checked against Indian pharmacy or manufacturer sources —
Cipla's own product page for Montair FX, Sanofi India's prescribing information
for Lantus. Several candidates were **left out** because verification came back
thin or contradictory: one search returned *Volini*, a pain spray, for *Volix*,
and another asserted a composition for Vertin that conflicted with other
sources. A wrong mapping is the worst failure this product has, so an
unverifiable brand stays out and becomes a question instead.

### Suffix-dropping is guarded structurally

A reading that loses its suffix resolves cleanly to the base product and
silently drops a molecule — "Telma H" read as "Telma" deletes a diuretic. Any
brand whose name is a strict prefix of another brand's is therefore treated as
confusable with its longer siblings, **derived from the table's own shape**
rather than maintained by hand. Adding a combination product automatically makes
its base demotable, so the guard cannot fall out of date.

An unlisted variant resolves to nothing and becomes a question. That is the
intended behaviour: `Telma CT` is telmisartan + chlorthalidone, and resolving it
to plain telmisartan would delete a diuretic from the list without a word.

---

## Out of scope, permanently

Diagnosis, triage and symptom checking. Dose recommendations. Drug interaction
assertions. Medicine ordering, lab booking, teleconsultation. Health scores and
streaks. Diagnostic lab partnerships. Advice of any kind.

The product's value rests on not being another party with an interest.
