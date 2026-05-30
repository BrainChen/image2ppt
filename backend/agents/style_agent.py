from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from backend.ast.slide_ast import SlideDocument, SlideElement
from backend.utils.logging_config import format_kv


logger = logging.getLogger(__name__)


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
                node.style.fontSize = node.style.fontSize or estimate_font_size(node, document)
                if not node.style.fontWeight and (node.metadata.get("role") == "title" or node.bbox[3] > image.shape[0] * 0.045):
                    node.style.fontWeight = "bold"
                node.style.align = node.style.align or "center"
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


def estimate_font_size(node: SlideElement, document: SlideDocument) -> float:
    image_height = document.slide.image_height or 900
    line_count = max(1, len((node.text or "").splitlines()))
    height_inches = node.bbox[3] / image_height * document.slide.height
    return round(max(6.0, min(48.0, height_inches * 72 * 0.58 / line_count)), 1)


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
