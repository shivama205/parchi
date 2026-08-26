"""Brand normalisation — PRD §7.

Resolves an Indian pharmaceutical brand name, as written on paper, to its
constituent molecules. The failure mode this module guards against is a *wrong
drug on a medication list*, so every ambiguity resolves toward silence or a
lowered confidence rather than a guess.

THE SEED TABLE IS DEMO-GRADE (PRD §7). Brand compositions change when
manufacturers reformulate, and a wrong mapping produces a confidently incorrect
medication list. Before any real patient use this must be replaced with a table
derived from an authoritative source — NPPA ceiling-price notifications or CDSCO
listings — with a human review step. Machine-readability of those sources has
NOT been verified; do that before committing to the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .models import Confidence, MedicationMention

# --------------------------------------------------------------------------
# Seed table — brand display name -> constituent molecules
# --------------------------------------------------------------------------
# Multi-molecule entries deliberately carry NO strength information. See
# _assign_strengths for why.

BRAND_TABLE: dict[str, tuple[str, ...]] = {
    # -- antihypertensives ------------------------------------------------
    "Telma": ("telmisartan",),
    "Telma H": ("telmisartan", "hydrochlorothiazide"),
    "Telma AM": ("telmisartan", "amlodipine"),
    "Telmikind": ("telmisartan",),
    "Amlong": ("amlodipine",),
    "Losar": ("losartan",),
    "Metolar": ("metoprolol",),
    "Betaloc": ("metoprolol",),
    "Concor": ("bisoprolol",),
    "Cardivas": ("carvedilol",),
    "Nebicard": ("nebivolol",),
    "Prazopress": ("prazosin",),
    # -- diuretics --------------------------------------------------------
    "Lasix": ("furosemide",),
    "Dytor": ("torsemide",),
    "Aldactone": ("spironolactone",),
    # -- antidiabetics ----------------------------------------------------
    "Glycomet": ("metformin",),
    "Glycomet GP": ("metformin", "glimepiride"),
    "Glycomet Trio": ("metformin", "glimepiride", "pioglitazone"),
    "Zoryl": ("glimepiride",),
    "Zoryl M": ("glimepiride", "metformin"),
    "Januvia": ("sitagliptin",),
    "Janumet": ("sitagliptin", "metformin"),
    "Istamet": ("sitagliptin", "metformin"),
    "Galvus": ("vildagliptin",),
    "Galvus Met": ("vildagliptin", "metformin"),
    # -- lipids / antiplatelets ------------------------------------------
    "Ecosprin": ("aspirin",),
    "Ecosprin AV": ("aspirin", "atorvastatin"),
    "Atorva": ("atorvastatin",),
    "Storvas": ("atorvastatin",),
    "Rosuvas": ("rosuvastatin",),
    "Rosuvas F": ("rosuvastatin", "fenofibrate"),
    "Clopilet": ("clopidogrel",),
    "Deplatt": ("clopidogrel",),
    # -- gastro -----------------------------------------------------------
    "Pan": ("pantoprazole",),
    "Pan-D": ("pantoprazole", "domperidone"),
    "Omez": ("omeprazole",),
    "Razo": ("rabeprazole",),
    "Nexpro": ("esomeprazole",),
    "Rantac": ("ranitidine",),
    # -- antimicrobials ---------------------------------------------------
    "Augmentin": ("amoxicillin", "clavulanic acid"),
    "Azee": ("azithromycin",),
    "Taxim-O": ("cefixime",),
    "Cifran": ("ciprofloxacin",),
    "Metrogyl": ("metronidazole",),
    # -- thyroid ----------------------------------------------------------
    "Thyronorm": ("levothyroxine",),
    "Eltroxin": ("levothyroxine",),
    # -- analgesia / neuro ------------------------------------------------
    "Dolo": ("paracetamol",),
    "Crocin": ("paracetamol",),
    "Nise": ("nimesulide",),
    "Gabapin": ("gabapentin",),
    "Gabapin NT": ("gabapentin", "nortriptyline"),
    "Pregabid": ("pregabalin",),
    # -- urology ----------------------------------------------------------
    "Urimax": ("tamsulosin",),
    "Urimax D": ("tamsulosin", "dutasteride"),
    # -- supplements ------------------------------------------------------
    "Shelcal": ("calcium carbonate", "cholecalciferol"),
}

#: Generic molecule names. Teaching-hospital prescribers write these (PRD §7).
GENERIC_MOLECULES = (
    "metformin", "glimepiride", "pioglitazone", "sitagliptin", "vildagliptin",
    "telmisartan", "losartan", "amlodipine", "hydrochlorothiazide", "prazosin",
    "metoprolol", "bisoprolol", "carvedilol", "nebivolol",
    "furosemide", "torsemide", "spironolactone",
    "atorvastatin", "rosuvastatin", "fenofibrate", "aspirin", "clopidogrel",
    "pantoprazole", "domperidone", "omeprazole", "rabeprazole", "esomeprazole",
    "ranitidine", "levothyroxine", "paracetamol", "nimesulide",
    "amoxicillin", "clavulanic acid", "azithromycin", "cefixime",
    "ciprofloxacin", "metronidazole",
    "gabapentin", "nortriptyline", "pregabalin",
    "tamsulosin", "dutasteride", "calcium carbonate", "cholecalciferol",
)

#: PRD §7 — known transcription confusions. A match against any member forces
#: confidence down, because the failure mode is a wrong drug.
#:
#: DELIBERATE REFINEMENT OF §7. The demotion applies only where the members
#: resolve to DIFFERENT molecule tuples, which is where §7's stated rationale
#: actually bites. Telma and Telmikind are both plain telmisartan, so mistaking
#: one for the other cannot produce a wrong drug and carries no penalty. Pan vs
#: Pan-D (adds domperidone) and Glycomet vs Glycomet GP (adds a sulfonylurea)
#: do differ, and are penalised. Applying the penalty to molecularly identical
#: pairs would make every printed telmisartan prescription unusable for no
#: safety gain.
CONFUSION_SETS: tuple[tuple[str, ...], ...] = (
    ("Telma", "Telmikind"),
    ("Pan", "Pan-D"),
    ("Glycomet", "Glycomet GP"),
)

#: Release-form modifiers that never change composition, so they may be dropped
#: when no exact table entry matches. Kept deliberately narrow. Composition-
#: changing suffixes — GP, AV, H, AM, D, M, NT, Trio, Met, Forte, Plus — are
#: NOT here: dropping one of those is exactly the silent under-read PRD §7
#: forbids ("Glycomet GP must never silently resolve to plain metformin").
RELEASE_MODIFIERS = frozenset({"sr", "xr", "cr", "er", "xl", "mr"})

#: Dose-frequency shorthand. Trailing occurrences are stripped before matching.
FREQUENCY_TOKENS = frozenset({
    "od", "bd", "bid", "tds", "tid", "qid", "qds", "hs", "sos",
    "stat", "prn", "ac", "pc", "qd",
})

#: Dosage-form and delivery-device words. A form word says nothing about
#: composition, so it can be dropped from ANYWHERE in a reading.
#:
#: Position matters here. Prescriptions put the form in front ("Tab. Telma 40"),
#: at the end ("AUGMENTIN 625MG TAB") and in the middle ("DOLO TAB 650MG"), and
#: real annotation data uses all three. Stripping only a leading form word left
#: "augmentin 625mg tab" with a trailing token nothing could match, so every one
#: of those readings failed to resolve.
FORM_TOKENS = frozenset({
    # oral solids
    "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules",
    "rotacap", "rotacaps", "sachet", "powder", "pwd", "granules",
    # oral liquids
    "syp", "syr", "syrup", "susp", "suspension", "soln", "solution", "elixir",
    "drop", "drops", "gargle", "mouthwash",
    # injectables and devices
    "inj", "injection", "vial", "ampoule", "prefilled", "solostar", "kwikpen",
    "penfill", "flexpen", "cartridge",
    # inhaled
    "mdi", "inhaler", "rotahaler", "respule", "respules", "neb", "nebuliser",
    "nebulizer",
    # topical
    "oint", "ointment", "cream", "gel", "lotion", "spray", "patch", "eye",
    "ear", "nasal",
})

#: Unit words that arrive as their own token — "60000 IU", "40 mg".
UNIT_TOKENS = frozenset({"mg", "mcg", "g", "gm", "ml", "iu", "u", "unit", "units"})

_STRENGTH_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>mg|mcg|g|gm|ml|iu|u|units?)?$"
)
_MG_FACTORS = {None: 1.0, "mg": 1.0, "g": 1000.0, "gm": 1000.0, "mcg": 0.001}


def _tokenise(text: str) -> list[str]:
    """Lowercase, split on whitespace and separators, drop stray punctuation."""
    cleaned = re.sub(r"[-/+,.()\[\]]", " ", (text or "").lower())
    return [t for t in cleaned.split() if t]


def _strength_match(token: str) -> re.Match | None:
    return _STRENGTH_RE.match(token)


def _strip_forms(tokens: list[str]) -> list[str]:
    """Drop every dosage-form and device word, wherever it appears."""
    return [t for t in tokens if t not in FORM_TOKENS]


def _is_droppable_tail(token: str) -> bool:
    return (
        _strength_match(token) is not None
        or token in FREQUENCY_TOKENS
        or token in UNIT_TOKENS
    )


def _strip_tail(tokens: list[str]) -> list[str]:
    """Strip trailing strength, unit and frequency tokens. PRD §7.

    "Telma 40" -> ["telma"]; "Telma 40 mg OD" -> ["telma"].
    """
    out = list(tokens)
    while out and _is_droppable_tail(out[-1]):
        out.pop()
    return out


def _parse_strengths_mg(tokens: list[str]) -> tuple[float, ...]:
    """Every strength in the reading, in written order, converted to mg.

    Handles a unit written as its own token ("40 mg", "60000 IU"). Returns ()
    if any strength carries a unit that is not mg-convertible — a partially
    converted list would break the parallel-count contract in §6.4, and silence
    is the correct answer.
    """
    out: list[float] = []
    i = 0
    while i < len(tokens):
        m = _strength_match(tokens[i])
        if not m:
            i += 1
            continue
        unit = m.group("unit")
        if unit is None and i + 1 < len(tokens) and tokens[i + 1] in UNIT_TOKENS:
            unit = tokens[i + 1]
            i += 1
        if unit not in _MG_FACTORS:
            return ()
        out.append(float(m.group("num")) * _MG_FACTORS[unit])
        i += 1
    return tuple(out)


# Exact-match index: a reading resolves only when every remaining token is
# accounted for.
_TABLE_BY_TOKENS: dict[tuple[str, ...], str] = {}
for _display in BRAND_TABLE:
    _TABLE_BY_TOKENS[tuple(_tokenise(_display))] = _display
for _mol in GENERIC_MOLECULES:
    _TABLE_BY_TOKENS.setdefault(tuple(_tokenise(_mol)), _mol)

_CONFUSABLE: dict[str, tuple[str, ...]] = {}
for _group in CONFUSION_SETS:
    for _member in _group:
        _CONFUSABLE[_member] = tuple(m for m in _group if m != _member)


def molecules_for(display_name: str) -> tuple[str, ...]:
    """Molecules for a table key or a bare generic name."""
    if display_name in BRAND_TABLE:
        return BRAND_TABLE[display_name]
    if display_name in GENERIC_MOLECULES:
        return (display_name,)
    return ()


@dataclass(frozen=True)
class BrandResolution:
    """The outcome of resolving one written brand name."""

    brand_text: str
    molecules: tuple[str, ...] = ()
    strengths_mg: tuple[float, ...] = ()
    matched_brand: str | None = None
    confusable_with: tuple[str, ...] = ()
    #: True when the match sits in a confusion set whose members differ
    #: molecularly, so extraction confidence must be demoted a step.
    demote_confidence: bool = False
    #: True when a release-form modifier had to be dropped to find a match.
    modifier_dropped: bool = False

    @property
    def resolved(self) -> bool:
        return bool(self.molecules)


def _match_exact(tokens: list[str]) -> tuple[str, ...] | None:
    """Match only when EVERY token is accounted for.

    §7 requires longest match first, so that "Glycomet GP" never silently
    resolves to plain metformin. Requiring an exact match achieves that more
    completely than longest-prefix would: a prefix match consumes "Glycomet" out
    of "Glycomet SR" or out of an unlisted variant like "Telma CT" and discards
    the rest in silence — dropping chlorthalidone off a medication list without
    a word. An unrecognised variant must reach the caregiver as a question, so
    leftover tokens mean "unresolved", not "close enough".
    """
    key = tuple(tokens)
    return key if key in _TABLE_BY_TOKENS else None


def _assign_strengths(
    molecules: tuple[str, ...], parsed: tuple[float, ...]
) -> tuple[float, ...]:
    """Strengths parallel to `molecules`, or () for silence. PRD §6.4 / SR-6.

    §6.4 requires parsed-count == molecule-count. That is necessary but not
    sufficient for a combination product, because the written order need not
    match the table order: "Glycomet GP 1/500" is glimepiride 1 + metformin 500,
    while the table lists (metformin, glimepiride). Two numbers against two
    molecules passes the count test and would attribute both backwards —
    precisely the confidently-wrong claim §6.4 exists to prevent.

    So strengths are attributed only for single-molecule products. Combination
    products stay silent, and the written strength survives verbatim in
    `brand_text` for a human to read. This is strictly more conservative than
    §6.4 and never violates it.
    """
    if len(parsed) != len(molecules):
        return ()
    if len(molecules) != 1:
        return ()
    return parsed


def resolve(brand_text: str) -> BrandResolution:
    """Resolve a written brand name to molecules and strengths.

    An unresolvable name yields empty molecules. SR-5 keeps that out of
    medication state; it becomes a NEEDS_CONFIRMATION finding instead.
    """
    raw_tokens = _tokenise(brand_text)
    if not raw_tokens:
        return BrandResolution(brand_text=brand_text)

    parsed = _parse_strengths_mg(raw_tokens)

    # Progressive relaxation, most conservative first. Each step drops only
    # tokens that cannot change composition, so a match is never bought by
    # discarding a molecule.
    head = _strip_tail(raw_tokens)
    candidates: list[tuple[list[str], bool]] = [(head, False)]

    without_forms = _strip_tail(_strip_forms(raw_tokens))
    if without_forms != head:
        candidates.append((without_forms, False))

    for base in (head, without_forms):
        relaxed = [t for t in base if t not in RELEASE_MODIFIERS]
        if relaxed and relaxed != base:
            candidates.append((relaxed, True))

    key = None
    modifier_dropped = False
    for tokens, dropped in candidates:
        if not tokens:
            continue
        key = _match_exact(tokens)
        if key is not None:
            modifier_dropped = dropped
            break
    if key is None:
        return BrandResolution(brand_text=brand_text)

    display = _TABLE_BY_TOKENS[key]
    molecules = molecules_for(display)
    confusable = _CONFUSABLE.get(display, ())
    # Only a molecularly divergent confusion carries a penalty — see
    # CONFUSION_SETS.
    demote = any(molecules_for(other) != molecules for other in confusable)
    return BrandResolution(
        brand_text=brand_text,
        molecules=molecules,
        strengths_mg=_assign_strengths(molecules, parsed),
        matched_brand=display,
        confusable_with=confusable,
        demote_confidence=demote,
        modifier_dropped=modifier_dropped,
    )


#: One step down. PRD §7 says a confusion "forces confidence down" — a demotion,
#: not a collapse to LOW. A clearly printed Glycomet GP stays usable at MEDIUM;
#: the same name in handwriting, already MEDIUM, falls to LOW and has to be
#: confirmed before it can reach a medication list.
_DEMOTE_ONE = {
    Confidence.HIGH: Confidence.MEDIUM,
    Confidence.MEDIUM: Confidence.LOW,
    Confidence.LOW: Confidence.LOW,
}


def effective_confidence(
    extracted: Confidence, resolution: BrandResolution
) -> Confidence:
    """Extraction confidence, lowered by what normalisation could not establish."""
    result = extracted
    if resolution.demote_confidence:
        result = _DEMOTE_ONE[result]
    if resolution.modifier_dropped:
        result = _DEMOTE_ONE[result]
    return result


def mention_from_reading(
    *,
    id: str,
    document_id: str,
    doc_date: date,
    brand_text: str,
    prescriber: str | None = None,
    confidence: Confidence = Confidence.HIGH,
    form: str | None = None,
    dose_pattern: str | None = None,
    duration_days: int | None = None,
    instruction: str | None = None,
    user_confirmed: bool = False,
    original_reading: str | None = None,
) -> MedicationMention:
    """Build a MedicationMention from an extracted reading.

    The single supported path from extraction output to the data model, so
    SR-5 and SR-6 hold by construction rather than by discipline.
    """
    res = resolve(brand_text)
    return MedicationMention(
        id=id,
        document_id=document_id,
        doc_date=doc_date,
        brand_text=brand_text,
        prescriber=prescriber,
        molecules=res.molecules,
        strengths_mg=res.strengths_mg,
        form=form,
        dose_pattern=dose_pattern,
        duration_days=duration_days,
        instruction=instruction,
        confidence=effective_confidence(confidence, res),
        user_confirmed=user_confirmed,
        original_reading=original_reading,
    )
