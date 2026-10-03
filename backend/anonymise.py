"""Stage 2 — anonymisation.

Runs before anything else touches the page, so every downstream artefact
(timeline, logs, cached extractions) is already de-identified.

Identifiers are not simply blacked out. Each one is replaced by a stable
pseudonym derived from a keyed hash, so the same patient still links across
documents from different hospitals. The key and the lookup table stay on the
machine that did the redaction; nothing in the exported record can be reversed
without them.

Microsoft Presidio is used when it is installed. It is not a hard dependency:
the pattern rules below cover the identifier shapes that appear on Indian
clinical paperwork and are enough to run the prototype.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field

# Keyed so that pseudonyms cannot be brute-forced from the output alone.
# In deployment this is read from the hospital's own secret store.
_KEY = os.environ.get("CANCERCARE_PSEUDONYM_KEY", "dev-only-not-for-deployment").encode()

# Ordered: the more specific a pattern, the earlier it runs.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("UHID", re.compile(r"\b(?:UHID|MRN|Reg(?:istration)?\s*No\.?|Hosp(?:ital)?\s*No\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{3,})", re.I)),
    ("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("PHONE", re.compile(r"\b(?:\+91[\-\s]?)?[6-9]\d{9}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("PINCODE", re.compile(r"\b\d{6}\b")),
    ("NAME", re.compile(r"(?:Patient\s*Name|Name\s*of\s*Patient|Patient)\s*[:\-]\s*([A-Z][A-Za-z.\s]{2,40})")),
    ("DOCTOR", re.compile(r"\bDr\.?\s+([A-Z][A-Za-z.\s]{2,40})")),
    ("ADDRESS", re.compile(r"(?:Address|Add\.?)\s*[:\-]\s*(.{5,80})", re.I)),
]

# Entity types we ask Presidio for when it is available.
PRESIDIO_ENTITIES = [
    "PERSON",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "LOCATION",
    "DATE_TIME",
    "IN_AADHAAR",
    "IN_PAN",
]


@dataclass
class Redaction:
    kind: str
    original: str
    pseudonym: str
    start: int
    end: int


@dataclass
class AnonymisationResult:
    text: str
    redactions: list[Redaction] = field(default_factory=list)
    engine: str = "patterns"

    @property
    def count(self) -> int:
        return len(self.redactions)

    def pseudonym_for(self, kind: str) -> str | None:
        for r in self.redactions:
            if r.kind == kind:
                return r.pseudonym
        return None


def pseudonym(kind: str, value: str) -> str:
    """Stable, keyed, non-reversible. Same input gives the same token forever,
    which is what keeps a patient's documents linkable after redaction."""
    normalised = re.sub(r"\s+", " ", value).strip().upper()
    digest = hmac.new(_KEY, f"{kind}:{normalised}".encode(), hashlib.sha256).hexdigest()
    return f"{kind}-{digest[:8].upper()}"


def _presidio_spans(text: str) -> list[tuple[str, int, int]] | None:
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore
    except ImportError:
        return None
    analyzer = AnalyzerEngine()
    results = analyzer.analyze(text=text, entities=PRESIDIO_ENTITIES, language="en")
    return [(r.entity_type, r.start, r.end) for r in results]


def anonymise(text: str, *, use_presidio: bool = True) -> AnonymisationResult:
    """Replace every identifier with a stable pseudonym.

    Spans are collected first and applied right-to-left so earlier offsets stay
    valid while the string is being rewritten.
    """
    spans: list[tuple[str, int, int]] = []
    engine = "patterns"

    if use_presidio:
        found = _presidio_spans(text)
        if found is not None:
            spans.extend(found)
            engine = "presidio+patterns"

    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            # Group 1 when the pattern has a capture group, so the label
            # ("Patient Name:") survives and only the value is replaced.
            start, end = (match.span(1) if match.groups() else match.span())
            spans.append((kind, start, end))

    # Drop spans contained inside an earlier, longer span.
    spans.sort(key=lambda s: (s[1], -(s[2] - s[1])))
    kept: list[tuple[str, int, int]] = []
    for kind, start, end in spans:
        if kept and start < kept[-1][2]:
            continue
        kept.append((kind, start, end))

    redactions: list[Redaction] = []
    out = text
    for kind, start, end in sorted(kept, key=lambda s: s[1], reverse=True):
        original = text[start:end]
        if not original.strip():
            continue
        token = pseudonym(kind, original)
        out = out[:start] + token + out[end:]
        redactions.append(Redaction(kind, original, token, start, end))

    redactions.reverse()
    return AnonymisationResult(text=out, redactions=redactions, engine=engine)
