from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from backend.agents.image_agent import ImageExtractor
from backend.agents.layout_agent import LayoutAgent
from backend.agents.ocr_agent import OcrAgent, merge_ocr_items
from backend.agents.raw_image_preprocessor import RawImagePreprocessor
from backend.agents.reasoning_agent import ReasoningAgent
from backend.agents.style_agent import StyleExtractor
from backend.ast.slide_ast import SlideDocument, SlideElement, Style
from backend.utils.logging_config import configure_logging, format_kv, log_stage
from backend.utils.model_config import is_reasoning_configured


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
logger = logging.getLogger("backend.pipeline")


@dataclass(frozen=True)
class ConversionResult:
    ast_path: Path
    pptx_path: Path | None
    crop_dir: Path
    artifact_dir: Path


def make_timestamp_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def convert_image_to_ppt(
    image_path: str | Path,
    *,
    conversion_id: str | None = None,
    ast_path: str | Path | None = None,
    pptx_path: str | Path | None = None,
    crop_dir: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    layout_json: str | Path | None = None,
    slide_width: float = 13.33,
    slide_height: float = 7.5,
    skip_ocr: bool = False,
    ocr_failure_mode: str | None = None,
    use_reasoning: bool | None = None,
    skip_compile: bool = False,
    mock_layout: bool = False,
    preprocess_raw_image: bool = False,
) -> ConversionResult:
    configure_logging()
    run_id = conversion_id or make_timestamp_id()
    requested_image_path = Path(image_path)
    image_path = resolve_image_path(requested_image_path)
    if image_path != requested_image_path:
        logger.warning("image.path_resolved %s", format_kv(run_id=run_id, requested=requested_image_path, resolved=image_path))

    source_image_path = image_path
    output_stem = f"{image_path.stem}_{run_id}"
    ast_path = Path(ast_path) if ast_path else PROJECT_ROOT / "outputs" / "ast" / f"{output_stem}_ast.json"
    pptx_path = Path(pptx_path) if pptx_path else PROJECT_ROOT / "outputs" / "ppt" / f"{output_stem}.pptx"
    crop_dir = Path(crop_dir) if crop_dir else PROJECT_ROOT / "outputs" / "crops" / output_stem
    artifact_dir = Path(artifact_dir) if artifact_dir else PROJECT_ROOT / "outputs" / "intermediates" / output_stem
    ast_path.parent.mkdir(parents=True, exist_ok=True)
    crop_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if pptx_path:
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
    ocr_failure_mode = resolve_ocr_failure_mode(ocr_failure_mode)

    preprocessed_image_path: Path | None = None
    if preprocess_raw_image:
        preprocessed_image_path = artifact_dir / "00_raw_image_preprocess" / f"{source_image_path.stem}_ppt.jpg"
        try:
            with log_stage(logger, "raw_image_preprocess", run_id=run_id, image=source_image_path, output=preprocessed_image_path):
                preprocessor = RawImagePreprocessor()
                image_path = preprocessor.convert_to_ppt_style_image(source_image_path, preprocessed_image_path)
            write_json_artifact(
                artifact_dir,
                "00_raw_image_preprocess.json",
                {
                    "source_image": str(source_image_path),
                    "preprocessed_image": str(preprocessed_image_path),
                    "model": preprocessor.model,
                    "base_url": preprocessor.base_url,
                    "response": preprocessor.last_response_info,
                },
            )
        except Exception as exc:
            write_json_artifact(
                artifact_dir,
                "00_raw_image_preprocess_error.json",
                {
                    "source_image": str(source_image_path),
                    "target_image": str(preprocessed_image_path),
                    "error": exception_to_artifact(exc),
                },
            )
            raise

    logger.info(
        "conversion.start %s",
        format_kv(
            run_id=run_id,
            source_image=source_image_path,
            image=image_path,
            ast=ast_path,
            pptx=pptx_path,
            crop_dir=crop_dir,
            artifact_dir=artifact_dir,
            layout_json=layout_json,
            skip_ocr=skip_ocr,
            ocr_failure_mode=ocr_failure_mode,
            use_reasoning=use_reasoning,
            skip_compile=skip_compile,
            mock_layout=mock_layout,
            preprocess_raw_image=preprocess_raw_image,
        ),
    )
    write_json_artifact(
        artifact_dir,
        "00_request.json",
        {
            "run_id": run_id,
            "requested_image": str(requested_image_path),
            "resolved_image": str(source_image_path),
            "effective_image": str(image_path),
            "preprocessed_image": str(preprocessed_image_path) if preprocessed_image_path else None,
            "ast_path": str(ast_path),
            "pptx_path": str(pptx_path) if pptx_path else None,
            "crop_dir": str(crop_dir),
            "artifact_dir": str(artifact_dir),
            "options": {
                "layout_json": str(layout_json) if layout_json else None,
                "slide_width": slide_width,
                "slide_height": slide_height,
                "skip_ocr": skip_ocr,
                "ocr_failure_mode": ocr_failure_mode,
                "use_reasoning": use_reasoning,
                "skip_compile": skip_compile,
                "mock_layout": mock_layout,
                "preprocess_raw_image": preprocess_raw_image,
            },
        },
    )

    with log_stage(logger, "image_size", run_id=run_id, image=image_path):
        image_width, image_height = image_size(image_path)
    logger.info("image.loaded %s", format_kv(run_id=run_id, width=image_width, height=image_height))
    write_json_artifact(
        artifact_dir,
        "01_image.json",
        {"path": str(image_path), "width": image_width, "height": image_height},
    )

    if layout_json:
        with log_stage(logger, "layout_from_json", run_id=run_id, layout_json=layout_json):
            raw = json.loads(Path(layout_json).read_text(encoding="utf-8"))
            write_json_artifact(artifact_dir, "02_layout_input.json", raw)
            document = SlideDocument.from_raw(
                raw,
                image_width=image_width,
                image_height=image_height,
                slide_width=slide_width,
                slide_height=slide_height,
            )
    elif mock_layout:
        with log_stage(logger, "layout_mock", run_id=run_id):
            document = create_mock_document(image_width, image_height, slide_width, slide_height)
            write_json_artifact(artifact_dir, "02_layout_mock.json", {"source": "mock_layout"})
    else:
        with log_stage(logger, "layout_vlm", run_id=run_id, image=image_path):
            layout_agent = LayoutAgent()
            try:
                document = layout_agent.parse(image_path, slide_width=slide_width, slide_height=slide_height)
            finally:
                write_text_artifact(artifact_dir, "02_layout_raw_response.txt", layout_agent.last_raw_response or "")
                write_json_artifact(artifact_dir, "02_layout_model.json", {"model": layout_agent.model, "base_url": layout_agent.base_url})
    document.clamp_to_image()
    logger.info("ast.after_layout %s", format_kv(run_id=run_id, nodes=len(document.walk())))
    write_document_artifact(artifact_dir, "02_after_layout_ast.json", document)
    write_bbox_overlay_artifact(artifact_dir, "02_after_layout_boxes.png", image_path, document)

    if not skip_ocr and not mock_layout:
        ocr_agent: OcrAgent | None = None
        try:
            with log_stage(logger, "ocr_vlm", run_id=run_id, image=image_path):
                ocr_agent = OcrAgent()
                try:
                    ocr_items = ocr_agent.extract(image_path)
                finally:
                    write_text_artifact(artifact_dir, "03_ocr_raw_response.txt", ocr_agent.last_raw_response or "")
                    write_json_artifact(artifact_dir, "03_ocr_model.json", {"model": ocr_agent.model, "base_url": ocr_agent.base_url})
                write_json_artifact(artifact_dir, "03_ocr_items.json", {"items": ocr_items})
                merge_ocr_items(document, ocr_items)
                document.clamp_to_image()
        except Exception as exc:
            write_json_artifact(
                artifact_dir,
                "03_ocr_error.json",
                {
                    "mode": ocr_failure_mode,
                    "error": exception_to_artifact(exc),
                    "model": getattr(ocr_agent, "model", None),
                    "base_url": getattr(ocr_agent, "base_url", None),
                },
            )
            if ocr_failure_mode == "fail":
                raise
            ocr_items = []
            write_json_artifact(artifact_dir, "03_ocr_items.json", {"items": ocr_items, "source": "ocr_error_fallback"})
            logger.warning(
                "ocr.failed_continuing %s",
                format_kv(run_id=run_id, error_type=exc.__class__.__name__, error=str(exc)),
            )
        logger.info("ast.after_ocr %s", format_kv(run_id=run_id, ocr_items=len(ocr_items), nodes=len(document.walk())))
        document.clamp_to_image()
        write_document_artifact(artifact_dir, "03_after_ocr_ast.json", document)
        write_bbox_overlay_artifact(artifact_dir, "03_after_ocr_boxes.png", image_path, document)
    else:
        logger.info("ocr.skipped %s", format_kv(run_id=run_id, skip_ocr=skip_ocr, mock_layout=mock_layout))
        write_json_artifact(artifact_dir, "03_ocr_skipped.json", {"skip_ocr": skip_ocr, "mock_layout": mock_layout})

    should_use_reasoning = use_reasoning if use_reasoning is not None else is_reasoning_configured()
    if should_use_reasoning and not mock_layout:
        with log_stage(logger, "reasoning_refine", run_id=run_id):
            before_nodes = len(document.walk())
            reasoning_agent = ReasoningAgent()
            try:
                document = reasoning_agent.refine(document)
            finally:
                write_text_artifact(artifact_dir, "04_reasoning_raw_response.txt", reasoning_agent.last_raw_response or "")
                write_json_artifact(artifact_dir, "04_reasoning_model.json", {"model": reasoning_agent.model, "base_url": reasoning_agent.base_url})
        document.clamp_to_image()
        logger.info("ast.after_reasoning %s", format_kv(run_id=run_id, before_nodes=before_nodes, nodes=len(document.walk())))
        write_document_artifact(artifact_dir, "04_after_reasoning_ast.json", document)
        write_bbox_overlay_artifact(artifact_dir, "04_after_reasoning_boxes.png", image_path, document)
    else:
        logger.info("reasoning.skipped %s", format_kv(run_id=run_id, enabled=should_use_reasoning, mock_layout=mock_layout))
        write_json_artifact(artifact_dir, "04_reasoning_skipped.json", {"enabled": should_use_reasoning, "mock_layout": mock_layout})

    with log_stage(logger, "style_extract", run_id=run_id):
        StyleExtractor().apply(document, image_path)
    document.clamp_to_image()
    write_document_artifact(artifact_dir, "05_after_style_ast.json", document)
    write_bbox_overlay_artifact(artifact_dir, "05_after_style_boxes.png", image_path, document)
    with log_stage(logger, "image_extract", run_id=run_id, crop_dir=crop_dir):
        ImageExtractor().extract(document, image_path, crop_dir)
    document.clamp_to_image()
    write_document_artifact(artifact_dir, "06_after_image_extract_ast.json", document)
    write_bbox_overlay_artifact(artifact_dir, "06_after_image_extract_boxes.png", image_path, document)
    write_json_artifact(artifact_dir, "06_crops.json", {"crops": image_crop_paths(document)})
    with log_stage(logger, "ast_save", run_id=run_id, ast=ast_path):
        document.save(ast_path)
    write_document_artifact(artifact_dir, "07_final_ast.json", document)
    write_bbox_overlay_artifact(artifact_dir, "07_final_boxes.png", image_path, document)
    logger.info("ast.saved %s", format_kv(run_id=run_id, ast=ast_path, nodes=len(document.walk())))

    compiled_path: Path | None = None
    if not skip_compile:
        with log_stage(logger, "ppt_compile", run_id=run_id, ast=ast_path, pptx=pptx_path):
            compile_result = compile_pptx(ast_path, pptx_path)
        compiled_path = pptx_path
        write_json_artifact(artifact_dir, "08_compile.json", {"skipped": False, "pptx_path": str(compiled_path), **compile_result})
    else:
        logger.info("compile.skipped %s", format_kv(run_id=run_id))
        write_json_artifact(artifact_dir, "08_compile.json", {"skipped": True, "pptx_path": None})

    logger.info("conversion.end %s", format_kv(run_id=run_id, ast=ast_path, pptx=compiled_path, crop_dir=crop_dir, artifact_dir=artifact_dir))
    return ConversionResult(ast_path=ast_path, pptx_path=compiled_path, crop_dir=crop_dir, artifact_dir=artifact_dir)


