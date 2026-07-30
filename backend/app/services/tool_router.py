from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Simple alias map used by the annotation safety-net fallback to detect category mentions.
_CATEGORY_ALIASES: dict[str, str] = {
    "advertisement board": "Advertisement Board",
    "ad board": "Advertisement Board",
    "adboard": "Advertisement Board",
    "poster": "Advertisement Board",
    "billboard": "Advertisement Board",
    "exit sign": "Exit Sign",
    "exit": "Exit Sign",
    "light": "Lights",
    "lights": "Lights",
    "map": "Map",
    "tv": "TV",
    "television": "TV",
    "ticket gate": "Ticket Gate",
    "gate": "Ticket Gate",
}

# Optional ``inspection_id`` argument, reused by every tool that can be scoped to one
# inspection. The database can hold multiple inspections; pass it when the user names a
# specific inspection, otherwise omit it to query across all inspections.
_INSPECTION_ID_ARG = {
    "inspection_id": {
        "type": "integer",
        "description": (
            "Optional: scope the query to a single inspection id. Omit to query across "
            "all inspections. Use get_inspections first if you do not know the id."
        ),
    }
}


def _params(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Build a tool parameter schema, merging in the optional inspection_id argument."""
    props = {**properties, **_INSPECTION_ID_ARG}
    return {"type": "object", "properties": props, "required": required or []}


# Tool definitions used by the LLM router. These map to InspectionDBClient methods.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_inspections",
        "description": "List every inspection in the database with its id, start time, ground-truth flag, and object/detection counts. Call this first when the user mentions a specific inspection or when you need an inspection_id for another tool.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_summary",
        "description": "Overall object counts and category breakdown. Use ONLY for generic inspection-wide summaries such as 'what did you find' or 'how many objects overall'. Do NOT use for category-specific summaries; use get_objects_by_category, get_category_objects_with_images, or get_category_objects_coordinates instead.",
        "parameters": _params({}),
    },
    {
        "name": "get_categories",
        "description": "Return the list of distinct object categories in the database. Use this when you need to know what categories exist.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_object_by_id",
        "description": "Detailed information, timeline, and image links for a single object by its object id (the id column of the objects table).",
        "parameters": _params(
            {"object_id": {"type": "integer", "description": "The numeric object id, e.g. 9."}},
            required=["object_id"],
        ),
    },
    {
        "name": "get_objects_by_category",
        "description": "List the UNIQUE objects belonging to a category such as Lights, Advertisement Board, or Ticket Gate. Use this when the user asks 'how many X were detected', 'how many X are there', or for details about objects of a specific category. Each object may have multiple per-frame detections; this tool counts distinct objects, not detections.",
        "parameters": _params(
            {
                "category": {"type": "string", "description": "Category name, e.g. Lights, Advertisement Board, Ticket Gate."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["category"],
        ),
    },
    {
        "name": "get_top_objects",
        "description": "The objects with the most detections (most frequently detected).",
        "parameters": _params({"n": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result.", "default": 5}}),
    },
    {
        "name": "get_recent_objects",
        "description": "The most recently seen objects (by last detection timestamp).",
        "parameters": _params({"limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result.", "default": 5}}),
    },
    {
        "name": "get_object_timeline",
        "description": "A temporal story for a single object: first/last seen times, duration, and key moments across its detections.",
        "parameters": _params(
            {"object_id": {"type": "integer", "description": "The numeric object id."}},
            required=["object_id"],
        ),
    },
    {
        "name": "get_object_image_paths",
        "description": "Markdown image links for the frames an object was detected in.",
        "parameters": _params(
            {"object_id": {"type": "integer", "description": "The numeric object id."}},
            required=["object_id"],
        ),
    },
    {
        "name": "get_category_timeline",
        "description": "First/last seen timestamps for every object in a category.",
        "parameters": _params(
            {"category": {"type": "string", "description": "Category name, e.g. Lights, Advertisement Board."}},
            required=["category"],
        ),
    },
    {
        "name": "get_category_windows",
        "description": "First/last detection windows for one or more categories. Use this when the user asks about multiple categories at once or wants to compare when categories were detected.",
        "parameters": _params(
            {"categories": {"type": "array", "items": {"type": "string"}, "description": "List of category names, e.g. [\"Lights\", \"Ticket Gate\"]."}},
            required=["categories"],
        ),
    },
    {
        "name": "get_category_objects_coordinates",
        "description": "The centroid and 3D bounding-box coordinates for every object in a category, ordered by first appearance. Use this when the user asks for X, Y, Z positions or coordinates.",
        "parameters": _params(
            {"category": {"type": "string", "description": "Category name, e.g. Ticket Gate."}},
            required=["category"],
        ),
    },
    {
        "name": "get_category_objects_with_images",
        "description": "Object ids, coordinates, and sample image frames for objects in a category. Use this when the user asks to see objects WITH their images, e.g. 'show me exit signs with object ids, coordinates, and images'.",
        "parameters": _params(
            {
                "category": {"type": "string", "description": "Category name, e.g. Exit Sign."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["category"],
        ),
    },
    {
        "name": "get_category_proximity",
        "description": "Counts how many objects from other categories are near objects in a target category. Use this when the user asks whether one category is close to another and only needs counts, not images.",
        "parameters": _params(
            {
                "target_category": {"type": "string", "description": "Category whose objects are the reference points, e.g. Ticket Gate."},
                "other_categories": {"type": "array", "items": {"type": "string"}, "description": "Categories to measure distance to, e.g. [\"Advertisement Board\", \"Lights\"]."},
                "radius_m": {"type": "number", "description": "Search radius in meters around each target object centroid.", "default": 2.0},
            },
            required=["target_category", "other_categories"],
        ),
    },
    {
        "name": "get_category_proximity_with_images",
        "description": "Objects from other categories within radius_m of each target-category object, including object IDs, distances, coordinates, and sample images. Use this when the user asks to SEE nearby objects, e.g. 'show me advertisement boards within 2 meters of lights' or 'display the ad boards near the ticket gates'.",
        "parameters": _params(
            {
                "target_category": {"type": "string", "description": "Category whose objects are the reference points, e.g. Lights."},
                "other_categories": {"type": "array", "items": {"type": "string"}, "description": "Categories to show nearby objects for, e.g. [\"Advertisement Board\"]."},
                "radius_m": {"type": "number", "description": "Search radius in meters around each target object centroid.", "default": 2.0},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
                "nearby_limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["target_category", "other_categories"],
        ),
    },
    {
        "name": "get_inspection_timeline",
        "description": "Full chronological log of every object detected during the inspection (or one inspection when inspection_id is given).",
        "parameters": _params({}),
    },
    {
        "name": "get_temporal_clusters",
        "description": "Time-window clusters showing which objects were seen together (e.g. 10 x Lights and 5 x Advertisement Board at the same moment).",
        "parameters": _params(
            {
                "window_ms": {"type": "integer", "description": "Milliseconds within which detections are considered part of the same cluster.", "default": 500},
                "top_n": {"type": "integer", "description": "Number of top clusters to return.", "default": 10},
            }
        ),
    },
    {
        "name": "get_detection_counts_by_category",
        "description": "Per-frame detection counts by category (how many times each category was detected across frames). Use ONLY when the user explicitly asks for per-frame detection counts, e.g. 'how many times was it seen' or 'how many per-frame detections'. For 'how many X were detected' or 'how many X are there', use get_objects_by_category instead.",
        "parameters": _params({}),
    },
    {
        "name": "get_objects_in_time_range",
        "description": "Objects whose detection span overlaps a time window. start_time and end_time can be ISO timestamps, clock times such as '16:51:45', or nanosecond integers. Use for 'what happened between X and Y' questions.",
        "parameters": _params(
            {
                "start_time": {"type": "string", "description": "Start time as ISO datetime, clock time (e.g. '16:51:45'), or ns integer."},
                "end_time": {"type": "string", "description": "End time as ISO datetime, clock time, or ns integer."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["start_time", "end_time"],
        ),
    },
    {
        "name": "get_detections_in_time_range",
        "description": "Per-frame detections captured within a time window. start_time and end_time can be ISO timestamps, clock times, or nanosecond integers. Use for 'show me detections around X' questions.",
        "parameters": _params(
            {
                "start_time": {"type": "string", "description": "Start time as ISO datetime, clock time, or ns integer."},
                "end_time": {"type": "string", "description": "End time as ISO datetime, clock time, or ns integer."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["start_time", "end_time"],
        ),
    },
    {
        "name": "get_objects_in_image",
        "description": "List every object detected in one specific image frame. Use when the user names or links an image filename and asks what objects are in it.",
        "parameters": _params(
            {
                "filename": {"type": "string", "description": "Image filename such as '1781168326856275000.jpg' or a full /inspection/images/ URL."},
            },
            required=["filename"],
        ),
    },
    {
        "name": "get_objects_near_position",
        "description": "Find objects whose centroid is within radius_m of a 3D point. Use when the user gives a coordinate or asks 'what is near (x, y, z)' or 'what did the camera see 2 meters from here'.",
        "parameters": _params(
            {
                "x": {"type": "number", "description": "X coordinate of the search point."},
                "y": {"type": "number", "description": "Y coordinate of the search point."},
                "z": {"type": "number", "description": "Z coordinate of the search point."},
                "radius_m": {"type": "number", "description": "Search radius in meters.", "default": 2.0},
                "category": {"type": "string", "description": "Optional category filter, e.g. 'Lights'."},
            },
            required=["x", "y", "z"],
        ),
    },
    {
        "name": "get_category_sample_images",
        "description": "Return example image links for a category. Use when the user asks to see examples of a category such as 'show me some advertisement boards'.",
        "parameters": _params(
            {
                "category": {"type": "string", "description": "Category name, e.g. Advertisement Board."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["category"],
        ),
    },
    {
        "name": "get_inspection_poses",
        "description": "Camera/robot poses recorded during the inspection (one per image, stored on the images table). Exposes the tf_translation / tf_rotation columns.",
        "parameters": _params({"limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."}}),
    },
    {
        "name": "get_object_distance",
        "description": "Distance in meters between the centroids of two object ids. Use when the user asks 'how far apart are object X and object Y'.",
        "parameters": _params(
            {
                "object_id_a": {"type": "integer"},
                "object_id_b": {"type": "integer"},
            },
            required=["object_id_a", "object_id_b"],
        ),
    },
    {
        "name": "get_category_bounding_box",
        "description": "Axis-aligned 3D bounding box of all objects in a category (centroid and bbox min/max). Use when the user asks for the spatial extent or area a category occupies.",
        "parameters": _params(
            {"category": {"type": "string", "description": "Category name, e.g. Ticket Gate."}},
            required=["category"],
        ),
    },
    {
        "name": "get_category_detection_timeline",
        "description": "Per-time-bucket detection counts for a category. Use for 'when were most X seen', 'busiest minute for X', or category activity over time.",
        "parameters": _params(
            {
                "category": {"type": "string", "description": "Category name, e.g. Lights."},
                "bucket_seconds": {"type": "integer", "description": "Bucket size in seconds.", "default": 60},
            },
            required=["category"],
        ),
    },
    {
        "name": "get_objects_by_category_in_time_range",
        "description": "Objects of a specific category whose detection span overlaps a time window. Use for 'were there any ticket gates after 4:53' or 'lights between X and Y'.",
        "parameters": _params(
            {
                "category": {"type": "string", "description": "Category name."},
                "start_time": {"type": "string", "description": "Start time as ISO, clock time, or ns integer."},
                "end_time": {"type": "string", "description": "End time as ISO, clock time, or ns integer."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["category", "start_time", "end_time"],
        ),
    },
    {
        "name": "get_object_movement",
        "description": "Centroid path of a single object across its detections. Use when the user asks if an object moved, its trajectory, or path.",
        "parameters": _params(
            {"object_id": {"type": "integer"}},
            required=["object_id"],
        ),
    },
    {
        "name": "get_nearest_objects_to_object",
        "description": "Other objects within radius_m of a specific object's centroid. Use for 'what was near object 9'.",
        "parameters": _params(
            {
                "object_id": {"type": "integer"},
                "radius_m": {"type": "number", "default": 2.0},
            },
            required=["object_id"],
        ),
    },
    {
        "name": "get_images_in_time_range",
        "description": "Images captured within a time window, optionally filtered by category. Use for 'show me what the camera saw at 4:51'.",
        "parameters": _params(
            {
                "start_time": {"type": "string", "description": "Start time as ISO, clock time, or ns integer."},
                "end_time": {"type": "string", "description": "End time as ISO, clock time, or ns integer."},
                "category": {"type": "string", "description": "Optional category filter."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["start_time", "end_time"],
        ),
    },
    {
        "name": "get_category_cooccurrence",
        "description": "Which categories most often appear together in the same temporal cluster. Use for 'which objects are usually seen together'.",
        "parameters": _params(
            {
                "window_ms": {"type": "integer", "default": 500},
                "top_n": {"type": "integer", "default": 10},
            }
        ),
    },
    {
        "name": "get_objects_in_temporal_cluster",
        "description": "Objects with coordinates detected around a specific time. Use for 'where was the 4:51 PM cluster' or 'what was at time T'.",
        "parameters": _params(
            {
                "center_time": {"type": "string", "description": "Cluster center time as ISO, clock time, or ns integer."},
                "window_ms": {"type": "integer", "default": 500},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            required=["center_time"],
        ),
    },
    {
        "name": "get_anomaly_types",
        "description": "List the defined anomaly type names (from the anomaly_types table). Use when the user asks what kinds of anomalies exist.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_anomaly_summary",
        "description": "Counts of abnormal image pairs and abnormalities grouped by anomaly type, and per inspection. Use for 'how many anomalies were found' or an overview of anomalies.",
        "parameters": _params({}),
    },
    {
        "name": "get_anomalies",
        "description": "List individual abnormalities: type, 2D pixel bounding box, note, the ground-truth and inspection image links, and the inspection id. Optional filters: anomaly_type, inspection_id, limit. Use for 'show me the anomalies' or 'what defects were found on the ticket gates'.",
        "parameters": _params(
            {
                "anomaly_type": {"type": "string", "description": "Optional: filter to one anomaly type name."},
                "limit": {"type": ["integer", "string"], "description": "Optional cap; use 'all' or omit to return every matching result."},
            }
        ),
    },
    {
        "name": "get_report_summary",
        "description": "Fetch the inspection report text, including anomaly findings, issues, state changes, and recommendations. Use this when the user asks about the written anomaly report or recommendations. Pair with get_anomalies for the structured abnormalities.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "highlight_in_rerun",
        "description": "Highlight specific 3D coordinates or objects in the Rerun viewer so the user can see where they are in the station. A final pass automatically highlights objects/coordinates from tool results when the user wants 3D visualization, so you usually do NOT need this tool for ordinary coordinate questions. Call it ONLY for explicit, specific 3D-highlight requests that name particular objects/coordinates, e.g. 'highlight objects 16 and 19 in the 3D viewer'. Provide object_ids, coordinates, or a category (any combination). The viewer is auto-launched if not running. By default this clears highlights from previous queries; set keep_existing=true only if the user explicitly asks to keep or add to the previous highlights.",
        "parameters": _params(
            {
                "object_ids": {"type": "array", "items": {"type": "integer"}, "description": "Object ids to highlight (centroids + 3D bboxes)."},
                "coordinates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "z": {"type": "number"},
                            "label": {"type": "string"},
                        },
                        "required": ["x", "y", "z"],
                    },
                    "description": "Raw 3D points to highlight.",
                },
                "category": {"type": "string", "description": "Highlight every object in this category."},
                "keep_existing": {"type": "boolean", "default": False, "description": "If true, keep previously highlighted objects/points in the viewer (the user explicitly asked to keep or add to them, e.g. 'keep the previous', 'add to the highlights', 'show alongside'). Default false: previous highlights are cleared before showing these."},
                "label": {"type": "string", "description": "Optional label for this highlight set."},
            }
        ),
    },
    {
        "name": "annotate_image",
        "description": "Analyze and draw annotations (boxes, circles, highlights) on one or more inspection images to mark anomalies or areas of interest. Provide exactly one of image_url, object_id, or category. For object_id or category, up to `limit` images are annotated (default 5). Use ONLY when the user EXPLICITLY asks to annotate / draw on / circle on / mark on / outline / show-on an IMAGE (e.g. 'annotate the image', 'draw on the image', 'circle the defect on the image', 'show the anomaly on the image'). Do NOT use this tool for generic 'show me', 'visualize', 'highlight', or 'where is' requests - those are 3D Rerun viewer requests, not image annotation.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "URL or path to a single image, e.g. /inspection/images/14.jpg or /reports/extracted_images/img-021.jpg."},
                "object_id": {"type": "integer", "description": "Numeric object id (also called track_id). All frames this object was detected in (up to `limit`) will be annotated."},
                "category": {"type": "string", "description": "Category name. Up to `limit` sample images of this category will be annotated."},
                "question": {"type": "string", "description": "What to look for or how to annotate. Defaults to the user's original question."},
                "limit": {"type": ["integer", "string"], "default": 5, "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
        },
    },
    {
        "name": "run_sql_query",
        "description": "Execute a read-only SQL SELECT query against the inspection database. Use ONLY when no other listed tool can answer the question. The query must start with SELECT.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A valid read-only SELECT SQL query."},
                "limit": {"type": ["integer", "string"], "default": 100, "description": "Optional cap; use 'all' or omit to return every matching result."},
            },
            "required": ["query"],
        },
    },
]

# Reduced tool set for testing with a minimal assistant.
REDUCED_TOOLS: list[dict[str, Any]] = [
    next(t for t in TOOLS if t["name"] == "get_categories"),
    next(t for t in TOOLS if t["name"] == "run_sql_query"),
    next(t for t in TOOLS if t["name"] == "get_report_summary"),
]


def _ollama_tools() -> list[dict[str, Any]]:
    """Convert our compact tool definitions into Ollama's native tool format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in TOOLS
    ]


