"""Run the reconciliation engine over the constructed scenario.

    python -m parchi.demo

Every value printed comes from parchi/fixtures.py and is invented (SR-9).
"""

from __future__ import annotations

from .fixtures import AS_OF, DOCUMENTS, LAB_RESULTS, MENTIONS
from .models import ATTENTION_RANK, MedStatus, analyte_display
from .reconcile import reconcile

_RULE = "─" * 78


def _heading(text: str) -> None:
    print(f"\n{text}\n{_RULE}")


def main() -> None:
    result = reconcile(MENTIONS, as_of=AS_OF, lab_results=LAB_RESULTS)

    print(_RULE)
    print(f"Parchi — reconciliation as of {AS_OF:%d %b %Y}")
    print(
        f"{len(DOCUMENTS)} documents · "
        f"{len({d.prescriber for d in DOCUMENTS})} prescribers · "
        f"{len(MENTIONS)} medication mentions · "
        f"{len(LAB_RESULTS)} lab results"
    )
    print("All source data is constructed. No real patient data.")

    _heading("CURRENT MEDICATION LIST")
    order = {
        MedStatus.ACTIVE: 0,
        MedStatus.LIKELY_ACTIVE: 1,
        MedStatus.POSSIBLY_STOPPED: 2,
        MedStatus.UNCERTAIN: 3,
        MedStatus.COURSE_COMPLETED: 4,
    }
    for state in sorted(result.states, key=lambda s: (order[s.status], s.molecule)):
        strength = (
            f"{state.current_strength_mg:g} mg"
            if state.current_strength_mg is not None
            else "strength not attributable"
        )
        print(f"  {state.molecule:<20} {state.status.value:<17} {strength}")
        print(
            f"  {'':<20} as {state.current_brand_text} · "
            f"{', '.join(state.prescribers) or 'unattributed'} · "
            f"last written {state.last_mentioned:%d %b %Y}"
        )
        print(f"  {'':<20} evidence: {', '.join(state.evidence_mention_ids)}")
        if state.open_question:
            print(f"  {'':<20} ? {state.open_question}")
        print()

    _heading("QUESTIONS WORTH ASKING")
    for finding in sorted(
        result.findings, key=lambda f: (ATTENTION_RANK[f.attention], f.kind.value)
    ):
        print(f"  [{finding.attention.value}] {finding.kind.value}")
        print(f"    {finding.summary}")
        print(f"    → {finding.question}")
        print(f"    evidence: {', '.join(finding.evidence)}")
        print()

    _heading("TRENDS")
    for series in result.series:
        name = analyte_display(series.analyte)
        direction = series.direction or "no consistent direction"
        print(f"  {name} ({series.canonical_unit}) — {direction}")
        for point in series.points:
            ref = (
                f"ref {point.ref_low:g}–{point.ref_high:g}"
                if point.ref_low is not None and point.ref_high is not None
                else "no range printed"
            )
            print(
                f"    {point.doc_date:%d %b %Y}  {point.canonical_value:>6g}  "
                f"{point.lab_name or 'lab not named':<18} {ref}  [{point.id}]"
            )
        print()


if __name__ == "__main__":
    main()