def resolve_image_path(image_path: str | Path) -> Path:
    requested_path = Path(image_path)
    if requested_path.exists():
        return requested_path

    parent = requested_path.parent if str(requested_path.parent) else Path(".")
    candidates = [
        parent / f"{requested_path.stem}{extension}"
        for extension in SUPPORTED_IMAGE_EXTENSIONS
        if (parent / f"{requested_path.stem}{extension}").exists()
    ]
    candidates.extend(
        candidate
        for candidate in parent.glob(f"{requested_path.stem}.*")
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS and candidate not in candidates
    )

    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        candidate_text = ", ".join(str(candidate) for candidate in candidates)
        raise FileNotFoundError(f"Image not found: {requested_path}. Multiple same-stem candidates found: {candidate_text}")
    raise FileNotFoundError(f"Image not found: {requested_path}. Check the file name or extension.")


def collect_input_images(input_path: str | Path) -> list[Path]:
    requested_path = Path(input_path)
    if requested_path.is_dir():
        images = sorted(
            path
            for path in requested_path.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        )
        if not images:
            extensions = ", ".join(SUPPORTED_IMAGE_EXTENSIONS)
            raise FileNotFoundError(f"No supported image files found in directory: {requested_path}. Supported: {extensions}")
        return images
    return [resolve_image_path(requested_path)]


