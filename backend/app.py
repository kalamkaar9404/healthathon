"""HTTP API and static file serving.

Run:  uvicorn backend.app:app --reload
Then: http://127.0.0.1:8000
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .anonymise import anonymise, pseudonym
from .assemble import assemble
from .confidence import DEFAULT_THRESHOLD
from .extract import RuleExtractor
from .schema import Document

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SAMPLES = ROOT / "samples"

app = FastAPI(title="CancerCare", version="0.1.0")
extractor = RuleExtractor()

# In-memory for the prototype. A deployment keeps this in the hospital's own
# store; nothing here is written to disk or sent anywhere.
SESSIONS: dict[str, dict] = {}


class ProcessRequest(BaseModel):
    texts: list[str] = []
    filenames: list[str] = []
    threshold: float = DEFAULT_THRESHOLD
    today: date | None = None


def _process(texts: list[str], filenames: list[str], today: date | None = None) -> dict:
    documents: list[Document] = []
    audit: list[dict] = []
    subject = None

    for i, raw in enumerate(texts):
        name = filenames[i] if i < len(filenames) else f"document-{i + 1}"

        # Stage 2 first, always. Nothing downstream ever sees the original.
        result = anonymise(raw)
        if subject is None:
            subject = result.pseudonym_for("NAME") or result.pseudonym_for("UHID")

        doc_id = f"doc-{i + 1}"
        doc = extractor.extract(doc_id, name, result.text)
        doc.redacted = True
        doc.redaction_count = result.count
        documents.append(doc)

        audit.append(
            {
                "document_id": doc_id,
                "filename": name,
                "kind": doc.kind.value,
                "engine": result.engine,
                "redactions": [{"kind": r.kind, "pseudonym": r.pseudonym} for r in result.redactions],
                "redaction_count": result.count,
                "fields": len(doc.drugs) + len(doc.labs) + len(doc.toxicities),
            }
        )

    record = assemble(subject or pseudonym("NAME", "unknown subject"), documents, today=today)
    return {"record": record.model_dump(mode="json"), "audit": audit}


@app.post("/api/process")
def process(req: ProcessRequest) -> JSONResponse:
    if not req.texts:
        raise HTTPException(400, "No documents supplied.")
    return JSONResponse(_process(req.texts, req.filenames, today=req.today))


@app.post("/api/upload")
async def upload(files: list[UploadFile]) -> JSONResponse:
    """Accepts text transcripts of documents. Image upload goes to the VLM path,
    which needs a GPU and is not loaded in the prototype."""
    texts, names = [], []
    for f in files:
        body = (await f.read()).decode("utf-8", errors="replace")
        texts.append(body)
        names.append(f.filename or "untitled")
    if not texts:
        raise HTTPException(400, "No files received.")
    return JSONResponse(_process(texts, names))


@app.get("/api/samples")
def samples() -> JSONResponse:
    """The bundled case, so the prototype demonstrates without any upload."""
    out = []
    for path in sorted(SAMPLES.glob("*.txt")):
        out.append({"filename": path.name, "text": path.read_text()})
    return JSONResponse({"documents": out})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "extractor": type(extractor).__name__, "threshold": DEFAULT_THRESHOLD}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
