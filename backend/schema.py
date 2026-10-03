"""Pydantic models shared across the pipeline.

Every value that reaches the clinician is a Field: it carries the value, where it
came from on the page, and how confident we are. Nothing in this system hands
back a bare number.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field as PField


class DocumentKind(str, Enum):
    PRESCRIPTION = "prescription"
    LAB_REPORT = "lab_report"
    PATHOLOGY = "pathology"
    DISCHARGE = "discharge_summary"


class BBox(BaseModel):
    """Where on the page a value was read from, in page-relative coordinates."""

    page: int = 0
    x: float
    y: float
    w: float
    h: float


class Provenance(BaseModel):
    document_id: str
    bbox: BBox | None = None
    raw_text: str | None = None


class ExtractedField(BaseModel):
    """One value read off a page."""

    name: str
    value: Any
    unit: str | None = None
    provenance: Provenance
    model_confidence: float = PField(ge=0.0, le=1.0)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name}={self.value}{(' ' + self.unit) if self.unit else ''}"


class DrugRecord(BaseModel):
    drug_raw: str
    drug_generic: str | None = None
    rxnorm: str | None = None
    dose: float | None = None
    dose_unit: str | None = None
    route: str | None = None
    cycle: int | None = None
    given_on: date | None = None
    provenance: Provenance
    model_confidence: float = 1.0


class LabRecord(BaseModel):
    test_raw: str
    test_canonical: str | None = None
    loinc: str | None = None
    value: float | None = None
    unit: str | None = None
    ref_low: float | None = None
    ref_high: float | None = None
    drawn_on: date | None = None
    provenance: Provenance
    model_confidence: float = 1.0


class ToxicityRecord(BaseModel):
    term_raw: str
    ctcae_term: str | None = None
    grade: int | None = None
    observed_on: date | None = None
    cycle: int | None = None
    provenance: Provenance
    model_confidence: float = 1.0


class Document(BaseModel):
    id: str
    kind: DocumentKind
    filename: str
    issuing_site: str | None = None
    document_date: date | None = None
    redacted: bool = False
    redaction_count: int = 0
    drugs: list[DrugRecord] = []
    labs: list[LabRecord] = []
    toxicities: list[ToxicityRecord] = []


# --- derived outputs -------------------------------------------------------


class ExposureSummary(BaseModel):
    drug_generic: str
    cumulative: float
    unit: str
    ceiling: float | None
    ceiling_basis: str | None
    fraction_of_ceiling: float | None
    cycles_recorded: int
    cycles_remaining_at_current_dose: float | None
    sources: list[str]
    confidence: float
    abstain: bool


class TrajectoryPoint(BaseModel):
    cycle: int | None
    observed_on: date | None
    grade: int


class ToxicityTrajectory(BaseModel):
    ctcae_term: str
    points: list[TrajectoryPoint]
    first_grade: int
    latest_grade: int
    peak_grade: int
    direction: Literal["worsening", "improving", "stable", "single observation"]
    confidence: float
    abstain: bool


class LabTrend(BaseModel):
    test_canonical: str
    loinc: str | None
    unit: str | None
    values: list[float]
    dates: list[date]
    baseline: float | None
    latest: float | None
    direction: Literal["rising", "falling", "stable", "insufficient data"]
    out_of_range: bool
    detector: str


class Flag(BaseModel):
    kind: Literal["overdue", "lab_trend", "exposure", "toxicity"]
    severity: Literal["note", "attention"]
    message: str
    evidence: list[str]


class TimelineEntry(BaseModel):
    on: date
    cycle: int | None
    kind: Literal["drug", "lab", "toxicity"]
    label: str
    detail: str
    document_id: str
    confidence: float
    abstain: bool


class PatientRecord(BaseModel):
    patient_pseudonym: str
    documents: list[Document]
    timeline: list[TimelineEntry]
    exposures: list[ExposureSummary]
    trajectories: list[ToxicityTrajectory]
    lab_trends: list[LabTrend]
    flags: list[Flag]
    field_count: int
    auto_filled: int
    held_for_review: int
