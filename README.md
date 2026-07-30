# MTR-Insight: Hong Kong MTR Inspection Robot Voice Assistant

A voice-to-voice assistant for Hong Kong MTR subway station inspection. It wraps a local realtime pipeline (STT → LLM → TTS) with live SQLite integration and an LLM tool router, so inspectors can ask spoken questions about objects the automated grounding pipeline detected and get spoken answers with inline images and annotations.

- **STT:** SenseVoice (FunASR, zh/en/ja/ko with emotion tags) by default; Whisper `large-v3` (faster-whisper, EN/zh/yue) via `STT_BACKEND=whisper`
- **LLM:** Ollama with a multimodal Gemma model — used for both chat **and** image annotation; vLLM as an alternative chat provider
- **TTS:** Piper (English + Chinese voices, browser SpeechSynthesis fallback for Cantonese)
- **Router:** one extra LLM call that picks which DB/report/annotation tools to run
- **Backend:** FastAPI + WebSocket · **Frontend:** React + Vite · **DB:** SQLite from the `inspection_grounding` pipeline

---

## Architecture

```
┌──────────────┐   WebSocket (JSON)   ┌──────────────────────────────────────────┐
│  React/Vite  │ ◄──────────────────► │              FastAPI Backend (:8000)      │
└──────┬───────┘                      │                                          │
       │ audio (webm/wav, base64)      │  SenseVoice/Whisper ──► transcript        │
       │                              │        │                                 │
       ▼                              │        ▼                                 │
┌──────────────┐                      │  Tool Router (Ollama tool-calling)       │
│ Browser TTS  │ ◄── audio chunks ─── │        │  selects tools (1st LLM call)     │
│ (fallback)   │                      │        ▼                                 │
└──────────────┘                      │  InspectionDBClient (SQLite) + Report   │
                                      │  + VisionAnnotator (base LLM)            │
                                      │        │  formatted context               │
                                      │        ▼                                 │
                                      │  Answering LLM (2nd LLM call, streams)  │
                                      │        │                                 │
                                      │        ▼                                 │
                                      │  Piper TTS ──► streamed audio chunks   │
                                      └──────────────────────────────────────────┘
```

---

## The full pipeline

Every turn is **two LLM calls** plus streaming TTS — not an agentic loop. The router *plans* tools up front; the answerer *synthesizes* the fetched data. There is no step where the model inspects a tool result and then decides to call another tool in the same turn.

1. **Capture** — Frontend records audio via `MediaRecorder` (Space = push-to-talk), base64-encodes it, sends `{"type":"user_audio", audio_base64, mime_type}` over the WebSocket.
2. **STT** — `VoicePipeline.handle_audio` runs SenseVoice (or Whisper) → returns transcript text, a language tag (`en`/`zh`/`yue`/…), and an emotion tag. Yields a `transcript` message to the client.
3. **Router — LLM call #1 (tool selection)** — `LocalLLM.stream_reply` calls `InspectionDBClient.lookup`, which calls `ToolRouter.select_tool`. The router sends the transcript + a system prompt (DB schema + all ~38 tool descriptions + multi-tool rules + prior-images/prior-tools context) to Ollama with native `tools`. It returns a list of `(tool_name, args)` — possibly several — and nothing else.
4. **Tool execution (multi-round)** — `lookup` runs the selected tools and appends their formatted outputs to a `prior_results` list. It then re-prompts the router with those results, allowing it to call additional tools in subsequent rounds (up to `TOOL_ROUTER_MAX_ROUNDS`). Once the router returns no further tools (or the round limit is reached), all formatted outputs are joined with `\n\n` into one `db_context` string.
5. **Report (conditional)** — If the router selected `get_report_summary`, `get_anomaly_summary`, or `get_anomalies`, `report_needed` is true and `InspectionReportClient.get_context()` (+ anomaly image URLs) is loaded as `report_context`, so the prose report coexists with the structured anomaly-table output. The report is lazy-loaded only on anomaly/findings intent — the router's decision, not a keyword gate.
6. **Rerun auto-highlight / station map overlay** — If the router called `highlight_in_rerun`, a note is prepended to `db_context` telling the answerer the highlights are shown in the Rerun viewer. Otherwise, if the tool results contain coordinates or object ids, `InspectionDBClient.auto_highlight` pushes them to the Rerun viewer automatically (no explicit tool call) and a similar note is prepended. `RerunVisualizer` also logs the pre-extracted downsampled station map as a static `world/map` entity the first time it connects, so highlights appear directly on the station map. See *Rerun 3D highlighting* below.
7. **Debug + image-link handling** — The selected tool calls (and the router's raw response) are sent to the frontend as a `tool_calls` message (debug panel). If `annotate_image` ran, its markdown image links are pulled *out* of `db_context` (so the model can't duplicate them) and the backend re-emits them as the first tokens of the reply to guarantee display.
8. **Answerer — LLM call #2 (synthesis, streamed)** — `_build_messages` assembles:
   - `system`: the assistant persona + style/coordinate/image-link rules
   - `system`: prior tool-call history (image links stripped)
   - `system`: `db_context` (the merged tool outputs)
   - `system`: `report_context` (only when fetched)
   - the last few chat turns (within `llm_history_char_budget`, image links stripped)
   - `user`: the transcript

   The answering LLM streams tokens. ` authDomain` thinking tags are stripped in-flight so only the visible answer streams out.
