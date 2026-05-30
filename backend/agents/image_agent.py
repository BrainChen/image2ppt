from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image

from backend.ast.slide_ast import SlideDocument, SlideElement
from backend.utils.logging_config import format_kv


logger = logging.getLogger(__name__)


class ImageExtractor:
    def extract(self, document: SlideDocument, image_path: str | Path, crop_dir: str | Path) -> SlideDocument:
        image_path = Path(image_path)
        crop_dir = Path(crop_dir)
        crop_dir.mkdir(parents=True, exist_ok=True)

        with Image.open(image_path) as image:
            crop_count = 0
            for node in document.walk():
                if node.type == "image":
                    crop_path = self._crop_node(image, node, crop_dir, image_path.stem)
                    if crop_path:
                        node.image_path = path_for_ast(crop_path)
                        crop_count += 1
        logger.info("image_extractor.done %s", format_kv(image=image_path, crop_dir=crop_dir, crops=crop_count))
        return document

    def _crop_node(self, image: Image.Image, node: SlideElement, crop_dir: Path, stem: str) -> Path | None:
        x, y, width, height = node.bbox
        if width < 2 or height < 2:
            return None
        left = max(0, int(round(x)))
        top = max(0, int(round(y)))
        right = min(image.width, int(round(x + width)))
        bottom = min(image.height, int(round(y + height)))
        if right <= left or bottom <= top:
            return None

        safe_id = re.sub(r"[^A-Za-z0-9_\\-]+", "_", node.id)
        crop_path = crop_dir / f"{stem}_{safe_id}.png"
        image.crop((left, top, right, bottom)).save(crop_path)
        return crop_path


def path_for_ast(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path.resolve())
