from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image

from backend.ast.slide_ast import SlideDocument
from backend.utils.json_utils import extract_json_object
from backend.utils.logging_config import format_kv
from backend.utils.model_config import load_model_config


logger = logging.getLogger(__name__)

LAYOUT_SYSTEM_PROMPT = """You are a PowerPoint layout parser.
Your job is to convert one slide image into a JSON Slide AST for editable PPT reconstruction."""


LAYOUT_USER_PROMPT = """Analyze this slide image.

Output ONLY valid JSON. Do not include Markdown fences or explanations.

Return this exact top-level shape:
{
  "slide": {
    "width": 13.33,
    "height": 7.5,
    "image_width": IMAGE_WIDTH,
    "image_height": IMAGE_HEIGHT,
    "children": []
  }
}

Detect only meaningful PowerPoint objects:
- title
- text
- container
- image
- icon
- line

Normalize object types to ONLY:
- text
- rect
- rounded_rect
- image
- line

For each object return:
- id: stable short string
- type: one of text, rect, rounded_rect, image, line
- bbox: [x, y, w, h] in absolute image pixels, origin at top-left
- parent_id: parent object id or null
- text: visible text if type is text; empty string if unsure
- children: nested objects if this object contains them
- metadata.role: optional source role such as title, icon, logo, container

Build a hierarchy tree with maximum depth 3.

Rules:
- bbox MUST be [x, y, width, height], not [x1, y1, x2, y2].
- width = right - left, height = bottom - top.
- Coordinates MUST use the original image pixel space IMAGE_WIDTH x IMAGE_HEIGHT, not a 0-1000 or normalized coordinate space.
- Prefer editable text and editable boxes over screenshot crops.
- Use type "image" for logos, icons, photos, complex diagrams, and shapes that are too hard to reconstruct.
- Do not invent invisible elements.
- Keep coordinates tight but include the full visible object.
- Do not output connectors/arrows unless they are visually important.
"""


class LayoutAgent:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 90,
    ) -> None:
        config = load_model_config("vlm", model=model, api_key=api_key, base_url=base_url)
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=timeout)
        self.last_raw_response: str | None = None
        logger.info("layout_agent.init %s", format_kv(model=self.model, base_url=self.base_url))

    def parse(
        self,
        image_path: str | Path,
        *,
        slide_width: float = 13.33,
        slide_height: float = 7.5,
    ) -> SlideDocument:
        image_path = Path(image_path)
        with Image.open(image_path) as image:
            image_width, image_height = image.size

        prompt = LAYOUT_USER_PROMPT.replace("IMAGE_WIDTH", str(image_width)).replace("IMAGE_HEIGHT", str(image_height))
        logger.info("layout_agent.request %s", format_kv(image=image_path, width=image_width, height=image_height, model=self.model))
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": LAYOUT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    ],
                },
            ],
            temperature=0.0,
        )
        content = _message_content_to_text(response.choices[0].message.content)
        self.last_raw_response = content
        logger.info("layout_agent.response %s", format_kv(chars=len(content)))
        raw = extract_json_object(content)
        document = SlideDocument.from_raw(
            raw,
            image_width=image_width,
            image_height=image_height,
            slide_width=slide_width,
            slide_height=slide_height,
            max_depth=3,
        )
        document.clamp_to_image()
        logger.info("layout_agent.parsed %s", format_kv(nodes=len(document.walk())))
        return document


def image_to_data_url(image_path: str | Path) -> str:
    image_path = Path(image_path)
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif hasattr(item, "text"):
                parts.append(str(item.text))
        return "\n".join(parts)
    return str(content)