class ToolRouter:
    """Uses the base LLM to pick inspection database tools via Ollama native tool calling.

    This is the first of two requests to the base model: the model chooses tools, the
    backend executes them, and then the base model is called again to produce the answer.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_raw_response: dict[str, Any] | None = None

    def _system_prompt(self, *, max_rounds: int = 3) -> str:
        return f"""\
You are a tool planner for an MTR subway station inspection assistant.
Your ONLY job is to select the right tools from the list below to gather the data needed to answer the user's question.
Do not answer the user directly. Do not output any text other than the tool calls.

## Database overview (new multi-inspection schema)

- `categories` table: `id`, `name`. Known names: Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign.
- `inspections` table: `id`, `started_at`, `is_gt`. There can be MULTIPLE inspections. Use get_inspections to list them.
- `images` table: `id`, `inspection_id`, `timestamp_ns`, `tf_translation_x/y/z`, `tf_rotation_x/y/z/w`, `filename`. Camera pose lives here (there is no separate poses table). `filename` is the on-disk frame name (e.g. `14.jpg`), served at `/inspection/images/<filename>`.
- `objects` table: `id` (the object id — there is NO track_id), `category_id` (→ categories.name), `centroid_x/y/z`, `min_x/y/z`, `max_x/y/z`, `is_gt`, `created_at`. An object has ONE centroid and 3D bbox; it does NOT have stored first_seen/last_seen or observation_count columns.
- `detections` table: `id`, `image_id` (→ images), `object_id` (→ objects), `centroid_x/y/z`, `min/max_x/y/z`. One row per per-frame detection. An object's detection count and first/last-seen timestamps are DERIVED by joining detections → images.
- `anomaly_types` table: `id`, `name`.
- `abnormal_detections` table: `id`, `gt_image` (→ images.id), `inspection_image` (→ images.id). An abnormal image PAIR (ground truth vs inspection).
- `abnormalities` table: `id`, `pair` (→ abnormal_detections.id), `type` (→ anomaly_types.id), `min_x`, `min_y`, `max_x`, `max_y` (2D pixel bbox), `note`.
  NOTE: the anomaly tables may not be populated yet. The anomaly tools will return a clear "not yet populated" message in that case — still call them when the user asks about anomalies.

