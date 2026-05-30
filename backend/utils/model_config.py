from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

from backend.utils.logging_config import format_kv


ModelKind = Literal["vlm", "reasoning"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelConfig:
    kind: ModelKind
    model: str
    api_key: str
    base_url: str | None = None


def load_model_config(kind: ModelKind, *, model: str | None = None, api_key: str | None = None, base_url: str | None = None) -> ModelConfig:
    load_dotenv()
    if kind == "vlm":
        return _load_vlm_config(model=model, api_key=api_key, base_url=base_url)
    return _load_reasoning_config(model=model, api_key=api_key, base_url=base_url)


def is_reasoning_configured() -> bool:
    load_dotenv()
    return bool(clean_env_value(os.getenv("REASONING_MODEL_NAME")))


def _load_vlm_config(*, model: str | None, api_key: str | None, base_url: str | None) -> ModelConfig:
    selected_model = clean_env_value(model) or clean_env_value(os.getenv("VLM_MODEL_NAME"))
    selected_api_key = clean_env_value(api_key) or clean_env_value(os.getenv("VLM_MODEL_API_KEY"))
    selected_base_url = clean_env_value(base_url) or clean_env_value(os.getenv("VLM_MODEL_BASE_URL"))
    if not selected_model:
        raise RuntimeError("VLM_MODEL_NAME is missing. Add it to .env or pass model.")
    if not selected_api_key:
        raise RuntimeError("VLM_MODEL_API_KEY is missing. Add it to .env or pass api_key.")
    logger.info("model.config %s", format_kv(kind="vlm", model=selected_model, base_url=selected_base_url))
    return ModelConfig(kind="vlm", model=selected_model, api_key=selected_api_key, base_url=selected_base_url)


def _load_reasoning_config(*, model: str | None, api_key: str | None, base_url: str | None) -> ModelConfig:
    selected_model = clean_env_value(model) or clean_env_value(os.getenv("REASONING_MODEL_NAME"))
    selected_api_key = clean_env_value(api_key) or clean_env_value(os.getenv("REASONING_MODEL_API_KEY"))
    selected_base_url = clean_env_value(base_url) or clean_env_value(os.getenv("REASONING_MODEL_BASE_URL"))
    if not selected_model:
        raise RuntimeError("REASONING_MODEL_NAME is missing. Set it in .env or disable reasoning refinement.")
    if not selected_api_key:
        raise RuntimeError("REASONING_MODEL_API_KEY is missing. Add it to .env or pass api_key.")
    logger.info(
        "model.config %s",
        format_kv(kind="reasoning", model=selected_model, base_url=selected_base_url),
    )
    return ModelConfig(kind="reasoning", model=selected_model, api_key=selected_api_key, base_url=selected_base_url)


def clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
