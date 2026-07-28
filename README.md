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
3. **Router — LLM call #1 (tool selection)** — `LocalLLM.stream_reply` calls `InspectionDBClient.lookup`, which calls `ToolRouter.select_tool`. The router sends the transcript + a system prompt (DB schema + all 32 tool descriptions + multi-tool rules + prior-images/prior-tools context) to Ollama with native `tools`. It returns a list of `(tool_name, args)` — possibly several — and nothing else.
4. **Tool execution** — `lookup` loops over the selected tools and runs `_execute_tool` for each. Each tool runs a SQL query (or vision annotation, or returns `None` for `get_report_summary`), and its result is formatted to text by a `_format_*` method. All formatted outputs are joined with `\n\n` into one `db_context` string. No re-prompting happens between tools.
5. **Report (conditional)** — If the router selected `get_report_summary`, `report_needed` is true and `InspectionReportClient.get_context()` (+ anomaly image URLs) is loaded as `report_context`. The report is lazy-loaded only on anomaly/findings intent — the router's decision, not a keyword gate.
6. **Debug + image-link handling** — The selected tool calls (and the router's raw response) are sent to the frontend as a `tool_calls` message (debug panel). If `annotate_image` ran, its markdown image links are pulled *out* of `db_context` (so the model can't duplicate them) and the backend re-emits them as the first tokens of the reply to guarantee display.
7. **Answerer — LLM call #2 (synthesis, streamed)** — `_build_messages` assembles:
   - `system`: the assistant persona + style/coordinate/image-link rules
   - `system`: prior tool-call history (image links stripped)
   - `system`: `db_context` (the merged tool outputs)
   - `system`: `report_context` (only when fetched)
   - the last few chat turns (within `llm_history_char_budget`, image links stripped)
   - `user`: the transcript

   The answering LLM streams tokens. ` authDomain` thinking tags are stripped in-flight so only the visible answer streams out.
8. **Streaming TTS** — As tokens accumulate, `handle_audio` splits the text at sentence boundaries, cleans markdown/URLs/punctuation via `_clean_for_tts`, and feeds each sentence to Piper, which yields WAV chunks streamed back to the client as `tts_audio_chunk` messages. Speech therefore begins before the LLM finishes generating.
9. **Playback** — The frontend plays chunks in order via the Web Audio API, renders the full text as a chat card, and inlines any `![...](/...images/...)` links as clickable thumbnails (lightbox). Cantonese with no matching Piper voice falls back to `window.speechSynthesis`.
10. **History** — After the turn completes, the `(transcript, reply)` pair is appended to `conversation_history` (capped 12) and the tool-call payload to `tool_call_history` (capped 6) — both fed back into the next turn so the router can detect changed parameters and reference prior images.

> **Key design point:** tool results reach the answerer as plain-text **system messages**, never as OpenAI-style `tool`/`function` messages. The backend pre-fetches and formats everything; the answerer just reads context and writes the answer.

---

## How multiple tools are chained together

### The mechanism (what "chaining" actually means here)

When a question needs more than one piece of data, the router returns **multiple tool calls in a single response**. The backend runs **all of them in one pass** and **merges** their formatted outputs into one `db_context`, which is injected as a single system message for the answerer.

```
transcript ──► ToolRouter.select_tool()  ──►  tool_calls = [
                                                  ("get_summary", {}),
                                                  ("get_category_objects_coordinates", {"category":"Ticket Gate"}),
                                                  ("get_category_proximity", {"target_category":"Ticket Gate", ...}),
                                                ]
                        │  (one LLM round-trip; no intermediate results seen by the model)
                        ▼
              for name, args in tool_calls:
                  results.append(_execute_tool(name, args))   # SQL → _format_* → text
              db_context = "\n\n".join(results)
                        │
                        ▼
              [system: db_context]  →  Answering LLM (streams synthesis)
```

Three things follow from this:

- **Tools are selected in parallel, not by output-dependency.** The router picks every tool up front using only what the user said (track IDs, categories, coordinates, times, radii). It cannot call tool A, read A's result, then decide to call B in the same turn. If an answer genuinely needs B's output to form B's arguments, the system can't do it in one turn (see *Limitations* below).
- **The merge is textual.** Each tool's output is independently formatted (`get_summary`→`_format_summary`, `get_category_proximity`→`_format_category_proximity`, …) and concatenated. The answerer does the cross-tool synthesis in prose.
- **Cross-turn "chaining" is real and supported** in two ways: (a) **re-calling with changed parameters**, and (b) **referencing previously shown images** ("annotate the previous image"). Both rely on `tool_call_history` / `conversation_history` injected into the router prompt.

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
Total aggregated objects: 345. Objects by category: Lights: 235, Advertisement Board: 53, ...