def batch_output_file(base_path: str | Path | None, output_stem: str, suffix: str, option_name: str) -> Path | None:
    if not base_path:
        return None

    base = Path(base_path)
    if base.suffix:
        raise ValueError(f"{option_name} must be a directory when input is a folder.")
    return base / f"{output_stem}{suffix}"


def batch_output_dir(base_path: str | Path | None, output_stem: str) -> Path | None:
    if not base_path:
        return None
    return Path(base_path) / output_stem


def should_preprocess_raw_image(value: bool | None, input_path: Path, image_path: Path) -> bool:
    if value is not None:
        return value
    return input_path.name == "raw-images" or image_path.parent.name == "raw-images"


def resolve_ocr_failure_mode(value: str | None) -> str:
    mode = (value or os.getenv("OCR_FAILURE_MODE") or "continue").strip().lower()
    if mode not in {"continue", "fail"}:
        raise ValueError("OCR failure mode must be 'continue' or 'fail'.")
    return mode


def write_document_artifact(artifact_dir: Path, name: str, document: SlideDocument) -> Path:
    return write_json_artifact(artifact_dir, name, document.model_dump(mode="json", exclude_none=True))


def write_bbox_overlay_artifact(artifact_dir: Path, name: str, image_path: str | Path, document: SlideDocument) -> Path:
    target = artifact_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(image_path) as source_image:
        image = source_image.convert("RGBA")

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_overlay_font(image.size)
    line_width = max(2, round(max(image.size) / 450))

    for node, depth in iter_nodes_with_depth(document):
        draw_element_bbox(draw, node, depth, image.size, font, line_width)

    composed = Image.alpha_composite(image, overlay).convert("RGB")
    composed.save(target)
    logger.debug("artifact.write %s", format_kv(path=target, boxes=len(document.walk())))
    return target


