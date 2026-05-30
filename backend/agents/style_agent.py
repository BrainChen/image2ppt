from __future__ import annotations

import logging
import re
from pathlib import Path

import cv2
import numpy as np

from backend.ast.slide_ast import SlideDocument, SlideElement
from backend.utils.logging_config import format_kv


logger = logging.getLogger(__name__)
LIST_MARKER_PATTERN = re.compile(r"^\s*[•●◦○▪■□‣⁃\-–—]\s+", re.MULTILINE)


class StyleExtractor:
    def apply(self, document: SlideDocument, image_path: str | Path) -> SlideDocument:
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(f"Cannot read image: {image_path}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        logger.info("style_extractor.start %s", format_kv(image=image_path, nodes=len(document.walk())))

        document.slide.background.fill = dominant_hex(edge_pixels(image))
        document.clamp_to_image()
        for node in document.slide.children:
            self._apply_node(node, image, document)
        logger.info("style_extractor.done %s", format_kv(background=document.slide.background.fill, nodes=len(document.walk())))
        return document

    def _apply_node(self, node: SlideElement, image: np.ndarray, document: SlideDocument) -> None:
        region = crop_region(image, node.bbox)
        if region.size:
            if node.type in {"rect", "rounded_rect"}:
                center = crop_region(image, inner_bbox(node.bbox, 0.22))
                fill = dominant_hex(center if center.size else region)
                stroke = dominant_hex(edge_pixels(region))
                node.style.fill = node.style.fill or fill
                if color_distance(fill, stroke) > 18:
                    node.style.stroke = node.style.stroke or stroke
                    node.style.strokeWidth = node.style.strokeWidth or 1.0
                else:
                    node.style.stroke = node.style.stroke or None
                    node.style.strokeWidth = node.style.strokeWidth or 0.0
                if node.type == "rounded_rect":
                    node.style.radius = node.style.radius or round(min(node.bbox[2], node.bbox[3]) * 0.12, 2)
            elif node.type == "text":
                bg = dominant_hex(region)
                node.style.color = node.style.color or text_color_hex(region, bg)
                node.style.fontSize = estimate_font_size(node, document, region)
                role = str(node.metadata.get("role") or "").lower()
                is_list = is_unordered_list_text(node.text or "") or role == "bullet"
                is_large_single_line = "\n" not in (node.text or "") and node.bbox[3] > image.shape[0] * 0.045
                if not node.style.fontWeight and (role in {"title", "heading"} or (is_large_single_line and not is_list)):
                    node.style.fontWeight = "bold"
                node.style.align = infer_text_align(node, region)
            elif node.type == "line":
                background = document.slide.background.fill or "#ffffff"
                node.style.stroke = node.style.stroke or dominant_hex(region, ignore_hex=background)
                node.style.strokeWidth = node.style.strokeWidth or estimate_line_width(node)

        for child in node.children:
            self._apply_node(child, image, document)


def crop_region(image: np.ndarray, bbox: list[float]) -> np.ndarray:
    height, width = image.shape[:2]
    x, y, w, h = bbox
    x1 = max(0, min(width, int(round(x))))
    y1 = max(0, min(height, int(round(y))))
    x2 = max(x1, min(width, int(round(x + w))))
    y2 = max(y1, min(height, int(round(y + h))))
    return image[y1:y2, x1:x2]


def inner_bbox(bbox: list[float], margin_ratio: float) -> list[float]:
    x, y, w, h = bbox
    mx = w * margin_ratio
    my = h * margin_ratio
    return [x + mx, y + my, max(1, w - 2 * mx), max(1, h - 2 * my)]


def edge_pixels(region: np.ndarray) -> np.ndarray:
    if region.size == 0:
        return region
    height, width = region.shape[:2]
    band = max(1, min(width, height, 12) // 6)
    top = region[:band, :, :]
    bottom = region[-band:, :, :]
    left = region[:, :band, :]
    right = region[:, -band:, :]
    return np.concatenate([top.reshape(-1, 3), bottom.reshape(-1, 3), left.reshape(-1, 3), right.reshape(-1, 3)], axis=0)


def dominant_hex(region: np.ndarray, *, ignore_hex: str | None = None) -> str:
    pixels = region.reshape(-1, 3) if region.ndim == 3 else region.reshape(-1, 3)
    if len(pixels) == 0:
        return "#ffffff"
    if len(pixels) > 60000:
        stride = max(1, len(pixels) // 60000)
        pixels = pixels[::stride]

    if ignore_hex:
        ignore = np.array(hex_to_rgb(ignore_hex), dtype=np.float32)
        distances = np.linalg.norm(pixels.astype(np.float32) - ignore, axis=1)
        filtered = pixels[distances > 35]
        if len(filtered) > 30:
            pixels = filtered

    quantized = (pixels.astype(np.uint8) // 16) * 16
    colors, counts = np.unique(quantized, axis=0, return_counts=True)
    color = colors[int(np.argmax(counts))] + 8
    return rgb_to_hex(color)


def text_color_hex(region: np.ndarray, background_hex: str) -> str:
    if region.size == 0:
        return "#111111"
    pixels = region.reshape(-1, 3)
    background = np.array(hex_to_rgb(background_hex), dtype=np.float32)
    distances = np.linalg.norm(pixels.astype(np.float32) - background, axis=1)
    foreground = pixels[distances > 45]
    if len(foreground) > 20:
        return dominant_hex(foreground.reshape(-1, 1, 3))
    return "#111111" if luminance(background) > 150 else "#ffffff"


def estimate_font_size(node: SlideElement, document: SlideDocument, region: np.ndarray | None = None) -> float:
    if region is not None and region.size:
        visual_size = estimate_font_size_from_ink(region, document)
        if visual_size is not None:
            return visual_size

    image_height = document.slide.image_height or 900
    line_count = max(1, len((node.text or "").splitlines()))
    height_inches = node.bbox[3] / image_height * document.slide.height
    line_height = height_inches * 72 / line_count
    ratio = 0.82 if line_count == 1 else 0.74
    return round(max(5.0, min(60.0, line_height * ratio)), 1)


def estimate_font_size_from_ink(region: np.ndarray, document: SlideDocument) -> float | None:
    mask = text_foreground_mask(region)
    if mask is None:
        return None

    height, width = mask.shape
    min_pixels_per_row = max(2, int(width * 0.008))
    rows = np.where(mask.sum(axis=1) >= min_pixels_per_row)[0]
    if len(rows) == 0:
        return None

    bands: list[tuple[int, int]] = []
    start = previous = int(rows[0])
    for row in rows[1:]:
        current = int(row)
        if current > previous + 2:
            bands.append((start, previous))
            start = current
        previous = current
    bands.append((start, previous))

    line_heights = [
        end - start + 1
        for start, end in bands
        if 3 <= end - start + 1 <= max(4, int(height * 0.96))
    ]
    if not line_heights:
        return None

    px_per_point = (document.slide.image_height or 900) / max(1.0, document.slide.height * 72)
    font_size = float(np.median(line_heights)) / px_per_point
    if not np.isfinite(font_size):
        return None
    return round(max(5.0, min(72.0, font_size)), 1)


def infer_text_align(node: SlideElement, region: np.ndarray) -> str:
    text = node.text or ""
    role = str(node.metadata.get("role") or "").lower()
    if is_unordered_list_text(text) or role == "bullet":
        return "left"
    if role in {"title", "subtitle", "caption", "footer", "page_number", "heading", "label"}:
        return "center"

    mask = text_foreground_mask(region)
    if mask is None:
        return "center"
    height, width = mask.shape
    min_pixels_per_col = max(2, int(height * 0.015))
    cols = np.where(mask.sum(axis=0) >= min_pixels_per_col)[0]
    if len(cols) == 0 or width <= 0:
        return "center"

    left_margin = int(cols[0]) / width
    right_margin = (width - 1 - int(cols[-1])) / width
    if left_margin < 0.08 and right_margin > 0.18:
        return "left"
    if right_margin < 0.08 and left_margin > 0.18:
        return "right"
    return "center"


def is_unordered_list_text(text: str) -> bool:
    return bool(LIST_MARKER_PATTERN.search(text))


def text_foreground_mask(region: np.ndarray) -> np.ndarray | None:
    if region.size == 0:
        return None
    background = np.array(hex_to_rgb(dominant_hex(region)), dtype=np.float32)
    pixels = region.astype(np.float32)
    distances = np.linalg.norm(pixels - background, axis=2)
    if not np.isfinite(distances).any():
        return None
    adaptive_threshold = float(np.percentile(distances, 88)) * 0.45
    threshold = max(30.0, min(70.0, adaptive_threshold))
    mask = distances > threshold
    if int(mask.sum()) < 8:
        return None
    return mask


def estimate_line_width(node: SlideElement) -> float:
    _, _, width, height = node.bbox
    thin_side = min(value for value in (width, height) if value > 0) if width > 0 and height > 0 else max(width, height)
    return round(max(0.75, min(4.0, thin_side / 3)), 2)


def color_distance(left_hex: str, right_hex: str) -> float:
    left = np.array(hex_to_rgb(left_hex), dtype=np.float32)
    right = np.array(hex_to_rgb(right_hex), dtype=np.float32)
    return float(np.linalg.norm(left - right))


def rgb_to_hex(rgb: np.ndarray) -> str:
    clipped = np.clip(rgb.astype(int), 0, 255)
    return "#{:02x}{:02x}{:02x}".format(int(clipped[0]), int(clipped[1]), int(clipped[2]))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    cleaned = value.strip().lstrip("#")
    if len(cleaned) != 6:
        return (255, 255, 255)
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def luminance(rgb: np.ndarray) -> float:
    return float(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2])
