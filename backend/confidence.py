"""Stage 6 — the confidence gate.

A field is auto-filled only if it clears the threshold. Below it, the field is
left blank and routed to the clinician. A blank field is a correct outcome; a
confidently wrong dose is not.

Confidence is a product of normalised terms, so any one weak signal pulls the
whole score down and the trace shows which one did it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Swept in evaluation to trace the coverage-against-error curve. This is the
# operating point, not a claim about accuracy.
DEFAULT_THRESHOLD = 0.70


@dataclass
class Confidence:
    score: float
    abstain: bool
    trace: dict[str, float]
    reasons: list[str]


def _density(n: int, ideal: int = 3) -> float:
    """More corroborating observations, more confidence. Saturates at `ideal`."""
    if n <= 0:
        return 0.0
    return min(1.0, n / ideal)


def score(
    *,
    model_confidence: float,
    resolved: bool,
    kind: str = "derived",
    observations: int = 1,
    margin: float = 1.0,
    baseline_quality: float = 1.0,
    threshold: float = DEFAULT_THRESHOLD,
) -> Confidence:
    """
    model_confidence  how clearly the model could read it
    resolved          whether the drug/lab/term mapped to a known code
    kind              "reading" for a value transcribed off one page;
                      "derived" for a claim assembled across documents
    observations      how many documents corroborate it (derived claims only)
    margin            separation from the runner-up reading (1.0 = unambiguous)
    baseline_quality  1.0 with a real pre-treatment baseline, lower without

    Density only applies to derived claims. For a transcribed field the claim is
    "this page says X", and one page is complete evidence for that — a clearly
    read dose does not become more true because a second document repeats it.
    A cumulative total is different: it is only as good as the completeness of
    the record behind it.
    """
    density = 1.0 if kind == "reading" else _density(observations)
    trace = {
        "model": round(model_confidence, 3),
        "resolution": 1.0 if resolved else 0.45,
        "density": round(density, 3),
        "margin": round(min(max(margin, 0.0), 1.0), 3),
        "baseline": round(min(max(baseline_quality, 0.0), 1.0), 3),
    }

    # Density and margin are weighted softly: a single clearly-read value from
    # one document should still be usable.
    s = (
        trace["model"]
        * trace["resolution"]
        * (0.55 + 0.45 * trace["density"])
        * (0.70 + 0.30 * trace["margin"])
        * (0.75 + 0.25 * trace["baseline"])
    )
    s = round(min(max(s, 0.0), 1.0), 3)

    reasons: list[str] = []
    if not resolved:
        reasons.append("drug or test name did not resolve to a known code")
    if kind != "reading" and observations < 2:
        reasons.append("only one document reports this")
    if margin < 0.6:
        reasons.append("a second reading was nearly as likely")
    if baseline_quality < 0.6:
        reasons.append("no pre-treatment baseline on file")

    return Confidence(score=s, abstain=s < threshold, trace=trace, reasons=reasons)
