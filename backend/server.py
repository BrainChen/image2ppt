from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse

from backend.pipeline import PROJECT_ROOT, convert_image_to_ppt
from backend.utils.logging_config import configure_logging, format_kv


configure_logging()
logger = logging.getLogger(__name__)
app = FastAPI(title="img2ppt MVP", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    skip_ocr: bool = Form(False),
    ocr_failure_mode: str | None = Form(None),
    use_reasoning: bool | None = Form(None),
    mock_layout: bool = Form(False),
) -> FileResponse:
    request_id = uuid4().hex[:8]
    suffix = Path(file.filename or "slide.png").suffix or ".png"
    upload_dir = PROJECT_ROOT / "outputs" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    image_path = upload_dir / f"{request_id}{suffix}"
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
        ),
    )

    output_path = PROJECT_ROOT / "outputs" / "ppt" / f"{image_path.stem}.pptx"
    result = convert_image_to_ppt(
        image_path,
        pptx_path=output_path,
        skip_ocr=skip_ocr,
        ocr_failure_mode=ocr_failure_mode,
        use_reasoning=use_reasoning,
        mock_layout=mock_layout,
    )
    if not result.pptx_path:
        raise RuntimeError("Conversion did not produce a PPTX file")
    logger.info(
        "api.convert.end %s",
        format_kv(request_id=request_id, pptx=result.pptx_path, ast=result.ast_path, artifacts=result.artifact_dir),
    )
    return FileResponse(
        result.pptx_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{Path(file.filename or image_path.name).stem}.pptx",
    )