9. **Streaming TTS** — As tokens accumulate, `handle_audio` splits the text at sentence boundaries, cleans markdown/URLs/punctuation via `_clean_for_tts`, and feeds each sentence to Piper, which yields WAV chunks streamed back to the client as `tts_audio_chunk` messages. Speech therefore begins before the LLM finishes generating.
10. **Playback** — The frontend plays chunks in order via the Web Audio API, renders the full text as a chat card, and inlines any `![...](/...images/...)` links as clickable thumbnails (lightbox). Cantonese with no matching Piper voice falls back to `window.speechSynthesis`.
11. **History** — After the turn completes, the `(transcript, reply)` pair is appended to `conversation_history` (capped 12) and the tool-call payload to `tool_call_history` (capped 6) — both fed back into the next turn so the router can detect changed parameters and reference prior images.

> **Key design point:** tool results reach the answerer as plain-text **system messages**, never as OpenAI-style `tool`/`function` messages. The backend pre-fetches and formats everything; the answerer just reads context and writes the answer.

---

## How multiple tools are chained together

### The mechanism

The router can run for **up to `TOOL_ROUTER_MAX_ROUNDS` rounds per turn** (default 3). In each round, the router returns one or more tool calls; the backend executes them, formats the outputs, and **feeds those results back** to the router. The router then decides whether it has enough information or whether to call additional tools in the next round.

```
transcript ──► ToolRouter.select_tool()  ──►  round 1 tool_calls
                        │
                        ▼
              for name, args in tool_calls:
                  results.append(_execute_tool(name, args))
                        │
                        ▼
              prior_results = formatted outputs from round 1
                        │
                        ▼
              ToolRouter.select_tool(query, prior_results)  ──►  round 2 tool_calls
                        │
                        ▼
              ... repeat until router returns no tools or max rounds ...
                        │
                        ▼
              db_context = "\n\n".join(all_results)
                        │
                        ▼
              [system: db_context]  →  Answering LLM (streams synthesis)
```

Key points:

- **Tools are selected in parallel within a round** — whenever the user's question requires more than one piece of data, return multiple tool calls in one response.
- **Tools can also be selected sequentially across rounds** — if a result is needed to decide the next tool's arguments (e.g. looking up `inspection_id`s before fetching coordinates), the router sees the prior round's output and can issue more calls.
- **The merge is textual.** Each tool's output is independently formatted (`get_summary`→`_format_summary`, `get_category_proximity`→`_format_category_proximity`, …) and concatenated. The answerer synthesizes the merged context in prose.
- **Duplicate calls are deduplicated.** The backend tracks which (tool, args) pairs have already run this turn and skips reruns, preventing infinite loops.
- **Cross-turn context** (re-calling with changed parameters, referencing previously shown images) still relies on `tool_call_history` / `conversation_history` injected into the router prompt.

### Worked example A — counts + coordinates + proximity (three tools, one turn)

> **User:** "Tell me about the inspection objects and their coordinates, and which objects were close to ticket gates."

