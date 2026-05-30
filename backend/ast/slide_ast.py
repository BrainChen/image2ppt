from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


ElementType = Literal["text", "rect", "rounded_rect", "image", "line"]


TYPE_ALIASES: dict[str, ElementType] = {
    "title": "text",
    "subtitle": "text",
    "paragraph": "text",
    "text_block": "text",
    "label": "text",
    "container": "rounded_rect",
    "box": "rect",
    "card": "rounded_rect",
    "module": "rounded_rect",
    "rounded_rectangle": "rounded_rect",
    "round_rect": "rounded_rect",
    "rectangle": "rect",
    "shape": "rect",
    "icon": "image",
    "logo": "image",
    "diagram": "image",
    "photo": "image",
    "picture": "image",
    "arrow": "line",
    "connector": "line",
}

COMMON_COORDINATE_EXTENTS = (100, 640, 700, 720, 768, 900, 960, 1000, 1024, 1080, 1200, 1280, 1440, 1600, 1920)


class Style(BaseModel):
    fill: str | None = None
    stroke: str | None = None
    strokeWidth: float | None = None
    radius: float | None = None
    fontSize: float | None = None
    fontWeight: str | None = None
    fontFamily: str | None = None
    color: str | None = None
    align: Literal["left", "center", "right"] | None = None
    opacity: float | None = None

    @field_validator("fill", "stroke", "color", mode="before")
    @classmethod
    def normalize_color(cls, value: Any) -> str | None:
        if value in (None, "", "none", "transparent"):
            return None
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return "#{:02x}{:02x}{:02x}".format(
                int(value[0]) % 256,
                int(value[1]) % 256,
                int(value[2]) % 256,
            )
        if not isinstance(value, str):
            return None
        value = value.strip()
        if re.fullmatch(r"[0-9a-fA-F]{6}", value):
            return f"#{value.lower()}"
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            return value.lower()
        return value


class SlideElement(BaseModel):
    id: str
    type: ElementType
    bbox: list[float] = Field(min_length=4, max_length=4)
    parent_id: str | None = None
    text: str | None = None
    image_path: str | None = None
    style: Style = Field(default_factory=Style)
    children: list["SlideElement"] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("bbox", mode="before")
    @classmethod
    def normalize_bbox(cls, value: Any) -> list[float]:
        if isinstance(value, dict):
            value = [value.get("x", 0), value.get("y", 0), value.get("w", value.get("width", 0)), value.get("h", value.get("height", 0))]
        if not isinstance(value, (list, tuple)) or len(value) < 4:
            raise ValueError("bbox must be [x, y, w, h]")
        return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]

    @model_validator(mode="after")
    def sanitize(self) -> "SlideElement":
        x, y, width, height = self.bbox
        self.bbox = [x, y, max(0.0, width), max(0.0, height)]
        if self.type == "text" and self.text is None:
            self.text = ""
        return self


class SlideSpec(BaseModel):
    width: float = 13.33
    height: float = 7.5
    image_width: int | None = None
    image_height: int | None = None
    background: Style = Field(default_factory=Style)
    children: list[SlideElement] = Field(default_factory=list)


class SlideDocument(BaseModel):
    slide: SlideSpec
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        image_width: int | None = None,
        image_height: int | None = None,
        slide_width: float = 13.33,
        slide_height: float = 7.5,
        max_depth: int = 3,
    ) -> "SlideDocument":
        slide_raw = raw.get("slide") if isinstance(raw.get("slide"), dict) else raw
        raw_children = slide_raw.get("children", [])
        if not isinstance(raw_children, list):
            raw_children = []

        context = _NormalizeContext()
        children = [
            _normalize_element(child, None, 1, max_depth, context)
            for child in raw_children
            if isinstance(child, dict)
        ]
        children = [child for child in children if child is not None]
        children = _rebuild_parent_tree(children)

        slide = SlideSpec(
            width=float(slide_raw.get("width", slide_width) or slide_width),
            height=float(slide_raw.get("height", slide_height) or slide_height),
            image_width=int(slide_raw.get("image_width") or image_width or 0) or None,
            image_height=int(slide_raw.get("image_height") or image_height or 0) or None,
            background=Style.model_validate(slide_raw.get("background", {})),
            children=children,
        )
        metadata = raw.get("metadata", {}) if isinstance(raw.get("metadata"), dict) else {}
        document = cls(slide=slide, metadata=metadata)
        normalization = normalize_bboxes_to_image(document)
        if normalization:
            document.metadata = {
                **document.metadata,
                "bbox_normalization": normalization.to_metadata(),
            }
        return document

    @classmethod
    def load(cls, path: str | Path) -> "SlideDocument":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def walk(self) -> list[SlideElement]:
        nodes: list[SlideElement] = []

        def visit(items: list[SlideElement]) -> None:
            for item in items:
                nodes.append(item)
                visit(item.children)

        visit(self.slide.children)
        return nodes

    def clamp_to_image(self) -> None:
        if not self.slide.image_width or not self.slide.image_height:
            return
        for node in self.walk():
            node.bbox = clamp_bbox(node.bbox, self.slide.image_width, self.slide.image_height)