All timestamps are nanoseconds since epoch. Time tools accept ISO datetimes, clock times such as "16:51:45" or "4:51 PM", or raw nanosecond integers.

Known category names (use these exact strings in arguments):
Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign.

## Multi-inspection

- The database can hold several inspections. Most tools accept an optional `inspection_id`.
- When the user names a specific inspection ("inspection 2", "the second inspection", "the ground-truth run"), pass `inspection_id`. If you do not know the id, call get_inspections first.
- When the user does NOT name an inspection, omit `inspection_id` to query across all inspections. The answerer can still reason across inspections because tool outputs include inspection ids where relevant.

## Tool reference

1. get_inspections — list inspections (id, started_at, is_gt, object/detection counts). Call first when you need an inspection_id.
2. get_summary — overall counts + category breakdown. Generic "what did you find" only.
3. get_categories — distinct category names.
4. get_object_by_id(object_id) — one object's details, timeline, image links.
5. get_objects_by_category(category, limit, inspection_id) — objects in a category.
6. get_top_objects(n, inspection_id) — most-detected objects.
7. get_recent_objects(limit, inspection_id) — most recently seen objects.
8. get_object_timeline(object_id) — first/last seen, duration, key moments for one object.
9. get_object_image_paths(object_id) — image links for one object.
10. get_category_timeline(category, inspection_id) — first/last seen per object in a category.
11. get_category_windows(categories[], inspection_id) — first/last windows for several categories.
12. get_category_objects_coordinates(category, inspection_id) — centroid + 3D bbox per object in a category.
13. get_category_objects_with_images(category, limit, inspection_id) — object ids, coordinates, and a sample image each.
14. get_category_proximity(target_category, other_categories[], radius_m, inspection_id) — counts of other-category objects near each target object (numbers only, no images).
14a. get_category_proximity_with_images(target_category, other_categories[], radius_m, limit, nearby_limit, inspection_id) — nearby objects with object IDs, distances, coordinates, and sample images. Use for 'show me X near Y'.
15. get_inspection_timeline(inspection_id) — chronological object log.
16. get_temporal_clusters(window_ms, top_n, inspection_id) — busiest moments grouped by time window.
17. get_detection_counts_by_category(inspection_id) — per-frame detection counts per category.
18. get_objects_in_time_range(start_time, end_time, limit, inspection_id) — objects whose span overlaps a window.
19. get_detections_in_time_range(start_time, end_time, limit, inspection_id) — per-frame detections in a window.
20. get_objects_in_image(filename) — every object detected in one specific image frame.
21. get_objects_near_position(x, y, z, radius_m, category, inspection_id) — objects within radius of a 3D point.
22. get_category_sample_images(category, limit, inspection_id) — a few example image links for a category.
23. get_inspection_poses(limit, inspection_id) — camera poses (from the images table).
24. get_object_distance(object_id_a, object_id_b) — centroid distance between two objects.
25. get_category_bounding_box(category, inspection_id) — spatial extent of a category.
26. get_category_detection_timeline(category, bucket_seconds, inspection_id) — per-bucket detection counts for a category.
27. get_objects_by_category_in_time_range(category, start_time, end_time, limit, inspection_id) — one category's objects in a window.
28. get_object_movement(object_id) — centroid path of one object across its detections.
29. get_nearest_objects_to_object(object_id, radius_m, inspection_id) — objects within radius of an object.
30. get_images_in_time_range(start_time, end_time, category, limit, inspection_id) — sample images in a window.
31. get_category_cooccurrence(window_ms, top_n, inspection_id) — category pairs seen together.
32. get_objects_in_temporal_cluster(center_time, window_ms, limit, inspection_id) — objects/detections around a time.
33. get_anomaly_types — anomaly type names.
34. get_anomaly_summary(inspection_id) — abnormality counts by type and by inspection.
35. get_anomalies(anomaly_type, inspection_id, limit) — individual abnormalities with type, 2D bbox, note, and gt/inspection image links.
36. get_report_summary — the written anomaly report text + recommendations.
37. highlight_in_rerun(object_ids[], coordinates[], category, inspection_id, label) — push 3D highlights to the Rerun viewer (only for explicit visual requests; coordinates from other tools auto-highlight).
38. run_sql_query(query, limit) — read-only SELECT escape hatch. Use ONLY when no other tool fits.
39. annotate_image(image_url | object_id | category, question, limit) — vision-annotate images.

