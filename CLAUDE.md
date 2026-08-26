# Parchi — working rules

## What this is
An agent that reads Indian families' medical paperwork and produces a
medication list with provenance, plus questions to ask the doctor.
It does NOT diagnose, advise, or assert drug interactions. Ever.

## Non-negotiable
- Findings are observations + questions. Never conclusions. Never advice.
- Low-confidence extractions never become established fact.
- Every derived claim cites a document id and date.
- No strength reported unless parsed-strength count == molecule count, and
  never inside a combination product (written order is not table order).
- Omission of a drug is only meaningful from the SAME prescriber in a
  comprehensive rewrite. See PRD §6.
- An unlisted brand variant resolves to nothing and becomes a question. Never
  fall back to the base product — "Telma CT" is not telmisartan alone.
- `reconcile()` is pure and deterministic. Do not add caching or mutation, and
  do not read a clock inside it — `as_of` is a parameter.
- No real patient data in this repo. Fixtures only, declared as such.

## Before changing a heuristic
COMPREHENSIVE_REWRITE_THRESHOLD and STALE_AFTER_DAYS are judgement calls,
not derived constants. Changing them changes product behaviour. Change the
test deliberately or not at all.

## Testing
`./.venv/bin/python -m pytest -q` must be green before any commit.
`test_no_finding_makes_a_clinical_claim` is the safety invariant. If it
fails, the product has drifted into practising medicine. Stop and fix.

## Hackathon compliance — do not regress these
Mandatory for submission (verified against the official rules):
- Gemini 3.5 or newer. The Pro tier's newest listed version is 3.1, which does
  not satisfy "3.5 or newer" on a literal reading — use Flash tier 3.5+.
- At least one of Google ADK / GenAI SDK / Antigravity SDK / GenKit, named
  explicitly. GEAP component names are not on the required list.
- At least one Google Cloud infra service (Cloud Run, Firestore).
- Architecture diagram, step-by-step README setup, ≤4-min video showing
  unedited live execution AND visible proof the backend runs on Google Cloud.
- All code newly created during the submission period (3–31 Aug 2026).
  Disclose any pre-existing code incorporated.

## Cost discipline
Flash for classification and printed extraction. Reserve the heavier model for
reconciliation judgement only. Never the heavy model for classification.
