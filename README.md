# CancerCare

Turning the paper a cancer patient carries into a structured, clinician-confirmed
treatment record.

Health-a-thon 2026 — Cancer track · Doctor / Care Team · Patient Follow-up and
Continuity of Care.

---

## The problem

A returning cancer patient's history arrives as loose paper from several
hospitals, with no consolidated record. The oncologist rebuilds the treatment
timeline by hand inside a short OPD slot.

Much of what decides the next cycle is not in any single document:

- cumulative anthracycline or platinum exposure across every prior cycle, against
  the lifetime ceiling
- the toxicity grade recorded last cycle, and whether it is climbing or settling
- whether counts have recovered enough to treat, and how many cycles were delayed
- which regimens have already been tried and failed

Labs done at a local facility between visits rarely reach the treating
oncologist. Digitising the pages does not fix this, because the answer has to be
*assembled across* documents, not read off one.

## What this does

```
scans ──▶ anonymise ──▶ extract ──▶ normalise ──▶ assemble ──▶ clinician confirms
          (stage 2)     (stage 3)   (stage 4)     (stage 5)    (stage 6 gate)
```

Only stage 3 uses a learned model. Everything that produces a clinical number is
deterministic, which is what makes the output auditable and reproducible — the
same documents always give the same record — and what lets the model stay small
enough to run inside the hospital.

## Run it

```bash
pip install -r requirements.txt
uvicorn backend.app:app --reload
# http://127.0.0.1:8000  →  "Load sample case"  →  "Build record"
```

Or `./run.sh`.

The bundled case is six synthetic documents for one patient across four sites and
six cycles. One of them is a district-hospital referral carrying a brand name the
index does not know, so you can see the confidence gate hold a field back rather
than guess it.

```bash
python -m pytest tests/ -q
```

## How it works

### Stage 2 — anonymisation (`backend/anonymise.py`)

Runs before anything else touches the page, so every downstream artefact —
timeline, logs, cached extractions — is already de-identified.

Identifiers are not blacked out. Each is replaced by a **stable keyed pseudonym**,
so the same patient still links across documents from different hospitals. Plain
redaction would break the timeline, which is the entire product. The key and the
lookup stay on the machine that did the redaction.

Microsoft Presidio is used when installed; it is not a hard dependency. The
pattern rules cover UHID/MRN, Aadhaar-shaped numbers, Indian mobile numbers,
email, PIN codes, names and addresses as they appear on Indian clinical
paperwork.

### Stage 3 — extraction (`backend/extract.py`)

Two implementations behind one interface.

`VLMExtractor` is the deployed path: a small vision-language model
(Qwen2.5-VL-3B or MedGemma-4B, LoRA tuned) reads the page image and emits JSON
directly, under constrained decoding against `OUTPUT_SCHEMA`, with a bounding box
per field. **There is no OCR stage.** Layout — which column a value sits in, which
label it belongs to — is a signal the model uses rather than a loss it has to
recover from, and removing the stage removes a place for errors to compound. It
needs a GPU and is not loaded in the prototype.

`RuleExtractor` produces the same objects from the redacted text of a document.
It is what makes this runnable on a laptop.

The prompt is deliberately blunt about not inferring: a value that is illegible
comes back null, never completed from context.

### Stage 4 — normalisation (`backend/normalise.py`)

Drug strings resolve generic → Indian brand index → fuzzy. **Indian prescribing is
brand-first and RxNorm covers it poorly**, so `backend/data/drugs.json` carries a
hand-seeded brand map (Adrim, Kemocarb, Oxitan, Endoxan …). Labs resolve to a
canonical name plus LOINC with a reference range; toxicity terms to CTCAE v5.0
preferred terms. Units are canonicalised.

An unresolved string is never guessed into something plausible. It is returned
unresolved, and that fact lowers the field's confidence.

### Stage 5 — assembly (`backend/assemble.py`)

Arithmetic and lookups, no model. Derives the three things no single document
contains:

| Output | How |
|---|---|
| Cumulative exposure | Sum per capped drug across all documents, against the lifetime ceiling in `data/ceilings.json`. Mixed units are reported as not summable rather than coerced. |
| Toxicity trajectory | CTCAE grade per cycle with direction of travel. A grade 3 that climbed steadily means something different from one that spiked once and settled. |
| Lab trend | Robust median/MAD baseline, then CUSUM for a sustained level shift rather than a single outlier. |
| Continuity flags | Overdue visits and out-of-range trends. Statements about data on file — no prediction, so no outcome labels are needed. |

### Stage 6 — the gate (`backend/confidence.py`)

A field is auto-filled only if it clears the threshold. Below it the field is left
blank and routed to the clinician. In a safety context a blank is a correct
outcome; a confidently wrong dose is not.

Confidence is a product of normalised terms — model certainty, whether the name
resolved, corroboration, margin over the runner-up, baseline quality — so one
weak signal pulls the score down and the trace shows which one did it.

Density only applies to *derived* claims. For a transcribed field the claim is
"this page says X", and one page is complete evidence for that; a clearly read
dose does not become more true because a second document repeats it. A cumulative
total is different, and is scored on how complete the record behind it is.

The threshold (`DEFAULT_THRESHOLD = 0.70`) is an operating point, not an accuracy
claim. Sweeping it traces the coverage-against-error curve, which is what should
be reported in evaluation rather than a single number.

## Evaluation

Not yet run. The extraction stage will be benchmarked against architectures from
the oncology extraction literature — shallow CNN, HiSAN, ClinicalBERT, and a
DocTR-plus-local-LLM two-stage pipeline — on two public datasets:

- **TCGA-Reports** — 9,523 scanned oncology pathology report PDFs, open access
- **MedRepBench** — 1,925 real report images with field-level structured labels
  and published VLM baselines

Scored on field-level precision, recall and F1 after unit and drug-code
normalisation, plus hallucination rate (fields emitted with no corresponding
value on the page), parameters, VRAM and latency per document.

No estimated figures are reported as measurements anywhere in this repository.

## What this does not claim

- **No clinical decisions.** It assembles and presents. It does not recommend a
  dose, diagnose, or decide whether to treat.
- **No causal attribution.** A value changing after a drug does not establish that
  the drug caused it; disease progression, a second agent and intercurrent
  illness produce the same signature.
- **Detection needs data.** Below three post-exposure measurements the statistical
  detectors are off and only guideline thresholds apply. In outpatient follow-up
  that is the common case.
- **Handwriting is hard.** Printed reports extract far better than cursive
  prescriptions. The two are reported separately rather than averaged.

## Layout

```
backend/
  app.py          FastAPI routes and static serving
  anonymise.py    stage 2 — PII detection, keyed pseudonymisation
  extract.py      stage 3 — VLM adapter, prompt, output schema, rule extractor
  normalise.py    stage 4 — drug/lab/toxicity/unit normalisation
  assemble.py     stage 5 — timeline, cumulative dose, trajectory, trends, flags
  confidence.py   stage 6 — scoring and abstention
  schema.py       shared models
  data/           ceilings, Indian brand index, LOINC and CTCAE vocabulary
frontend/
  index.html      one screen
  styles.css      the chart
  app.js          client
samples/          six synthetic documents, one patient, four sites
tests/            18 tests over the derived quantities and the gate
```

## Data

Everything in `samples/` is synthetic and written for this repository. No real
patient data is in this repository and none should be committed to it —
`.gitignore` excludes image and PDF uploads for that reason. Any pilot on real
documents needs institutional ethics approval first.