def iter_nodes_with_depth(document: SlideDocument) -> list[tuple[SlideElement, int]]:
    nodes: list[tuple[SlideElement, int]] = []

    def visit(items: list[SlideElement], depth: int) -> None:
        for item in items:
            nodes.append((item, depth))
            visit(item.children, depth + 1)

    visit(document.slide.children, 0)
    return nodes


def draw_element_bbox(
    draw: ImageDraw.ImageDraw,
    node: SlideElement,
    depth: int,
    image_size: tuple[int, int],
    font: ImageFont.ImageFont,
    line_width: int,
) -> None:
    image_width, image_height = image_size
    x, y, box_width, box_height = node.bbox
    left = int(round(max(0.0, min(float(image_width), x))))
    top = int(round(max(0.0, min(float(image_height), y))))
    right = int(round(max(0.0, min(float(image_width), x + box_width))))
    bottom = int(round(max(0.0, min(float(image_height), y + box_height))))
    if right <= left or bottom <= top:
        return

    color = color_for_node_type(node.type, depth)
    draw.rectangle((left, top, right, bottom), outline=color, width=line_width)

    label = f"{node.id} · {node.type} · [{x:.0f},{y:.0f},{box_width:.0f},{box_height:.0f}]"
    label_left, label_top, label_right, label_bottom = draw.textbbox((0, 0), label, font=font)
    label_width = label_right - label_left
    label_height = label_bottom - label_top
    padding = max(3, line_width + 1)
    label_x = left
    label_y = top - label_height - padding * 2
    if label_y < 0:
        label_y = top
    if label_x + label_width + padding * 2 > image_width:
        label_x = max(0, image_width - label_width - padding * 2)

    background = (0, 0, 0, 180)
    draw.rectangle(
        (label_x, label_y, label_x + label_width + padding * 2, label_y + label_height + padding * 2),
        fill=background,
    )
    draw.text((label_x + padding, label_y + padding), label, fill=(255, 255, 255, 255), font=font)


