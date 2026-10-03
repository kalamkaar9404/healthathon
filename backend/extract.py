"""Stage 3 — extraction.

Two implementations behind one interface.

`VLMExtractor` is the real one: a small vision-language model reads the page
image and emits JSON directly, with no OCR stage in between. It is not loaded
in the prototype because it needs a GPU, but the prompt and the output schema
it is constrained to are here, because they are the contract the rest of the
pipeline is written against.

`RuleExtractor` reads the redacted text of a document and produces the same
objects. It is what makes the prototype runnable on a laptop, and it doubles as
the fixture generator for tests.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Protocol

from .normalise import (
    dose_to_mg,
    normalise_drug,
    normalise_lab,
    normalise_route,
    normalise_toxicity,
)
from .schema import (
    BBox,
    Document,
    DocumentKind,
    DrugRecord,
    LabRecord,
    Provenance,
    ToxicityRecord,
)

# The JSON the model is constrained to emit. Constrained decoding against this
# schema is what stops a 3B model inventing a field name halfway down a page.
OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["document_kind", "drugs", "labs", "toxicities"],
    "properties": {
        "document_kind": {"enum": [k.value for k in DocumentKind]},
        "document_date": {"type": ["string", "null"], "format": "date"},
        "issuing_site": {"type": ["string", "null"]},
        "drugs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["drug", "bbox"],
                "properties": {
                    "drug": {"type": "string"},
                    "dose": {"type": ["number", "null"]},
                    "dose_unit": {"type": ["string", "null"]},
                    "route": {"type": ["string", "null"]},
                    "cycle": {"type": ["integer", "null"]},
                    "given_on": {"type": ["string", "null"], "format": "date"},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "labs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test", "value", "bbox"],
                "properties": {
                    "test": {"type": "string"},
                    "value": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "ref_low": {"type": ["number", "null"]},
                    "ref_high": {"type": ["number", "null"]},
                    "drawn_on": {"type": ["string", "null"], "format": "date"},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "toxicities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["term", "bbox"],
                "properties": {
                    "term": {"type": "string"},
                    "grade": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                    "observed_on": {"type": ["string", "null"], "format": "date"},
                    "cycle": {"type": ["integer", "null"]},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}

PROMPT = """You are reading one page of an oncology clinical document.

Report only what is printed or written on this page. If a value is illegible or
absent, return null for it. Do not infer a value from context, do not complete a
partial number, and do not supply a typical value for the drug or test.

For every item you return, give the bounding box of the region you read it from,
as [x, y, w, h] in page-relative coordinates from 0 to 1, and a confidence
between 0 and 1 reflecting how clearly you could read it.

