from __future__ import annotations

import base64
import io
import logging
import mimetypes
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

from backend.utils.logging_config import format_kv
from backend.utils.model_config import clean_env_value


logger = logging.getLogger(__name__)

DEFAULT_NANOBANANA_PRO_MODEL = "google/gemini-3-pro-image-preview"
DEFAULT_NANOBANANA_PRO_BASE_URL = "https://openrouter.ai/api/v1"

PPT_STYLE_IMAGE_PROMPT = """Convert the provided photo into a clean, presentation-ready 16:9 PowerPoint slide image.

Keep the original photo's subject, important objects, and factual visual information, but redesign it as a polished PPT-style slide:
- clean composition, crisp edges, balanced layout, professional visual hierarchy
- simplified background suitable for a presentation deck
- modern corporate keynote style, high readability, subtle depth, tasteful lighting
- no extra captions, no watermarks, no logos, no random text
- preserve recognizable content and proportions where possible

Output exactly one finished slide-style image."""


class RawImagePreprocessor:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120,
    ) -> None:
        load_dotenv()
        self.model = (
            clean_env_value(model)
            or clean_env_value(os.getenv("NANOBANANA_PRO_MODEL_NAME"))
            or DEFAULT_NANOBANANA_PRO_MODEL
        )
        self.api_key = (
            clean_env_value(api_key)
            or clean_env_value(os.getenv("NANOBANANA_PRO_API_KEY"))
            or clean_env_value(os.getenv("OPENROUTER_API_KEY"))
        )
        self.base_url = (
            clean_env_value(base_url)
            or clean_env_value(os.getenv("NANOBANANA_PRO_BASE_URL"))
            or DEFAULT_NANOBANANA_PRO_BASE_URL
        )
        if not self.api_key:
            raise RuntimeError("NANOBANANA_PRO_API_KEY or OPENROUTER_API_KEY is missing. Add it to .env or disable raw image preprocessing.")

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout)
        self.last_response_info: dict[str, Any] | None = None
        logger.info("raw_image_preprocessor.init %s", format_kv(model=self.model, base_url=self.base_url))

    def convert_to_ppt_style_image(
        self,
        image_path: str | Path,
        output_path: str | Path,
        *,
        aspect_ratio: str = "16:9",
        prompt: str = PPT_STYLE_IMAGE_PROMPT,
        max_retries: int = 2,
    ) -> Path:
        image_path = Path(image_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "raw_image_preprocess.request %s",
                    format_kv(image=image_path, output=output_path, model=self.model, attempt=attempt),
                )
                completion = self.client.chat.completions.create(
                    extra_headers=self._extra_headers(),
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Please generate an image: {prompt}. Do not output text.",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_to_data_url(image_path)},
                                },
                            ],
                        }
                    ],
                    max_tokens=4096,
                    temperature=0.7,
                    extra_body={
                        "response_format": {"type": "text"},
                        "include_image_data": True,
                        "image_config": {"aspect_ratio": aspect_ratio},
                    },
                )
                image_bytes, response_info = extract_image_bytes(completion)
                save_image_bytes(image_bytes, output_path)
                self.last_response_info = {
                    **response_info,
                    "model": self.model,
                    "base_url": self.base_url,
                    "output_path": str(output_path),
                    "bytes": len(image_bytes),
                }
                logger.info("raw_image_preprocess.done %s", format_kv(output=output_path, bytes=len(image_bytes)))
                return output_path
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "raw_image_preprocess.failed_attempt %s",
                    format_kv(image=image_path, attempt=attempt, max_retries=max_retries, error=str(exc)),
                )
                if attempt < max_retries:
                    time.sleep(1)

        raise RuntimeError(f"Raw image preprocessing failed after {max_retries} attempts: {last_error}") from last_error

    def _extra_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        referer = clean_env_value(os.getenv("NANOBANANA_PRO_HTTP_REFERER"))
        title = clean_env_value(os.getenv("NANOBANANA_PRO_APP_TITLE"))
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        return headers


def extract_image_bytes(completion: Any) -> tuple[bytes, dict[str, Any]]:
    image_url = extract_image_url(completion)
    if image_url:
        if image_url.startswith("data:image/") and ";base64," in image_url:
            base64_data = image_url.split(";base64,", 1)[1]
            return base64.b64decode(base64_data), {"source": "message.images.data_url"}
        if image_url.startswith("http://") or image_url.startswith("https://"):
            with urllib.request.urlopen(image_url, timeout=60) as response:
                return response.read(), {"source": "message.images.url", "original_url": image_url}

    text = completion_to_text(completion)
    data_url_match = re.search(r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\n\r]+)", text)
    if data_url_match:
        return base64.b64decode(data_url_match.group(1)), {"source": "content.data_url"}

    base64_matches = re.findall(r"[A-Za-z0-9+/=]{1000,}", text)
    if base64_matches:
        base64_data = max(base64_matches, key=len)
        return base64.b64decode(base64_data), {"source": "content.base64"}

    raise ValueError("Could not extract image data from Nanobanana Pro response.")


def extract_image_url(completion: Any) -> str | None:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return None

    message = getattr(choices[0], "message", None)
    model_extra = getattr(message, "model_extra", None) if message else None
    if isinstance(model_extra, dict):
        images = model_extra.get("images")
        if isinstance(images, list) and images:
            image_data = images[0]
            if isinstance(image_data, dict):
                image_url = image_data.get("image_url")
                if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                    return image_url["url"]

    content = getattr(message, "content", None) if message else None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                image_url = item.get("image_url")
                if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                    return image_url["url"]
    return None


def completion_to_text(completion: Any) -> str:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content or "")


def save_image_bytes(image_bytes: bytes, output_path: Path) -> None:
    if not image_bytes:
        raise ValueError("Generated image data is empty.")
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.convert("RGB").save(output_path, format="JPEG", quality=95, optimize=True)


def image_to_data_url(image_path: str | Path) -> str:
    image_path = Path(image_path)
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"