def color_for_node_type(node_type: str, depth: int) -> tuple[int, int, int, int]:
    colors = {
        "text": (255, 64, 129, 255),
        "rect": (0, 188, 212, 255),
        "rounded_rect": (124, 77, 255, 255),
        "image": (255, 193, 7, 255),
        "line": (76, 175, 80, 255),
    }
    base_color = colors.get(node_type, (255, 255, 255, 255))
    depth_boost = min(depth * 22, 60)
    return (
        min(base_color[0] + depth_boost, 255),
        min(base_color[1] + depth_boost, 255),
        min(base_color[2] + depth_boost, 255),
        base_color[3],
    )


def load_overlay_font(image_size: tuple[int, int]) -> ImageFont.ImageFont:
    font_size = max(12, min(32, round(max(image_size) / 86)))
    for font_name in ("Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(font_name, font_size)
        except OSError:
            continue
    return ImageFont.load_default()


def write_json_artifact(artifact_dir: Path, name: str, data: object) -> Path:
    target = artifact_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("artifact.write %s", format_kv(path=target))
    return target


def write_text_artifact(artifact_dir: Path, name: str, text: str) -> Path:
    target = artifact_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    logger.debug("artifact.write %s", format_kv(path=target, chars=len(text)))
    return target


def image_crop_paths(document: SlideDocument) -> list[dict[str, str]]:
    return [
        {"id": node.id, "image_path": node.image_path}
        for node in document.walk()
        if node.type == "image" and node.image_path
    ]


def exception_to_artifact(exc: Exception) -> dict[str, object]:
    data: dict[str, object] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    for attr in ("status_code", "code", "request_id", "body"):
        value = getattr(exc, attr, None)
        if value is not None:
            data[attr] = value
    return data


def compile_pptx(ast_path: str | Path, pptx_path: str | Path) -> dict[str, object]:
    ast_path = Path(ast_path)
    pptx_path = Path(pptx_path)
    local_tsx = PROJECT_ROOT / "node_modules" / ".bin" / "tsx"
    compiler = PROJECT_ROOT / "backend" / "compiler" / "ppt_compiler.ts"

    if local_tsx.exists():
        command = [str(local_tsx), str(compiler), "--input", str(ast_path), "--output", str(pptx_path)]
    else:
        command = ["npx", "tsx", str(compiler), "--input", str(ast_path), "--output", str(pptx_path)]

    logger.info("compiler.command %s", format_kv(command=" ".join(command)))
    result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "PPT compiler failed.\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n"
            "If dependencies are missing, run: npm install"
        )
    if result.stdout.strip():
        logger.info("compiler.stdout %s", format_kv(stdout=result.stdout.strip()))
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def image_size(image_path: str | Path) -> tuple[int, int]:
    with Image.open(image_path) as image:
        return image.size