Return JSON matching the supplied schema and nothing else."""


class Extractor(Protocol):
    def extract(self, document_id: str, filename: str, text: str) -> Document: ...


# --- rules -----------------------------------------------------------------

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d/%m/%y")

_DRUG_LINE = re.compile(
    r"^[ \t]*(?:\d+[.)][ \t]*)?(?P<drug>[A-Za-z][A-Za-z0-9 \-]{2,40}?)[ \t]+"
    r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg/m2|mg/m\^2|mg|g|mcg|units?)"
    r"(?:\s*(?P<route>IV|I\.V\.|PO|P\.O\.|SC|IM))?\s*$",
    re.I | re.M,
)
_LAB_LINE = re.compile(
    r"^[ \t]*(?P<test>[A-Za-z][A-Za-z0-9 ./\-]{1,30}?)[ \t]*[:\-]?[ \t]+"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mg/dL|g/dL|U/L|mmol/L|10\^9/L|%)?"
    r"(?:\s*\(\s*(?P<lo>\d+(?:\.\d+)?)\s*[-–]\s*(?P<hi>\d+(?:\.\d+)?)\s*\))?\s*$",
    re.I | re.M,
)
_TOX_LINE = re.compile(
    r"^[ \t]*(?P<term>[A-Za-z][A-Za-z \-]{2,40}?)[ \t]*[-–:]?[ \t]*(?:CTCAE[ \t]*)?[Gg]rade[ \t]*(?P<grade>[1-5])[ \t]*$",
    re.M,
)
_CYCLE = re.compile(r"\bcycle\s*(?:no\.?|number)?\s*[:\-]?\s*(\d{1,2})\b", re.I)
_DOC_DATE = re.compile(r"\b(?:date|dated|on)\s*[:\-]?\s*(\d{1,2}[/-][A-Za-z0-9]{1,4}[/-]\d{2,4})", re.I)
_SITE = re.compile(
    r"^[ \t]*([A-Z][A-Za-z&.' ]{6,60}(?:Hospital|Cancer Centre|Cancer Center|Institute|Lab(?:oratory)?))[ \t]*$",
    re.M,
)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _kind_of(text: str) -> DocumentKind:
    low = text.lower()
    if "histopath" in low or "biopsy" in low or "specimen" in low:
        return DocumentKind.PATHOLOGY
    if "discharge" in low:
        return DocumentKind.DISCHARGE
    if "chemotherapy" in low or "prescription" in low or "rx" in low:
        return DocumentKind.PRESCRIPTION
    return DocumentKind.LAB_REPORT


def _bbox(text: str, span: tuple[int, int]) -> BBox:
    """A stand-in for the model's own box: position in the text mapped onto the
    page, so the review pane can still point at roughly the right place."""
    y = span[0] / max(len(text), 1)
    return BBox(page=0, x=0.08, y=round(0.06 + y * 0.82, 4), w=0.84, h=0.035)


class RuleExtractor:
    """Deterministic extractor over redacted text. Same output type as the VLM."""

    def extract(self, document_id: str, filename: str, text: str) -> Document:
        kind = _kind_of(text)
        doc_date = _parse_date(m.group(1)) if (m := _DOC_DATE.search(text)) else None
        site = m.group(1).strip() if (m := _SITE.search(text)) else None
        cycle = int(m.group(1)) if (m := _CYCLE.search(text)) else None

        drugs: list[DrugRecord] = []
        for m in _DRUG_LINE.finditer(text):
            raw = m.group("drug").strip()
            generic, rxnorm, res_conf = normalise_drug(raw)
            if generic is None and res_conf == 0.0:
                # Unresolved strings are kept, not dropped: the clinician sees
                # the row and decides. They just score low.
                pass
            value, unit = dose_to_mg(float(m.group("dose")), m.group("unit"))
            drugs.append(
                DrugRecord(
                    drug_raw=raw,
                    drug_generic=generic,
                    rxnorm=rxnorm,
                    dose=value,
                    dose_unit=unit,
                    route=normalise_route(m.group("route")),
                    cycle=cycle,
                    given_on=doc_date,
                    provenance=Provenance(document_id=document_id, bbox=_bbox(text, m.span()), raw_text=m.group(0).strip()),
                    model_confidence=round(0.72 + 0.25 * res_conf, 3),
                )
            )

        labs: list[LabRecord] = []
        for m in _LAB_LINE.finditer(text):
            raw = m.group("test").strip()
            canonical, loinc, unit, ref, res_conf = normalise_lab(raw)
            if canonical is None:
                continue  # a bare "Age 54" line is not a lab result
            lo = float(m.group("lo")) if m.group("lo") else (ref[0] if ref else None)
            hi = float(m.group("hi")) if m.group("hi") else (ref[1] if ref else None)
            labs.append(
                LabRecord(
                    test_raw=raw,
                    test_canonical=canonical,
                    loinc=loinc,
                    value=float(m.group("value")),
                    unit=m.group("unit") or unit,
                    ref_low=lo,
                    ref_high=hi,
                    drawn_on=doc_date,
                    provenance=Provenance(document_id=document_id, bbox=_bbox(text, m.span()), raw_text=m.group(0).strip()),
                    model_confidence=round(0.74 + 0.24 * res_conf, 3),
                )
            )

        toxicities: list[ToxicityRecord] = []
        for m in _TOX_LINE.finditer(text):
            raw = m.group("term").strip()
            term, res_conf = normalise_toxicity(raw)
            toxicities.append(
                ToxicityRecord(
                    term_raw=raw,
                    ctcae_term=term,
                    grade=int(m.group("grade")),
                    observed_on=doc_date,
                    cycle=cycle,
                    provenance=Provenance(document_id=document_id, bbox=_bbox(text, m.span()), raw_text=m.group(0).strip()),
                    model_confidence=round(0.70 + 0.28 * res_conf, 3),
                )
            )

        return Document(
            id=document_id,
            kind=kind,
            filename=filename,
            issuing_site=site,
            document_date=doc_date,
            drugs=drugs,
            labs=labs,
            toxicities=toxicities,
        )


class VLMExtractor:
    """Scan -> fields, with no OCR stage.

    Left unloaded in the prototype. The constructor documents exactly what the
    deployed path is, so the swap is a one-line change in app.py.
    """

    def __init__(self, model_id: str = "Qwen/Qwen2.5-VL-3B-Instruct", adapter: str | None = None):
        self.model_id = model_id
        self.adapter = adapter  # LoRA weights for the oncology document schema

    def extract(self, document_id: str, filename: str, text: str) -> Document:  # pragma: no cover
        raise NotImplementedError(
            "Load the model with vLLM and call it with PROMPT and OUTPUT_SCHEMA "
            "under constrained decoding. Not loaded in the prototype: it needs a GPU."
        )