## Multi-tool flow examples

Example A (multi-tool + cross-source):
User: "Tell me about the objects found during the inspection and their coordinates, and which objects were close to ticket gates."
Plan:
1. get_summary
2. get_category_objects_coordinates(category="Ticket Gate")
3. get_category_proximity(target_category="Ticket Gate", other_categories=["Lights", "Advertisement Board", "Map", "TV", "Exit Sign"])

Example A2 (multi-tool + re-call with changed parameter):
User: "For the exit signs recorded around 4:56 PM, what were their coordinates, and what objects were within a 1 meter radius of them?"
Plan:
1. get_objects_by_category_in_time_range(category="Exit Sign", start_time="16:56:00", end_time="16:57:00")
2. get_category_proximity(target_category="Exit Sign", other_categories=["Lights", "Advertisement Board", "Map", "TV", "Ticket Gate"], radius_m=1.0)
Note: even if a previous turn already called get_category_proximity for Exit Sign with radius_m=3.0, the user now asks for 1 m, so you MUST call it again with radius_m=1.0.

Example B:
User: "How many times were advertisement boards seen, and what is near coordinate (-18, 32, -6)?"
Plan:
1. get_detection_counts_by_category
2. get_objects_near_position(x=-18, y=32, z=-6, radius_m=2.0)