Router (call #1) returns three tool calls in one response:
```
get_summary {}
get_category_objects_coordinates {category: "Ticket Gate"}
get_category_proximity {target_category: "Ticket Gate",
                        other_categories: ["Lights","Advertisement Board","Map","TV","Exit Sign"]}
```
Backend runs all three, each producing formatted text, and joins them:
```
Inspection database context (...):
Total objects: 60. Objects by category: Lights: 28, Advertisement Board: 14, ...

Objects in category 'Ticket Gate' with coordinates (ordered by first appearance):
- Object 109: first seen ..., centroid (-18.22, 32.17, -6.85), ...

Proximity summary for 'Ticket Gate' within 2.0 m of Lights, Advertisement Board, Map, TV, Exit Sign:
Total nearby objects: 8 x Lights, 3 x Advertisement Board
Per-target breakdown:
- Object 109 at (-18.22, 32.17, -6.85): within 2.0 m — 2 x Lights, 1 x Advertisement Board
```
Answerer (call #2) reads that single context block and synthesizes one coherent spoken answer covering totals, ticket-gate positions, and what's near them.

### Worked example B — anomalies (structured DB + prose report)

> **User:** "What anomalies did you find, and were any near the ticket gates?"

Router returns:
```
get_anomaly_summary {}                                  # anomaly overview
get_anomalies {}                                        # individual abnormalities + image links
get_report_summary {}                                   # prose report + recommendations
get_category_proximity {target_category: "Ticket Gate",
                         other_categories: ["Lights","Advertisement Board", ...]}   # "near ticket gates" part
```
`get_anomaly_summary` / `get_anomalies` read the `anomaly_types` / `abnormal_detections` / `abnormalities` tables and return typed abnormalities with 2D pixel bboxes, notes, and the ground-truth vs inspection image links for each pair. `get_report_summary` returns `None` from `_execute_tool`; its real text is fetched by `llm_service` into `report_context` (loaded because `report_needed` now also triggers on the anomaly tools). So the answerer receives **two** system messages — `db_context` (structured abnormalities + proximity) and `report_context` (prose + `![...](/reports/extracted_images/...)` links) — and weaves them together: "the report found cracks near gate 3; the abnormalities table lists 2 scratches on the ticket-gate panels; around that area there are 8 lights and 3 ad boards within 2 m." (If the anomaly tables are not yet populated, the anomaly tools return a clear "not yet populated" string instead of raising.)

### Worked example C — images + live annotation (vision tool)

> **User:** "Show me what the camera saw at 4:53 PM, and highlight any anomalies on it."

Router returns:
```
get_images_in_time_range {start_time: "16:53:00", end_time: "16:54:00"}
annotate_image {category: "Exit Sign", question: "highlight any anomalies"}   # or object_id, or image_url
```
`get_images_in_time_range` returns markdown links to source frames. `annotate_image` resolves frames (by `category`/`object_id`/`image_url`), sends each to the **base LLM** (multimodal) with a strict-JSON prompt, draws boxes/circles/highlights with OpenCV, writes each annotated PNG to `annotated_image_cache_dir`, and returns `![annotated result](/annotated/images/<hash>.png)` links plus a description. The backend then **strips those annotated links out of `db_context`** and **re-emits them as the first tokens** of the reply, so the image is guaranteed to render even if the model would otherwise paraphrase it away. The answerer only writes a short summary of what the annotation found.

### Worked example B2 — Rerun 3D highlight (alongside the spoken answer)

> **User:** "What are the coordinates of the ticket gates? Show me where they are."

Router returns:
```
get_category_objects_coordinates {category: "Ticket Gate"}
```
`get_category_objects_coordinates` returns the gate centroids + 3D bboxes as text for the answerer to speak. Because those results contain `(x, y, z)` coordinates, the answerer's **auto-highlight** then pushes the gate centroids (bright red points) + 3D bboxes to the Rerun viewer automatically — no `highlight_in_rerun` call needed. (The explicit `highlight_in_rerun` tool is only required for pure-visualization requests that produce no coordinates in the tool output.) Rerun I/O runs on a background thread so the chat turn never blocks; if no viewer is running and `RERUN_AUTO_SPAWN=true`, the backend launches one. Highlights share the grounding scene's app id + leveling frame, so they overlay the grounding map. The answerer cites a short status string ("Highlighted 3 objects in the Rerun viewer (grounding world frame)").

### Worked example D — multi-hop temporal + spatial (one turn, parallel)

> **User:** "Walk me through what happened at 4:51 PM — which objects were seen, where, and which were near each other."

Router returns:
```
get_objects_in_temporal_cluster {center_time: "16:51:45", window_ms: 5000}
get_category_proximity {target_category: "Lights",
                        other_categories: ["Advertisement Board","Ticket Gate","Map","TV","Exit Sign"]}
```
Both run in parallel; the cluster tool already returns object coordinates for the time window, so the answerer can describe *where* the cluster was and *what was near what* in one answer — without a dependent second call.

### Cross-turn: re-calling when a parameter changes

The router is shown the **exact arguments** used in prior tool calls (`tool_call_history`) and is instructed to re-call whenever a required argument differs. A previous answer is **not** reused:

```
Turn 1: "What's within 3 meters of the exit signs?"  → get_category_proximity(..., radius_m=3.0)
Turn 2: "And within 1 meter?"                        → get_category_proximity(..., radius_m=1.0)   # re-called
```
The same applies to changing the category, object id, time window, coordinates, `inspection_id`, or `limit`.

### Cross-turn: referencing a previously shown image

If the user says "annotate the previous image" / "the second one" / "the one with the backpack", the router resolves it from a numbered list of **prior image URLs** (built from `conversation_history`) and calls `annotate_image` with that exact `image_url`. This replaces brittle regex matching. (If the router blanks on an explicit annotation request, a safety-net fallback uses the most recent prior image / an object id / a category.)

### Limitations (be aware)

- **`run_sql_query` / `query_database` are the escape hatch** for anything the fixed tools can't express (aggregations, custom joins, multi-category summaries with `GROUP BY`).
- **Router disabled or returns nothing → no DB context.** With `tool_router_enabled=false` (or an empty tool list), `lookup` returns `None`; the answerer replies from general knowledge/history. There is no keyword fallback.
- **Multi-round depends on the model using prior results.** The backend exposes prior round outputs to the router and asks it to stop when satisfied, but the tool-calling model may still decide everything up front. `TOOL_ROUTER_MAX_ROUNDS` sets the ceiling; the model decides the actual number of rounds used.

---

## Module responsibilities

| File | Role |
|------|------|
| `app/main.py` | FastAPI app, WebSocket `/ws` lifecycle, static image mounts, keeps `conversation_history` (≤12) + `tool_call_history` (≤6), per-turn task cancel/interrupt. REST: `/health`, `/status`, `/voices`, `/reports/image-list`, `/annotate-image`. |
| `app/config.py` | `Settings` (pydantic) from env/`.env`; resolves relative paths against `backend/`. |
| `app/models.py` | Pydantic client/server message models. |
| `app/services/pipeline.py` | `VoicePipeline.handle_audio` — STT → stream LLM → sentence-chunk → TTS → yield `transcript`/`llm_token`/`tts_audio_chunk`/`llm_done`. |
| `app/services/stt_service.py` | `build_stt` → `SenseVoiceSTT` (default, emotion+lang tags) or `WhisperSTT`. Returns `STTResult`. |
| `app/services/tool_router.py` | `ToolRouter.select_tool` — Ollama native tool-calling (call #1). `TOOLS`/`_ollama_tools`. Prior-image + prior-tool context. Annotation safety-net fallback. |
| `app/services/db_service.py` | `InspectionDBClient.lookup` → router → `_execute_tool` per tool → join. All SQL query methods + `_format_*` formatters. `_parse_time_string` for clock/ns/ISO times. `annotate_image` orchestration. |
| `app/services/report_service.py` | `InspectionReportClient` — reads `.txt`/`.pdf` (via `pdftotext`) reports, lists `extracted_images`. Loaded only when `get_report_summary` was selected. |
| `app/services/vision_service.py` | `VisionAnnotator.annotate` — sends image to the **base LLM** (multimodal), strict-JSON parsing with retries, normalizes coords, draws annotations with OpenCV. |
| `app/services/rerun_service.py` | `RerunVisualizer.highlight` — pushes 3D object centroids/bboxes + raw coordinates to a Rerun viewer, sharing the grounding scene's app id + leveling frame. Logs the station map (`world/map`) from a pre-extracted downsampled `.npy` so highlights land on the map. All Rerun I/O on a background daemon thread (never blocks the chat); auto-launches a viewer when `RERUN_AUTO_SPAWN=true`. Called by the `highlight_in_rerun` tool AND by `InspectionDBClient.auto_highlight` (coordinate-bearing answers). |
| `app/services/llm_service.py` | `LocalLLM.stream_reply` (call #2): gather context, `_build_messages`, stream via Ollama/vLLM, strip `thinking`. `preload_model`, `runtime_status`. |
| `app/services/tts_service.py` | `PiperTTS` — voice selection per language, streaming WAV chunks, Python API + CLI fallback. |
| `app/services/runtime_status.py` | `nvidia-smi` VRAM snapshot. |

**Frontend:** `App.jsx` (state, WebSocket, push-to-talk, playback), `useWebSocket`/`useRecorder`/`useAudioPlayback` hooks, `TranscriptCards`/`ChatHistory`/`MarkdownImageText`/`StatusPanel`/`DebugPanel`/`ImageAnnotator`/`ReportImageGallery` components.

---

## WebSocket messages

**Client → server:** `user_audio`, `interrupt`, `set_voice`, `clear_context`, `ping`
**Server → client:** `ready`, `runtime_update`, `transcript` (text/emotion/raw), `tool_calls` (debug: selected tools + router raw), `llm_token` (streaming text), `tts_audio_chunk` (base64 audio + voice/language), `llm_done`, `interrupted`, `error`, `pong`

---

## Tool reference (~38 tools)

Each maps to an `InspectionDBClient` method; the router's system prompt describes each with expected output and example queries. Most tools take an optional `inspection_id` to scope to one inspection — omit it to query across all inspections.

| Tool | Args | Use case |
|------|------|----------|
| `get_inspections` | — | List inspections (id, started_at, is_gt, counts). Call first when you need an `inspection_id`. |
| `get_summary` | `inspection_id?` | "What did you find?", overall counts + category breakdown |
| `get_categories` | — | "What categories exist?" |
| `get_object_by_id` | `object_id` | "Tell me about object 109" |
| `get_objects_by_category` | `category`, `limit?`, `inspection_id?` | "Tell me about the lights" |
| `get_top_objects` | `n?`, `inspection_id?` | "Most-detected objects?" |
| `get_recent_objects` | `limit?`, `inspection_id?` | "Most recently seen?" |
| `get_object_timeline` | `object_id` | "When was object 109 seen?" |
| `get_object_image_paths` | `object_id` | "Show me images of object 109" |
| `get_category_timeline` | `category`, `inspection_id?` | "When were the lights detected?" |
| `get_category_windows` | `categories[]`, `inspection_id?` | "When were lights and gates detected?" |
| `get_category_objects_coordinates` | `category`, `inspection_id?` | "Coordinates of the ticket gates?" |
| `get_category_objects_with_images` | `category`, `limit?`, `inspection_id?` | "Exit signs with IDs, coords, and images" |
| `get_category_proximity` | `target_category`, `other_categories[]`, `radius_m?`, `inspection_id?` | "What's close to ticket gates?" |
| `get_inspection_timeline` | `inspection_id?` | "Walk me through the inspection" |
| `get_temporal_clusters` | `window_ms?`, `top_n?`, `inspection_id?` | "Busiest moments?" |
| `get_category_cooccurrence` | `window_ms?`, `top_n?`, `inspection_id?` | "Which objects appear together?" |
| `get_objects_in_temporal_cluster` | `center_time`, `window_ms?`, `limit?`, `inspection_id?` | "Where was the 4:51 PM cluster?" |
| `get_objects_in_time_range` | `start_time`, `end_time`, `limit?`, `inspection_id?` | "What happened between 4:51 and 4:52?" |
| `get_detections_in_time_range` | `start_time`, `end_time`, `limit?`, `inspection_id?` | "Detections around 16:53:45" |
| `get_objects_by_category_in_time_range` | `category`, `start_time`, `end_time`, `limit?`, `inspection_id?` | "Ticket gates after 4:53?" |
| `get_detection_counts_by_category` | `inspection_id?` | "How many times were lights detected?" |
| `get_category_detection_timeline` | `category`, `bucket_seconds?`, `inspection_id?` | "When were most lights seen?" |
| `get_objects_near_position` | `x`, `y`, `z`, `radius_m?`, `category?`, `inspection_id?` | "What's near (-18, 32, -6)?" |
| `get_nearest_objects_to_object` | `object_id`, `radius_m?`, `inspection_id?` | "What was near object 109?" |
| `get_object_distance` | `object_id_a`, `object_id_b` | "How far apart are objects 109 and 110?" |
| `get_object_movement` | `object_id` | "Did object 109 move?" |
| `get_category_bounding_box` | `category`, `inspection_id?` | "What area do the ticket gates occupy?" |
| `get_images_in_time_range` | `start_time`, `end_time`, `category?`, `limit?`, `inspection_id?` | "What did the camera see at 4:51?" |
| `get_category_sample_images` | `category`, `limit?`, `inspection_id?` | "Show me some advertisement boards" |
| `get_inspection_poses` | `limit?`, `inspection_id?` | "What poses did the camera have?" (reads `images.tf_*`) |
| `get_anomaly_types` | — | "What kinds of anomalies exist?" |
| `get_anomaly_summary` | `inspection_id?` | "How many anomalies were found?" (counts by type / inspection) |
| `get_anomalies` | `anomaly_type?`, `inspection_id?`, `limit?` | "Show me the anomalies" (typed abnormalities + image-pair links) |
| `get_report_summary` | — | "What anomalies did you find?" (loads prose report context) |
| `highlight_in_rerun` | `object_ids[]?`, `coordinates[]?`, `category?`, `inspection_id?`, `label?` | "Highlight objects 16 and 19 in the 3D viewer" (explicit push; coordinate-bearing answers auto-highlight without this tool) |
| `run_sql_query` / `query_database` | `query` (SELECT), `limit` | Escape hatch for ad-hoc/aggregation SQL |
| `annotate_image` | `image_url` XOR `object_id` XOR `category`, `question?`, `limit` | "Highlight anomalies on object 109's image" (base LLM vision) |

Times accept ISO datetimes, clock strings (`"16:51:45"`, `"4:51 PM"`), or nanosecond integers; bare "at 4:53" is read as a one-minute window. The old `get_filtered_objects` tool is gone (the `filtered_objects` table no longer exists); `track_id` is now `object_id` (the `objects.id` column).

---

## Rerun 3D highlighting

When the AI's answer involves coordinates or specific objects, the assistant **automatically** pushes those 3D positions into a [Rerun](https://www.rerun.io) viewer so the inspector can *see* where things are in the station, alongside the spoken answer — no explicit "show me in Rerun" request is needed. Highlights share the **grounding pipeline's** Rerun app id (`inspection_grounding_rerun`) and world frame, so they overlay the grounding map/bboxes rather than appearing in a disconnected scene.

**Auto-highlight (the default behavior).** After the tools run, the answerer scans the tool results for `(x, y, z)` coordinate tuples and `Object N` ids. If it finds any, it pushes them to the Rerun viewer via `InspectionDBClient.auto_highlight` — so any coordinate-bearing answer ("what are the coordinates of the ticket gates", "what's near (-18, 32, -6)", "how far apart are objects 9 and 10") is visualized automatically. The router does **not** have to call a special tool for this. The explicit `highlight_in_rerun` tool still exists for pure-visualization requests that produce no coordinates in the tool output (e.g. "highlight objects 16 and 19 in the 3D viewer"), and to force a highlight by `category`.

**Auto-launch.** If no viewer is reachable on `RERUN_VIEWER_ADDR` and `RERUN_AUTO_SPAWN=true` (default), the backend launches one itself (`rerun --port 9876`, detached) the first time it has something to visualize, then attaches. Set `RERUN_AUTO_SPAWN=false` to require a manually-started `rerun` viewer.

1. (Optional) Start the viewer yourself:
   ```bash
   pip install rerun-sdk        # already in backend/requirements.txt
   rerun                        # listens on 127.0.0.1:9876 by default
   ```
   With `RERUN_AUTO_SPAWN=true` you can skip this — the backend launches a viewer on first highlight.
2. Ask a coordinate/visualization question, e.g. *"What are the coordinates of the ticket gates?"* The router calls `get_category_objects_coordinates`; the answerer's auto-highlight then pushes the gate centroids + 3D bboxes to the viewer.
3. `RerunVisualizer.highlight` does a fast port probe, returns an optimistic status string immediately, and hands the actual Rerun I/O to a **background daemon thread** so the chat turn never blocks on the viewer. The worker attaches to a running viewer via gRPC (`rr.connect_grpc` to `rerun+http://<addr>/proxy`, the rerun 0.35 protocol) or spawns one, sets `world` to `RIGHT_HAND_Z_UP` (matching the grounding bridge), and logs everything as static entities in the chatbot's own recording.
4. **Station map overlay.** The first time the worker connects, it loads the photo-colored global map PCD (`MTR Inspection Database/outputs/colored_map.pcd`) and logs it as a static `world/leveled/camera_init/colored_map` entity. The PCD stores raw `camera_init`-frame point positions and per-point RGBA colors, so the chatbot logs a genuine colored point cloud, not a monochrome cloud. Highlights therefore sit on the full colored station map in the chatbot's own recording.
5. **Frame alignment:** the DB stores object centroids/bboxes in the tilted `camera_init` frame. `RerunVisualizer` pre-rotates every point by the leveling matrix (`RERUN_LEVELING_RPY_DEG`, default `0.0,20.0,0.0` — the 20° pitch used by the 2026-06-11 inspection run, mirroring the grounding `rerun_bridge_node` `leveling_rpy_deg`), so highlights and the station map land level in the grounding world frame — the same convention the bridge uses for `world/bboxes3d`.
6. **Static logging:** the station map, context cloud, trajectory, and highlights are all logged with `static=True` so they render immediately regardless of the viewer's timeline position (logging at a non-zero `set_time_sequence` tick was the cause of the "empty viewer" bug — a fresh viewer scrubbed to time 0 showed nothing).
7. The tool returns a short status string ("Highlighted 3 objects in the Rerun viewer (grounding world frame)") that the answerer cites; the spoken answer still gives the (x, y, z) coordinates.

It is fully tolerant: `RERUN_ENABLED=false`, a missing `rerun-sdk`, or a viewer that cannot be reached or spawned all degrade to a friendly status string — the chat turn never fails because of Rerun. Highlight by `object_ids`, by raw `coordinates` (`{x, y, z, label?}`), or by `category`, optionally scoped to one `inspection_id`.

> **Station map file.** The visualizer reads the colored global map directly from `MTR Inspection Database/outputs/colored_map.pcd`. If the PCD is missing, it simply skips the map overlay (highlights still work). The legacy `RERUN_MAP_POINTS_PATH=./data/station_map.npz` path is no longer used by the visualizer but is retained for compatibility.

---

## Database schema

`MTR Inspection Database/inspection_v2.db` (new multi-inspection schema, written by the `inspection_grounding` pipeline):

- **`categories`** — `id`, `name`. Fixed set: Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign.
- **`inspections`** — `id`, `started_at`, `is_gt`. **Multiple inspections** can coexist; scope tools with `inspection_id`.
- **`images`** — `id`, `inspection_id`, `timestamp_ns`, `tf_translation_x/y/z`, `tf_rotation_x/y/z/w`, `filename`. Camera pose lives here (no separate poses table). `filename` (e.g. `14.jpg`) is served at `/inspection/images/<filename>`.
- **`objects`** — one row per tracked object: `id` (the object id — there is no `track_id`), `category_id` (→ `categories.name`), `centroid_x/y/z`, `min_x/y/z`, `max_x/y/z`, `is_gt`, `created_at`. An object has ONE centroid + 3D bbox; it does **not** store `first_seen`/`last_seen` or a detection count — those are **derived** from `detections`.
- **`detections`** — one row per per-frame detection: `id`, `image_id` (→ `images`), `object_id` (→ `objects`), `centroid_x/y/z`, `min/max_x/y/z`. An object's detection count = `COUNT(detections)`; first/last seen = `MIN/MAX(images.timestamp_ns)` over its detections→images.

Anomaly tables (added by the writer later; the tools no-op cleanly until then):

- **`anomaly_types`** — `id`, `name`.
- **`abnormal_detections`** — `id`, `gt_image` (→ `images.id`), `inspection_image` (→ `images.id`). An abnormal **image pair** (ground truth vs inspection).
- **`abnormalities`** — `id`, `pair` (→ `abnormal_detections.id`), `type` (→ `anomaly_types.id`), `min_x`, `min_y`, `max_x`, `max_y` (2D pixel bbox), `note`.

Derived columns the tools compute: `detection_count` = `COUNT(detections)` per object; `first_seen_ns` / `last_seen_ns` = `MIN/MAX(images.timestamp_ns)` over an object's detections→images. The old `observations`, `inspection_poses`, and `filtered_objects` tables and the `track_id` / `observation_count` / `total_point_count` / `aggregated_pcd_path` / `point_count` / `pcd_path` / `mask_path` columns are gone.

---

## Configuration

Key env vars (see `backend/.env.example` for the full list):

| Var | Default | Purpose |
|-----|---------|---------|
| `LLM_PROVIDER` | `ollama` | `ollama` or `vllm` (chat) |
| `LLM_MODEL_NAME` | `gemma4:26b` | Base chat model — must be **multimodal** (also used for annotation) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for chat + annotation |
| `TOOL_ROUTER_ENABLED` | `true` | Router as sole DB gatekeeper |
| `TOOL_ROUTER_MODEL` | `gemma4:26b` | Model for tool selection (call #1) |
| `STT_BACKEND` | `sensevoice` | `sensevoice` or `whisper` |
| `VISION_*` | — | `VISION_MAX_TOKENS`, `VISION_TEMPERATURE`, `VISION_REQUEST_TIMEOUT_S` — tune the annotation task only |
| `INSPECTION_DB_PATH` / `INSPECTION_IMAGE_DIR` | `../MTR Inspection Database/inspection_v2.db` / `../MTR Inspection Database/outputs/images` | SQLite DB + source camera frames |
| `RERUN_ENABLED` / `RERUN_VIEWER_ADDR` | `true` / `127.0.0.1:9876` | Push 3D highlights to a running `rerun` viewer over TCP |
| `RERUN_APP_ID` / `RERUN_LEVELING_RPY_DEG` | `inspection_grounding_rerun` / `0.0,20.0,0.0` | Match the grounding scene's app id + leveling rotation so highlights overlay the grounding map |
| `RERUN_AUTO_SPAWN` | `true` | If no viewer is reachable, launch one on first highlight (else require a manually-started `rerun`) |
| `RERUN_MAP_ENABLED` / `RERUN_MAP_PCD_PATH` | `true` / `../MTR Inspection Database/outputs/colored_map.pcd` | Overlay the photo-colored global map PCD in the chatbot's own recording so highlights land on the map |
| `REPORTS_DIR` / `ANNOTATED_IMAGE_CACHE_DIR` | `../reports` / `./annotated_images` | Reports + annotated-image cache |
| `PIPER_*` | — | Piper voice/model paths |

---

## Setup

### Docker
```bash
docker compose up --build   # Ollama + backend (:8000) + frontend; models must be pre-pulled
```

### Local
```bash
# Backend
cd backend && ./setup_all.sh && source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend
cd frontend && npm install && npm run dev
```

### Required Ollama model
```bash
# One multimodal model serves both chat and image annotation (annotate_image reuses the base LLM).
ollama pull gemma4:26b      # other options: qwen2.5-vl, llava, llama3.2-vision
```

---

## Useful commands
```bash
docker compose logs -f backend
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
curl -X POST "http://localhost:8000/annotate-image" -F "image=@frame.jpg" -F "question=What anomalies are in this image?"
rerun                                            # optional: start the 3D viewer (auto-launched on first highlight when RERUN_AUTO_SPAWN=true)
# Generate the station map .npy from the grounding .rrd (one-time):
.venv/bin/python scripts/extract_station_map.py --rrd /path/to/output_mtr.rrd --out data/station_map.npz --max-points 1500000
sqlite3 "MTR Inspection Database/inspection_v2.db"
#   .tables
#   SELECT c.name AS category, COUNT(*) AS objects FROM objects o JOIN categories c ON c.id=o.category_id GROUP BY c.name ORDER BY objects DESC;
#   SELECT id, started_at, is_gt FROM inspections;
```