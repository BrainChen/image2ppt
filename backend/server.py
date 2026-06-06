from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.pipeline import PROJECT_ROOT, convert_image_to_ppt, make_timestamp_id
from backend.utils.logging_config import configure_logging, format_kv


load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)
app = FastAPI(title="img2ppt MVP", version="0.1.0")
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
JOB_LOCK = threading.Lock()
JOBS: dict[str, "ConversionJob"] = {}


@dataclass
class ConversionJob:
    id: str
    request_id: str
    filename: str
    output_stem: str
    image_path: str
    ast_path: str
    pptx_path: str
    crop_dir: str
    artifact_dir: str
    options: dict[str, object]
    status: str = "queued"
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str | None = None
    completed_at: str | None = None


def verify_form_access_token(access_token: str | None = Form(None)) -> None:
    verify_access_token(access_token)


def verify_query_access_token(access_token: str | None = Query(None)) -> None:
    verify_access_token(access_token)


def verify_access_token(access_token: str | None) -> None:
    error = access_token_error(access_token)
    if error:
        raise HTTPException(status_code=error[1], detail=error[0])


def access_token_error(access_token: str | None) -> tuple[str, int] | None:
    expected = os.getenv("IMG2PPT_ACCESS_TOKEN", "").strip()
    if not expected:
        return ("Server access token is not configured.", 503)
    if not access_token or not secrets.compare_digest(access_token, expected):
        return ("Invalid or missing access token.", 401)
    return None


@app.get("/health")
def health(_access: None = Depends(verify_query_access_token)) -> dict[str, str]:
    return {"status": "ok"}


@app.middleware("http")
async def protect_output_files(request: Request, call_next):
    if request.url.path.startswith("/outputs/"):
        error = access_token_error(request.query_params.get("access_token"))
        if error:
            return JSONResponse({"detail": error[0]}, status_code=error[1])
    return await call_next(request)


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    skip_ocr: bool = Form(False),
    ocr_failure_mode: str | None = Form(None),
    use_reasoning: bool | None = Form(None),
    mock_layout: bool = Form(False),
    preprocess_raw_image: bool = Form(False),
    _access: None = Depends(verify_form_access_token),
) -> FileResponse:
    request_id = make_timestamp_id()
    suffix = Path(file.filename or "slide.png").suffix or ".png"
    source_stem = safe_stem(Path(file.filename or "slide").stem or "slide")
    output_stem = f"{source_stem}_{request_id}"
    upload_dir = PROJECT_ROOT / "outputs" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    image_path = upload_dir / f"{output_stem}{suffix}"
    content = await file.read()
    image_path.write_bytes(content)
    logger.info(
        "api.convert.start %s",
        format_kv(
            request_id=request_id,
            filename=file.filename,
            bytes=len(content),
            skip_ocr=skip_ocr,
            ocr_failure_mode=ocr_failure_mode,
            use_reasoning=use_reasoning,
            mock_layout=mock_layout,
            preprocess_raw_image=preprocess_raw_image,
        ),
    )

    result = convert_image_to_ppt(
        image_path,
        conversion_id=request_id,
        ast_path=PROJECT_ROOT / "outputs" / "ast" / f"{output_stem}_ast.json",
        pptx_path=PROJECT_ROOT / "outputs" / "ppt" / f"{output_stem}.pptx",
        crop_dir=PROJECT_ROOT / "outputs" / "crops" / output_stem,
        artifact_dir=PROJECT_ROOT / "outputs" / "intermediates" / output_stem,
        skip_ocr=skip_ocr,
        ocr_failure_mode=ocr_failure_mode,
        use_reasoning=use_reasoning,
        mock_layout=mock_layout,
        preprocess_raw_image=preprocess_raw_image,
    )
    if not result.pptx_path:
        raise RuntimeError("Conversion did not produce a PPTX file")
    create_ppt_preview(result.pptx_path, result.artifact_dir)
    logger.info(
        "api.convert.end %s",
        format_kv(request_id=request_id, pptx=result.pptx_path, ast=result.ast_path, artifacts=result.artifact_dir),
    )
    return FileResponse(
        result.pptx_path,
        media_type=PPTX_MEDIA_TYPE,
        filename=f"{Path(file.filename or image_path.name).stem}.pptx",
    )


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    skip_ocr: bool = Form(False),
    ocr_failure_mode: str | None = Form(None),
    use_reasoning: bool | None = Form(None),
    mock_layout: bool = Form(False),
    preprocess_raw_image: bool = Form(True),
    _access: None = Depends(verify_form_access_token),
) -> dict[str, object]:
    request_id = make_timestamp_id()
    job_id = uuid.uuid4().hex
    original_filename = file.filename or "raw-image.png"
    suffix = Path(original_filename).suffix or ".png"
    source_stem = safe_stem(Path(original_filename).stem or "raw-image")
    output_stem = f"{source_stem}_{request_id}"
    upload_dir = PROJECT_ROOT / "outputs" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    image_path = upload_dir / f"{output_stem}{suffix}"
    content = await file.read()
    image_path.write_bytes(content)

    job = ConversionJob(
        id=job_id,
        request_id=request_id,
        filename=original_filename,
        output_stem=output_stem,
        image_path=str(image_path),
        ast_path=str(PROJECT_ROOT / "outputs" / "ast" / f"{output_stem}_ast.json"),
        pptx_path=str(PROJECT_ROOT / "outputs" / "ppt" / f"{output_stem}.pptx"),
        crop_dir=str(PROJECT_ROOT / "outputs" / "crops" / output_stem),
        artifact_dir=str(PROJECT_ROOT / "outputs" / "intermediates" / output_stem),
        options={
            "skip_ocr": skip_ocr,
            "ocr_failure_mode": ocr_failure_mode,
            "use_reasoning": use_reasoning,
            "mock_layout": mock_layout,
            "preprocess_raw_image": preprocess_raw_image,
        },
    )
    with JOB_LOCK:
        JOBS[job_id] = job

    logger.info(
        "api.jobs.create %s",
        format_kv(
            job_id=job_id,
            request_id=request_id,
            filename=original_filename,
            bytes=len(content),
            preprocess_raw_image=preprocess_raw_image,
        ),
    )
    background_tasks.add_task(run_job, job_id)
    return job_response(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, _access: None = Depends(verify_query_access_token)) -> dict[str, object]:
    return job_response(require_job(job_id))


