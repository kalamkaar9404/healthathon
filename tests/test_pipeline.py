"""Tests for the parts that must not silently change: the derived quantities.

The extraction stage is evaluated on benchmarks, not here. What these tests
protect is the arithmetic and the gate, because a wrong cumulative dose is the
one error in this pipeline with a permanent clinical consequence.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from healthathon.backend.anonymise import anonymise, pseudonym
from healthathon.backend.assemble import assemble
from healthathon.backend.confidence import score
from healthathon.backend.extract import RuleExtractor
from healthathon.backend.normalise import dose_to_mg, normalise_drug

SAMPLES = pathlib.Path(__file__).resolve().parents[1] / "samples"


def build_record():
    ex = RuleExtractor()
    docs = []
    for i, path in enumerate(sorted(SAMPLES.glob("*.txt"))):
        result = anonymise(path.read_text())
        doc = ex.extract(f"doc-{i + 1}", path.name, result.text)
        doc.redacted = True
        doc.redaction_count = result.count
        docs.append(doc)
    return assemble("TEST", docs, today=date(2026, 6, 1))


# --- anonymisation ---------------------------------------------------------


def test_identifiers_are_removed():
    text = "Patient Name: Lakshmi Narayanan\nUHID: SCC/2026/04417\nPhone: 9845012233"
    out = anonymise(text)
    assert "Lakshmi" not in out.text
    assert "9845012233" not in out.text
    assert "SCC/2026/04417" not in out.text
    assert out.count >= 3


def test_pseudonyms_are_stable_across_documents():
    """The whole timeline depends on this: redaction must not break linkage."""
    a = pseudonym("NAME", "Lakshmi Narayanan")
    b = pseudonym("NAME", "lakshmi  narayanan")
    assert a == b
    assert a != pseudonym("NAME", "Someone Else")


def test_pseudonyms_are_not_the_original():
    assert "LAKSHMI" not in pseudonym("NAME", "Lakshmi Narayanan")


# --- normalisation ---------------------------------------------------------


def test_indian_brand_resolves_to_generic():
    generic, rxnorm, conf = normalise_drug("Adrim 50 inj")
    assert generic == "doxorubicin"
    assert rxnorm == "3639"
    assert conf >= 0.9


def test_unknown_brand_does_not_resolve():
    generic, rxnorm, conf = normalise_drug("Rubidox 60")
    assert generic is None
    assert conf == 0.0


def test_unrecognised_unit_is_not_guessed():
    assert dose_to_mg(5, "spoons") == (None, None)
    assert dose_to_mg(1, "g") == (1000.0, "mg")
    assert dose_to_mg(60, "mg/m2") == (60, "mg/m2")


# --- derived quantities ----------------------------------------------------


def test_cumulative_dose_sums_across_hospitals():
    rec = build_record()
    dox = next(e for e in rec.exposures if e.drug_generic == "doxorubicin")
    # Four documents record a 60 mg/m2 dose; the fifth is a lab report with no
    # drugs, and the sixth withheld cisplatin only.
    assert dox.cycles_recorded == 4
    assert dox.cumulative == 240.0
    assert dox.ceiling == 550.0
    assert abs(dox.fraction_of_ceiling - 240 / 550) < 1e-3  # stored rounded to 4dp


def test_cisplatin_approaching_ceiling_is_flagged():
    rec = build_record()
    cis = next(e for e in rec.exposures if e.drug_generic == "cisplatin")
    assert cis.fraction_of_ceiling >= 0.7
    assert any(f.kind == "exposure" for f in rec.flags)


def test_uncapped_drug_reports_no_ceiling():
    rec = build_record()
    cyc = next(e for e in rec.exposures if e.drug_generic == "cyclophosphamide")
    assert cyc.ceiling is None
    assert cyc.fraction_of_ceiling is None


def test_toxicity_trajectory_direction():
    rec = build_record()
    neuro = next(t for t in rec.trajectories if "neuropathy" in t.ctcae_term.lower())
    assert [p.grade for p in neuro.points] == [1, 2, 3]
    assert neuro.direction == "worsening"
    assert neuro.latest_grade == 3


def test_single_observation_is_not_called_a_trend():
    rec = build_record()
    for t in rec.trajectories:
        if len(t.points) == 1:
            assert t.direction == "single observation"


def test_rising_creatinine_is_flagged():
    rec = build_record()
    creat = next(t for t in rec.lab_trends if t.test_canonical == "Serum creatinine")
    assert creat.direction == "rising"
    assert creat.out_of_range is True


def test_overdue_flag_uses_the_last_document():
    rec = build_record()
    assert any(f.kind == "overdue" for f in rec.flags)


# --- the gate --------------------------------------------------------------


def test_unresolved_drug_is_held_not_guessed():
    rec = build_record()
    held = [e for e in rec.timeline if e.abstain]
    assert held, "the unresolvable brand should not have been auto-filled"
    assert any("rubidox" in e.label.lower() for e in held)


def test_clear_reading_clears_the_gate():
    c = score(model_confidence=0.95, resolved=True, kind="reading")
    assert not c.abstain
    assert c.score > 0.7


def test_unresolved_name_fails_the_gate():
    c = score(model_confidence=0.95, resolved=False, kind="reading")
    assert c.abstain
    assert "did not resolve" in " ".join(c.reasons)


def test_derived_claim_needs_corroboration():
    """One document supporting a derived total scores lower than four do."""
    thin = score(model_confidence=0.95, resolved=True, observations=1, margin=0.25)
    thick = score(model_confidence=0.95, resolved=True, observations=4, margin=1.0)
    assert thin.score < thick.score


def test_every_timeline_entry_names_its_source():
    rec = build_record()
    for entry in rec.timeline:
        assert entry.document_id
