"""MVP web app: upload an order, watch it flow through the five steps,
confirm, and see the row appear in the (mock) ERP.

Business-facing demo of the pipeline - the UI never shows code, it shows
the document in, the extraction, the per-line verdicts and the ERP row.
The ERP write stays behind an explicit confirm click (the guardrail).
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .config import load_config
from .models import PipelineRun, RunStatus
from .pipeline import Pipeline

STATIC_DIR = Path(__file__).resolve().parent / "webstatic"
ALLOWED_SUFFIXES = {".eml", ".pdf", ".xlsx", ".xlsm", ".csv", ".txt"}


def create_app() -> FastAPI:
    app = FastAPI(title="Order workflow MVP", docs_url=None, redoc_url=None)
    config = load_config()
    pipeline = Pipeline(config)
    runs: dict[str, PipelineRun] = {}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/info")
    def info() -> dict:
        return {
            "mode": "llm" if config.llm_enabled() else "heuristic",
            "model": config.model if config.llm_enabled() else None,
        }

    @app.get("/api/samples")
    def samples() -> list[dict]:
        if not config.samples_dir.exists():
            return []
        return [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted(config.samples_dir.iterdir())
            if p.suffix.lower() in ALLOWED_SUFFIXES
        ]

    def _run_to_json(run: PipelineRun) -> JSONResponse:
        return JSONResponse(run.model_dump(mode="json"))

    @app.post("/api/process")
    async def process(file: UploadFile) -> JSONResponse:
        suffix = Path(file.filename or "order.txt").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(415, f"Unsupported file type: {suffix}")
        payload = await file.read()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            run = pipeline.process(tmp_path, display_name=file.filename or tmp_path.name)
        finally:
            tmp_path.unlink(missing_ok=True)
        runs[run.run_id] = run
        return _run_to_json(run)

    @app.post("/api/process-sample/{name}")
    def process_sample(name: str) -> JSONResponse:
        path = (config.samples_dir / name).resolve()
        if not path.is_file() or path.parent != config.samples_dir.resolve():
            raise HTTPException(404, "Sample not found")
        run = pipeline.process(path, display_name=name)
        runs[run.run_id] = run
        return _run_to_json(run)

    confirm_lock = threading.Lock()

    @app.post("/api/confirm/{run_id}")
    def confirm(run_id: str) -> JSONResponse:
        # Serialized: two near-simultaneous confirms (double-click) must not
        # both pass the status check and double-write the ERP.
        with confirm_lock:
            run = runs.get(run_id)
            if run is None:
                raise HTTPException(404, "Run not found")
            if run.status != RunStatus.AWAITING_CONFIRMATION:
                raise HTTPException(409, f"Run is not awaiting confirmation (status: {run.status.value})")
            return _run_to_json(pipeline.confirm(run))

    @app.get("/api/erp/orders")
    def erp_orders() -> list[dict]:
        return pipeline.erp.list_orders()

    return app
