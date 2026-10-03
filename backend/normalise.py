"""Stage 4 — normalisation.

Drug strings resolve generic -> Indian brand index -> fuzzy. Lab names resolve
to a canonical name plus LOINC. Toxicity terms resolve to CTCAE v5.0 preferred
terms. Units are converted to one canonical unit per quantity.

An unresolved string is never guessed into something plausible. It is returned
unresolved and carries that fact forward, where it lowers the field's
confidence and usually sends it to the clinician instead of the record.
"""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def _drugs() -> dict:
    return json.loads((DATA / "drugs.json").read_text())


@lru_cache(maxsize=1)
def _vocab() -> dict:
    return json.loads((DATA / "vocab.json").read_text())


@lru_cache(maxsize=1)
def _ceilings() -> dict:
    return json.loads((DATA / "ceilings.json").read_text())


def ceilings() -> dict:
    return _ceilings()


def _clean(s: str) -> str:
    # Strip the strength suffix that Indian brand names usually carry
    # ("Adrim 50", "Kemocarb 450 inj").
    s = s.lower().strip()
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|mcg|g|ml|iu|units?)\b", " ", s)
    s = re.sub(r"\b(inj|injection|tab|tablet|cap|capsule|vial|infusion)\b", " ", s)
    return re.sub(r"[^a-z0-9\s\-]", " ", s).strip()


def normalise_drug(raw: str) -> tuple[str | None, str | None, float]:
    """Return (generic, rxnorm, resolution_confidence)."""
    key = _clean(raw)
    if not key:
        return None, None, 0.0

    rx = _drugs()["rxnorm"]
    brands = _drugs()["brands"]

    # 1. already a generic
    for generic in rx:
        if generic in key:
            return generic, rx[generic], 1.0

    # 2. known Indian brand
    for brand, generic in brands.items():
        if brand in key:
            return generic, rx.get(generic), 0.95

    # 3. fuzzy against both tables — deliberately strict
    candidates = list(rx) + list(brands)
    match = difflib.get_close_matches(key.split()[0], candidates, n=1, cutoff=0.86)
    if match:
        hit = match[0]
        generic = brands.get(hit, hit)
        return generic, rx.get(generic), 0.7

    return None, None, 0.0


def normalise_lab(raw: str) -> tuple[str | None, str | None, str | None, tuple[float, float] | None, float]:
    """Return (canonical, loinc, unit, reference range, resolution_confidence)."""
    key = re.sub(r"[^a-z0-9\s.]", " ", raw.lower()).strip()
    labs = _vocab()["labs"]

    if key in labs:
        e = labs[key]
        return e["canonical"], e["loinc"], e["unit"], tuple(e["ref"]), 1.0

    for name, e in labs.items():
        if name in key:
            return e["canonical"], e["loinc"], e["unit"], tuple(e["ref"]), 0.9

    match = difflib.get_close_matches(key, list(labs), n=1, cutoff=0.85)
    if match:
        e = labs[match[0]]
        return e["canonical"], e["loinc"], e["unit"], tuple(e["ref"]), 0.7

    return None, None, None, None, 0.0


def normalise_toxicity(raw: str) -> tuple[str | None, float]:
    key = re.sub(r"[^a-z0-9\s\-]", " ", raw.lower()).strip()
    terms = _vocab()["ctcae"]

    if key in terms:
        return terms[key], 1.0
    for name, term in terms.items():
        if name in key:
            return term, 0.9
    match = difflib.get_close_matches(key, list(terms), n=1, cutoff=0.85)
    if match:
        return terms[match[0]], 0.7
    return None, 0.0


def normalise_route(raw: str | None) -> str | None:
    if not raw:
        return None
    return _vocab()["routes"].get(raw.lower().strip())


# --- units -----------------------------------------------------------------

_DOSE_TO_MG = {"mg": 1.0, "g": 1000.0, "mcg": 0.001, "ug": 0.001}


def dose_to_mg(value: float, unit: str | None) -> tuple[float | None, str | None]:
    """Canonicalise a dose to mg, or to mg/m2 if it is already body-surface
    normalised. Returns (None, None) for a unit we do not recognise rather than
    assuming one."""
    if unit is None:
        return None, None
    u = unit.lower().replace(" ", "").replace("^", "").replace("²", "2")

    if u in ("mg/m2", "mgm2"):
        return value, "mg/m2"
    if u in ("units", "unit", "iu"):
        return value, "units"
    if u in _DOSE_TO_MG:
        return value * _DOSE_TO_MG[u], "mg"
    if u.endswith("/m2"):
        base = u[:-3]
        if base in _DOSE_TO_MG:
            return value * _DOSE_TO_MG[base], "mg/m2"
    return None, None


def bsa_dubois(height_cm: float, weight_kg: float) -> float:
    """Du Bois body surface area, used to convert a flat mg dose to mg/m2 when
    the chart records height and weight but not the normalised dose."""
    return 0.007184 * (height_cm**0.725) * (weight_kg**0.425)
