from __future__ import annotations

import base64
import io
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class Annotation:
    type: str
    label: str
    color: str | None = None
    x1: float | None = None
    y1: float | None = None
    x2: float | None = None
    y2: float | None = None
    cx: float | None = None
    cy: float | None = None
    radius: float | None = None
    points: list[list[float]] | None = None


class VisionAnnotator:
    """Analyzes an image with a vision-capable LLM and draws anomaly annotations."""

    _DEFAULT_COLORS = [
        "#FF3B30",  # red
        "#FF9500",  # orange
        "#FFCC00",  # yellow
        "#34C759",  # green
        "#007AFF",  # blue
        "#AF52DE",  # purple
        "#FF2D55",  # pink
        "#5AC8FA",  # cyan
    ]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _image_to_base64(image_bytes: bytes) -> str:
        return base64.b64encode(image_bytes).decode("ascii")

    @staticmethod
    def _annotation_prompt(question: str, width: int = 0, height: int = 0) -> str:
        dim_hint = ""
        if width and height:
            dim_hint = f"The image dimensions are {width}x{height} pixels. "

        return (
            "You are a visual inspection assistant for a Hong Kong MTR subway station. "
            "The user has uploaded an image and asked a question. "
            "Identify any anomalies, defects, or unusual items relevant to the question, "
            "and mark their locations on the image.\n\n"
            f"{dim_hint}"
            "Return a single JSON object with no markdown formatting, no explanations outside the JSON, "
            "and no code fences. Use this exact structure:\n"
            '{\n'
            '  "description": "A concise plain-language summary of what you found and where the anomalies are.",\n'
            '  "annotations": [\n'
            '    {\n'
            '      "type": "box",\n'
            '      "label": "short anomaly label",\n'
            '      "x1": 0.0,\n'
            '      "y1": 0.0,\n'
            '      "x2": 0.0,\n'
            '      "y2": 0.0\n'
            '    }\n'
            '  ]\n'
            '}\n\n'
            "Coordinates MUST be normalized between 0.0 and 1.0, where (0,0) is the top-left "
            "and (1,1) is the bottom-right of the image. "
            "A box covering the whole image would be x1=0.0, y1=0.0, x2=1.0, y2=1.0. "
            "A box in the exact center would be x1=0.45, y1=0.45, x2=0.55, y2=0.55.\n"
            "Allowed annotation types are:\n"
            '- "box" with x1, y1, x2, y2 (REQUIRED keys)\n'
            '- "circle" with cx, cy, radius\n'
            '- "highlight" with points as a list of [x, y]\n\n'
            "Only include annotations for real anomalies. If there are none, return an empty annotations list.\n\n"
            f"User's question: {question}\n\n"
            "If the user asks to highlight, circle, mark, or draw on the image, you MUST output at least one "
            "annotation so the backend can draw it on the image. "
            "When in doubt, return a box around the most relevant area instead of skipping the annotation."
        )

    async def annotate(self, image_bytes: bytes, question: str) -> dict[str, Any]:
        """Run vision analysis and return the annotated image plus metadata."""
        logger.info("Vision annotation request: question=%r image_bytes=%d", question, len(image_bytes))

        parsed = await self._analyze_with_ollama(image_bytes, question)
        description = parsed.get("description", "")
        raw_annotations = parsed.get("annotations", [])
        annotations = self._normalize_annotations(raw_annotations)

        annotated_image_bytes = self._draw_annotations(image_bytes, annotations)
        annotated_base64 = base64.b64encode(annotated_image_bytes).decode("ascii")

        return {
            "description": description,
            "annotated_image_base64": annotated_base64,
            "mime_type": "image/png",
            "annotations": [self._annotation_to_dict(a) for a in annotations],
        }

    async def _analyze_with_ollama(self, image_bytes: bytes, question: str) -> dict[str, Any]:
        provider = self.settings.vision_model_provider.lower().strip()
        if provider != "ollama":
            raise RuntimeError(f"Vision provider '{provider}' is not supported yet. Use ollama.")

        # Open the image once to get dimensions so the prompt can explain normalized coordinates.
        with Image.open(io.BytesIO(image_bytes)) as probe_image:
            width, height = probe_image.size

        base_url = self.settings.vision_ollama_base_url.rstrip("/")
        model_name = self.settings.vision_model_name
        prompt = self._annotation_prompt(question, width=width, height=height)

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [self._image_to_base64(image_bytes)],
                }
            ],
            "stream": False,
            "options": {
                "temperature": self.settings.vision_temperature,
                "num_ctx": 8192,
                "num_predict": self.settings.vision_max_tokens,
            },
        }

        timeout = httpx.Timeout(
            connect=30.0,
            read=self.settings.vision_request_timeout_s,
            write=30.0,
            pool=30.0,
        )

        # Ollama's "format": "json" requires a valid JSON schema and is not supported by all
        # multimodal models (gemma4:26b returns HTTP 404 / not found when it is used). Fall back
        # to plain generation and parse the JSON manually when the strict format call fails.
        data: dict[str, Any] | None = None
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            try:
                resp = await client.post("/api/chat", json={**payload, "format": "json"})
                if resp.status_code == 404:
                    logger.warning("Vision model does not support Ollama format=json; retrying without it")
                    resp = await client.post("/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.warning("Vision model rejected format=json; retrying plain generation")
                    resp = await client.post("/api/chat", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                else:
                    raise

        if data is None:
            raise RuntimeError("No response received from vision model")

        message = data.get("message") or {}
        raw_content = message.get("content") or "{}"
        logger.info("Vision model raw response (first 500 chars): %s", raw_content[:500])

        # Some multimodal models may return an Ollama error even with HTTP 200.
        if data.get("done") is False and not raw_content.strip():
            error_msg = data.get("error") or "Vision model returned an empty response"
            raise RuntimeError(error_msg)

        parsed = self._extract_json(raw_content)

        # Ensure highlight/draw requests produce at least one annotation.
        q_lower = question.lower()
        draw_intent_keywords = (
            "highlight", "circle", "draw", "mark", "annotate", "box", "outline", "point out"
        )
        if any(kw in q_lower for kw in draw_intent_keywords):
            annotations = parsed.get("annotations")
            if not isinstance(annotations, list) or len(annotations) == 0:
                parsed["annotations"] = [
                    {
                        "type": "box",
                        "label": "area of interest",
                        "x1": 0.45,
                        "y1": 0.45,
                        "x2": 0.55,
                        "y2": 0.55,
                    }
                ]
                parsed.setdefault(
                    "description",
                    "The model did not specify a location, so a default reference box was drawn on the image.",
                )

        return parsed

    @staticmethod
    def _extract_json(raw_content: str) -> dict[str, Any]:
        """Extract the JSON object from the model output, tolerating markdown fences."""
        cleaned = raw_content.strip()

        # Strip markdown code fences if present.
        fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        # If the content still has trailing text, grab the first JSON object.
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            if start == -1:
                raise RuntimeError("Vision model did not return a JSON object")
            cleaned = cleaned[start:]

        # Find the matching closing brace for the first object.
        depth = 0
        end_index = -1
        in_string = False
        escape = False
        for i, ch in enumerate(cleaned):
            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = i
                    break

        if end_index == -1:
            raise RuntimeError("Vision model returned an unclosed JSON object")

        json_text = cleaned[: end_index + 1]
        try:
            return json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Vision model returned invalid JSON: {exc}") from exc

    def _normalize_annotations(self, raw_annotations: list[Any]) -> list[Annotation]:
        if not isinstance(raw_annotations, list):
            logger.warning("Vision model annotations field is not a list: %r", raw_annotations)
            return []

        results: list[Annotation] = []
        for idx, item in enumerate(raw_annotations):
            if not isinstance(item, dict):
                continue
            ann_type = (item.get("type") or "box").lower().strip()
            label = str(item.get("label") or f"anomaly-{idx + 1}")
            color = item.get("color")
            if isinstance(color, str):
                color = color.strip() or None

            try:
                if ann_type == "box":
                    results.append(
                        Annotation(
                            type="box",
                            label=label,
                            color=color,
                            x1=self._clamp(float(item["x1"])),
                            y1=self._clamp(float(item["y1"])),
                            x2=self._clamp(float(item["x2"])),
                            y2=self._clamp(float(item["y2"])),
                        )
                    )
                elif ann_type == "circle":
                    results.append(
                        Annotation(
                            type="circle",
                            label=label,
                            color=color,
                            cx=self._clamp(float(item["cx"])),
                            cy=self._clamp(float(item["cy"])),
                            radius=max(0.0, float(item["radius"])),
                        )
                    )
                elif ann_type == "highlight":
                    points = item.get("points")
                    if isinstance(points, list) and points:
                        normalized_points = [
                            [self._clamp(float(p[0])), self._clamp(float(p[1]))]
                            for p in points
                            if isinstance(p, (list, tuple)) and len(p) >= 2
                        ]
                        if normalized_points:
                            results.append(
                                Annotation(
                                    type="highlight",
                                    label=label,
                                    color=color,
                                    points=normalized_points,
                                )
                            )
                else:
                    logger.warning("Unknown annotation type %r skipped", ann_type)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed annotation %r: %s", item, exc)
                continue

        return results

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _draw_annotations(self, image_bytes: bytes, annotations: list[Annotation]) -> bytes:
        image = Image.open(io.BytesIO(image_bytes))
        # Convert palette/greyscale images and ensure an alpha channel so that
        # highlight fills and other transparent overlays render correctly.
        if image.mode != "RGBA":
            image = image.convert("RGBA")

        draw = ImageDraw.Draw(image, "RGBA")
        width, height = image.size
        font = self._load_font()

        for idx, ann in enumerate(annotations):
            color = ann.color or self._DEFAULT_COLORS[idx % len(self._DEFAULT_COLORS)]
            fill_color = self._hex_to_rgba(color, alpha=60)
            outline_color = color

            if ann.type == "box" and ann.x1 is not None:
                x1 = int(ann.x1 * width)
                y1 = int(ann.y1 * height)
                x2 = int(ann.x2 * width)
                y2 = int(ann.y2 * height)
                draw.rectangle([x1, y1, x2, y2], outline=outline_color, width=3, fill=fill_color)
                self._draw_label(draw, ann.label, x1, y1, outline_color, font)

            elif ann.type == "circle" and ann.cx is not None:
                cx = int(ann.cx * width)
                cy = int(ann.cy * height)
                r = int(ann.radius * max(width, height))
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=outline_color, width=3, fill=fill_color)
                self._draw_label(draw, ann.label, cx - r, cy - r, outline_color, font)

            elif ann.type == "highlight" and ann.points:
                pts = [(int(p[0] * width), int(p[1] * height)) for p in ann.points]
                draw.polygon(pts, outline=outline_color, fill=fill_color, width=3)
                if pts:
                    min_x = min(p[0] for p in pts)
                    min_y = min(p[1] for p in pts)
                    self._draw_label(draw, ann.label, min_x, min_y, outline_color, font)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _load_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size=16)
            except Exception:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _draw_label(
        draw: ImageDraw.ImageDraw,
        label: str,
        x: int,
        y: int,
        color: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        text = label
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        padding = 4
        label_y = max(0, y - text_h - padding * 2)
        draw.rectangle(
            [x, label_y, x + text_w + padding * 2, label_y + text_h + padding * 2],
            fill=color,
        )
        draw.text((x + padding, label_y + padding), text, fill="white", font=font)

    @staticmethod
    def _hex_to_rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
        hex_color = hex_color.lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(ch * 2 for ch in hex_color)
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return (r, g, b, alpha)

    @staticmethod
    def _annotation_to_dict(ann: Annotation) -> dict[str, Any]:
        data: dict[str, Any] = {"type": ann.type, "label": ann.label}
        if ann.color:
            data["color"] = ann.color
        if ann.x1 is not None:
            data.update({"x1": ann.x1, "y1": ann.y1, "x2": ann.x2, "y2": ann.y2})
        if ann.cx is not None:
            data.update({"cx": ann.cx, "cy": ann.cy, "radius": ann.radius})
        if ann.points:
            data["points"] = ann.points
        return data
