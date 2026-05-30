from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_FILE = PROJECT_ROOT / "outputs" / "logs" / "img2ppt.log"
_CONFIGURED = False
_ACTIVE_LOG_FILE: Path | None = None


def configure_logging(level: str | None = None, log_file: str | Path | None = None) -> Path:
    global _CONFIGURED, _ACTIVE_LOG_FILE
    selected_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    selected_file = Path(log_file or os.getenv("LOG_FILE") or DEFAULT_LOG_FILE)
    if not selected_file.is_absolute():
        selected_file = PROJECT_ROOT / selected_file
    selected_file.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, selected_level, logging.INFO))

    if _CONFIGURED:
        active_file = _ACTIVE_LOG_FILE or selected_file
        logging.getLogger(__name__).debug("logging.already_configured level=%s file=%s", selected_level, active_file)
        return active_file

    if not _CONFIGURED:
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        file_handler = RotatingFileHandler(
            selected_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        _CONFIGURED = True
        _ACTIVE_LOG_FILE = selected_file

    logging.getLogger(__name__).info("logging.configured level=%s file=%s", selected_level, selected_file)
    return selected_file


def format_kv(**fields: object) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={safe_log_value(value)}")
    return " ".join(parts)


def safe_log_value(value: object) -> str:
    text = str(value).replace("\n", "\\n")
    if len(text) > 240:
        return f"{text[:237]}..."
    return text


@contextmanager
def log_stage(logger: logging.Logger, stage: str, **fields: object) -> Iterator[None]:
    started_at = time.perf_counter()
    logger.info("stage.start name=%s %s", stage, format_kv(**fields))
    try:
        yield
    except Exception:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception("stage.error name=%s elapsed_ms=%s %s", stage, elapsed_ms, format_kv(**fields))
        raise
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info("stage.end name=%s elapsed_ms=%s %s", stage, elapsed_ms, format_kv(**fields))
