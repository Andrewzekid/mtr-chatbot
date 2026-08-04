# MTR-Insight: Hong Kong MTR Inspection Robot Voice Assistant

A voice-to-voice assistant for Hong Kong MTR subway station inspection. It wraps a local realtime pipeline (STT → LLM → TTS) with live SQLite integration and an LLM tool router, so inspectors can ask spoken questions about objects the automated grounding pipeline detected and get spoken answers with inline images and annotations.

- **STT:** SenseVoice (FunASR, zh/en/ja/ko with emotion tags) by default; Whisper `large-v3` (faster-whisper, EN/zh/yue) via `STT_BACKEND=whisper`
- **LLM:** Ollama with a multimodal Gemma model — used for both chat **and** image annotation; vLLM as an alternative chat provider
- **TTS:** Piper (English + Chinese voices, browser SpeechSynthesis fallback for Cantonese)
- **Router:** one LLM call that picks which DB/annotation tools to run (multi-round), plus a **final-pass LLM call** that decides what to highlight in the 3D Rerun viewer
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
│ Browser TTS  │ ◄── audio chunks ─── │        │  selects tools (LLM call #1,     │
│ (fallback)   │                      │        │  multi-round, dedup repeated calls)│
└──────────────┘                      │        ▼                                 │
                                       │  InspectionDBClient (SQLite) + Vision   │
                                       │  + Report + deterministic top-ups        │
                                       │        │  formatted db_context            │
                                       │        ▼                                 │
                                       │  Final-pass highlight decider           │
                                       │  (LLM call #2, set_rerun_highlight)     │
                                       │        │  → Rerun viewer (bg thread)     │
                                       │        ▼                                 │
                                       │  Answering LLM (call #3, streams)       │
                                       │        │                                 │
                                       │        ▼                                 │
                                       │  Piper TTS ──► streamed audio chunks   │
                                       └──────────────────────────────────────────┘
```

---

## The full pipeline

Every turn is **three LLM calls** plus streaming TTS — not an agentic loop. The router *plans* tools up front (possibly over multiple rounds); a *final-pass* decider then chooses what to highlight in the 3D Rerun viewer; the answerer *synthesizes* the fetched data. There is no step where the model inspects a tool result and then decides to call another tool in the same turn — the router's multi-round behavior is a re-prompt with prior results, not a free agentic loop.

1. **Capture** — Frontend records audio via `MediaRecorder` (Space = push-to-talk), base64-encodes it, sends `{"type":"user_audio", audio_base64, mime_type}` over the WebSocket.
2. **STT** — `VoicePipeline.handle_audio` runs SenseVoice (or Whisper) → returns transcript text, a language tag (`en`/`zh`/`yue`/…), and an emotion tag. Yields a `transcript` message to the client.
3. **Router — LLM call #1 (tool selection, multi-round)** — `LocalLLM.stream_reply` calls `InspectionDBClient.lookup`, which calls `ToolRouter.select_tool`. The router sends the transcript + a system prompt (DB schema + all ~40 tool descriptions + multi-tool rules + prior-images/prior-tools context) to Ollama with native `tools`. It returns a list of `(tool_name, args)` — possibly several — and nothing else. After the batch executes, the formatted outputs are fed back to the router and it can issue more calls (up to `TOOL_ROUTER_MAX_ROUNDS`). Repeated calls are deduplicated by `(name, normalized_args)` so the same tool+args never runs twice in one turn. See *How multiple tools are chained together* below.
4. **Tool execution (multi-round)** — `lookup` runs each round's selected tools and appends their formatted outputs to a `prior_results` list. Round after round, the router sees the accumulated results and either calls more tools or stops. Once the router returns no further tools (or the round limit is reached), all formatted outputs are joined with `\n\n` into one `db_context` string.
5. **Safety net + anomaly top-up (deterministic).** Two backend-side passes run after the router stops, to guarantee complete data without relying on the model:
   - **No-tool safety net.** If the router returned nothing for a query that names a real category or is a broad object question ("tell me about the objects"), `_safety_net_calls` deterministically runs `get_summary` / `get_objects_by_category` so the answerer never invents categories.
   - **Anomaly completeness.** For any anomaly-related query (`_ANOMALY_QUERY_RE`: anomaly / abnormal / findings / issues / problems / defects / report), the backend tops up whichever of `get_anomaly_locations` and `get_anomalies` the router skipped, preserving any `anomaly_id` / `anomaly_type` / `inspection_id` scope parsed from the query or the router's own args. The pair is also cached (`_last_anomaly_trio_context`) so generic anomaly follow-ups ("tell me more about them") reuse the cached context instead of re-querying the DB.
6. **Final pass — LLM call #2 (Rerun highlight decision).** After the tool loop ends (and no explicit `highlight_in_rerun` ran), `ToolRouter.decide_highlights` sends the user's question + the merged `db_context` back to the router model with a single `set_rerun_highlight` tool. The model decides what to light up in the 3D viewer — by `category`, `categories`, `object_ids`, or raw `coordinates` — with descriptive labels (objects as `'Object <id>: <category>'`, anomaly locations using the exact rich label from `get_anomaly_locations`, e.g. `'Anomaly 4: state_change, overhead monitor/screen'`). The backend validates the decision against the tools that ran (`_decision_matches_tool_results`): a category-level highlight is rejected for proximity / anomaly queries and a deterministic fallback runs instead. The status string is stored in `last_highlight_status` and surfaced to the answerer.
   > **Anomalies are ALWAYS highlighted.** When any anomaly tool ran (`get_anomalies` / `get_anomaly_summary` / `get_anomaly_locations`), the final pass pushes the anomaly camera positions to the Rerun viewer — scoped to the `anomaly_id` / `anomaly_type` / `inspection_id` the user asked about via `_scoped_anomaly_coordinates`. So if the user asks **about anomalies, for an inspection summary, or to compare inspections**, the abnormality locations are lit up in the 3D viewer alongside the spoken answer, with no `highlight_in_rerun` call required. This also covers generic anomaly follow-ups that reuse the cached anomaly trio (the cached path still runs the final-pass highlight decision so follow-ups like "highlight anomaly 4" can refine the viewer). If the LLM decider returns nothing, the deterministic fallback marks the scoped anomaly locations anyway.
7. **Debug + image-link handling** — The selected tool calls (and the router's raw response) are sent to the frontend as a `tool_calls` message (debug panel, includes `highlight.status` / `highlight.args` / `highlight_history` / `rerun_stats`). If `annotate_image` ran, its markdown image links are pulled *out* of `db_context` (so the model can't duplicate them) and the backend re-emits them as the first tokens of the reply to guarantee display.
8. **Answerer — LLM call #3 (synthesis, streamed)** — `_build_messages` assembles:
   - `system`: the assistant persona + style/coordinate/image-link rules + live DB facts (real categories + multi-inspection listing)
   - `system`: prior tool-call history (image links stripped)
   - `system`: `db_context` (the merged tool outputs, with annotate_image guidance prepended when annotation ran)
   - the last few chat turns (within `llm_history_char_budget`, image links stripped)
   - `user`: the transcript

   When the final pass highlighted something, a note is prepended to `db_context` telling the answerer the highlights are shown in the Rerun viewer. When `get_anomaly_locations` ran for an explicit "where" question, a note forces the answerer to quote several exact `(x, y, z)` anomaly coordinates. The answering LLM streams tokens. ` authDomain` thinking tags are stripped in-flight so only the visible answer streams out.
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
- **Repeated calls are deduplicated.** The backend tracks which `(tool, normalized_args)` pairs have already run this turn and skips reruns (the router re-prompt never sees a result it already produced), preventing infinite loops.
- **The merge is textual.** Each tool's output is independently formatted (`get_summary`→`_format_summary`, `get_category_proximity`→`_format_category_proximity`, …) and concatenated. The answerer synthesizes the merged context in prose.
- **Deterministic top-ups run after the router stops.** The no-tool safety net and the anomaly completeness top-up (see *The full pipeline*, step 5) guarantee complete data without relying on the model. These run on the same `db_context` the answerer will see.
- **A final LLM pass decides highlights.** After the tool loop and top-ups, `ToolRouter.decide_highlights` (LLM call #2) reads the merged `db_context` and the user's question and decides what to push to the Rerun viewer. This is separate from tool selection and runs once per turn (see *The full pipeline*, step 6).
- **Cross-turn context** (re-calling with changed parameters, referencing previously shown images) still relies on `tool_call_history` / `conversation_history` injected into the router prompt.

### Worked example A — counts + coordinates + proximity (three tools, one turn)

> **User:** "Tell me about the inspection objects and their coordinates, and which objects were close to ticket gates."

Router (round 1) returns three tool calls in one response:
```
get_summary {}
get_category_objects_coordinates {category: "Ticket Gate"}
get_category_proximity {target_category: "Ticket Gate",
                        other_categories: ["Lights","Advertisement Board","Map","TV","Exit Sign"]}
```
Backend runs all three (no repeats needed), each producing formatted text, and joins them:
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
Answerer (call #3) reads that single context block and synthesizes one coherent spoken answer covering totals, ticket-gate positions, and what's near them. (In between, the final-pass LLM call #2 sees a `Ticket Gate` category question + a proximity query and pushes the specific nearby object ids to the Rerun viewer — the answerer cites the viewer status.)

### Worked example B — anomalies (structured DB)

> **User:** "What anomalies did you find, and were any near the ticket gates?"

Router (round 1) returns:
```
get_anomaly_summary {}                                  # anomaly overview
get_anomalies {}                                        # individual abnormalities + image links
get_category_proximity {target_category: "Ticket Gate",
                         other_categories: ["Lights","Advertisement Board", ...]}   # "near ticket gates" part
```
After the router stops, the anomaly completeness top-up notices `get_anomaly_locations` was skipped and runs it (so the answerer has the 3D camera positions of every abnormal pair). The final-pass LLM then decides what to highlight: because an anomaly tool ran, it pushes the **anomaly camera positions** to the Rerun viewer (scoped to the user's `anomaly_id` / `anomaly_type` / `inspection_id` if any were named) — the abnormality locations are lit up automatically alongside the spoken answer. If the user instead asked for an **inspection summary** or to **compare inspections**, the anomaly tools still run (the answerer's system prompt explicitly tells it to "always mention ANOMALY DATA" for those intents) and the final pass still marks the anomaly locations in the viewer.

`get_anomaly_summary` / `get_anomalies` / `get_anomaly_locations` read the `anomaly_types` / `abnormal_detections` / `abnormalities` tables. `get_anomalies` returns typed abnormalities with 2D pixel bboxes, notes, affected object/location, camera position, and the ground-truth vs inspection image links for each pair. `get_anomaly_locations` returns one row per abnormality (numbered by the user-facing `abnormalities.id`, not the internal pair id) with its 3D camera position and a rich label like `Anomaly 4: state_change, overhead monitor/screen`. So the answerer receives a single `db_context` system message with the structured abnormalities + 3D locations + proximity, and walks through them anomaly by anomaly (per the answering prompt's anomaly rule), e.g. "the abnormalities table lists 2 scratches on the ticket-gate panels; around that area there are 8 lights and 3 ad boards within 2 m."

### Worked example C — images + live annotation (vision tool)

> **User:** "Show me what the camera saw at 4:53 PM, and highlight any anomalies on it."

Router (round 1) returns:
```
get_images_in_time_range {start_time: "16:53:00", end_time: "16:54:00"}
annotate_image {category: "Exit Sign", question: "highlight any anomalies"}   # or object_id, or image_url
```
`get_images_in_time_range` returns markdown links to source frames. `annotate_image` resolves frames (by `category`/`object_id`/`image_url`), sends each to the **base LLM** (multimodal) with a strict-JSON prompt, draws boxes/circles/highlights with OpenCV, writes each annotated PNG to `annotated_image_cache_dir`, and returns `![annotated result](/annotated/images/<hash>.png)` links plus a description. The backend then **strips those annotated links out of `db_context`** and **re-emits them as the first tokens** of the reply, so the image is guaranteed to render even if the model would otherwise paraphrase it away. The answerer only writes a short summary of what the annotation found.

### Worked example B2 — Rerun 3D highlight (alongside the spoken answer)

> **User:** "What are the coordinates of the ticket gates? Show me where they are."

Router (round 1) returns:
```
get_category_objects_coordinates {category: "Ticket Gate"}
```
`get_category_objects_coordinates` returns the gate centroids + 3D bboxes as text for the answerer to speak. After the tool runs, the **final-pass LLM** (`decide_highlights`) sees the user asked about the `Ticket Gate` category and pushes the gate centroids (bright red points) + 3D bboxes to the Rerun viewer via `set_rerun_highlight(category='Ticket Gate')` — no `highlight_in_rerun` call needed. (The explicit `highlight_in_rerun` tool is only required for pure-visualization requests that name particular object ids/coordinates, e.g. "highlight objects 16 and 19 in the 3D viewer".) Rerun I/O runs on a background thread so the chat turn never blocks; if no viewer is running and `RERUN_AUTO_SPAWN=true`, the backend launches one. Highlights share the grounding scene's app id + leveling frame, so they overlay the grounding map. The answerer cites a short status string ("Highlighted 3 objects in the Rerun viewer (grounding world frame)").

### Worked example D — multi-hop temporal + spatial (one turn, parallel)

> **User:** "Walk me through what happened at 4:51 PM — which objects were seen, where, and which were near each other."

Router (round 1) returns:
```
get_objects_in_temporal_cluster {center_time: "16:51:45", window_ms: 5000}
get_category_proximity {target_category: "Lights",
                        other_categories: ["Advertisement Board","Ticket Gate","Map","TV","Exit Sign"]}
```
Both run in parallel; the cluster tool already returns object coordinates for the time window, so the answerer can describe *where* the cluster was and *what was near what* in one answer — without a dependent second round. (The final-pass LLM call #2 then highlights the cluster objects + the specific nearby proximity pairs in the Rerun viewer.)

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
- **Router disabled or returns nothing → no DB context.** With `tool_router_enabled=false` (or an empty tool list), `lookup` returns `None`; the answerer replies from general knowledge/history. There is no keyword fallback. (The deterministic safety-net/top-up passes only run when the router is enabled.)
- **Multi-round depends on the model using prior results.** The backend exposes prior round outputs to the router and asks it to stop when satisfied, but the tool-calling model may still decide everything up front. `TOOL_ROUTER_MAX_ROUNDS` sets the ceiling; the model decides the actual number of rounds used.
- **The final-pass highlight is best-effort.** `decide_highlights` runs once after the tools finish. If the LLM returns a category-level highlight for a proximity/anomaly query, the backend rejects it (`_decision_matches_tool_results`) and a deterministic fallback highlights the specific object ids / anomaly camera positions. If both the LLM and the fallback produce nothing, the viewer simply isn't updated that turn.

---

## Module responsibilities

| File | Role |
|------|------|
| `app/main.py` | FastAPI app, WebSocket `/ws` lifecycle, static image mounts, keeps `conversation_history` (≤12) + `tool_call_history` (≤6), per-turn task cancel/interrupt. REST: `/health`, `/status`, `/voices`, `/annotate-image`. |
| `app/config.py` | `Settings` (pydantic) from env/`.env`; resolves relative paths against `backend/`. |
| `app/models.py` | Pydantic client/server message models. |
| `app/services/pipeline.py` | `VoicePipeline.handle_audio` — STT → stream LLM → sentence-chunk → TTS → yield `transcript`/`llm_token`/`tts_audio_chunk`/`llm_done`. |
| `app/services/stt_service.py` | `build_stt` → `SenseVoiceSTT` (default, emotion+lang tags) or `WhisperSTT`. Returns `STTResult`. |
| `app/services/tool_router.py` | `ToolRouter.select_tool` — Ollama native tool-calling (call #1, multi-round). `TOOLS`/`_ollama_tools`. Prior-image + prior-tool context. Annotation safety-net fallback. `decide_highlights` — final-pass `set_rerun_highlight` decision (call #2) that reads the merged tool results and picks what to highlight in the Rerun viewer. |
| `app/services/db_service.py` | `InspectionDBClient.lookup` → router → `_execute_tool` per tool → join. All SQL query methods + `_format_*` formatters. `_parse_time_string` for clock/ns/ISO times. `annotate_image` orchestration. No-tool safety net (`_safety_net_calls`). Anomaly completeness top-up (`_resolve_anomaly_scope` + `get_anomaly_locations`/`get_anomalies` top-up) + cached anomaly trio short-circuit for follow-ups. Final-pass highlight application (`_apply_highlight_decision`, `_decision_matches_tool_results`, `_scoped_anomaly_coordinates`) with deterministic fallback. |
| `app/services/vision_service.py` | `VisionAnnotator.annotate` — sends image to the **base LLM** (multimodal), strict-JSON parsing with retries, normalizes coords, draws annotations with OpenCV. |
| `app/services/rerun_service.py` | `RerunVisualizer.highlight` — pushes 3D object centroids/bboxes + raw coordinates to a Rerun viewer, sharing the grounding scene's app id + leveling frame. Logs the station map (`world/leveled/camera_init/colored_map`) from the pre-extracted colored `.pcd` so highlights land on the map. All Rerun I/O on a background daemon thread (never blocks the chat); auto-launches a viewer when `RERUN_AUTO_SPAWN=true`. Called by the `highlight_in_rerun` tool, the `clear_rerun_markings` tool, AND by the final-pass highlight decision (`_apply_highlight_decision` / deterministic fallback). |
| `app/services/llm_service.py` | `LocalLLM.stream_reply` (call #3): gather context, `_build_messages` (system prompt + live DB facts + tool history + db_context), stream via Ollama/vLLM, strip `thinking`. Annotated-image links are stripped from `db_context` and re-emitted as the first reply tokens. Highlight / anomaly-where notes prepended to `db_context` from `last_highlight_status` + `get_anomaly_locations`. `preload_model`, `runtime_status`. |
| `app/services/tts_service.py` | `PiperTTS` — voice selection per language, streaming WAV chunks, Python API + CLI fallback. |
| `app/services/runtime_status.py` | `nvidia-smi` VRAM snapshot. |

**Frontend:** `App.jsx` (state, WebSocket, push-to-talk, playback), `useWebSocket`/`useRecorder`/`useAudioPlayback` hooks, `TranscriptCards`/`ChatHistory`/`MarkdownImageText`/`StatusPanel`/`DebugPanel`/`ImageAnnotator` components.

---

## WebSocket messages

**Client → server:** `user_audio`, `interrupt`, `set_voice`, `clear_context`, `ping`
**Server → client:** `ready`, `runtime_update`, `transcript` (text/emotion/raw), `tool_calls` (debug: selected tools + router raw + `highlight.status`/`highlight.args` + `highlight_history` + `rerun_stats`), `llm_token` (streaming text), `tts_audio_chunk` (base64 audio + voice/language), `llm_done`, `interrupted`, `error`, `pong`

---

## Tool reference (~40 tools)

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
| `get_category_proximity` | `target_category`, `other_categories[]`, `radius_m?`, `inspection_id?` | "What's close to ticket gates?" (counts only) |
| `get_category_proximity_with_images` | `target_category`, `other_categories[]`, `radius_m?`, `limit?`, `nearby_limit?`, `inspection_id?` | "Show me lights near ticket gates" (returns specific nearby object ids — the final pass highlights exactly those ids, not the whole categories) |
| `get_objects_proximity_with_images` | `object_ids[]`, `target_category`, `radius_m?`, `limit?`, `nearby_limit?`, `inspection_id?` | "What's near object 109?" (returns nearby object ids — the final pass highlights the source + nearby ids) |
| `get_inspection_timeline` | `inspection_id?` | "Walk me through the inspection" |
| `get_temporal_clusters` | `window_ms?`, `top_n?`, `inspection_id?` | "Busiest moments?" |
| `get_category_cooccurrence` | `window_ms?`, `top_n?`, `inspection_id?` | "Which objects appear together?" |
| `get_objects_in_temporal_cluster` | `center_time`, `window_ms?`, `limit?`, `inspection_id?` | "Where was the 4:51 PM cluster?" |
| `get_objects_in_time_range` | `start_time`, `end_time`, `limit?`, `inspection_id?` | "What happened between 4:51 and 4:52?" |
| `get_objects_in_image` | `image_id`, `inspection_id?` | "What objects are in this frame?" |
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
| `get_anomalies` | `anomaly_id?`, `anomaly_type?`, `inspection_id?`, `limit?` | "Show me the anomalies" (typed abnormalities + image-pair links) |
| `get_anomaly_locations` | `inspection_id?` | "Where are the anomalies?" — 3D camera positions per abnormality, numbered by user-facing `abnormalities.id`. Always runs alongside the other anomaly tools; the final pass pushes these coordinates to the Rerun viewer with rich labels like `Anomaly 4: state_change, overhead monitor/screen` |
| `highlight_in_rerun` | `object_ids[]?`, `coordinates[]?`, `category?`, `keep_existing?`, `label?` | "Highlight objects 16 and 19 in the 3D viewer" (explicit push; the final pass handles ordinary coordinate/category highlights without this tool) |
| `clear_rerun_markings` | — | "Clear the 3D map markings" — wipes the Rerun viewer (use only when the user explicitly asks to clear/reset) |
| `run_sql_query` / `query_database` | `query` (SELECT), `limit` | Escape hatch for ad-hoc/aggregation SQL |
| `annotate_image` | `image_url` XOR `object_id` XOR `category`, `question?`, `limit` | "Highlight anomalies on object 109's image" (base LLM vision) |

Times accept ISO datetimes, clock strings (`"16:51:45"`, `"4:51 PM"`), or nanosecond integers; bare "at 4:53" is read as a one-minute window. The old `get_filtered_objects` tool is gone (the `filtered_objects` table no longer exists); `track_id` is now `object_id` (the `objects.id` column).

---

## Rerun 3D highlighting

When the AI's answer involves coordinates, specific objects, or anomalies, the assistant **automatically** pushes those 3D positions into a [Rerun](https://www.rerun.io) viewer so the inspector can *see* where things are in the station, alongside the spoken answer — no explicit "show me in Rerun" request is needed. Highlights share the **grounding pipeline's** Rerun app id (`inspection_grounding_rerun`) and world frame, so they overlay the grounding map/bboxes rather than appearing in a disconnected scene.

**Final-pass LLM decider (the default behavior).** After the tools finish, `ToolRouter.decide_highlights` (LLM call #2) reads the merged `db_context` + the user's question and decides what to highlight via a single `set_rerun_highlight` tool call. It can pick `category` / `categories` / `object_ids` / raw `coordinates`, with descriptive labels (objects as `'Object <id>: <category>'`, anomaly locations using the exact rich label from `get_anomaly_locations`, e.g. `'Anomaly 4: state_change, overhead monitor/screen'`). The backend validates the decision (`_decision_matches_tool_results`): for proximity and anomaly queries a category-level highlight is rejected and a deterministic fallback runs instead (proximity queries highlight the specific nearby object ids; anomaly queries highlight the anomaly camera positions). The router does **not** have to call a special tool for this. The explicit `highlight_in_rerun` tool still exists for pure-visualization requests that name particular object ids/coordinates (e.g. "highlight objects 16 and 19 in the 3D viewer"), and `clear_rerun_markings` wipes the viewer when the user explicitly asks to clear/reset it.

**Anomalies are ALWAYS highlighted.** Whenever any anomaly tool ran (`get_anomalies` / `get_anomaly_summary` / `get_anomaly_locations`), the final pass pushes the anomaly camera positions to the Rerun viewer — scoped to the `anomaly_id` / `anomaly_type` / `inspection_id` the user asked about via `_scoped_anomaly_coordinates`. So if the user asks **about anomalies, for an inspection summary, or to compare inspections**, the abnormality locations are lit up in the 3D viewer alongside the spoken answer, with no `highlight_in_rerun` call required. This also covers generic anomaly follow-ups that reuse the cached anomaly trio (the cached path still runs the final-pass highlight decision so follow-ups like "highlight anomaly 4" can refine the viewer). If the LLM decider returns nothing, the deterministic fallback marks the scoped anomaly locations anyway.

**Auto-launch.** If no viewer is reachable on `RERUN_VIEWER_ADDR` and `RERUN_AUTO_SPAWN=true` (default), the backend launches one itself (`rerun --port 9876`, detached) the first time it has something to visualize, then attaches. Set `RERUN_AUTO_SPAWN=false` to require a manually-started `rerun` viewer.

1. (Optional) Start the viewer yourself:
   ```bash
   pip install rerun-sdk        # already in backend/requirements.txt
   rerun                        # listens on 127.0.0.1:9876 by default
   ```
   With `RERUN_AUTO_SPAWN=true` you can skip this — the backend launches a viewer on first highlight.
2. Ask a coordinate/visualization question, e.g. *"What are the coordinates of the ticket gates?"* The router calls `get_category_objects_coordinates`; the final-pass LLM then pushes the gate centroids + 3D bboxes to the viewer via `set_rerun_highlight(category='Ticket Gate')`.
3. `RerunVisualizer.highlight` does a fast port probe, returns an optimistic status string immediately, and hands the actual Rerun I/O to a **background daemon thread** so the chat turn never blocks on the viewer. The worker attaches to a running viewer via gRPC (`rr.connect_grpc` to `rerun+http://<addr>/proxy`, the rerun 0.35 protocol) or spawns one, sets `world` to `RIGHT_HAND_Z_UP` (matching the grounding bridge), and logs everything as static entities in the chatbot's own recording.
4. **Station map overlay.** The first time the worker connects, it loads the photo-colored global map PCD (`MTR Inspection Database/outputs/colored_map.pcd`) and logs it as a static `world/leveled/camera_init/colored_map` entity. The PCD stores raw `camera_init`-frame point positions and per-point RGBA colors, so the chatbot logs a genuine colored point cloud, not a monochrome cloud. Highlights therefore sit on the full colored station map in the chatbot's own recording.
5. **Frame alignment:** the DB stores object centroids/bboxes in the tilted `camera_init` frame. `RerunVisualizer` pre-rotates every point by the leveling matrix (`RERUN_LEVELING_RPY_DEG`, default `0.0,20.0,0.0` — the 20° pitch used by the 2026-06-11 inspection run, mirroring the grounding `rerun_bridge_node` `leveling_rpy_deg`), so highlights and the station map land level in the grounding world frame — the same convention the bridge uses for `world/bboxes3d`.
6. **Static logging:** the station map, context cloud, trajectory, and highlights are all logged with `static=True` so they render immediately regardless of the viewer's timeline position (logging at a non-zero `set_time_sequence` tick was the cause of the "empty viewer" bug — a fresh viewer scrubbed to time 0 showed nothing).
7. The tool returns a short status string ("Highlighted 3 objects in the Rerun viewer (grounding world frame)") that the answerer cites; the spoken answer still gives the (x, y, z) coordinates.

It is fully tolerant: `RERUN_ENABLED=false`, a missing `rerun-sdk`, or a viewer that cannot be reached or spawned all degrade to a friendly status string — the chat turn never fails because of Rerun. Highlight by `object_ids`, by raw `coordinates` (`{x, y, z, label?}`), or by `category`, optionally scoped to one `inspection_id`.

> **Station map file.** The visualizer reads the colored global map directly from `MTR Inspection Database/outputs/colored_map.pcd`. If the PCD is missing, it simply skips the map overlay (highlights still work). The legacy `RERUN_MAP_POINTS_PATH=./data/station_map.npz` path is no longer used by the visualizer but is retained for compatibility.

---

## Database schema

`MTR Inspection Database/inspection_v2_mtr_new.db` (new multi-inspection schema, written by the `inspection_grounding` pipeline):

- **`categories`** — `id`, `name`. Fixed set: Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign.
- **`inspections`** — `id`, `started_at`, `is_gt`. **Multiple inspections** can coexist; scope tools with `inspection_id`.
- **`images`** — `id`, `inspection_id`, `timestamp_ns`, `tf_translation_x/y/z`, `tf_rotation_x/y/z/w`, `filename`. Camera pose lives here (no separate poses table). `filename` (e.g. `14.jpg`) is served at `/inspection/images/<filename>`.
- **`objects`** — one row per tracked object: `id` (the object id — there is no `track_id`), `category_id` (→ `categories.name`), `centroid_x/y/z`, `min_x/y/z`, `max_x/y/z`, `is_gt`, `created_at`. An object has ONE centroid + 3D bbox; it does **not** store `first_seen`/`last_seen` or a detection count — those are **derived** from `detections`.
- **`detections`** — one row per per-frame detection: `id`, `image_id` (→ `images`), `object_id` (→ `objects`), `centroid_x/y/z`, `min/max_x/y/z`. An object's detection count = `COUNT(detections)`; first/last seen = `MIN/MAX(images.timestamp_ns)` over its detections→images.

Anomaly tables (populated in `inspection_v2_mtr_new.db`):

- **`anomaly_types`** — `id`, `name`. 7 types: missing_object, foreign_object, relocation, state_change, crack and structure damage, stain/graffiti, content_change.
- **`abnormal_detections`** — `id`, `gt_image` (→ `images.id`), `inspection_image` (→ `images.id`), `status`, `summary` (prose summary of the pair), `viewpoint_change`. An abnormal **image pair** (ground truth vs inspection).
- **`abnormalities`** — `id`, `pair` (→ `abnormal_detections.id`), `type` (→ `anomaly_types.id`), `object` (affected object), `location` (where in the scene), `min_x`, `min_y`, `max_x`, `max_y` (2D pixel bbox), `note`.

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
| `INSPECTION_DB_PATH` / `INSPECTION_IMAGE_DIR` | `../MTR Inspection Database/inspection_v2_mtr_new.db` / `../MTR Inspection Database/outputs/images` | SQLite DB + source camera frames |
| `RERUN_ENABLED` / `RERUN_VIEWER_ADDR` | `true` / `127.0.0.1:9876` | Push 3D highlights to a running `rerun` viewer over TCP |
| `RERUN_APP_ID` / `RERUN_LEVELING_RPY_DEG` | `inspection_grounding_rerun` / `0.0,20.0,0.0` | Match the grounding scene's app id + leveling rotation so highlights overlay the grounding map |
| `RERUN_AUTO_SPAWN` | `true` | If no viewer is reachable, launch one on first highlight (else require a manually-started `rerun`) |
| `RERUN_MAP_ENABLED` / `RERUN_MAP_PCD_PATH` | `true` / `../MTR Inspection Database/outputs/colored_map.pcd` | Overlay the photo-colored global map PCD in the chatbot's own recording so highlights land on the map |
| `ANNOTATED_IMAGE_CACHE_DIR` | `./data/annotated_images` | Annotated-image cache (writes from the `annotate_image` tool) |
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
sqlite3 "MTR Inspection Database/inspection_v2_mtr_new.db"
#   .tables
#   SELECT c.name AS category, COUNT(*) AS objects FROM objects o JOIN categories c ON c.id=o.category_id GROUP BY c.name ORDER BY objects DESC;
#   SELECT id, started_at, is_gt FROM inspections;
```