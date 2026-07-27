from __future__ import annotations

import base64
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import cv2
import httpx
import numpy as np

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
            "Only include annotations for REAL anomalies or objects that are actually visible in the image. "
            "Never fabricate a box for something that is not present.\n\n"
            f"User's question: {question}\n\n"
            "If the user asks to highlight, circle, mark, or draw on something AND that thing is visible in "
            "the image, output at least one annotation marking it. If the requested target is NOT visible in "
            "the image (e.g. 'draw the exit sign' when no exit sign is present), return an EMPTY annotations "
            "list and explain in the description that the target was not found. Do NOT draw a placeholder or "
            "default box when nothing was found."
        )

    @staticmethod
    def _retry_prompt(question: str, error: Exception, failed_raw: str) -> str:
        """Build a stricter prompt that asks the model to fix its previous invalid output."""
        hint = (
            f"{question}\n\n"
            "IMPORTANT: Your previous response could not be parsed and was rejected "
            f"with this error: {error}. "
            "Respond again with a SINGLE valid JSON object only — no markdown, no code fences, "
            "no text before or after the JSON — using the exact structure requested above. "
            "Make sure every annotation has the required coordinate keys."
        )
        if failed_raw.strip():
            hint += (
                '\nYour previous (invalid) output was:\n"""\n'
                f"{failed_raw[:800]}\n"
                '"""\nFix it and return only the corrected JSON object.'
            )
        return hint

    async def annotate(self, image_bytes: bytes, question: str) -> dict[str, Any]:
        """Run vision analysis and return the annotated image plus metadata.

        If the vision model returns output that cannot be parsed (invalid JSON,
        no JSON object, empty response, HTTP error), the call is retried up to
        ``vision_max_retries`` times with a repair hint that shows the model its
        previous invalid output and asks for strict JSON.
        """
        logger.info("Vision annotation request: question=%r image_bytes=%d", question, len(image_bytes))

        max_retries = int(getattr(self.settings, "vision_max_retries", 2))
        parsed: dict[str, Any] | None = None
        raw_content = ""
        last_error: Exception | None = None
        attempt_question = question

        for attempt in range(max_retries + 1):
            try:
                parsed, raw_content = await self._analyze_with_ollama(image_bytes, attempt_question)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - retry any vision failure
                last_error = exc
                # _analyze_with_ollama stores the raw text it received before parsing failed.
                failed_raw = getattr(self, "_last_raw_content", "") or ""
                logger.warning(
                    "Vision annotation attempt %d/%d failed: %s",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt >= max_retries:
                    break
                attempt_question = self._retry_prompt(question, exc, failed_raw)

        if parsed is None:
            assert last_error is not None
            raise RuntimeError(
                f"Vision annotation failed after {max_retries + 1} attempts: {last_error}"
            ) from last_error

        description = parsed.get("description", "")
        raw_annotations = parsed.get("annotations", [])
        # Decode once to get pixel dimensions so we can normalize any pixel-valued
        # coordinates the model emits (some models mix normalized x with pixel y).
        _decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        height, width = _decoded.shape[:2] if _decoded is not None else (0, 0)
        annotations = self._normalize_annotations(raw_annotations, width=width, height=height)

        annotated_image_bytes = self._draw_annotations(image_bytes, annotations)
        annotated_base64 = base64.b64encode(annotated_image_bytes).decode("ascii")

        return {
            "description": description,
            "annotated_image_base64": annotated_base64,
            "mime_type": "image/png",
            "annotations": [self._annotation_to_dict(a) for a in annotations],
            "raw_response": raw_content,
        }

    async def _analyze_with_ollama(self, image_bytes: bytes, question: str) -> tuple[dict[str, Any], str]:
        provider = self.settings.vision_model_provider.lower().strip()
        if provider != "ollama":
            raise RuntimeError(f"Vision provider '{provider}' is not supported yet. Use ollama.")

        # Decode the image once to get dimensions so the prompt can explain normalized coordinates.
        _decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if _decoded is None:
            raise RuntimeError("Could not decode image for vision analysis")
        height, width = _decoded.shape[:2]

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

        # Surface Ollama-level errors first.
        ollama_error = data.get("error")
        if ollama_error:
            raise RuntimeError(f"Ollama vision error: {ollama_error}")

        message = data.get("message") or {}
        raw_content = message.get("content") or "{}"
        # Stash the raw text so the retry loop can include it in a repair hint even
        # when _extract_json raises before this method returns.
        self._last_raw_content = raw_content
        logger.info("Vision model raw response (first 500 chars): %s", raw_content[:500])

        # Some multimodal models may return an Ollama error even with HTTP 200.
        if data.get("done") is False and not raw_content.strip():
            error_msg = data.get("error") or "Vision model returned an empty response"
            raise RuntimeError(error_msg)

        parsed = self._extract_json(raw_content)

        # Honor the model's annotation verdict. An empty "annotations" list means
        # the model found nothing to annotate (e.g. "draw the exit sign" when no
        # exit sign is present). The prompt already strongly instructs the model to
        # emit at least one annotation for highlight/draw requests when something IS
        # there, so an empty list is a deliberate "nothing found" — do NOT inject a
        # fake center box, which would mislead the user into thinking a target was
        # located when it was not.

        return parsed, raw_content

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
            logger.warning("Vision model returned invalid JSON. Raw content: %r", raw_content[:2000])
            raise RuntimeError(f"Vision model returned invalid JSON: {exc}") from exc

    @staticmethod
    def _repair_box_coords(item: dict[str, Any]) -> dict[str, Any] | None:
        """Try to convert common malformed box representations into x1/y1/x2/y2.

        Handles:
        - separate x1/y1/x2/y2 keys (already correct)
        - a 4-element array under "box" or "bbox"
        - x/y/width/height keys
        - x1/y1/width/height keys
        """
        # Direct keys.
        if all(k in item for k in ("x1", "y1", "x2", "y2")):
            # Return a clean dict with only the four canonical keys so stray
            # keys the model sometimes emits (e.g. a typo'd "ysl") never leak.
            try:
                return {
                    "x1": float(item["x1"]),
                    "y1": float(item["y1"]),
                    "x2": float(item["x2"]),
                    "y2": float(item["y2"]),
                }
            except (ValueError, TypeError):
                return item

        # Array form: [x1, y1, x2, y2]
        for key in ("box", "bbox", "coordinates", "coords"):
            val = item.get(key)
            if isinstance(val, (list, tuple)) and len(val) == 4:
                try:
                    x1, y1, x2, y2 = (float(v) for v in val)
                    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
                except (ValueError, TypeError):
                    continue

        # x, y, width, height form.
        if all(k in item for k in ("x", "y", "width", "height")):
            try:
                x1 = float(item["x"])
                y1 = float(item["y"])
                x2 = x1 + float(item["width"])
                y2 = y1 + float(item["height"])
                return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
            except (ValueError, TypeError):
                pass

        # x1, y1, width, height form.
        if all(k in item for k in ("x1", "y1", "width", "height")):
            try:
                x1 = float(item["x1"])
                y1 = float(item["y1"])
                x2 = x1 + float(item["width"])
                y2 = y1 + float(item["height"])
                return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
            except (ValueError, TypeError):
                pass

        return None

    def _normalize_annotations(
        self, raw_annotations: list[Any], width: int = 0, height: int = 0
    ) -> list[Annotation]:
        if not isinstance(raw_annotations, list):
            logger.warning("Vision model annotations field is not a list: %r", raw_annotations)
            return []

        results: list[Annotation] = []
        for idx, item in enumerate(raw_annotations):
            if not isinstance(item, dict):
                continue
            raw_type = item.get("type") or "box"
            # Tolerate models that wrap the type in punctuation, e.g. "<box>" or "[box]".
            if isinstance(raw_type, list):
                ann_type = "box"
            else:
                ann_type = re.sub(r"[^a-z0-9]", "", str(raw_type).lower().strip()) or "box"
            label = str(item.get("label") or f"anomaly-{idx + 1}")
            color = item.get("color")
            if isinstance(color, str):
                color = color.strip() or None

            try:
                if ann_type == "box":
                    repaired = self._repair_box_coords(item) or item
                    # Coordinates may be normalized (0-1) OR raw pixels. Anything > 1.0
                    # is treated as pixels and divided by the relevant image dimension.
                    x1 = self._coord_to_normalized(repaired["x1"], width)
                    y1 = self._coord_to_normalized(repaired["y1"], height)
                    x2 = self._coord_to_normalized(repaired["x2"], width)
                    y2 = self._coord_to_normalized(repaired["y2"], height)
                    # Sort so x1 <= x2 and y1 <= y2.
                    x1, x2 = sorted((x1, x2))
                    y1, y2 = sorted((y1, y2))
                    results.append(
                        Annotation(
                            type="box",
                            label=label,
                            color=color,
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                        )
                    )
                elif ann_type == "circle":
                    radius = float(item["radius"])
                    if max(width, height) and radius > 1.0:
                        radius = radius / max(width, height)
                    results.append(
                        Annotation(
                            type="circle",
                            label=label,
                            color=color,
                            cx=self._coord_to_normalized(item["cx"], width),
                            cy=self._coord_to_normalized(item["cy"], height),
                            radius=max(0.0, radius),
                        )
                    )
                elif ann_type == "highlight":
                    points = item.get("points")
                    if isinstance(points, list) and points:
                        normalized_points = [
                            [self._coord_to_normalized(p[0], width), self._coord_to_normalized(p[1], height)]
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

    @staticmethod
    def _coord_to_normalized(value: Any, dim: int) -> float:
        """Normalize a coordinate that may be in [0,1] or in raw pixels.

        Values > 1.0 are assumed to be pixel offsets and are divided by ``dim``
        (the image width or height). Everything is then clamped to [0,1].
        """
        v = float(value)
        if dim and v > 1.0:
            v = v / dim
        return VisionAnnotator._clamp(v)

    def _draw_annotations(self, image_bytes: bytes, annotations: list[Annotation]) -> bytes:
        """Draw annotations as transparent outlines (no fill) using OpenCV.

        Boxes, circles, and highlights are rendered as a colored outline only —
        the interior stays transparent so the underlying image is fully visible.
        A small solid label tag is drawn above each annotation.
        """
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("Could not decode image for annotation")
        height, width = image.shape[:2]

        for idx, ann in enumerate(annotations):
            color_hex = ann.color or self._DEFAULT_COLORS[idx % len(self._DEFAULT_COLORS)]
            bgr = self._hex_to_bgr(color_hex)

            if ann.type == "box" and ann.x1 is not None:
                x1 = int(ann.x1 * width)
                y1 = int(ann.y1 * height)
                x2 = int(ann.x2 * width)
                y2 = int(ann.y2 * height)
                cv2.rectangle(image, (x1, y1), (x2, y2), bgr, thickness=3)
                self._draw_label(image, ann.label, x1, y1, bgr)

            elif ann.type == "circle" and ann.cx is not None:
                cx = int(ann.cx * width)
                cy = int(ann.cy * height)
                r = int(ann.radius * max(width, height))
                cv2.circle(image, (cx, cy), r, bgr, thickness=3)
                self._draw_label(image, ann.label, cx - r, cy - r, bgr)

            elif ann.type == "highlight" and ann.points:
                pts = np.array(
                    [[int(p[0] * width), int(p[1] * height)] for p in ann.points],
                    dtype=np.int32,
                )
                cv2.polylines(image, [pts], isClosed=True, color=bgr, thickness=3)
                if len(pts):
                    min_x = int(pts[:, 0].min())
                    min_y = int(pts[:, 1].min())
                    self._draw_label(image, ann.label, min_x, min_y, bgr)

        ok, buffer = cv2.imencode(".png", image)
        if not ok:
            raise RuntimeError("Could not encode annotated image to PNG")
        return buffer.tobytes()

    @staticmethod
    def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
        """Convert a #RRGGBB hex string to the BGR tuple OpenCV expects."""
        hex_color = hex_color.lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(ch * 2 for ch in hex_color)
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return (b, g, r)

    @staticmethod
    def _text_color_for(bgr: tuple[int, int, int]) -> tuple[int, int, int]:
        """Pick black or white text for a label tag based on background luminance."""
        b, g, r = bgr
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return (0, 0, 0) if luminance > 160 else (255, 255, 255)

    @staticmethod
    def _draw_label(
        image: np.ndarray,
        label: str,
        x: int,
        y: int,
        bgr: tuple[int, int, int],
    ) -> None:
        """Draw a filled label tag with contrasting text above (x, y)."""
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.5
        thickness = 1
        (text_w, text_h), baseline = cv2.getTextSize(label, font, font_scale, thickness)
        padding = 4
        label_y = max(0, y - text_h - padding * 2 - baseline)
        cv2.rectangle(
            image,
            (x, label_y),
            (x + text_w + padding * 2, label_y + text_h + padding * 2 + baseline),
            bgr,
            thickness=-1,
        )
        cv2.putText(
            image,
            label,
            (x + padding, label_y + text_h + padding),
            font,
            font_scale,
            VisionAnnotator._text_color_for(bgr),
            thickness,
            lineType=cv2.LINE_AA,
        )

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