class _NormalizeContext:
    def __init__(self) -> None:
        self.counter = 0
        self.used_ids: set[str] = set()

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        candidate = f"{prefix}_{self.counter}"
        while candidate in self.used_ids:
            self.counter += 1
            candidate = f"{prefix}_{self.counter}"
        self.used_ids.add(candidate)
        return candidate

    def clean_id(self, value: Any, prefix: str) -> str:
        if isinstance(value, str) and value.strip():
            candidate = re.sub(r"[^A-Za-z0-9_\\-]+", "_", value.strip())
            if candidate not in self.used_ids:
                self.used_ids.add(candidate)
                return candidate
        return self.next_id(prefix)


def _normalize_type(raw_type: Any) -> ElementType:
    value = str(raw_type or "rect").strip().lower()
    if value in {"text", "rect", "rounded_rect", "image", "line"}:
        return value  # type: ignore[return-value]
    return TYPE_ALIASES.get(value, "rect")


def _normalize_element(
    raw: dict[str, Any],
    parent_id: str | None,
    depth: int,
    max_depth: int,
    context: _NormalizeContext,
) -> SlideElement | None:
    element_type = _normalize_type(raw.get("type") or raw.get("role") or raw.get("kind"))
    prefix = {"text": "txt", "image": "img", "line": "line"}.get(element_type, "node")
    element_id = context.clean_id(raw.get("id"), prefix)
    bbox = raw.get("bbox") or raw.get("bounds") or raw.get("box")
    if bbox is None:
        return None

    style_raw = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    raw_type = str(raw.get("type") or raw.get("role") or raw.get("kind") or "").lower()
    if raw_type and raw_type not in {"text", "rect", "rounded_rect", "image", "line"}:
        metadata = {**metadata, "source_type": raw_type}

    text = raw.get("text", raw.get("content", raw.get("label", "" if element_type == "text" else None)))
    image_path = raw.get("image_path") or raw.get("path")
    node = SlideElement(
        id=element_id,
        type=element_type,
        bbox=bbox,
        parent_id=raw.get("parent_id") or parent_id,
        text=str(text) if text is not None else None,
        image_path=str(image_path) if image_path else None,
        style=Style.model_validate(style_raw),
        metadata=metadata,
    )

    if depth < max_depth:
        raw_children = raw.get("children", [])
        if isinstance(raw_children, list):
            node.children = [
                child
                for child in (
                    _normalize_element(item, node.id, depth + 1, max_depth, context)
                    for item in raw_children
                    if isinstance(item, dict)
                )
                if child is not None
            ]
    return node


def _rebuild_parent_tree(children: list[SlideElement]) -> list[SlideElement]:
    all_nodes: list[SlideElement] = []

    def collect(nodes: list[SlideElement]) -> None:
        for node in nodes:
            all_nodes.append(node)
            collect(node.children)

    collect(children)
    if not all_nodes:
        return []

    node_by_id = {node.id: node for node in all_nodes}
    for node in all_nodes:
        node.children = []

    roots: list[SlideElement] = []
    for node in all_nodes:
        parent = node_by_id.get(node.parent_id or "")
        if parent is not None and parent.id != node.id:
            parent.children.append(node)
        else:
            node.parent_id = None
            roots.append(node)
    return roots


@dataclass(frozen=True)
class BboxNormalization:
    input_format: str
    source_width: float
    source_height: float
    scale_x: float
    scale_y: float

    def to_metadata(self) -> dict[str, float | str]:
        return {
            "input_format": self.input_format,
            "source_width": round(self.source_width, 4),
            "source_height": round(self.source_height, 4),
            "scale_x": round(self.scale_x, 6),
            "scale_y": round(self.scale_y, 6),
        }


