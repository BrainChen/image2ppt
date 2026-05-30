from __future__ import annotations

import json
import logging

from openai import OpenAI

from backend.ast.slide_ast import SlideDocument
from backend.utils.json_utils import extract_json_object
from backend.utils.logging_config import format_kv
from backend.utils.model_config import load_model_config


logger = logging.getLogger(__name__)

REASONING_SYSTEM_PROMPT = """You are a senior PowerPoint reconstruction planner.
You refine a Slide AST so it becomes easier to compile into an editable PPTX."""


REASONING_USER_PROMPT = """Refine this Slide AST JSON.

Output ONLY valid JSON with the same top-level shape:
{
  "slide": {
    "width": 13.33,
    "height": 7.5,
    "image_width": 1600,
    "image_height": 900,
    "children": []
  }
}

Allowed element types:
- text
- rect
- rounded_rect
- image
- line

What you may improve:
- normalize invalid or vague element types
- fix parent_id and children hierarchy
- group text under the obvious containing rect/rounded_rect
- remove obvious duplicate text nodes
- convert complex icons/logos/diagrams to image
- add metadata.role when useful
- keep maximum tree depth at 3

Hard rules:
- Do NOT invent new visible content.
- Do NOT materially change text content.
- Do NOT materially change bbox coordinates.
- Do NOT remove image_path values.
- Preserve slide size, image_width, and image_height.
- Preserve style fields when present.

Slide AST:
AST_JSON
"""


class ReasoningAgent:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 3600,
    ) -> None:
        config = load_model_config("reasoning", model=model, api_key=api_key, base_url=base_url)
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=timeout)
        self.last_raw_response: str | None = None
        logger.info("reasoning_agent.init %s", format_kv(model=self.model, base_url=self.base_url))

    def refine(self, document: SlideDocument) -> SlideDocument:
        ast_json = json.dumps(document.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2)
        logger.info("reasoning_agent.request %s", format_kv(model=self.model, nodes=len(document.walk()), chars=len(ast_json)))
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": REASONING_SYSTEM_PROMPT},
                {"role": "user", "content": REASONING_USER_PROMPT.replace("AST_JSON", ast_json)},
            ],
        )
        content = response.choices[0].message.content
        self.last_raw_response = content if isinstance(content, str) else str(content)
        logger.info("reasoning_agent.response %s", format_kv(chars=len(self.last_raw_response)))
        raw = extract_json_object(self.last_raw_response)
        refined = SlideDocument.from_raw(
            raw,
            image_width=document.slide.image_width,
            image_height=document.slide.image_height,
            slide_width=document.slide.width,
            slide_height=document.slide.height,
            max_depth=3,
        )
        refined.clamp_to_image()
        logger.info("reasoning_agent.refined %s", format_kv(nodes=len(refined.walk())))
        return refined