Example C:
User: "Show me some advertisement boards and how far apart are objects 16 and 19."
Plan:
1. get_category_sample_images(category="Advertisement Board", limit=5)
2. get_object_distance(object_id_a=16, object_id_b=19)

Example D (anomalies — structured + report, multi-tool):
User: "What anomalies did you find, and show me the ones on the ticket gates?"
Plan:
1. get_anomaly_summary
2. get_anomalies()
3. get_report_summary
(Note: get_anomalies returns image links for each abnormality's inspection frame; get_report_summary returns the prose report. If the anomaly tables are not yet populated, those tools return a clear message — still call them.)

Example E (Rerun highlight — final pass):
User: "What are the coordinates of the ticket gates? Show me where they are."
Plan:
1. get_category_objects_coordinates(category="Ticket Gate")
(A final pass highlights these coordinates in the Rerun viewer — no separate highlight_in_rerun call needed. The answerer describes the coordinates and notes they are shown in the viewer.)

Example E2 (explicit highlight of specific objects):
User: "Highlight objects 16 and 19 in the 3D viewer."
Plan:
1. highlight_in_rerun(object_ids=[16, 19], label="objects 16 and 19")

Example F (category summary):
User: "Give me a summary of all the lights and advertisement boards found."
Plan:
1. get_objects_by_category(category="Lights", limit=20)
2. get_objects_by_category(category="Advertisement Board", limit=20)

Example G (annotation):
User: "Highlight any anomalies on the image of object 16."
Plan:
1. annotate_image(object_id=16, question="highlight any anomalies")

Example H (referencing a previously shown image):
User: "Annotate the previous image for advertisement board defects."
Context: prior images list ends with /inspection/images/14.jpg.
Plan:
1. annotate_image(image_url="/inspection/images/14.jpg", question="highlight advertisement board defects")
Note: "the previous image" means the LAST url in the prior-images list — do NOT sample a fresh category image.

Example I (count distinct objects, not detections):
User: "How many lights were detected?"
Plan:
1. get_objects_by_category(category="Lights")
(Use get_objects_by_category because the user wants the number of unique light objects. Use get_detection_counts_by_category only for explicit per-frame detection questions like "how many times were lights seen".)

## Rules

- In each round, call every tool you need based on what you already know. You may (and should) return MULTIPLE tool calls in one response whenever the user's question requires more than one piece of data — for example coordinates AND nearby objects, or images AND a count, or anomalies AND the report. Combine tools freely.
- This is a multi-round router: after the backend executes your tool calls, it will show you the results and give you another chance to call more tools if you still need information. Up to {max_rounds} rounds are allowed. If the results from the current round are enough, stop — return no tool calls. Only call additional tools when you genuinely need their output.
- A final pass runs AFTER your tools finish and decides which objects/coordinates to show in the Rerun viewer based on the results and the user's question. You do NOT need to call highlight_in_rerun for ordinary coordinate questions - the final pass highlights them. Only call highlight_in_rerun for explicit, specific 3D-highlight requests that name particular objects (e.g. 'highlight objects 16 and 19 in the 3D viewer').
- A previously answered question does NOT substitute for a fresh tool call when the parameters differ. The user's previous question and your previous answer are NOT a source of truth — only fresh tool calls are. If the user repeats or refines a question with DIFFERENT parameters (a different radius_m, time window, category, object id, coordinates, limit, target_category, other_categories, n, inspection_id, etc.), you MUST call the relevant tool again with the new parameters. When in doubt whether the parameters match, call the tool.
- Use the "Previously called tools" list (when provided) to compare the user's new parameters against the arguments used before. If any required argument changed, re-call the tool with the new value.
- Only call get_category_objects_coordinates for categories explicitly named by the user or for the reference category in a proximity question. Never call it for all categories at once.
- For proximity questions, pass all other known categories as other_categories unless the user names a specific subset.
- For questions about how many objects are in a specific category (e.g. "how many exit signs were found", "how many lights were detected"), use get_objects_by_category(category). It returns distinct objects, not per-frame detections. Use get_detection_counts_by_category ONLY when the user explicitly asks for per-frame detection counts such as "how many times was it seen".
- For time ranges, use 24-hour clock strings. If the user says a bare time like "4:51", assume PM because inspections run in the late afternoon.
- When the user asks about something happening "at" or "around" a bare time (e.g. "what did the camera see at 4:53", "show me detections around 4:53"), use a one-minute window from that minute to the next minute, NOT a 10-second window.
- Use the exact category names listed above.
- DO NOT use run_sql_query for ordinary object, category, count, coordinate, temporal, proximity, or image questions. run_sql_query is ONLY for questions that genuinely cannot be answered by the tools above. Prefer structured tools; they return correctly formatted results.
- When the user asks about anomalies, findings, issues, problems, defects, state changes, or recommendations, call get_anomaly_summary / get_anomalies and/or get_report_summary. Do not call coordinate tools for pure anomaly questions.
- CRITICAL image rule: if the user says 'show me', 'display', 'see', 'with images', 'pictures', 'frames', or asks for visual examples of objects, ALWAYS call an image-returning tool. The image-returning tools are: get_category_sample_images, get_category_objects_with_images, get_object_image_paths, get_images_in_time_range, get_category_proximity_with_images, annotate_image, and get_anomalies. NOTE: annotate_image draws boxes ON an image - use it ONLY for explicit image-annotation requests (annotate / draw on / circle on / mark on the image); for generic 'show me'/'display' use the non-annotating image tools. Generic 'highlight'/'visualize'/'where is' is a 3D Rerun viewer request, not image annotation. Do NOT answer with counts or coordinates only when the user asked to see images.
- For 'show me X near Y within R meters' queries, use get_category_proximity_with_images so the nearby objects include sample frames.
- For 'show me X at time T' queries, use get_images_in_time_range (optionally with category) to return actual frames.
- Do not output explanatory text; only emit tool calls.
"""

    @staticmethod
    def _extract_image_urls(text: str) -> list[str]:
        """Return image URLs found in a text turn, in order of appearance."""
        urls: list[str] = []
        for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
            urls.append(match.group(1))
        for match in re.finditer(r"((?:/(?:inspection|annotated)/images/|/reports/(?:extracted_)?images/)[^\s\)\"]+)", text):
            if match.group(1) not in urls:
                urls.append(match.group(1))
        return urls

    @staticmethod
    def _prior_image_urls(chat_history: Sequence[tuple[str, str]] | None) -> list[str]:
        """Ordered, de-duplicated list of image URLs previously shown to the user."""
        if not chat_history:
            return []
        urls: list[str] = []
        for _, assistant_text in chat_history:
            if not assistant_text:
                continue
            for url in ToolRouter._extract_image_urls(assistant_text):
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _image_context_from_history(chat_history: Sequence[tuple[str, str]] | None) -> str:
        """Build a numbered list of prior image URLs for the router prompt.

        The router LLM uses this list to resolve natural-language image references
        ("the previous image", "the second one", "the one with the backpack") to a
        concrete image_url, replacing brittle regex matching.
        """
        urls = ToolRouter._prior_image_urls(chat_history)
        if not urls:
            return ""
        numbered = "\n".join(f"  {i + 1}. {url}" for i, url in enumerate(urls[-8:]))
        return (
            "\n\nPrior images shown in this conversation (most recent last, numbered):"
            f"\n{numbered}\n"
            "When the user asks to annotate/highlight/circle/draw on/mark/outline a previously "
            "shown image (\"the previous image\", \"the last image\", \"that image\", \"this image\", "
            "\"the second image\", \"image 3\", \"the one with the backpack\", etc.), call "
            "annotate_image with image_url set to the EXACT matching URL from the list above "
            "(verbatim, full path). \"the previous/last image\" = the last URL in the list; "
            "\"the first image\" = the first; \"the Nth image\" / \"image N\" = the Nth. If the user "
            "describes the image by content, pick the URL that best matches. Never invent a URL "
            "that is not in the list. Only fall back to a category or object_id when the user did "
            "NOT refer to a specific prior image (e.g. \"annotate an advertisement board\")."
        )

    @staticmethod
    def _prior_tool_context(tool_history: Sequence[dict[str, object]] | None) -> str:
        """Build a compact list of previously called tools and their exact arguments.

        Only the tool name + arguments are included (not the outputs): the point is to let
        the router see WHICH parameters were used before so it can detect when the user is
        asking the same kind of question with DIFFERENT parameters and must re-call the tool.
        """
        if not tool_history:
            return ""
        calls: list[str] = []
        for entry in tool_history:
            for call in entry.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name", "unknown")
                args = call.get("args", {})
                try:
                    arg_str = dict(args) if isinstance(args, dict) else args
                except Exception:
                    arg_str = args
                calls.append(f"- {name}({arg_str})")
        if not calls:
            return ""
        recent = calls[-8:]
        return (
            "\n\nPreviously called tools in this conversation (most recent last), with the EXACT "
            "arguments used:\n" + "\n".join(recent)
        )

    @staticmethod
    def _round_context(prior_results: Sequence[str] | None) -> str:
        """Build the in-turn results block appended to the user message.

        This lets the router see what previous tool calls returned so it can decide
        whether to stop or call more tools in the next round.
        """
        if not prior_results:
            return ""
        parts = ["\n\n=== Results from tools already called this turn ==="]
        for i, res in enumerate(prior_results, start=1):
            parts.append(f"--- result {i} ---\n{res}")
        return "\n".join(parts)

    def select_tool(
        self,
        query: str,
        chat_history: Sequence[tuple[str, str]] | None = None,
        tool_history: Sequence[dict[str, object]] | None = None,
        prior_results: Sequence[str] | None = None,
    ) -> list[tuple[str, Any]]:
        """Return a list of (tool_name, args) chosen by the base model.

        If prior_results is non-empty, the model is prompted with those results and
        may return an additional round of tool calls. An empty tool_calls response
        means the model is satisfied and no further tools are needed.
        """
        if not self.settings.tool_router_enabled:
            return []

        q = query.lower()
        prior_images = self._image_context_from_history(chat_history)
        prior_tools = self._prior_tool_context(tool_history)

        # Detect EXPLICIT image-annotation intent up front: the user names the image as
        # the canvas ("annotate", "draw on the image", "circle on the image", ...). We do
        # NOT force a tool here - the router LLM resolves which image (URL / object_id /
        # category) the user means from the prior-images context above, which is far more
        # robust than regex heuristics. `wants_image_annotation` only drives a safety-net
        # fallback below. Generic "highlight"/"visualize"/"show where" is a 3D Rerun
        # request, NOT image annotation, so it must NOT trigger annotate_image here.
        image_annotation_keywords = (
            "annotate", "draw on", "circle on", "mark on", "outline on",
            "box on", "show on the image", "on the image",
        )
        wants_image_annotation = any(kw in q for kw in image_annotation_keywords)

        # Whether to fetch the anomaly report is the router LLM's decision, not a
        # keyword gate. get_report_summary / get_anomaly_* are listed in TOOLS and the
        # system prompt instructs the model to call them for anomaly/finding/problem
        # questions, so the model decides whether the report is relevant.

        user_content = query + self._round_context(prior_results)

        payload = {
            "model": self.settings.tool_router_model,
            "messages": [
                {"role": "system", "content": self._system_prompt(max_rounds=self.settings.tool_router_max_rounds) + prior_images + prior_tools},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "tools": _ollama_tools(),
            "options": {
                "temperature": self.settings.tool_router_temperature,
                "num_ctx": self.settings.tool_router_n_ctx,
            },
        }

        try:
            # 26B models can take a while to load; allow a generous read timeout.
            timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
            with httpx.Client(
                base_url=self.settings.ollama_base_url.rstrip("/"), timeout=timeout
            ) as client:
                resp = client.post("/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                # Expose the raw LLM message so the debug UI can show the
                # tool-calling model's reasoning / tool_calls block.
                self.last_raw_response = data.get("message") or {}
        except httpx.HTTPStatusError as exc:
            logger.warning("Tool router HTTP error: %s %s", exc.response.status_code, exc.response.text[:200])
            return []
        except Exception as exc:
            logger.warning("Tool router request failed: %s", exc)
            return []

        message = data.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            logger.warning("Tool router returned unexpected tool_calls type: %r", tool_calls)
            return []

        valid_names = {t["name"] for t in TOOLS}
        results: list[tuple[str, dict[str, Any]]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            func = call.get("function") or {}
            name = func.get("name")
            args = func.get("arguments") or {}
            if isinstance(name, str) and name in valid_names:
                results.append((name, args))

        # Safety net: if this was clearly an annotation request but the router did not
        # emit an annotate_image call (it blanked or chose a non-annotation tool),
        # synthesize a best-effort annotate_image so the user's request is not lost.
        # The LLM is expected to resolve the image reference normally; this only fires
        # as a last resort and prefers the most recently shown image.
        if wants_image_annotation and not any(name == "annotate_image" for name, _ in results):
            fallback: dict[str, Any] = {"question": query}
            prior_urls = self._prior_image_urls(chat_history)
            if prior_urls:
                fallback["image_url"] = prior_urls[-1]
            else:
                id_match = re.search(r"(?:track|object)\s*#?\s*(\d+)", q)
                if id_match:
                    fallback["object_id"] = int(id_match.group(1))
                else:
                    for alias, canonical in _CATEGORY_ALIASES.items():
                        if alias in q:
                            fallback["category"] = canonical
                            break
            if "image_url" in fallback or "object_id" in fallback or "category" in fallback:
                logger.info(
                    "annotate_image safety-net fallback for annotation query: %r args=%s",
                    query, fallback,
                )
                results.append(("annotate_image", fallback))

        # Image safety net: if the user asked to "show me" / "display" something and the
        # router returned no image-returning tool, force a reasonable image tool so the
        # UI actually displays frames instead of just text.
        image_returning_tools = {
            "annotate_image",
            "get_category_sample_images",
            "get_category_objects_with_images",
            "get_object_image_paths",
            "get_images_in_time_range",
            "get_category_proximity_with_images",
            "get_anomalies",
        }
        image_keywords = (
            "show me", "show us", "display", "see", "with images", "with pictures",
            "pictures", "frames", "images of", "photos of",
        )
        wants_images = any(kw in q for kw in image_keywords)
        if wants_images and not any(name in image_returning_tools for name, _ in results):
            fallback = {}
            # Prefer a category image tool if a category is mentioned.
            mentioned_category: str | None = None
            for alias, canonical in _CATEGORY_ALIASES.items():
                if alias in q:
                    mentioned_category = canonical
                    break
            prior_urls = self._prior_image_urls(chat_history)
            if mentioned_category:
                # Return sample frames for the category (not annotation).
                fallback = {"category": mentioned_category, "limit": 5}
            elif prior_urls and wants_image_annotation:
                # Explicit image-annotation request referencing a prior image; only then
                # do we fall back to annotate_image. A plain "show me the previous image"
                # (no annotation intent) is left for the router to handle rather than
                # forcing boxes to be drawn on it.
                fallback = {"image_url": prior_urls[-1], "question": query}
            if fallback:
                # Decide which tool to use based on the fallback fields.
                if "image_url" in fallback:
                    logger.info("annotate_image safety-net fallback for image query: %r", query)
                    results.append(("annotate_image", fallback))
                else:
                    logger.info(
                        "get_category_objects_with_images safety-net fallback for image query: %r",
                        query,
                    )
                    results.append(("get_category_objects_with_images", fallback))

        logger.info("Tool router selected %s for query: %r", results, query)
        return results

    # ------------------------------------------------------------------
    # Final-pass highlight decision (runs after the tool-calling loop ends)
    # ------------------------------------------------------------------

    # The single tool exposed to the router during the final highlight pass. Its
    # parameters mirror ``highlight_in_rerun`` so the decision can be applied by
    # InspectionDBClient._apply_highlight_decision unchanged.
    _HIGHLIGHT_DECISION_TOOL: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "set_rerun_highlight",
            "description": (
                "Highlight specific objects and/or raw 3D coordinates in the Rerun viewer. "
                "Call this with exactly the objects/coordinates the user would want to SEE IN 3D "
                "given the tool results. If nothing spatial is relevant or the user did not ask "
                "about locations/visualization, call no tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "object_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Object ids to highlight (centroids + 3D bboxes).",
                    },
                    "coordinates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                                "label": {"type": "string"},
                            },
                            "required": ["x", "y", "z"],
                        },
                        "description": "Raw 3D points to highlight.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Highlight every object in this category.",
                    },
                    "keep_existing": {
                        "type": "boolean",
                        "description": "If true, keep previously highlighted objects/points (user explicitly asked to keep/add to them). Default false: clear previous highlights first.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional label for this highlight set.",
                    },
                },
            },
        },
    }

    def decide_highlights(
        self,
        query: str,
        tool_results_text: str,
        chat_history: Sequence[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        """Final-pass highlight decision, run after the tool-calling loop ends.

        Asks the router model to look at the user's question plus the accumulated
        tool results and decide which objects/coordinates (if any) should be shown
        in the Rerun viewer. Returns the ``set_rerun_highlight`` tool-call arguments
        dict, or ``None`` when the model calls no tool / on any failure. Never
        raises (best-effort: a failed decision just means no auto-highlight).
        """
        if not query.strip() or not tool_results_text.strip():
            return None

        system_prompt = (
            "You are the post-tool highlight decider for a subway-station inspection assistant. "
            "The tools already ran and returned the results shown below. Decide which objects or "
            "coordinates from those results should be highlighted in the 3D Rerun viewer the user "
            "watches alongside this chat.\n\n"
            "Default behavior: highlight EVERY object or category that is relevant to the user's "
            "question. The user expects to see the things they asked about in the viewer unless they "
            "explicitly asked not to.\n\n"
            "Rules:\n"
            "1. Call set_rerun_highlight with object_ids and/or coordinates (and optionally "
            "category/label) for ALL spatial objects relevant to the user's question.\n"
            "2. If the user asks about a category, highlight that category. If they ask about objects "
            "near / within / around another category (e.g. 'lights within 2m of advertisement boards'), "
            "highlight BOTH the reference category and the nearby objects/category.\n"
            "3. Prefer exact object_ids when specific objects are returned in the tool results; otherwise "
            "use category.\n"
            "4. Do NOT highlight unrelated context or objects that are only incidental to the question. "
            "Highlight ONLY objects that answer what the user asked.\n"
            "5. Do not call this tool for pure image-annotation requests (annotate / draw on / circle on / "
            "mark on the image) or for off-topic questions.\n"
            "6. Set keep_existing=true ONLY if the user explicitly asked to keep or add to previous "
            "highlights (e.g. 'keep the previous', 'add to the highlights', 'show alongside the "
            "previous'). Otherwise leave it false so previous highlights are cleared first.\n"
            "7. At most one tool call. Output nothing but the tool call.\n\n"
            "Examples:\n"
            "- User: 'how many lights were detected?' -> set_rerun_highlight(category='Lights').\n"
            "- User: 'where are the lights?' -> set_rerun_highlight(category='Lights').\n"
            "- User: 'show me the lights within 2m of advertisement boards' -> "
            "set_rerun_highlight(category='Lights') or include object_ids for both Lights and "
            "Advertisement Board from the tool results.\n"
            "- User: 'show me object 12 in the viewer' -> set_rerun_highlight(object_ids=[12]).\n"
            "- User: 'annotate the previous image' -> NO tool (image annotation, not 3D highlight)."
        )

        history_block = ""
        if chat_history:
            recent = list(chat_history)[-4:]
            lines = []
            for user_text, assistant_text in recent:
                lines.append(f"User: {user_text}")
                lines.append(f"Assistant: {assistant_text[:300]}")
            history_block = "\n\nRecent conversation:\n" + "\n".join(lines)

        user_content = (
            f"User question: {query}"
            f"{history_block}\n\n"
            f"=== Tool results ===\n{tool_results_text}"
        )

        payload = {
            "model": self.settings.tool_router_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "tools": [self._HIGHLIGHT_DECISION_TOOL],
            "options": {
                "temperature": self.settings.tool_router_temperature,
                "num_ctx": self.settings.tool_router_n_ctx,
            },
        }

        try:
            # Same generous read timeout as select_tool: 26B/e4b models on CPU are slow.
            timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
            with httpx.Client(
                base_url=self.settings.ollama_base_url.rstrip("/"), timeout=timeout
            ) as client:
                resp = client.post("/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("decide_highlights request failed: %s", exc)
            return None

        tool_calls = (data.get("message") or {}).get("tool_calls") or []
        if not isinstance(tool_calls, list):
            return None
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            func = call.get("function") or {}
            if func.get("name") == "set_rerun_highlight":
                args = func.get("arguments") or {}
                if isinstance(args, dict) and (
                    args.get("object_ids") or args.get("coordinates") or args.get("category")
                ):
                    return args
        return None