Objects in category 'Ticket Gate' with coordinates (ordered by first appearance):
- Track 218: first seen ..., centroid (-18.22, 32.17, -6.85), ...

Proximity summary for 'Ticket Gate' within 2.0 m of Lights, Advertisement Board, Map, TV, Exit Sign:
Total nearby objects: 45 x Lights, 12 x Advertisement Board
Per-target breakdown:
- Track 218 at (-18.22, 32.17, -6.85): within 2.0 m — 2 x Lights, 1 x Advertisement Board
```
Answerer (call #2) reads that single context block and synthesizes one coherent spoken answer covering totals, ticket-gate positions, and what's near them.

### Worked example B — cross-source (DB + report)

> **User:** "Were there any anomalies near the ticket gates?"

Router returns:
```
get_report_summary {}                                  # anomaly part
get_category_proximity {target_category: "Ticket Gate",
                         other_categories: ["Lights","Advertisement Board", ...]}   # "near ticket gates" part
```
`get_report_summary` itself returns `None` from `_execute_tool`; its real text is fetched by `llm_service` into `report_context`. So the answerer receives **two** system messages — `db_context` (proximity) and `report_context` (anomaly text + `![...](/reports/extracted_images/...)` links) — and weaves them together: "the report found cracks near gate 3; around the ticket-gate area there are 45 lights and 12 ad boards within 2 m."

### Worked example C — images + live annotation (vision tool)

> **User:** "Show me what the camera saw at 4:53 PM, and highlight any anomalies on it."

Router returns:
```
get_images_in_time_range {start_time: "16:53:00", end_time: "16:54:00"}
annotate_image {category: "Exit Sign", question: "highlight any anomalies"}   # or track_id, or image_url
```
`get_images_in_time_range` returns markdown links to source frames. `annotate_image` resolves frames (by `category`/`track_id`/`image_url`), sends each to the **base LLM** (multimodal) with a strict-JSON prompt, draws boxes/circles/highlights with OpenCV, writes each annotated PNG to `annotated_image_cache_dir`, and returns `![annotated result](/annotated/images/<hash>.png)` links plus a description. The backend then **strips those annotated links out of `db_context`** and **re-emits them as the first tokens** of the reply, so the image is guaranteed to render even if the model would otherwise paraphrase it away. The answerer only writes a short summary of what the annotation found.

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
The same applies to changing the category, track ID, time window, coordinates, or `limit`.

### Cross-turn: referencing a previously shown image

If the user says "annotate the previous image" / "the second one" / "the one with the backpack", the router resolves it from a numbered list of **prior image URLs** (built from `conversation_history`) and calls `annotate_image` with that exact `image_url`. This replaces brittle regex matching. (If the router blanks on an explicit annotation request, a safety-net fallback uses the most recent prior image / a track ID / a category.)

### Limitations (be aware)

- **No within-turn dependency.** The router can't do "find track X, then look up what's near X's coordinates" in one turn, because it doesn't see `get_object_*`'s result before planning. Workarounds: ask the user for the ID up front, or split across turns (turn 1 reveals the ID, turn 2 uses it — the second turn sees the first via history).
- **`run_sql_query` / `query_database` are the escape hatch** for anything the fixed tools can't express (aggregations, custom joins, multi-category summaries with `GROUP BY`).
- **Router disabled or returns nothing → no DB context.** With `tool_router_enabled=false` (or an empty tool list), `lookup` returns `None`; the answerer replies from general knowledge/history. There is no keyword fallback.

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
| `app/services/llm_service.py` | `LocalLLM.stream_reply` (call #2): gather context, `_build_messages`, stream via Ollama/vLLM, strip `thinking`. `preload_model`, `runtime_status`. |
| `app/services/tts_service.py` | `PiperTTS` — voice selection per language, streaming WAV chunks, Python API + CLI fallback. |
| `app/services/runtime_status.py` | `nvidia-smi` VRAM snapshot. |

**Frontend:** `App.jsx` (state, WebSocket, push-to-talk, playback), `useWebSocket`/`useRecorder`/`useAudioPlayback` hooks, `TranscriptCards`/`ChatHistory`/`MarkdownImageText`/`StatusPanel`/`DebugPanel`/`ImageAnnotator`/`ReportImageGallery` components.

---

## WebSocket messages

**Client → server:** `user_audio`, `interrupt`, `set_voice`, `clear_context`, `ping`
**Server → client:** `ready`, `runtime_update`, `transcript` (text/emotion/raw), `tool_calls` (debug: selected tools + router raw), `llm_token` (streaming text), `tts_audio_chunk` (base64 audio + voice/language), `llm_done`, `interrupted`, `error`, `pong`

---

## Tool reference (32 tools)

Each maps to an `InspectionDBClient` method; the router's system prompt describes each with expected output and example queries.

| Tool | Args | Use case |
|------|------|----------|
| `get_summary` | — | "What did you find?", overall counts |
| `get_observation_counts_by_category` | — | "How many times were lights detected?" |
| `get_objects_by_category` | `category`, `limit` | "Tell me about the lights" |
| `get_object_by_track_id` | `track_id` | "Tell me about track 218" |
| `get_top_objects` | `n` | "Biggest objects?" |
| `get_recent_objects` | `limit` | "Most recently seen?" |
| `get_object_timeline` | `track_id` | "When was track 218 seen?" |
| `get_object_image_paths` | `track_id` | "Show me images of track 218" |
| `get_category_timeline` | `category` | "When were the lights detected?" |
| `get_category_windows` | `categories[]` | "When were lights and gates detected?" |
| `get_category_objects_coordinates` | `category` | "Coordinates of the ticket gates?" |
| `get_category_objects_with_images` | `category`, `limit` | "Exit signs with IDs, coords, and images" |
| `get_category_proximity` | `target_category`, `other_categories[]`, `radius_m` | "What's close to ticket gates?" |
| `get_inspection_timeline` | — | "Walk me through the inspection" |
| `get_temporal_clusters` | `window_ms`, `top_n` | "Busiest moments?" |
| `get_category_cooccurrence` | `window_ms`, `top_n` | "Which objects appear together?" |
| `get_objects_in_temporal_cluster` | `center_time`, `window_ms`, `limit` | "Where was the 4:51 PM cluster?" |
| `get_objects_in_time_range` | `start_time`, `end_time`, `limit` | "What happened between 4:51 and 4:52?" |
| `get_observations_in_time_range` | `start_time`, `end_time`, `limit` | "Detections around 16:53:45" |
| `get_objects_by_category_in_time_range` | `category`, `start_time`, `end_time`, `limit` | "Ticket gates after 4:53?" |
| `get_category_observation_timeline` | `category`, `bucket_seconds` | "When were most lights seen?" |
| `get_objects_near_position` | `x`, `y`, `z`, `radius_m`, `category?` | "What's near (-18, 32, -6)?" |
| `get_nearest_objects_to_track` | `track_id`, `radius_m` | "What was near track 218?" |
| `get_object_distance` | `track_id_a`, `track_id_b` | "How far apart are tracks 218 and 165?" |
| `get_object_movement` | `track_id` | "Did track 218 move?" |
| `get_category_bounding_box` | `category` | "What area do the ticket gates occupy?" |
| `get_images_in_time_range` | `start_time`, `end_time`, `category?`, `limit` | "What did the camera see at 4:51?" |
| `get_category_sample_images` | `category`, `limit` | "Show me some advertisement boards" |
| `get_inspection_poses` | `limit` | "What poses did the camera have?" |
| `get_filtered_objects` | `limit` | "What was filtered out?" |
| `get_report_summary` | — | "What anomalies did you find?" (loads report context) |
| `get_categories` | — | "What categories exist?" |
| `run_sql_query` / `query_database` | `query` (SELECT), `limit` | Escape hatch for ad-hoc/aggregation SQL |
| `annotate_image` | `image_url` XOR `track_id` XOR `category`, `question?`, `limit` | "Highlight anomalies on track 218's image" (base LLM vision) |

Times accept ISO datetimes, clock strings (`"16:51:45"`, `"4:51 PM"`), or nanosecond integers; bare "at 4:53" is read as a one-minute window.

---

## Database schema

`inspection_mtr.db`:

- **`objects`** — one row per tracked object: `track_id` (PK), `category`, `observation_count`, `total_point_count`, `centroid_x/y/z`, `bbox3d_min/max_x/y/z`, `first_seen_ns`, `last_seen_ns`, `aggregated_pcd_path`.
- **`observations`** — one row per per-frame detection: `timestamp_ns`, `track_id`, `category`, `image_file_name`, `image_path`, `centroid_x/y/z`, `point_count`, `pcd_path`, `mask_path`.
- **`inspection_poses`** — camera pose per image: `image_path`, `tf_translation_x/y/z`, `tf_rotation_x/y/z/w`.
- **`filtered_objects`** — audit of dropped tracks: `track_id`, `category`, `reason`, `point_count`, `first/last_seen_ns`, `created_at`.

Categories: Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign.

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
| `INSPECTION_DB_PATH` / `INSPECTION_IMAGE_DIR` | — | SQLite DB + source camera frames |
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
sqlite3 /home/wangyiming/code/object_detection_app/output/inspection_mtr.db
#   .tables
#   SELECT category, COUNT(*) FROM objects GROUP BY category ORDER BY COUNT(*) DESC;
```