from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Tool definitions used by the LLM router. These map to InspectionDBClient methods.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_summary",
        "description": "Overall object counts and category breakdown for the inspection database. Use ONLY for generic inspection-wide summaries such as 'what did you find' or 'how many objects overall'. Do NOT use for category-specific summaries; use run_sql_query with GROUP BY category instead.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_object_by_track_id",
        "description": "Detailed information, timeline, and image links for a single object by its track ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "integer", "description": "The numeric track/object ID, e.g. 5."}
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "get_objects_by_category",
        "description": "List objects belonging to a category such as Lights, Advertisement Board, or Ticket Gate.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category name, e.g. Lights, Advertisement Board, Ticket Gate.",
                },
                "limit": {"type": "integer", "description": "Maximum objects to return.", "default": 10},
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_top_objects",
        "description": "The largest objects by total point count.",
        "parameters": {
            "type": "object",
            "properties": {"n": {"type": "integer", "description": "Number of objects to return.", "default": 5}},
        },
    },
    {
        "name": "get_recent_objects",
        "description": "The most recently seen objects.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of objects to return.", "default": 5}
            },
        },
    },
    {
        "name": "get_object_timeline",
        "description": "A temporal story for a single track: first/last seen times, duration, and key moments.",
        "parameters": {
            "type": "object",
            "properties": {"track_id": {"type": "integer", "description": "The numeric track/object ID."}},
            "required": ["track_id"],
        },
    },
    {
        "name": "get_object_image_paths",
        "description": "Markdown image links for the frames captured for a single track.",
        "parameters": {
            "type": "object",
            "properties": {"track_id": {"type": "integer", "description": "The numeric track/object ID."}},
            "required": ["track_id"],
        },
    },
    {
        "name": "get_category_timeline",
        "description": "First/last seen timestamps for every object in a category.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category name, e.g. Lights, Advertisement Board.",
                }
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_category_windows",
        "description": "First/last detection windows for one or more categories. Use this when the user asks about multiple categories at once or wants to compare when categories were detected.",
        "parameters": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of category names, e.g. [\"Lights\", \"Ticket Gate\"].",
                }
            },
            "required": ["categories"],
        },
    },
    {
        "name": "get_category_objects_coordinates",
        "description": "The centroid and 3D bounding-box coordinates for every object in a category, ordered by first appearance. Use this when the user asks for X, Y, Z positions or coordinates.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category name, e.g. Ticket Gate.",
                }
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_category_proximity",
        "description": "Counts how many objects from other categories are near objects in a target category. Use this when the user asks whether one category is close to another.",
        "parameters": {
            "type": "object",
            "properties": {
                "target_category": {
                    "type": "string",
                    "description": "Category whose objects are the reference points, e.g. Ticket Gate.",
                },
                "other_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Categories to measure distance to, e.g. [\"Advertisement Board\", \"Lights\"].",
                },
                "radius_m": {
                    "type": "number",
                    "description": "Search radius in meters around each target object centroid.",
                    "default": 2.0,
                },
            },
            "required": ["target_category", "other_categories"],
        },
    },
    {
        "name": "get_inspection_timeline",
        "description": "Full chronological log of every object detected during the inspection.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_temporal_clusters",
        "description": "Time-window clusters showing which objects were seen together (e.g. 10 x Lights and 5 x Advertisement Board at the same moment).",
        "parameters": {
            "type": "object",
            "properties": {
                "window_ms": {
                    "type": "integer",
                    "description": "Milliseconds within which observations are considered part of the same cluster.",
                    "default": 500,
                },
                "top_n": {"type": "integer", "description": "Number of top clusters to return.", "default": 10},
            },
        },
    },
    {
        "name": "get_report_summary",
        "description": "Fetch the inspection report text, including anomaly findings, issues, state changes, and recommendations. Use this when the user asks about anomalies, findings, problems, or what was wrong during the inspection.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_observation_counts_by_category",
        "description": "Per-frame observation counts by category (how many times each category was detected). Use when the user asks about detections, observations, or distinguishes 'how many times was it seen' from 'how many distinct objects exist'.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_objects_in_time_range",
        "description": "Objects whose detection span overlaps a time window. start_time and end_time can be ISO timestamps, clock times such as '16:51:45', or nanosecond integers. Use for 'what happened between X and Y' questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "Start time as ISO datetime, clock time (e.g. '16:51:45'), or ns integer.",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time as ISO datetime, clock time, or ns integer.",
                },
                "limit": {"type": "integer", "description": "Maximum objects to return.", "default": 50},
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "get_observations_in_time_range",
        "description": "Per-frame observations captured within a time window. start_time and end_time can be ISO timestamps, clock times, or nanosecond integers. Use for 'show me detections around X' questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "Start time as ISO datetime, clock time, or ns integer.",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time as ISO datetime, clock time, or ns integer.",
                },
                "limit": {"type": "integer", "description": "Maximum observations to return.", "default": 50},
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "get_objects_near_position",
        "description": "Find objects whose centroid is within radius_m of a 3D point. Use when the user gives a coordinate or asks 'what is near (x, y, z)' or 'what did the camera see 2 meters from here'.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate of the search point."},
                "y": {"type": "number", "description": "Y coordinate of the search point."},
                "z": {"type": "number", "description": "Z coordinate of the search point."},
                "radius_m": {"type": "number", "description": "Search radius in meters.", "default": 2.0},
                "category": {
                    "type": "string",
                    "description": "Optional category filter, e.g. 'Lights'.",
                },
            },
            "required": ["x", "y", "z"],
        },
    },
    {
        "name": "get_category_sample_images",
        "description": "Return a few example image links for a category. Use when the user asks to see examples of a category such as 'show me some advertisement boards'.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category name, e.g. Advertisement Board."},
                "limit": {"type": "integer", "description": "Number of sample images.", "default": 5},
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_inspection_poses",
        "description": "Camera/robot poses recorded during the inspection (one per image). Exposes the inspection_poses table.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Maximum poses to return.", "default": 20}}},
    },
    {
        "name": "get_filtered_objects",
        "description": "Objects or tracks that were filtered out by the merge layer and the reason they were dropped. Exposes the filtered_objects audit table.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Maximum rows to return.", "default": 50}}},
    },
    {
        "name": "get_object_distance",
        "description": "Distance in meters between the centroids of two track IDs. Use when the user asks 'how far apart are track X and track Y'.",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id_a": {"type": "integer"},
                "track_id_b": {"type": "integer"},
            },
            "required": ["track_id_a", "track_id_b"],
        },
    },
    {
        "name": "get_category_bounding_box",
        "description": "Axis-aligned 3D bounding box of all objects in a category (centroid and bbox3d min/max). Use when the user asks for the spatial extent or area a category occupies.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category name, e.g. Ticket Gate."}
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_category_observation_timeline",
        "description": "Per-time-bucket observation counts for a category. Use for 'when were most X seen', 'busiest minute for X', or category activity over time.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category name, e.g. Lights."},
                "bucket_seconds": {"type": "integer", "description": "Bucket size in seconds.", "default": 60},
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_objects_by_category_in_time_range",
        "description": "Objects of a specific category whose detection span overlaps a time window. Use for 'were there any ticket gates after 4:53' or 'lights between X and Y'.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category name."},
                "start_time": {"type": "string", "description": "Start time as ISO, clock time, or ns integer."},
                "end_time": {"type": "string", "description": "End time as ISO, clock time, or ns integer."},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["category", "start_time", "end_time"],
        },
    },
    {
        "name": "get_object_movement",
        "description": "Centroid path of a single track across its observations. Use when the user asks if a track moved, its trajectory, or path.",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "integer"},
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "get_nearest_objects_to_track",
        "description": "Other objects within radius_m of a specific track's centroid. Use for 'what was near track 218'.",
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "integer"},
                "radius_m": {"type": "number", "default": 2.0},
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "get_images_in_time_range",
        "description": "Sample images captured within a time window, optionally filtered by category. Use for 'show me what the camera saw at 4:51'.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": "Start time as ISO, clock time, or ns integer."},
                "end_time": {"type": "string", "description": "End time as ISO, clock time, or ns integer."},
                "category": {"type": "string", "description": "Optional category filter."},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "get_category_cooccurrence",
        "description": "Which categories most often appear together in the same temporal cluster. Use for 'which objects are usually seen together'.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_ms": {"type": "integer", "default": 500},
                "top_n": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "get_objects_in_temporal_cluster",
        "description": "Objects with coordinates detected around a specific time. Use for 'where was the 4:51 PM cluster' or 'what was at time T'.",
        "parameters": {
            "type": "object",
            "properties": {
                "center_time": {"type": "string", "description": "Cluster center time as ISO, clock time, or ns integer."},
                "window_ms": {"type": "integer", "default": 500},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["center_time"],
        },
    },
    {
        "name": "run_sql_query",
        "description": "Execute a read-only SQL SELECT query against the inspection database. Use ONLY when no other tool fits the user's question. The query must start with SELECT.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A valid SELECT query."},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["query"],
        },
    },
    {
        "name": "annotate_image",
        "description": "Analyze and draw annotations (boxes, circles, highlights) on an inspection image to mark anomalies or areas of interest. Provide exactly one of image_url, track_id, or category. Use when the user asks to highlight, circle, draw, mark, annotate, or point out anomalies in an image.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "URL or path to the image, e.g. /inspection/images/1781168192465731000.jpg or /reports/images/anomaly_001.jpg.",
                },
                "track_id": {
                    "type": "integer",
                    "description": "Numeric track/object ID. The first available frame for this track will be annotated.",
                },
                "category": {
                    "type": "string",
                    "description": "Category name. A sample image of this category will be annotated.",
                },
                "question": {
                    "type": "string",
                    "description": "What to look for or how to annotate. Defaults to the user's original question.",
                },
            },
        },
    },
    {
        "name": "get_categories",
        "description": "Return the list of distinct object categories in the database. Use this when you need to know what categories exist before writing a SQL query.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "query_database",
        "description": "Execute a read-only SQL SELECT query against the inspection database. This is the primary tool for answering object, coordinate, temporal, and counting questions. Use get_categories first if you are unsure of category names.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql_query": {"type": "string", "description": "A valid read-only SELECT SQL query."},
                "limit": {"type": "integer", "default": 100, "description": "Maximum rows to return."},
            },
            "required": ["sql_query"],
        },
    },
]

