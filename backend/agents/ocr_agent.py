from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image

from backend.agents.layout_agent import image_to_data_url
from backend.ast.slide_ast import (
    SlideDocument,
    SlideElement,
    Style,
    bbox_center_inside,
    bbox_iou,
    distance_between_bboxes,
    normalize_bbox_items_to_image,
)
from backend.utils.json_utils import extract_json_object
from backend.utils.logging_config import format_kv
from backend.utils.model_config import load_model_config


logger = logging.getLogger(__name__)

OCR_SYSTEM_PROMPT = """You are an OCR engine for PowerPoint slide reconstruction.
Only extract visible text and its bounding box."""


OCR_USER_PROMPT = """Extract all readable text from this slide image.

Output ONLY valid JSON:
{
  "items": [
    {
      "id": "ocr_1",
      "text": "visible text",
      "bbox": [x, y, w, h]
    }
  ]
}

Rules:
- bbox must be absolute original image pixels, origin top-left.
- Do NOT output coordinates from a 0-1000, normalized, resized, or display coordinate space.
- If you estimate positions on any temporary grid, convert every bbox back to original image pixels before output.
- Preserve line breaks inside a text block when visually grouped.
- Encode line breaks inside JSON strings as \\n; never put literal newlines inside a JSON string.
- Do not describe graphics.
- Do not extract decorative illegible text.
"""


class OcrAgent:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 3600,
    ) -> None:
        config = load_model_config("vlm", model=model, api_key=api_key, base_url=base_url)
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=timeout)
        self.last_raw_response: str | None = None
        logger.info("ocr_agent.init %s", format_kv(model=self.model, base_url=self.base_url))

    def extract(self, image_path: str | Path) -> list[dict[str, Any]]:
        image_path = Path(image_path)
        with Image.open(image_path) as image:
            image_width, image_height = image.size

        logger.info("ocr_agent.request %s", format_kv(image=image_path, width=image_width, height=image_height, model=self.model))
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": OCR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{OCR_USER_PROMPT}\nImage size: {image_width}x{image_height}px."},
                        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    ],
                },
            ],
            temperature=0.0,
        )
        content = response.choices[0].message.content
        self.last_raw_response = content if isinstance(content, str) else str(content)
        logger.info("ocr_agent.response %s", format_kv(chars=len(self.last_raw_response)))
        raw = extract_json_object(self.last_raw_response)
        items = raw.get("items", [])
        normalized_items = items if isinstance(items, list) else []
        normalize_bbox_items_to_image(normalized_items, image_width, image_height)
        logger.info("ocr_agent.extracted %s", format_kv(items=len(normalized_items)))
        return normalized_items


def merge_ocr_items(document: SlideDocument, items: list[dict[str, Any]]) -> None:
    existing_text_nodes = [node for node in document.walk() if node.type == "text"]
    used_ids = {node.id for node in document.walk()}
    updated_count = 0
    inserted_count = 0

    for index, item in enumerate(items, start=1):
        text = str(item.get("text") or "").strip()
        bbox = item.get("bbox")
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            continue
        normalized_bbox = [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
        target = _best_text_match(existing_text_nodes, normalized_bbox)
        if target is not None:
            if not target.text or len(text) >= len(target.text.strip()):
                target.text = text
                updated_count += 1
            continue

        element_id = _unique_id(str(item.get("id") or f"ocr_{index}"), used_ids)
        text_node = SlideElement(id=element_id, type="text", bbox=normalized_bbox, text=text, style=Style())
        parent = _smallest_containing_container(document, normalized_bbox)
        if parent is not None:
            text_node.parent_id = parent.id
            parent.children.append(text_node)
        else:
            document.slide.children.append(text_node)
        existing_text_nodes.append(text_node)
        inserted_count += 1
    logger.info("ocr_agent.merged %s", format_kv(items=len(items), updated=updated_count, inserted=inserted_count, nodes=len(document.walk())))


def _best_text_match(nodes: list[SlideElement], bbox: list[float]) -> SlideElement | None:
    scored: list[tuple[float, float, SlideElement]] = []
    for node in nodes:
        overlap = bbox_iou(node.bbox, bbox)
        center_bonus = 0.25 if bbox_center_inside(bbox, node.bbox) or bbox_center_inside(node.bbox, bbox) else 0.0
        distance = distance_between_bboxes(node.bbox, bbox)
        score = overlap + center_bonus
        if score > 0.1:
            scored.append((score, -distance, node))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _smallest_containing_container(document: SlideDocument, bbox: list[float]) -> SlideElement | None:
    containers = [
        node
        for node in document.walk()
        if node.type in {"rect", "rounded_rect"} and bbox_center_inside(bbox, node.bbox)
    ]
    if not containers:
        return None
    containers.sort(key=lambda node: node.bbox[2] * node.bbox[3])
    return containers[0]


def _unique_id(candidate: str, used_ids: set[str]) -> str:
    base = "".join(char if char.isalnum() or char in "_-" else "_" for char in candidate.strip()) or "ocr"
    value = base
    suffix = 1
    while value in used_ids:
        suffix += 1
        value = f"{base}_{suffix}"
    used_ids.add(value)
    return value
