"""Stage 5 — assembly.

Everything here is arithmetic and lookups. No learned model sits in this path,
which is what makes the output auditable and reproducible: the same documents
always produce the same record.

Three things are derived that no single document contains:

  cumulative exposure   lifetime dose per capped drug, against its ceiling
  toxicity trajectory   CTCAE grade per cycle, with direction of travel
  continuity flags      overdue visits and lab trends across the timeline
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from .confidence import score as confidence_score
from .normalise import ceilings
from .schema import (
    Document,
    ExposureSummary,
    Flag,
    LabTrend,
    PatientRecord,
    TimelineEntry,
    ToxicityTrajectory,
    TrajectoryPoint,
)

# Typical interval between cycles; a visit this far past the last one is flagged.
EXPECTED_CYCLE_DAYS = 21
OVERDUE_MULTIPLE = 2.0


# --- cumulative exposure ---------------------------------------------------


def cumulative_exposure(documents: list[Document]) -> list[ExposureSummary]:
    caps = ceilings()
    by_drug: dict[str, list] = defaultdict(list)

    for doc in documents:
        for d in doc.drugs:
            if d.drug_generic and d.dose is not None and d.dose_unit:
                by_drug[d.drug_generic].append((d, doc.id))

    out: list[ExposureSummary] = []
    for generic, entries in sorted(by_drug.items()):
        units = {d.dose_unit for d, _ in entries}
        cap = caps.get(generic)

        # Mixed units mean we cannot sum honestly. Say so rather than coerce.
        if len(units) > 1:
            out.append(
                ExposureSummary(
                    drug_generic=generic,
                    cumulative=0.0,
                    unit="mixed",
                    ceiling=cap["ceiling"] if cap else None,
                    ceiling_basis="Doses recorded in different units; not summed.",
                    fraction_of_ceiling=None,
                    cycles_recorded=len(entries),
                    cycles_remaining_at_current_dose=None,
                    sources=sorted({doc_id for _, doc_id in entries}),
                    confidence=0.0,
                    abstain=True,
                )
            )
            continue

        unit = units.pop()
        total = sum(d.dose for d, _ in entries)
        resolved = all(d.rxnorm is not None for d, _ in entries)
        model_conf = min(d.model_confidence for d, _ in entries)

        conf = confidence_score(
            model_confidence=model_conf,
            resolved=resolved,
            observations=len(entries),
            # A sum is only as trustworthy as the completeness of the records
            # behind it; more cycles on file is more margin.
            margin=min(1.0, len(entries) / 4),
        )

        fraction = None
        remaining = None
        basis = None
        ceiling = None
        if cap and cap["unit"] == unit:
            ceiling = cap["ceiling"]
            basis = cap["basis"]
            fraction = round(total / ceiling, 4)
            last_dose = entries[-1][0].dose or 0
            if last_dose > 0:
                remaining = round(max(0.0, (ceiling - total) / last_dose), 1)

        out.append(
            ExposureSummary(
                drug_generic=generic,
                cumulative=round(total, 2),
                unit=unit,
                ceiling=ceiling,
                ceiling_basis=basis,
                fraction_of_ceiling=fraction,
                cycles_recorded=len(entries),
                cycles_remaining_at_current_dose=remaining,
                sources=sorted({doc_id for _, doc_id in entries}),
                confidence=conf.score,
                abstain=conf.abstain,
            )
        )
    return out


# --- toxicity trajectory ---------------------------------------------------


def toxicity_trajectories(documents: list[Document]) -> list[ToxicityTrajectory]:
    by_term: dict[str, list] = defaultdict(list)
    for doc in documents:
        for t in doc.toxicities:
            if t.ctcae_term and t.grade is not None:
                by_term[t.ctcae_term].append(t)

    out: list[ToxicityTrajectory] = []
    for term, records in sorted(by_term.items()):
        records.sort(key=lambda r: (r.observed_on or date.min, r.cycle or 0))
        points = [TrajectoryPoint(cycle=r.cycle, observed_on=r.observed_on, grade=r.grade) for r in records]
        grades = [p.grade for p in points]

        if len(grades) == 1:
            direction = "single observation"
        elif grades[-1] > grades[0]:
            direction = "worsening"
        elif grades[-1] < grades[0]:
            direction = "improving"
        else:
            direction = "stable"

        conf = confidence_score(
            model_confidence=min(r.model_confidence for r in records),
            resolved=True,
            observations=len(records),
            # One reading cannot establish a direction, so the margin is low
            # until there are at least three.
            margin=min(1.0, len(records) / 3),
        )

        out.append(
            ToxicityTrajectory(
                ctcae_term=term,
                points=points,
                first_grade=grades[0],
                latest_grade=grades[-1],
                peak_grade=max(grades),
                direction=direction,
                confidence=conf.score,
                abstain=conf.abstain,
            )
        )
    return out


# --- lab trends ------------------------------------------------------------


def _cusum(values: list[float], k: float = 0.5, h: float = 4.5) -> bool:
    """Two-sided CUSUM on the standardised series. Detects a sustained level
    shift rather than one outlier, which is the whole point: a single odd
    reading is noise, three in the same direction is a trend."""
    if len(values) < 3:
        return False
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    sigma = max(1.4826 * mad, 1e-6)

    pos = neg = 0.0
    for v in values:
        z = (v - med) / sigma
        pos = max(0.0, pos + z - k)
        neg = max(0.0, neg - z - k)
        if pos > h or neg > h:
            return True
    return False


def lab_trends(documents: list[Document]) -> list[LabTrend]:
    by_test: dict[str, list] = defaultdict(list)
    for doc in documents:
        for lab in doc.labs:
            if lab.test_canonical and lab.value is not None:
                by_test[lab.test_canonical].append(lab)

    out: list[LabTrend] = []
    for test, records in sorted(by_test.items()):
        records.sort(key=lambda r: r.drawn_on or date.min)
        values = [r.value for r in records]
        dates = [r.drawn_on for r in records if r.drawn_on]

        if len(values) < 2:
            direction, detector = "insufficient data", "none"
        else:
            baseline = values[0]
            latest = values[-1]
            delta = latest - baseline
            spread = max(abs(baseline), 1e-6)
            if abs(delta) / spread < 0.15:
                direction, detector = "stable", "relative change"
            elif delta > 0:
                direction = "rising"
                detector = "CUSUM" if _cusum(values) else "relative change"
            else:
                direction = "falling"
                detector = "CUSUM" if _cusum(values) else "relative change"

        last = records[-1]
        out_of_range = bool(
            last.value is not None
            and (
                (last.ref_low is not None and last.value < last.ref_low)
                or (last.ref_high is not None and last.value > last.ref_high)
            )
        )

        out.append(
            LabTrend(
                test_canonical=test,
                loinc=last.loinc,
                unit=last.unit,
                values=values,
                dates=dates,
                baseline=values[0] if values else None,
                latest=values[-1] if values else None,
                direction=direction,
                out_of_range=out_of_range,
                detector=detector,
            )
        )
    return out


# --- flags -----------------------------------------------------------------


def continuity_flags(
    documents: list[Document],
    exposures: list[ExposureSummary],
    trajectories: list[ToxicityTrajectory],
    trends: list[LabTrend],
    today: date | None = None,
) -> list[Flag]:
    """Observations from the timeline. Nothing here is a prediction: every flag
    is a statement about data already on file, so no outcome labels are needed."""
    today = today or date.today()
    flags: list[Flag] = []

    dated = [d.document_date for d in documents if d.document_date]
    if dated:
        last_seen = max(dated)
        gap = (today - last_seen).days
        if gap > EXPECTED_CYCLE_DAYS * OVERDUE_MULTIPLE:
            weeks = gap // 7
            flags.append(
                Flag(
                    kind="overdue",
                    severity="attention",
                    message=f"No document in {weeks} weeks. Last record {last_seen.isoformat()}; cycles usually run {EXPECTED_CYCLE_DAYS} days apart.",
                    evidence=[f"last document {last_seen.isoformat()}"],
                )
            )

    for e in exposures:
        if e.fraction_of_ceiling is None:
            continue
        if e.fraction_of_ceiling >= 0.9:
            flags.append(
                Flag(
                    kind="exposure",
                    severity="attention",
                    message=f"{e.drug_generic.title()} at {e.cumulative:g} of {e.ceiling:g} {e.unit} — {e.fraction_of_ceiling:.0%} of the lifetime ceiling.",
                    evidence=e.sources,
                )
            )
        elif e.fraction_of_ceiling >= 0.7:
            flags.append(
                Flag(
                    kind="exposure",
                    severity="note",
                    message=f"{e.drug_generic.title()} at {e.fraction_of_ceiling:.0%} of its lifetime ceiling — room for about {e.cycles_remaining_at_current_dose:g} more at the current dose.",
                    evidence=e.sources,
                )
            )

    for t in trajectories:
        if t.direction == "worsening" and t.latest_grade >= 3:
            flags.append(
                Flag(
                    kind="toxicity",
                    severity="attention",
                    message=f"{t.ctcae_term} has climbed to grade {t.latest_grade} over {len(t.points)} recorded cycles.",
                    evidence=[f"grades {', '.join(str(p.grade) for p in t.points)}"],
                )
            )
        elif t.direction == "worsening":
            flags.append(
                Flag(
                    kind="toxicity",
                    severity="note",
                    message=f"{t.ctcae_term} rising: grade {t.first_grade} to {t.latest_grade}.",
                    evidence=[f"grades {', '.join(str(p.grade) for p in t.points)}"],
                )
            )

    for tr in trends:
        if tr.direction in ("rising", "falling") and len(tr.values) >= 3:
            severity = "attention" if tr.out_of_range else "note"
            flags.append(
                Flag(
                    kind="lab_trend",
                    severity=severity,
                    message=f"{tr.test_canonical} {tr.direction} across {len(tr.values)} visits: {tr.values[0]:g} to {tr.values[-1]:g} {tr.unit or ''}".strip() + ("; latest is outside the reference range." if tr.out_of_range else "."),
                    evidence=[f"detector: {tr.detector}"],
                )
            )

    order = {"attention": 0, "note": 1}
    flags.sort(key=lambda f: order[f.severity])
    return flags


# --- timeline --------------------------------------------------------------


def build_timeline(documents: list[Document]) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []

    for doc in documents:
        for d in doc.drugs:
            if not d.given_on:
                continue
            conf = confidence_score(model_confidence=d.model_confidence, resolved=d.rxnorm is not None, kind="reading")
            dose = f"{d.dose:g} {d.dose_unit}" if d.dose is not None else "dose not legible"
            entries.append(
                TimelineEntry(
                    on=d.given_on,
                    cycle=d.cycle,
                    kind="drug",
                    label=(d.drug_generic or d.drug_raw).title(),
                    detail=f"{dose}{(', ' + d.route) if d.route else ''}",
                    document_id=doc.id,
                    confidence=conf.score,
                    abstain=conf.abstain,
                )
            )
        for lab in doc.labs:
            if not lab.drawn_on:
                continue
            conf = confidence_score(model_confidence=lab.model_confidence, resolved=lab.loinc is not None, kind="reading")
            flagged = ""
            if lab.value is not None and lab.ref_high is not None and lab.value > lab.ref_high:
                flagged = " (above range)"
            elif lab.value is not None and lab.ref_low is not None and lab.value < lab.ref_low:
                flagged = " (below range)"
            entries.append(
                TimelineEntry(
                    on=lab.drawn_on,
                    cycle=None,
                    kind="lab",
                    label=lab.test_canonical or lab.test_raw,
                    detail=f"{lab.value:g} {lab.unit or ''}{flagged}".strip(),
                    document_id=doc.id,
                    confidence=conf.score,
                    abstain=conf.abstain,
                )
            )
        for t in doc.toxicities:
            if not t.observed_on:
                continue
            conf = confidence_score(model_confidence=t.model_confidence, resolved=t.ctcae_term is not None, kind="reading")
            entries.append(
                TimelineEntry(
                    on=t.observed_on,
                    cycle=t.cycle,
                    kind="toxicity",
                    label=t.ctcae_term or t.term_raw,
                    detail=f"grade {t.grade}" if t.grade else "grade not recorded",
                    document_id=doc.id,
                    confidence=conf.score,
                    abstain=conf.abstain,
                )
            )

    entries.sort(key=lambda e: (e.on, e.kind))
    return entries


def assemble(patient_pseudonym: str, documents: list[Document], today: date | None = None) -> PatientRecord:
    timeline = build_timeline(documents)
    exposures = cumulative_exposure(documents)
    trajectories = toxicity_trajectories(documents)
    trends = lab_trends(documents)
    flags = continuity_flags(documents, exposures, trajectories, trends, today=today)

    held = sum(1 for e in timeline if e.abstain)
    return PatientRecord(
        patient_pseudonym=patient_pseudonym,
        documents=documents,
        timeline=timeline,
        exposures=exposures,
        trajectories=trajectories,
        lab_trends=trends,
        flags=flags,
        field_count=len(timeline),
        auto_filled=len(timeline) - held,
        held_for_review=held,
    )