def normalize_bboxes_to_image(document: SlideDocument) -> BboxNormalization | None:
    image_width = document.slide.image_width
    image_height = document.slide.image_height
    if not image_width or not image_height:
        return None

    nodes = document.walk()
    if not nodes:
        return None

    input_format = infer_bbox_input_format(nodes)
    boxes = [bbox_to_xywh(node.bbox, input_format) for node in nodes]
    max_x = max((box[0] + box[2] for box in boxes), default=0.0)
    max_y = max((box[1] + box[3] for box in boxes), default=0.0)
    source_width = infer_coordinate_extent(max_x, float(image_width))
    source_height = infer_coordinate_extent(max_y, float(image_height))
    scale_x = float(image_width) / source_width if source_width > 0 else 1.0
    scale_y = float(image_height) / source_height if source_height > 0 else 1.0

    changed = input_format != "xywh" or not math.isclose(scale_x, 1.0) or not math.isclose(scale_y, 1.0)
    if not changed:
        return None

    for node, box in zip(nodes, boxes, strict=True):
        x, y, width, height = box
        node.bbox = clamp_bbox(
            [x * scale_x, y * scale_y, width * scale_x, height * scale_y],
            image_width,
            image_height,
        )

    return BboxNormalization(
        input_format=input_format,
        source_width=source_width,
        source_height=source_height,
        scale_x=scale_x,
        scale_y=scale_y,
    )


def infer_bbox_input_format(nodes: list[SlideElement]) -> str:
    if not nodes:
        return "xywh"

    corner_like = 0
    much_better_as_corners = 0
    lower_nodes_with_corner_height = 0
    for node in nodes:
        x, y, third, fourth = node.bbox
        if third > x and fourth > y:
            corner_like += 1

        xywh_area = max(0.0, third) * max(0.0, fourth)
        corner_area = max(0.0, third - x) * max(0.0, fourth - y)
        if corner_area > 0 and xywh_area > corner_area * 2.2:
            much_better_as_corners += 1
        if y > 100 and fourth > y:
            lower_nodes_with_corner_height += 1

    total = len(nodes)
    corner_ratio = corner_like / total
    better_ratio = much_better_as_corners / total
    lower_corner_ratio = lower_nodes_with_corner_height / total
    if corner_ratio >= 0.85 and (better_ratio >= 0.45 or lower_corner_ratio >= 0.45):
        return "x1y1x2y2"
    return "xywh"


def bbox_to_xywh(bbox: list[float], input_format: str) -> list[float]:
    x, y, third, fourth = bbox
    if input_format == "x1y1x2y2":
        return [x, y, max(0.0, third - x), max(0.0, fourth - y)]
    return [x, y, max(0.0, third), max(0.0, fourth)]


def infer_coordinate_extent(max_coordinate: float, image_extent: float) -> float:
    if max_coordinate <= 0 or image_extent <= 0:
        return image_extent
    if max_coordinate >= image_extent * 0.72:
        return image_extent

    for candidate in COMMON_COORDINATE_EXTENTS:
        if candidate >= image_extent * 0.9:
            continue
        if max_coordinate <= candidate and max_coordinate >= candidate * 0.55:
            return float(candidate)
    return image_extent


def clamp_bbox(bbox: list[float], image_width: int, image_height: int) -> list[float]:
    x, y, width, height = bbox
    x = min(max(0.0, x), float(image_width))
    y = min(max(0.0, y), float(image_height))
    width = min(max(0.0, width), float(image_width) - x)
    height = min(max(0.0, height), float(image_height) - y)
    return [round(x, 2), round(y, 2), round(width, 2), round(height, 2)]


def bbox_iou(left: list[float], right: list[float]) -> float:
    left_x1, left_y1, left_w, left_h = left
    right_x1, right_y1, right_w, right_h = right
    left_x2, left_y2 = left_x1 + left_w, left_y1 + left_h
    right_x2, right_y2 = right_x1 + right_w, right_y1 + right_h
    inter_w = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    inter_h = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    inter = inter_w * inter_h
    union = left_w * left_h + right_w * right_h - inter
    return inter / union if union > 0 else 0.0


def bbox_center_inside(inner: list[float], outer: list[float]) -> bool:
    x, y, width, height = inner
    cx, cy = x + width / 2, y + height / 2
    ox, oy, ow, oh = outer
    return ox <= cx <= ox + ow and oy <= cy <= oy + oh


def distance_between_bboxes(left: list[float], right: list[float]) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    return math.hypot((lx + lw / 2) - (rx + rw / 2), (ly + lh / 2) - (ry + rh / 2))


def dump_json(data: Any, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