@app.get("/api/jobs/{job_id}/download")
def download_job_ppt(job_id: str, _access: None = Depends(verify_query_access_token)) -> FileResponse:
    job = require_job(job_id)
    pptx_path = Path(job.pptx_path)
    if not pptx_path.exists():
        raise HTTPException(status_code=404, detail="PPTX is not ready yet")
    return FileResponse(
        pptx_path,
        media_type=PPTX_MEDIA_TYPE,
        filename=f"{Path(job.filename).stem or 'img2ppt'}.pptx",
    )


def run_job(job_id: str) -> None:
    job = require_job(job_id)
    update_job(job_id, status="running", started_at=current_timestamp(), error=None)
    try:
        result = convert_image_to_ppt(
            Path(job.image_path),
            conversion_id=job.request_id,
            ast_path=Path(job.ast_path),
            pptx_path=Path(job.pptx_path),
            crop_dir=Path(job.crop_dir),
            artifact_dir=Path(job.artifact_dir),
            skip_ocr=bool(job.options["skip_ocr"]),
            ocr_failure_mode=job.options["ocr_failure_mode"] if isinstance(job.options["ocr_failure_mode"], str) else None,
            use_reasoning=job.options["use_reasoning"] if isinstance(job.options["use_reasoning"], bool) else None,
            mock_layout=bool(job.options["mock_layout"]),
            preprocess_raw_image=bool(job.options["preprocess_raw_image"]),
        )
        if result.pptx_path:
            create_ppt_preview(result.pptx_path, result.artifact_dir)
        update_job(job_id, status="done", completed_at=current_timestamp())
        logger.info("api.jobs.done %s", format_kv(job_id=job_id, pptx=job.pptx_path, artifacts=job.artifact_dir))
    except Exception as exc:
        logger.exception("api.jobs.failed %s", format_kv(job_id=job_id, error=str(exc)))
        update_job(job_id, status="failed", error=str(exc), completed_at=current_timestamp())


def create_ppt_preview(pptx_path: str | Path, artifact_dir: str | Path) -> Path | None:
    pptx_path = Path(pptx_path)
    artifact_dir = Path(artifact_dir)
    target = artifact_dir / "09_ppt_preview.png"
    if not pptx_path.exists():
        return None

    qlmanage = shutil.which("qlmanage")
    if not qlmanage:
        logger.warning("ppt.preview.unavailable %s", format_kv(reason="qlmanage not found", pptx=pptx_path))
        return None

    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="img2ppt-preview-") as temp_dir:
        command = [qlmanage, "-t", "-s", "1600", "-o", temp_dir, str(pptx_path)]
        result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
        generated = Path(temp_dir) / f"{pptx_path.name}.png"
        if result.returncode != 0 or not generated.exists():
            logger.warning(
                "ppt.preview.failed %s",
                format_kv(returncode=result.returncode, stdout=result.stdout.strip(), stderr=result.stderr.strip()),
            )
            return None
        shutil.move(str(generated), target)
    logger.info("ppt.preview.created %s", format_kv(pptx=pptx_path, preview=target))
    return target


def job_response(job: ConversionJob) -> dict[str, object]:
    data = asdict(job)
    artifacts = collect_job_artifacts(job)
    data["artifacts"] = artifacts
    data["download_url"] = f"/api/jobs/{job.id}/download" if Path(job.pptx_path).exists() else None
    data["ready_count"] = sum(1 for artifact in artifacts if artifact["ready"])
    return data


def collect_job_artifacts(job: ConversionJob) -> list[dict[str, object]]:
    artifact_dir = Path(job.artifact_dir)
    preprocessed_image = first_image(artifact_dir / "00_raw_image_preprocess")
    stages: list[tuple[str, str, Path | None]] = [
        ("raw_image_preprocess", "Nanobanana 预处理图", preprocessed_image),
        ("layout_boxes", "02 布局框检测图", artifact_dir / "02_after_layout_boxes.png"),
        ("ppt_preview", "最终 PPT 截图", artifact_dir / "09_ppt_preview.png"),
    ]
    return [artifact_payload(key, label, path) for key, label, path in stages]


def first_image(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            return path
    return None


def artifact_payload(key: str, label: str, path: Path | None) -> dict[str, object]:
    ready = bool(path and path.exists())
    return {
        "key": key,
        "label": label,
        "ready": ready,
        "url": project_url(path) if ready and path else None,
        "filename": path.name if path else None,
        "updated_at": path.stat().st_mtime if ready and path else None,
    }


def project_url(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT)
    return "/" + quote(relative.as_posix(), safe="/")


def require_job(job_id: str) -> ConversionJob:
    with JOB_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def update_job(job_id: str, **changes: object) -> None:
    with JOB_LOCK:
        job = JOBS[job_id]
        for key, value in changes.items():
            setattr(job, key, value)


def current_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return stem or "raw-image"


outputs_dir = PROJECT_ROOT / "outputs"
outputs_dir.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=outputs_dir), name="outputs")

static_dir = PROJECT_ROOT / "backend" / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