# Reduced tool set for testing a SQL-first, minimal-tool assistant.
# Old tools remain in TOOLS for backward compatibility but are not exposed to the LLM.
REDUCED_TOOLS: list[dict[str, Any]] = [
    next(t for t in TOOLS if t["name"] == "get_categories"),
    next(t for t in TOOLS if t["name"] == "query_database"),
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

    This is the first of two requests to the base model: the model chooses tools,
    the backend executes them, and then the base model is called again to produce
    the final answer.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_raw_response: dict[str, Any] | None = None

    def _system_prompt(self) -> str:
        return """\\
You are a tool planner for an MTR subway station inspection assistant.
Your ONLY job is to select the right tools from the list below to gather the data needed to answer the user's question.
Do not answer the user directly. Do not output any text other than the tool calls.

## Database overview

- `objects` table: one aggregated row per distinct tracked object. Columns include `track_id`, `category`, `centroid_x/y/z`, `bbox3d_min/max`, `first_seen_ns`, `last_seen_ns`, `observation_count`, `total_point_count`.
- `observations` table: one row per per-frame detection. Columns include `timestamp_ns`, `track_id`, `category`, `image_path`, `centroid`, `bbox`, `point_count`.
- `inspection_poses` table: camera/robot pose per image (currently empty in this dataset).
- `filtered_objects` table: audit of tracks dropped by the merge/filter layer.

All timestamps are nanoseconds since epoch. Time tools accept ISO datetimes, clock times such as "16:51:45" or "4:51 PM", or raw nanosecond integers.

Known category names (use these exact strings in arguments):
Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign.

## Tool reference

1. get_summary
   - Purpose: high-level overview of the inspection. Use ONLY for generic inspection-wide summaries such as "what did you find" or "how many objects overall". Do NOT use for category-specific summaries.
   - Args: none.
   - Output sample:
       Total aggregated objects: 345
       Objects by category:
       - Lights: 235
       - Advertisement Board: 53
       - Ticket Gate: 29
   - Use for: "what did you find", "how many objects overall".
   - Example: User: "What did the robot detect?" -> get_summary

2. get_observation_counts_by_category
   - Purpose: per-frame detection counts per category.
   - Args: none.
   - Output sample:
       Per-frame observation counts by category:
       - Lights: 881
       - Advertisement Board: 408
       - Ticket Gate: 147
   - Use for: "how many times was X seen", "observations", "detections".
   - Example: User: "How many times were advertisement boards seen?" -> get_observation_counts_by_category

3. get_objects_by_category
   - Purpose: list example objects in a category.
   - Args: category (string, required), limit (integer, default 10).
   - Output sample:
       Found 10 object(s) in category 'Advertisement Board':
       - Track 6: 19 observations, 2451 points, centroid (1.55, 19.73, 1.24)
   - Use for: "tell me about the lights".
   - Example: User: "Tell me about the ticket gates." -> get_objects_by_category(category="Ticket Gate", limit=10)

4. get_top_objects
   - Purpose: largest objects by total point count.
   - Args: n (integer, default 5).
   - Output sample:
       Top 5 objects by total point count:
       - Track 383 (TV): 15321 points, 105 observations
   - Use for: "largest", "biggest", "most points".
   - Example: User: "What were the biggest objects?" -> get_top_objects(n=5)

5. get_recent_objects
   - Purpose: most recently seen objects.
   - Args: limit (integer, default 5).
   - Output sample:
       5 most recently seen object(s):
       - Track 1455 (Ticket Gate): last seen at 2026-06-11 16:53:46.877
   - Use for: "what was seen last", "recent objects".
   - Example: User: "What did the robot see most recently?" -> get_recent_objects(limit=5)

6. get_object_by_track_id
   - Purpose: details for one specific object.
   - Args: track_id (integer, required).
   - Output sample:
       Track ID: 218
       Category: Ticket Gate
       Observations: 12
       Total points: 1847
       Centroid: (-18.22, 32.17, -6.85)
   - Use when the user mentions a track or object ID.
   - Example: User: "Tell me about track 218." -> get_object_by_track_id(track_id=218)

7. get_object_timeline
   - Purpose: chronological observations for one track.
   - Args: track_id (integer, required).
   - Output sample:
       Track 218 (Ticket Gate) timeline:
       - First seen: 2026-06-11 16:51:27.387
       - Last seen: 2026-06-11 16:51:29.187
       - Observations: 12
       - Visible for about 1.8 seconds
   - Use for: "when was track X seen".
   - Example: User: "When was track 218 detected?" -> get_object_timeline(track_id=218)

8. get_object_image_paths
   - Purpose: image links for one track.
   - Args: track_id (integer, required).
   - Output sample:
       Showing 5 of 8 frames for track 218 (Ticket Gate):
       ![Track 218 frame](/inspection/images/1781167925584555000.jpg)
   - Use for: "show me images of track X".
   - Example: User: "Show me images of track 218." -> get_object_image_paths(track_id=218)

9. get_category_timeline
   - Purpose: first/last seen timestamps for every object in a category.
   - Args: category (string, required).
   - Output sample:
       Timeline for category 'Lights' (235 object(s)):
       - Track 1: first seen 2026-06-11 16:50:40.790, last seen ..., 4 observations over 1.2 seconds
   - Use for: "when were the lights detected".
   - Example: User: "When were the lights first seen?" -> get_category_timeline(category="Lights")

10. get_category_windows
    - Purpose: first/last detection windows for multiple categories.
    - Args: categories (list of strings, required).
    - Output sample:
        Detection windows for 2 category/categories:
        - Lights: first seen ..., last seen ..., 235 distinct object(s)
        - Ticket Gate: first seen ..., last seen ..., 29 distinct object(s)
    - Use for comparing multiple categories at once.
    - Example: User: "When were lights and ticket gates detected?" -> get_category_windows(categories=["Lights", "Ticket Gate"])

11. get_category_objects_coordinates
    - Purpose: centroid and 3D bounding box for every object in a category, ordered by first appearance.
    - Args: category (string, required).
    - Output sample:
        Objects in category 'Ticket Gate' with coordinates (ordered by first appearance):
        - Track 218: first seen 2026-06-11 16:51:27.387, centroid (-18.22, 32.17, -6.85), bbox3d min ..., max ...
    - Use ONLY when the user asks for coordinates/positions of a SPECIFIC category. Do not call for every category in broad "all objects" questions.
    - Example: User: "What are the coordinates of the ticket gates?" -> get_category_objects_coordinates(category="Ticket Gate")

12. get_category_proximity
    - Purpose: count objects from other categories near each object in a target category. Also returns target centroids.
    - Args: target_category (string, required), other_categories (list of strings, required), radius_m (number, default 2.0).
    - Output sample:
        Proximity summary for 'Ticket Gate' within 2.0 m of Lights, Advertisement Board:
        Total nearby objects: 45 x Lights, 12 x Advertisement Board
        Per-target breakdown:
        - Track 218 at (-18.22, 32.17, -6.85): within 2.0 m — 2 x Lights, 1 x Advertisement Board
    - Use whenever the user asks what is near/close to/around a category.
    - Pass ALL other known categories as other_categories unless the user specifies a subset.
    - Example: User: "Which objects are close to ticket gates?" -> get_category_proximity(target_category="Ticket Gate", other_categories=["Lights", "Advertisement Board", "Map", "TV", "Exit Sign"])

13. get_inspection_timeline
    - Purpose: chronological log of every object detected.
    - Args: none.
    - Output sample:
        Full inspection timeline: 345 objects from ... to ... (8.5 minutes).
        Chronological object log:
        - 2026-06-11 16:50:40.790: Track 1 (Advertisement Board), 4 observations
    - Use for: "walk me through the inspection".
    - Example: User: "Walk me through the inspection." -> get_inspection_timeline

14. get_temporal_clusters
    - Purpose: busiest moments grouped by time window.
    - Args: window_ms (integer, default 500), top_n (integer, default 10).
    - Output sample:
        Top 10 busiest moments (observations grouped within 500 ms):
        1. 2026-06-11 16:51:45.385 to 16:51:50.485: 106 total observations — 106 x Lights
    - Use for: "were objects seen in groups", "busiest moment".
    - Example: User: "Were objects seen in groups?" -> get_temporal_clusters(window_ms=500, top_n=10)

15. get_report_summary
    - Purpose: fetch anomaly report text and image references. This is the ONLY tool that returns anomaly findings.
    - Args: none.
    - Output sample:
        --- final_summary.txt ---
        === FINAL INSPECTION SUMMARY ===
        Five images contained anomalies...
    - Use when the user asks about anomalies, findings, issues, problems, state changes, recommendations, or what was wrong during the inspection.
    - Example: User: "What anomalies did you find?" -> get_report_summary

16. get_objects_in_time_range
    - Purpose: objects whose detection span overlaps a time window.
    - Args: start_time (string or integer, required), end_time (string or integer, required), limit (integer, default 50).
    - Time strings: ISO datetime, clock time, or ns integer. The inspection ran around 4:50 PM local time, so prefer 24-hour clock (e.g. "16:51:00").
    - Output sample:
        12 object(s) detected between 2026-06-11 16:51:00.000 and 2026-06-11 16:52:00.000:
        - Track 218 (Ticket Gate): 12 observations, centroid (-18.22, 32.17, -6.85)
    - Use for: "what happened between X and Y".
    - Example: User: "What happened between 4:51 and 4:52?" -> get_objects_in_time_range(start_time="16:51:00", end_time="16:52:00")

17. get_observations_in_time_range
    - Purpose: per-frame observations captured within a time window.
    - Args: start_time (string or integer, required), end_time (string or integer, required), limit (integer, default 50).
    - Output sample:
        50 observation(s) between 2026-06-11 16:53:40.000 and 2026-06-11 16:53:50.000:
        - 2026-06-11 16:53:40.877: Track 1455 (Ticket Gate), 96 points at (-15.99, 42.53, -6.18)
    - Use for: "show me detections around time T".
    - Example: User: "Show me detections around 16:53:45." -> get_observations_in_time_range(start_time="16:53:40", end_time="16:53:50")

18. get_objects_near_position
    - Purpose: objects within a radius of a 3D point.
    - Args: x, y, z (numbers, required), radius_m (number, default 2.0), category (string, optional).
    - Output sample:
        16 object(s) within 2.0 m of (-18.00, 32.00, -6.00):
        - Track 218 (Ticket Gate): distance 0.89 m, centroid (-18.22, 32.17, -6.85)
    - Use when the user gives coordinates or asks "what is near (x, y, z)".
    - Example: User: "What is near coordinate (-18, 32, -6)?" -> get_objects_near_position(x=-18, y=32, z=-6, radius_m=2.0)

19. get_category_sample_images
    - Purpose: a few example image links for a category.
    - Args: category (string, required), limit (integer, default 5).
    - Output sample:
        Sample images for category 'Advertisement Board':
        ![Advertisement Board sample](/inspection/images/1781167935783813000.jpg)
    - Use when the user asks to see examples of a category.
    - Example: User: "Show me some advertisement boards." -> get_category_sample_images(category="Advertisement Board")

20. get_inspection_poses
    - Purpose: camera/robot pose records from the inspection_poses table.
    - Args: limit (integer, default 20).
    - Output sample:
        First 20 inspection pose(s):
        - camera_001.jpg: translation (...), rotation (..., ...)
    - Use when the user asks about camera trajectory, robot poses, or inspection poses.
    - Example: User: "What poses did the camera have?" -> get_inspection_poses(limit=20)

21. get_filtered_objects
    - Purpose: audit of objects dropped by the merge layer.
    - Args: limit (integer, default 50).
    - Output sample:
        Most recent 5 filtered object(s):
        - Track 999 (Lights): reason='too few points', 12 points, ...
    - Use when the user asks what was removed/filtered/dropped.
    - Example: User: "What was filtered out?" -> get_filtered_objects(limit=50)

22. get_object_distance
    - Purpose: distance between the centroids of two tracks.
    - Args: track_id_a, track_id_b (integers, required).
    - Output sample:
        Distance between Track 218 (Ticket Gate) and Track 165 (Ticket Gate): 0.26 m
    - Use when the user asks "how far apart are track X and track Y".
    - Example: User: "How far apart are track 218 and track 165?" -> get_object_distance(track_id_a=218, track_id_b=165)

23. get_category_bounding_box
    - Purpose: spatial extent of all objects in a category.
    - Args: category (string, required).
    - Output sample:
        Spatial extent for category 'Ticket Gate' (29 object(s)):
        - Centroid range: x [-18.50, -15.20], y [30.10, 45.30], z [-7.10, -5.90]
        - Bounding box min: ..., max: ...
    - Use when the user asks "what area does X occupy" or spatial extent.
    - Example: User: "What area do the ticket gates occupy?" -> get_category_bounding_box(category="Ticket Gate")

24. get_category_observation_timeline
    - Purpose: per-time-bucket observation counts for a category.
    - Args: category (string, required), bucket_seconds (integer, default 60).
    - Use for: "when were most lights seen", "busiest minute for ticket gates".
    - Example: User: "When were most advertisement boards seen?" -> get_category_observation_timeline(category="Advertisement Board", bucket_seconds=60)

25. get_objects_by_category_in_time_range
    - Purpose: objects of one category detected in a time window.
    - Args: category, start_time, end_time (required), limit (integer, default 50).
    - Use for: "were there ticket gates after 4:53", "lights between 4:51 and 4:52".
    - Example: User: "Show me ticket gates after 4:53." -> get_objects_by_category_in_time_range(category="Ticket Gate", start_time="16:53:00", end_time="16:54:00")

26. get_object_movement
    - Purpose: centroid path of a track across its observations.
    - Args: track_id (integer, required).
    - Use for: "did track 218 move", "what was the path of track 218".
    - Example: User: "Did track 218 move?" -> get_object_movement(track_id=218)

27. get_nearest_objects_to_track
    - Purpose: objects within radius_m of a specific track's centroid.
    - Args: track_id (integer, required), radius_m (number, default 2.0).
    - Use for: "what was near track 218".
    - Example: User: "What was near track 218?" -> get_nearest_objects_to_track(track_id=218, radius_m=2.0)

28. get_images_in_time_range
    - Purpose: sample images captured in a time window.
    - Args: start_time, end_time (required), category (optional), limit (integer, default 5).
    - Use for: "show me what the camera saw at 4:51".
    - Example: User: "Show me images from 4:51 to 4:52." -> get_images_in_time_range(start_time="16:51:00", end_time="16:52:00")

29. get_category_cooccurrence
    - Purpose: which categories appear together most often in temporal clusters.
    - Args: window_ms (integer, default 500), top_n (integer, default 10).
    - Use for: "which objects are usually seen together".
    - Example: User: "Which categories were seen together?" -> get_category_cooccurrence(window_ms=500, top_n=10)

30. get_objects_in_temporal_cluster
    - Purpose: objects with coordinates detected around a specific time.
    - Args: center_time (required), window_ms (integer, default 500), limit (integer, default 50).
    - Use for: "where was the 4:51 PM cluster", "what was detected at 16:51:45".
    - Example: User: "Where was the cluster at 4:51 PM?" -> get_objects_in_temporal_cluster(center_time="16:51:45", window_ms=5000)

31. run_sql_query
    - Purpose: execute a read-only SELECT query. Preferred for summarization, aggregation, and any question that combines counts, timestamps, and coordinates for one or more categories.
    - Args: query (string, required), limit (integer, default 100).
    - Use for:
      - category-specific summaries (e.g., "summary of lights and advertisement boards") -> aggregate with GROUP BY category
      - counts + coordinates together -> SELECT category, COUNT(*), AVG(centroid_x), AVG(centroid_y), AVG(centroid_z), MIN(first_seen_ns), MAX(last_seen_ns) FROM objects WHERE category IN (...) GROUP BY category
      - ad-hoc questions that cannot be answered by the tools above.
    - Example: User: "How many distinct tracks have more than 50 observations?" -> run_sql_query(query="SELECT COUNT(*) FROM objects WHERE observation_count > 50")
    - Example: User: "Tell me about all the lights and advertisement boards, their timestamps and coordinates." -> run_sql_query(query="SELECT category, COUNT(*) as object_count, MIN(first_seen_ns) as first_seen, MAX(last_seen_ns) as last_seen, AVG(centroid_x) as avg_x, AVG(centroid_y) as avg_y, AVG(centroid_z) as avg_z, MIN(centroid_x) as min_x, MAX(centroid_x) as max_x, MIN(centroid_y) as min_y, MAX(centroid_y) as max_y, MIN(centroid_z) as min_z, MAX(centroid_z) as max_z FROM objects WHERE category IN ('Lights', 'Advertisement Board') GROUP BY category")

32. annotate_image
    - Purpose: analyze an inspection image with a vision model and draw annotations (boxes, circles, highlights) around anomalies or areas of interest.
    - Args: provide exactly one of image_url, track_id, or category. Optionally pass question to guide what to look for.
    - image_url: a URL or path such as /inspection/images/1781168192465731000.jpg or /reports/images/anomaly_001.jpg.
    - track_id: use the first available frame for this track.
    - category: use a random sample image of this category.
    - Output sample:
        Annotated image:
        ![annotated](/annotated/images/a1b2c3d4.png)

        Description: A small crack was highlighted on the left panel of the advertisement board.
    - Use for: "highlight anomalies in this image", "circle the defect on track 218", "draw on an advertisement board image", "mark what is wrong with this picture".
    - Example: User: "Circle the anomaly on the image of track 218." -> annotate_image(track_id=218, question="circle the anomaly")
    - Example: User: "Highlight defects on an advertisement board." -> annotate_image(category="Advertisement Board", question="highlight defects")

## Multi-tool flow examples

Example A:
User: "Tell me about the objects found during the inspection and their coordinates, and which objects were close to ticket gates."
Plan:
1. get_summary
2. get_category_objects_coordinates(category="Ticket Gate")
3. get_category_proximity(target_category="Ticket Gate", other_categories=["Lights", "Advertisement Board", "Map", "TV", "Exit Sign"])

Example B:
User: "How many times were advertisement boards seen, and what is near coordinate (-18, 32, -6)?"
Plan:
1. get_observation_counts_by_category
2. get_objects_near_position(x=-18, y=32, z=-6, radius_m=2.0)

Example C:
User: "Show me some advertisement boards and how far apart are tracks 218 and 165."
Plan:
1. get_category_sample_images(category="Advertisement Board", limit=5)
2. get_object_distance(track_id_a=218, track_id_b=165)

Example D:
User: "What anomalies did you find, and how many objects were detected?"
Plan:
1. get_summary
2. get_report_summary

Example E:
User: "Where was the cluster at 4:51 PM?"
Plan:
1. get_objects_in_temporal_cluster(center_time="16:51:45", window_ms=5000)

Example F:
User: "Give me a summary of all the lights and advertisement boards found."
Plan:
1. run_sql_query(query="SELECT category, COUNT(*) as object_count, MIN(first_seen_ns) as first_seen, MAX(last_seen_ns) as last_seen, AVG(centroid_x) as avg_x, AVG(centroid_y) as avg_y, AVG(centroid_z) as avg_z, MIN(centroid_x) as min_x, MAX(centroid_x) as max_x, MIN(centroid_y) as min_y, MAX(centroid_y) as max_y, MIN(centroid_z) as min_z, MAX(centroid_z) as max_z FROM objects WHERE category IN ('Lights', 'Advertisement Board') GROUP BY category")

Example G:
User: "Highlight any anomalies on the image of track 218."
Plan:
1. annotate_image(track_id=218, question="highlight any anomalies")

## Rules

- Call every tool that is needed in a single turn. Do not chain sequentially.
- Only call get_category_objects_coordinates for categories explicitly named by the user or for the reference category in a proximity question. Never call it for all categories at once.
- For proximity questions, pass all other known categories as other_categories unless the user names a specific subset.
- For time ranges, use 24-hour clock strings. If the user says a bare time like "4:51", assume PM because the inspection ran around 4:50 PM.
- Use the exact category names listed above.
- When the user asks for a summary of specific categories (e.g., "summary of lights and advertisement boards", "tell me about the lights and advertisement boards"), do NOT call get_summary. Use run_sql_query with GROUP BY category that returns COUNT, MIN/MAX first_seen_ns/last_seen_ns, and AVG/MIN/MAX centroid coordinates.
- When the user asks about anomalies, findings, issues, problems, state changes, recommendations, or what was wrong, call get_report_summary. Do not call coordinate tools for anomaly questions.
- Do not output explanatory text; only emit tool calls.
"""

    def select_tool(self, query: str) -> list[tuple[str, Any]]:
        """Return a list of (tool_name, args) chosen by the base model."""
        if not self.settings.tool_router_enabled:
            return []

        q = query.lower()

        # Annotation requests take priority when the user asks to draw/highlight/mark an image,
        # and we can identify which image (URL, track, or category) to annotate.
        annotation_keywords = (
            "highlight", "circle", "draw", "mark", "annotate", "outline", "point out",
        )
        wants_annotation = any(kw in q for kw in annotation_keywords)
        if wants_annotation:
            args: dict[str, Any] = {}
            track_match = re.search(r"(?:track|object)\s*#?\s*(\d+)", q)
            if track_match:
                args["track_id"] = int(track_match.group(1))
            else:
                for alias, canonical in {
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
                }.items():
                    if alias in q:
                        args["category"] = canonical
                        break
            url_match = re.search(r"(/[^\s]+\.(?:jpg|jpeg|png))", q, re.IGNORECASE)
            if url_match:
                args["image_url"] = url_match.group(1)
            if args:
                args["question"] = query
                logger.info("Forcing annotate_image for annotation query: %r args=%s", query, args)
                self.last_raw_response = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "annotate_image", "arguments": args}}
                    ],
                }
                return [("annotate_image", args)]

        anomaly_keywords = (
            "anomaly", "anomalies", "finding", "findings", "issue", "issues",
            "problem", "problems", "wrong", "unusual", "recommendation", "recommendations",
            "missing object", "missing objects", "foreign object", "foreign objects",
            "state change", "state changes", "defect", "defects", "damage", "damaged",
        )
        if any(kw in q for kw in anomaly_keywords):
            logger.info("Forcing get_report_summary for anomaly query: %r", query)
            self.last_raw_response = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_report_summary", "arguments": {}}}
                ],
            }
            return [("get_report_summary", {})]

        payload = {
            "model": self.settings.tool_router_model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": query},
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

        logger.info("Tool router selected %s for query: %r", results, query)
        return results