def create_mock_document(image_width: int, image_height: int, slide_width: float, slide_height: float) -> SlideDocument:
    title_h = image_height * 0.09
    module_w = image_width * 0.26
    module_h = image_height * 0.16
    document = SlideDocument(
        slide={
            "width": slide_width,
            "height": slide_height,
            "image_width": image_width,
            "image_height": image_height,
            "children": [
                SlideElement(
                    id="txt_title",
                    type="text",
                    bbox=[image_width * 0.15, image_height * 0.05, image_width * 0.7, title_h],
                    text="Editable Slide Title",
                    style=Style(fontWeight="bold"),
                    metadata={"role": "title"},
                ),
                SlideElement(
                    id="node_center",
                    type="rounded_rect",
                    bbox=[(image_width - module_w) / 2, (image_height - module_h) / 2, module_w, module_h],
                    children=[
                        SlideElement(
                            id="txt_center",
                            type="text",
                            bbox=[image_width * 0.39, image_height * 0.45, image_width * 0.22, image_height * 0.06],
                            text="AI Engine",
                            parent_id="node_center",
                        )
                    ],
                ),
            ],
        }
    )
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert slide image(s) into editable PPTX.")
    parser.add_argument("input", help="Input slide image path or directory containing images")
    parser.add_argument("--ast", dest="ast_path", help="Output Slide AST JSON path, or directory for folder input")
    parser.add_argument("--out", dest="pptx_path", help="Output PPTX path, or directory for folder input")
    parser.add_argument("--crop-dir", help="Directory for extracted image/icon crops; folder input creates one subdirectory per image")
    parser.add_argument("--artifact-dir", help="Directory for intermediate artifacts; folder input creates one subdirectory per image")
    parser.add_argument("--layout-json", help="Use an existing layout JSON instead of calling the VLM")
    parser.add_argument("--slide-width", type=float, default=13.33, help="PPT slide width in inches")
    parser.add_argument("--slide-height", type=float, default=7.5, help="PPT slide height in inches")
    parser.add_argument("--skip-ocr", action="store_true", help="Skip OCR agent")
    parser.add_argument(
        "--ocr-failure-mode",
        choices=["continue", "fail"],
        default=None,
        help="What to do if OCR fails. Default: OCR_FAILURE_MODE or continue",
    )
    reasoning_group = parser.add_mutually_exclusive_group()
    reasoning_group.add_argument("--use-reasoning", dest="use_reasoning", action="store_true", default=None, help="Refine AST with REASONING_* model")
    reasoning_group.add_argument("--skip-reasoning", dest="use_reasoning", action="store_false", help="Disable reasoning AST refinement")
    parser.add_argument("--skip-compile", action="store_true", help="Only generate AST and crops")
    parser.add_argument("--mock-layout", action="store_true", help="Use a deterministic mock layout for smoke tests")
    raw_image_group = parser.add_mutually_exclusive_group()
    raw_image_group.add_argument(
        "--preprocess-raw-images",
        dest="preprocess_raw_images",
        action="store_true",
        default=None,
        help="Convert raw photos to PPT-style slide images with Nanobanana Pro before all pipeline nodes",
    )
    raw_image_group.add_argument(
        "--no-preprocess-raw-images",
        dest="preprocess_raw_images",
        action="store_false",
        help="Disable automatic raw image preprocessing",
    )
    parser.add_argument("--log-level", default=None, help="Logging level, e.g. DEBUG, INFO, WARNING")
    parser.add_argument("--log-file", default=None, help="Log file path. Default: outputs/logs/img2ppt.log")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_file = configure_logging(level=args.log_level, log_file=args.log_file)
    logger.info("cli.start %s", format_kv(log_file=log_file))
    input_path = Path(args.input)
    image_paths = collect_input_images(input_path)
    is_batch = input_path.is_dir()
    logger.info("cli.inputs %s", format_kv(input=args.input, images=len(image_paths), batch=is_batch))

    results: list[ConversionResult] = []
    for image_path in image_paths:
        conversion_id = make_timestamp_id() if is_batch else None
        output_stem = f"{image_path.stem}_{conversion_id}" if conversion_id else image_path.stem
        preprocess_raw_image = should_preprocess_raw_image(args.preprocess_raw_images, input_path, image_path)
        logger.info(
            "cli.convert_image %s",
            format_kv(image=image_path, conversion_id=conversion_id, preprocess_raw_image=preprocess_raw_image),
        )
        result = convert_image_to_ppt(
            image_path,
            conversion_id=conversion_id,
            ast_path=batch_output_file(args.ast_path, output_stem, "_ast.json", "--ast") if is_batch else args.ast_path,
            pptx_path=batch_output_file(args.pptx_path, output_stem, ".pptx", "--out") if is_batch else args.pptx_path,
            crop_dir=batch_output_dir(args.crop_dir, output_stem) if is_batch else args.crop_dir,
            artifact_dir=batch_output_dir(args.artifact_dir, output_stem) if is_batch else args.artifact_dir,
            layout_json=args.layout_json,
            slide_width=args.slide_width,
            slide_height=args.slide_height,
            skip_ocr=args.skip_ocr,
            ocr_failure_mode=args.ocr_failure_mode,
            use_reasoning=args.use_reasoning,
            skip_compile=args.skip_compile,
            mock_layout=args.mock_layout,
            preprocess_raw_image=preprocess_raw_image,
        )
        results.append(result)

    print(
        json.dumps(
            [
                {
                    "ast": str(result.ast_path),
                    "pptx": str(result.pptx_path) if result.pptx_path else None,
                    "crops": str(result.crop_dir),
                    "artifacts": str(result.artifact_dir),
                }
                for result in results
            ]
            if is_batch
            else {
                "ast": str(results[0].ast_path),
                "pptx": str(results[0].pptx_path) if results[0].pptx_path else None,
                "crops": str(results[0].crop_dir),
                "artifacts": str(results[0].artifact_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
