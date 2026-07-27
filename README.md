# MTR-Insight: Hong Kong MTR Inspection Robot Voice Assistant

A voice-to-voice chatbot layer for Hong Kong MTR subway station inspection. It extends a local realtime voice pipeline (STT → LLM → TTS) with live SQLite integration so inspectors can ask about objects detected by the automated grounding pipeline.

- **STT:** SenseVoice (English / Cantonese / Mandarin) via FunASR
- **LLM:** Ollama (default `gemma4:e2b`), with vLLM as an alternative provider
- **TTS:** Piper (multiple English and Chinese voices, with browser SpeechSynthesis fallback for Cantonese)
- **Backend:** FastAPI + WebSocket
- **Frontend:** React + Vite
- **Inspection DB:** SQLite from `inspection_grounding` pipeline
- **Vision model:** Ollama multimodal model (default `llama3.2-vision`) for image anomaly annotation

---

## Architecture

```
┌─────────────┐     WebSocket      ┌────────────────────────────────────────────────┐
│  React/Vite  │ ◄──────────────► │              FastAPI Backend                    │
│  (Port 3000) │    JSON msgs     │  (Port 8000)                                    │
└──────┬──────┘                   │                                                │
       │                          │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
       │ audio (webm/wav)         │  │SenseVoice│  │   LLM    │  │   Piper TTS  │  │
       │                          │  │  (STT)   │  │ (Ollama) │  │              │  │
       ▼                          │  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
┌──────────────┐                  │       │             │               │          │
│  Browser TTS │                  │       │    ┌────────┴────────┐     │          │
│  (fallback)  │                  │       │    │ Tool Router     │     │          │
└──────────────┘                  │       │    │ (Ollama tool    │     │          │
                                  │       │    │  calling)       │     │          │
                                  │       │    └────────┬────────┘     │          │
                                  │       │             │               │          │
                                  │       │    ┌────────┴────────┐     │          │
                                  │       │    │ InspectionDB    │     │          │
                                  │       │    │ Client          │     │          │
                                  │       │    │ (SQLite)        │     │          │
                                  │       │    └─────────────────┘     │          │
                                  └────────────────────────────────────────────────┘
```

### Request flow

1. User holds **Space** to record audio via `MediaRecorder` (WebM format).
2. Release sends base64-encoded audio to the backend over WebSocket.
3. Backend transcribes with **SenseVoice** → detects language (en/yue/zh) + emotion.
4. The **Tool Router** (an Ollama call with tool definitions) decides which DB queries are needed.
5. **InspectionDBClient** executes the chosen queries against the SQLite database.
6. The main **LLM** receives the query + DB results + optional report context → streams a text answer.
7. **Piper TTS** synthesizes the answer sentence-by-sentence as LLM tokens arrive → streams audio chunks.
8. Frontend plays audio via Web Audio API; the full text renders as a chat card with inline images.

---

## Backend file reference

### `app/config.py`

**`class Settings(BaseSettings)`** — All configuration via env vars or `.env` file.

| Field | Default | Purpose |
|-------|---------|---------|
| `host` / `port` | `0.0.0.0:8000` | Uvicorn bind address |
| `cors_origin` | `http://localhost:5173` | CORS allowed origin |
| `llm_provider` | `"ollama"` | `"ollama"` or `"vllm"` |
| `llm_model_name` | `"gemma4:26b"` | Primary LLM model |
| `ollama_base_url` | `http://localhost:11434` | Ollama server URL |
| `ollama_model_name` | `"gemma4:26b"` | Ollama-specific model name |
| `ollama_thinking` | `True` | Pass `think` param to Ollama (for models that support `<think>` tags) |
| `llm_n_ctx` | `16384` | Context window size |
| `llm_max_tokens` | `1024` | Max output tokens |
| `llm_temperature` | `0.5` | LLM temperature |
| `llm_history_turns` / `llm_history_char_budget` | `4` / `1600` | Chat history management |
| `tool_router_enabled` | `True` | Enable LLM-based tool routing |
| `tool_router_model` | `"gemma4:26b"` | Model used for tool selection |
| `tool_router_temperature` | `0.0` | Tool router temperature (greedy) |
| `sensevoice_model_dir` | `./models/SenseVoiceSmall` | Path to SenseVoice model |
| `sensevoice_device` | `"cuda:0"` | Device for STT |
| `inspection_db_path` | absolute path | SQLite DB path |
| `inspection_image_dir` | absolute path | Source camera frames |
| `reports_dir` | `../reports` | Anomaly report directory |
| `piper_*` | various | Piper TTS voice/model paths |
| `vision_*` | various | Vision model settings |

Relative paths in `.env` are resolved relative to `backend/`.

---

### `app/main.py` — FastAPI application entry point

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `GET` | `/status` | Runtime snapshot (model loaded, VRAM, TTS status) |
| `GET` | `/voices` | Lists all available Piper voice models |
| `GET` | `/reports/image-list` | Returns URLs of extracted anomaly images |
| `POST` | `/annotate-image` | Upload an image → vision model annotates anomalies |
| `WS` | `/ws` | Main WebSocket for the voice chat pipeline |

**WebSocket message protocol:**

*Client → Server:*
| `type` | Payload | Purpose |
|--------|---------|---------|
| `"user_audio"` | `{ audio_base64, mime_type }` | Send recorded audio |
| `"interrupt"` | `{ request_id? }` | Cancel the current response |
| `"set_voice"` | `{ voice_id }` | Switch TTS voice for this session |
| `"clear_context"` | `{}` | Reset conversation history |

*Server → Client:*
| `type` | Payload | Purpose |
|--------|---------|---------|
| `"ready"` | `{ runtime }` | Connection established, model status |
| `"runtime_update"` | `{ runtime }` | Model loading completed |
| `"transcript"` | `{ transcript, transcript_emotion, transcript_raw, request_id }` | STT result |
| `"tool_calls"` | `{ tool_calls, tool_router_raw, request_id }` | Debug info: what DB tools were selected |
| `"llm_token"` | `{ token, request_id }` | Streaming LLM text token |
| `"tts_audio_chunk"` | `{ audio_base64, sample_rate, request_id, is_final_chunk, tts_voice_id, tts_voice_reason, tts_text_language }` | Streaming TTS audio |
| `"llm_done"` | `{ text, request_id, tts_text_language, tts_voice_reason? }` | LLM stream complete |
| `"interrupted"` | `{ reason, request_id }` | Current request was cancelled |
| `"error"` | `{ error }` | Error message |

---

### `app/models.py` — Pydantic models

- **`ClientAudioMessage`**: inbound user audio (base64, mime type)
- **`ClientInterruptMessage`**: interrupt request (optional `request_id`)
- **`ServerMessage`**: all outbound server message types with optional fields
- **`ImageAnnotationRequest`**: question for the annotator
- **`ImageAnnotationResponse`**: annotated image result (description, base64, annotations list)

---

### `app/services/stt_service.py` — Speech-to-Text (SenseVoice)

**`class SenseVoiceSTT`**

| Method | Description |
|--------|-------------|
| `_init_model()` | Loads FunASR `AutoModel` from `sensevoice_model_dir`. Falls back gracefully if model files are missing. |
| `_resolve_device()` | Checks CUDA capability vs torch arch list; falls back to CPU if incompatible. |
| `transcribe_with_metadata(audio_bytes, suffix)` | Writes audio to a temp file, runs FunASR `generate()`, returns `STTResult` with cleaned text, language tag, and emotion tag. |
| `transcribe_bytes(audio_bytes, suffix)` | Convenience wrapper that returns just the text. |
| `_extract_language_tag(raw_text, payload)` | Parses `<\|...\|>` tags from SenseVoice output for language detection (`zh`, `yue`, `en`, etc.). |
| `_extract_emotion_tag(raw_text, payload)` | Parses emotion tags (`happy`, `sad`, `angry`, `neutral`, etc.) from SenseVoice output. |
| `_normalize_emotion(value)` | Maps various emotion strings to a canonical set: happy, sad, angry, fear, surprised, disgust, neutral. |
| `_clean_transcript(text)` | Strips all `<\|...\|>` tags and normalizes whitespace. |

**`@dataclass STTResult`**: `text`, `language_tag`, `emotion_tag`, `raw_text`

---

### `app/services/tts_service.py` — Text-to-Speech (Piper)

**`class PiperTTS`**

| Method | Description |
|--------|-------------|
| `__init__` | Resolves piper binary, loads voice model paths for English, Chinese, Cantonese. Sets up persistent g2pW cache. |
| `preload_voices()` | Warms up English and Chinese voice models by synthesizing a short test phrase. Returns `{ english: bool, chinese: bool }`. |
| `runtime_status()` | Returns `tts_available`, current/last voice IDs and reasons. |
| `resolve_voice_model(voice_id)` | Locates an onnx model by voice ID or path. |
| `resolve_tts_language(text, stt_language_tag)` | Determines which language voice to use by combining detected script (CJK vs Latin) with the STT language tag. |
| `list_available_voices()` | Lists all `.onnx` files in the piper voices directory with their config paths. |
| `stream_synthesize(text, voice_model_path, stt_language_tag)` | Main TTS entry point. Splits mixed-language text into runs, selects the best model for each run, and yields WAV chunks. |
| `synthesize(text, ...)` | Non-streaming convenience that joins all chunks. |
| `_stream_single_language_synthesis(...)` | Tries candidate models in priority order (selected voice → language-specific → fallback → default). Uses `piper-tts` Python bindings first, then the CLI binary. |
| `_stream_with_python_piper(text, model_path)` | Uses the `piper.voice.PiperVoice` Python API with a voice cache to avoid reloading models. Yields WAV chunks. |
| `_synthesize_with_model(text, model_path)` | Falls back to the piper CLI binary. Detects the `zh_CN-xiao_ya-medium` CLI incompatibility and handles it gracefully. |
| `_split_text_for_streaming(text)` | Simple regex sentence splitter. |
| `_split_text_with_pysbd(text, language)` | PySBD-based sentence segmentation with language-specific segmenter cache. |
| `_split_mixed_language_runs(text)` | Splits text into contiguous runs of CJK and Latin scripts so each can be synthesized by the appropriate voice. |
| `_sanitize_for_chinese_voice(text)` | Strips Latin characters for Chinese voice models that cannot handle them. |
| `_detect_language(text)` | Heuristic: counts CJK vs Latin characters to decide `"chinese"`, `"english"`, or `None`. |
| `_language_from_tag(tag)` | Maps language tags (`"yue"`, `"cmn"`, `"en"`, etc.) to canonical names. |
| `_resolve_piper_executable()` | Searches for the piper binary (configured path, `/opt/piper/piper`, `$PATH`). |
| `_validate_paths()` | Sets `_fallback = True` if the binary or model is missing. |

**Voice selection priority:**
1. Explicit `voice_model_path` passed by the client (via `set_voice`)
2. For Cantonese → Cantonese-specific model (or browser fallback)
3. For Chinese → primary Chinese → fallback Chinese → English default
4. For English → selected voice → default model

---

### `app/services/llm_service.py` — LLM provider

**`class LocalLLM`**

| Method | Description |
|--------|-------------|
| `stream_reply(prompt, chat_history, tool_calls_callback)` | Main entry point. Calls `InspectionDBClient.lookup()` to gather DB context, optionally fetches report context if `get_report_summary` was called, then streams the LLM response token by token. |
| `_system_prompt()` | Returns the system prompt instructing the LLM about its role, style, thinking mode, image link handling, coordinate formatting, and summarization rules. |
| `_build_messages(prompt, chat_history, db_context, report_context)` | Assembles the message array: system prompt → optional DB context → optional report context → up to `llm_history_turns` turns of conversation → current user prompt. |
| `preload_model()` | Sends a minimal "Hi" request to warm the model up. |
| `runtime_status()` | Probes Ollama or vLLM for reachability, configured model, and running models list. |
| `_stream_ollama(messages, model_name)` | Streams from Ollama `/api/chat`. Handles `<think>...</think>` tags by stripping thinking content from the output so only the final answer reaches the user. |
| `_stream_vllm(messages)` | Streams from vLLM `/v1/chat/completions`. Auto-discovers the correct model name if 400/404 errors occur, and halves `max_tokens` on context-length errors. |
| `_stream_vllm_with_retries(messages)` | Retries vLLM up to 6 times with exponential backoff (handles cold-start race conditions). |
| `_discover_vllm_model(client, headers)` | Probes `/v1/models` to find the actual deployed model name when the configured name is wrong. |

**Two-pass architecture:**
1. The tool router makes the **first** LLM call (in `db_service.lookup()`) to decide which DB queries to run.
2. `stream_reply` makes the **second** LLM call with the DB results injected as system context.

---

### `app/services/pipeline.py` — Voice pipeline orchestrator

**`class VoicePipeline`**

| Method | Description |
|--------|-------------|
| `__init__(settings)` | Instantiates STT, DB client (with optional ToolRouter), report client, LLM, and TTS. |
| `handle_audio(audio_bytes, suffix, voice_model_path, chat_history, tool_calls_callback)` | The core async generator. Steps: (1) transcribe audio → yield `transcript`, (2) stream LLM tokens → yield `llm_token`, (3) for each sentence-ending boundary, synthesize TTS and yield `tts_audio_chunk`, (4) flush remaining TTS at end → yield `llm_done`. |
| `_stream_tts_segments(segments, voice_model_path, language_tag, request_id, mark_last_final)` | Synthesizes a list of text segments and yields `ServerMessage` + byte count pairs. |
| `_extract_flushable_segments(text, keep_last_complete)` | Splits accumulated text at sentence boundaries (`.。！？!?;；\n`). When `keep_last_complete=True`, retains one completed sentence in the remainder to avoid wrong "final" markers mid-stream. |
| `_clean_for_tts(text)` | Strips markdown image tags `![...](...)`, link syntax `[...](...)`, URLs, bold/italic/backtick markers, bullets, numbered lists, parentheses, and normalizes whitespace. Protects decimal numbers so coordinates like `-18.22` survive cleanup. |

---

### `app/services/tool_router.py` — LLM-based tool selection

**`class ToolRouter`**

Uses Ollama's native tool-calling API to decide which `InspectionDBClient` methods to run. This is the *first* of two LLM calls.

| Method | Description |
|--------|-------------|
| `select_tool(query)` | Sends the user query + system prompt + 32 tool definitions to Ollama at `tool_router_temperature=0.0` (greedy). Returns a list of `(tool_name, args)` tuples. Falls back to empty list on HTTP/parse errors. |
| `_system_prompt()` | A detailed prompt describing every tool, its purpose, expected output, example queries, and multi-tool flow examples. |

**Special case:** If the query contains anomaly/finding/issue keywords, `get_report_summary` is forced without calling the LLM.

**`TOOLS`** — List of 32 tool definitions (see the full tool table below).

**`REDUCED_TOOLS`** — A minimal 3-tool set exposing only `get_categories`, `query_database`, and `get_report_summary`. Not currently active.

**`_ollama_tools()`** — Converts the internal tool definitions into Ollama's `{"type": "function", "function": {...}}` format.

If the router is disabled (`tool_router_enabled=false`) or fails, `InspectionDBClient.lookup()` falls back to rule-based keyword matching.

---

### `app/services/db_service.py` — SQLite inspection database client

**`class InspectionDBClient`**

The largest service file (~1650 lines). Provides structured queries, a natural-language `lookup()` method, tool execution routing, and result formatting.

**Properties:**
- `last_tool_calls` / `last_tool_results` — Records what tools were selected and their outputs, forwarded to the frontend debug panel.

**Connection management:**
| Method | Description |
|--------|-------------|
| `_connect()` | Lazily opens a SQLite connection with `row_factory = sqlite3.Row`. |
| `close()` | Closes the connection. |

**Query methods (each returns raw dict/list data):**

| Method | Description |
|--------|-------------|
| `get_summary()` | Total object count + per-category counts from `objects` table. |
| `get_categories()` | Distinct category names. |
| `get_objects_by_category(category, limit)` | Objects in a category sorted by `total_point_count DESC`. |
| `get_category_objects_with_coordinates(category)` | All objects in a category with centroid + bbox3d, ordered by `first_seen_ns`. |
| `get_category_proximity(target_category, other_categories, radius_m)` | Per-target-object counts of nearby objects from other categories within radius. |
| `get_object_by_track_id(track_id)` | Full object detail + all per-frame observations. |
| `get_top_objects(n)` | Largest objects by `total_point_count`. |
| `get_recent_objects(limit)` | Most recently seen objects by `last_seen_ns`. |
| `get_object_timeline(track_id)` | Every observation for a track, ordered by timestamp. |
| `get_object_image_paths(track_id)` | Distinct image paths for a track. |
| `get_category_timeline(category)` | First/last seen for every object in a category. |
| `get_inspection_timeline()` | All objects ordered by `first_seen_ns`. |
| `get_category_windows(categories)` | First/last seen windows for multiple categories. |
| `get_temporal_clusters(window_ms, top_n)` | Consecutive runs of first-seen timestamps grouped by a time window, with category counts per cluster. |
| `get_observation_counts_by_category()` | Per-frame observation counts per category from `observations` table. |
| `get_objects_in_time_range(start, end, limit)` | Objects whose detection span overlaps a time window. |
| `get_observations_in_time_range(start, end, limit)` | Per-frame observations within a time window. |
| `get_objects_near_position(x, y, z, radius_m, category?)` | Objects within radius of a 3D point. |
| `get_category_sample_images(category, limit)` | Random sample of image paths for a category. |
| `get_inspection_poses(limit)` | Camera/robot poses from `inspection_poses` table. |
| `get_filtered_objects(limit)` | Objects dropped by the merge/filter layer. |
| `get_object_distance(track_id_a, track_id_b)` | Euclidean distance between two track centroids. |
| `get_category_bounding_box(category)` | Axis-aligned 3D bounding box of all objects in a category. |
| `get_category_observation_timeline(category, bucket_seconds)` | Per-time-bucket observation counts. |
| `get_objects_by_category_in_time_range(category, start, end, limit)` | Objects of a category overlapping a time window. |
| `get_object_movement(track_id)` | Centroid path of a track across its observations. |
| `get_nearest_objects_to_track(track_id, radius_m)` | Other objects near a specific track's centroid. |
| `get_images_in_time_range(start, end, category?, limit)` | Sample images from a time window. |
| `get_category_cooccurrence(window_ms, top_n)` | Which category pairs appear together most often in temporal clusters. |
| `get_objects_in_temporal_cluster(center_time, window_ms, limit)` | Objects with coordinates detected around a specific time. |
| `run_sql_query(query, limit)` | Executes an arbitrary read-only `SELECT` query with safety checks. |

**Natural language routing:**
| Method | Description |
|--------|-------------|
| `lookup(query)` | Decides whether the query is DB-related (checks ~80 keywords). If yes, tries the LLM router first; if that returns nothing, falls back to regex-based keyword matching for track IDs, categories, coordinates, clusters, etc. |

**Time parsing:**
| Method | Description |
|--------|-------------|
| `_parse_time_string(value)` | Converts ISO datetimes, clock times (`"16:51:45"`, `"4:51 PM"`), or nanosecond integers to epoch-ns. Uses the inspection base date and auto-shifts by ±12h for colloquial AM/PM omission. |

**Formatters (each returns formatted text for LLM context injection):**
- `_format_summary()`, `_format_category()`, `_format_object()`, `_format_top_objects()`...
- All `_format_*` methods (30+ of them) produce clean text descriptions from the raw query results.

**Category aliases** map colloquial terms to DB strings:
`"ad board"`/`"poster"` → `"Advertisement Board"`, `"light"` → `"Lights"`, `"gate"` → `"Ticket Gate"`, etc.

---

### `app/services/report_service.py` — Inspection report reader

**`class InspectionReportClient`**

| Method | Description |
|--------|-------------|
| `_load_context()` | Reads all `.txt` and `.pdf` files from `reports_dir`. Uses `pdftotext` (system tool) for PDF extraction. Caches the result. |
| `get_context()` | Returns the cached combined report text, or loads it on first call. |
| `get_image_urls()` | Lists anomaly images in `reports_dir/extracted_images/` as `/reports/images/<filename>` URLs. |
| `lookup(query)` | Packages the report context with image references. Used when the tool router selected `get_report_summary`. |

---

### `app/services/vision_service.py` — Image anomaly annotator

**`class VisionAnnotator`**

| Method | Description |
|--------|-------------|
| `annotate(image_bytes, question)` | Sends the image + question to an Ollama vision model, parses the JSON response, draws annotations on the image, and returns `{ description, annotated_image_base64, annotations }`. |
| `_analyze_with_ollama(image_bytes, question)` | Constructs a prompt asking the vision model to return normalized coordinates (0.0–1.0) in a JSON structure. Tries `format: "json"` first, falls back to plain generation if the model doesn't support it. |
| `_annotation_prompt(question, width, height)` | Builds a detailed prompt instructing the vision model to identify anomalies and return `{ "description": "...", "annotations": [...] }` with box/circle/highlight types. |
| `_extract_json(raw_content)` | Extracts a JSON object from the model output, tolerating markdown fences, trailing text, and nested braces. |
| `_normalize_annotations(raw_annotations)` | Validates and normalizes annotation dicts into `Annotation` dataclass instances. |
| `_draw_annotations(image_bytes, annotations)` | Draws boxes, circles, and highlight polygons on the image using Pillow. Colors cycle through a palette. |
| `_draw_label(draw, label, x, y, color, font)` | Draws a colored label background + white text above an annotation. |
| `_load_font()` | Searches system font paths for a bold truetype font (DejaVu, Liberation, Noto, Helvetica, Arial). |
| `_hex_to_rgba(hex_color, alpha)` | Converts hex color string to RGBA tuple. |
| `_annotation_to_dict(ann)` | Converts `Annotation` back to a dict for the API response. |

**`@dataclass Annotation`**: `type` ("box"|"circle"|"highlight"), `label`, `color`, coordinates.

**Supported annotation types:**
- `"box"` with `x1, y1, x2, y2` (normalized 0–1)
- `"circle"` with `cx, cy, radius` (normalized)
- `"highlight"` with `points` (list of `[x, y]` pairs)

---

### `app/services/runtime_status.py`

**`get_vram_status()`** — Runs `nvidia-smi` to query GPU memory usage. Returns `vram_used_mb`, `vram_total_mb`, `vram_percent`, `gpu_available`.

---

## Frontend file reference

### `src/App.jsx` — Main application component

State management for the entire voice chat UI. Key responsibilities:

- **WebSocket lifecycle**: Connects via `useWebSocket`, dispatches all message types.
- **Push-to-talk**: Keyboard listeners for `Space` key (down = start recording, up = stop & send). If the assistant is speaking, pressing Space interrupts first.
- **Audio playback**: Routes TTS audio chunks to `useAudioPlayback` for sequential Web Audio playback; falls back to `window.speechSynthesis` when no server TTS was generated (Cantonese fallback).
- **Message handling**: Processes `ready`, `runtime_update`, `transcript`, `tool_calls`, `llm_token`, `tts_audio_chunk`, `llm_done`, `interrupted`, `error` message types.
- **Chat history**: Maintains a list of `{ id, userText, assistantText, interrupted }` turns, capped at 12.
- **Interrupt handling**: Cancels active pipeline tasks on the server, tracks interrupted request IDs to ignore late-arriving tokens.

### `src/hooks/useWebSocket.js`

Manages WebSocket connection lifecycle:
- Automatic reconnect with exponential backoff (1s → 2s → 4s → ... → 30s max)
- Message queue for messages sent while disconnected (flushed on reconnect)
- `onMessage` stored in a ref so the handler can change without reconnecting

Returns `{ socketState: "connecting"|"connected"|"reconnecting"|"disconnected"|"error", send }`.

### `src/hooks/useRecorder.js`

Manages microphone recording via `MediaRecorder`:
- Calls `getUserMedia` once and reuses the stream
- Records to `audio/webm` (with fallback to browser default)
- Converts the Blob to base64 and calls `onReady({ base64, mimeType })`
- Returns `{ isRecording, startRecording, stopRecording }`

### `src/hooks/useAudioPlayback.js`

Sequential audio playback via Web Audio API:
- Decodes base64 → `AudioBuffer` and plays through a promise chain
- Generation-based cancellation: incrementing a counter invalidates queued chunks
- Tracks `ttsStreamActive` state for UI feedback
- Methods: `ensureAudioContext`, `enqueueChunk`, `stopAll`, `hasPendingPlayback`

### `src/components/TranscriptCards.jsx`

Live cards showing:
- **You**: current transcript text + raw SenseVoice output + emotion emoji
- **Assistant**: streaming text (rendered by `MarkdownImageText`) with clickable image thumbnails → lightbox

### `src/components/ChatHistory.jsx`

Scrollable history of previous turns, each showing user text and assistant text + inline images. Supports lightbox on any rendered image.

### `src/components/MarkdownImageText.jsx`

**`renderTextWithImages(text, onImageClick)`** — Splits text around markdown image links `![alt](/path/to/image.jpg)` and renders:
- Regular text as `<span>`
- Images as clickable `<button>` thumbnails (resolved against `API_ROOT`)

Supports optional lightbox management (internal or delegated via `onImageClick`).

### `src/components/StatusPanel.jsx`

Displays runtime info: model name, English/Chinese/Cantonese voices, current voice + reason, VRAM usage.

### `src/components/DebugPanel.jsx`

Collapsible debug view showing:
- Raw tool-calling model response (the full Ollama message object)
- Every tool call: name, arguments, raw output
  
Hidden when there are no tool calls.

### `src/components/ImageAnnotator.jsx`

Standalone annotation widget:
1. Pick an image file
2. Type a question (e.g., "What anomalies are in this image?")
3. Click "Annotate Image" → POST to `/annotate-image`
4. Shows original preview + annotated result side-by-side
5. Description text from the vision model
6. Lightbox for full-size annotated image

The `annotate_image` tool is also exposed to the voice-pipeline tool router, so the assistant can automatically annotate inspection images during chat (e.g. "Highlight anomalies on the image of track 218"). Annotated results are cached under `/annotated/images/`.

### `src/components/ReportImageGallery.jsx`

Fetches `/reports/image-list` on mount and displays anomaly images as a clickable thumbnail grid with lightbox. Hidden when no images exist.

---

## Tool reference (32 tools available to the LLM router)

Each tool maps to an `InspectionDBClient` method. The tool router's system prompt includes detailed descriptions, expected output, and example queries for every tool.

| Tool name | Args | Returns | Use case / Example query |
|-----------|------|---------|--------------------------|
| `get_summary` | none | Total count + per-category breakdown | "What did you find?", "How many objects overall?" |
| `get_observation_counts_by_category` | none | Per-frame observation counts per category | "How many times were lights detected?" |
| `get_objects_by_category` | `category`, `limit` (10) | Track IDs, observations, points, centroid | "Tell me about the advertisement boards" |
| `get_object_by_track_id` | `track_id` | Full detail + per-frame observations | "Tell me about track 218" |
| `get_top_objects` | `n` (5) | Tracks sorted by total point count | "What were the biggest objects?" |
| `get_recent_objects` | `limit` (5) | Most recently seen objects | "What did the robot see most recently?" |
| `get_object_timeline` | `track_id` | First/last seen, duration, key moments | "When was track 218 seen?" |
| `get_object_image_paths` | `track_id` | Markdown image links for the track's frames | "Show me images of track 218" |
| `get_category_timeline` | `category` | First/last seen per-object in a category | "When were the lights detected?" |
| `get_category_windows` | `categories` (list) | First/last detection for each category | "When were lights and ticket gates detected?" |
| `get_category_objects_coordinates` | `category` | Centroid + bbox3d per object, ordered | "What are the coordinates of the ticket gates?" |
| `get_category_proximity` | `target_category`, `other_categories`, `radius_m` (2.0) | Per-target nearby counts | "Which objects are close to ticket gates?" |
| `get_inspection_timeline` | none | Chronological log of all objects | "Walk me through the inspection" |
| `get_temporal_clusters` | `window_ms` (500), `top_n` (10) | Busiest moments grouped by time | "Were objects seen together?", "Busiest moment?" |
| `get_report_summary` | none | Anomaly report text + image references | "What anomalies did you find?" |
| `get_objects_in_time_range` | `start_time`, `end_time`, `limit` (50) | Objects overlapping a time window | "What happened between 4:51 and 4:52?" |
| `get_observations_in_time_range` | `start_time`, `end_time`, `limit` (50) | Per-frame detections in a time window | "Show me detections around 16:53:45" |
| `get_objects_near_position` | `x`, `y`, `z`, `radius_m` (2.0), `category?` | Objects within radius of (x, y, z) | "What is near (-18, 32, -6)?" |
| `get_category_sample_images` | `category`, `limit` (5) | Random sample of image links | "Show me some advertisement boards" |
| `get_inspection_poses` | `limit` (20) | Camera/robot pose records | "What poses did the camera have?" |
| `get_filtered_objects` | `limit` (50) | Objects dropped by the merge layer | "What was filtered out?" |
| `get_object_distance` | `track_id_a`, `track_id_b` | Distance between two track centroids | "How far apart are track 218 and track 165?" |
| `get_category_bounding_box` | `category` | Axis-aligned 3D bbox of all objects | "What area do the ticket gates occupy?" |
| `get_category_observation_timeline` | `category`, `bucket_seconds` (60) | Per-bucket observation counts | "When were most lights seen?" |
| `get_objects_by_category_in_time_range` | `category`, `start_time`, `end_time`, `limit` (50) | Category objects in a time window | "Were there ticket gates after 4:53?" |
| `get_object_movement` | `track_id` | Centroid path across observations | "Did track 218 move?" |
| `get_nearest_objects_to_track` | `track_id`, `radius_m` (2.0) | Objects near a specific track | "What was near track 218?" |
| `get_images_in_time_range` | `start_time`, `end_time`, `category?`, `limit` (5) | Sample images from a time window | "Show me what the camera saw at 4:51" |
| `get_category_cooccurrence` | `window_ms` (500), `top_n` (10) | Category pairs that appear together | "Which objects are usually seen together?" |
| `get_objects_in_temporal_cluster` | `center_time`, `window_ms` (500), `limit` (50) | Objects + coordinates around a time | "Where was the cluster at 4:51 PM?" |
| `run_sql_query` | `query` (SELECT), `limit` (100) | Arbitrary SQL query result | Ad-hoc questions, category-specific summaries |
| `annotate_image` | `image_url` XOR `track_id` XOR `category`, `question?` | Annotated image via vision model | "Highlight anomalies on the image of track 218" |
| `query_database` | `sql_query`, `limit` (100) | Alias for `run_sql_query` | Ad-hoc SQL |
| `get_categories` | none | List of distinct categories | "What categories exist?" |

### Multi-tool flow examples

The router can call multiple tools in a single turn for compound questions:
- "Tell me about the inspection objects and their coordinates, and which objects were close to ticket gates" → `get_summary` + `get_category_objects_coordinates("Ticket Gate")` + `get_category_proximity("Ticket Gate", [...])`
- "What anomalies did you find, and how many objects were detected?" → `get_summary` + `get_report_summary`
- "How many times were advertisement boards seen, and what is near (-18, 32, -6)?" → `get_observation_counts_by_category` + `get_objects_near_position(-18, 32, -6)`

---

## Database schema

The inspection SQLite database (`inspection_mtr.db`) contains these tables:

**`objects`** — One row per distinct tracked object:
| Column | Type | Description |
|--------|------|-------------|
| `track_id` | INTEGER PK | Unique object ID |
| `category` | TEXT | e.g. Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign |
| `observation_count` | INTEGER | Number of per-frame observations |
| `total_point_count` | INTEGER | Total LiDAR points |
| `centroid_x/y/z` | REAL | 3D centroid |
| `bbox3d_min_x/y/z` | REAL | Min corner of 3D bounding box |
| `bbox3d_max_x/y/z` | REAL | Max corner of 3D bounding box |
| `first_seen_ns` | INTEGER | First detection timestamp (epoch ns) |
| `last_seen_ns` | INTEGER | Last detection timestamp (epoch ns) |
| `aggregated_pcd_path` | TEXT | Path to merged point cloud |

**`observations`** — One row per per-frame detection:
| Column | Type | Description |
|--------|------|-------------|
| `timestamp_ns` | INTEGER | Detection time (epoch ns) |
| `track_id` | INTEGER | FK to objects |
| `category` | TEXT | Category name |
| `image_file_name` | TEXT | Frame image filename |
| `image_path` | TEXT | Full path to the image |
| `centroid_x/y/z` | REAL | Per-frame centroid |
| `point_count` | INTEGER | Points in this observation |
| `pcd_path` | TEXT | Per-frame point cloud path |
| `mask_path` | TEXT | Instance mask path |

**`inspection_poses`** — Camera/robot pose per image:
| Column | Type | Description |
|--------|------|-------------|
| `image_path` | TEXT | Frame image |
| `tf_translation_x/y/z` | REAL | Camera translation |
| `tf_rotation_x/y/z/w` | REAL | Camera rotation (quaternion) |

**`filtered_objects`** — Audit of tracks dropped by the merge layer:
| Column | Type | Description |
|--------|------|-------------|
| `track_id` | INTEGER | Dropped track ID |
| `category` | TEXT | Category |
| `reason` | TEXT | Why it was dropped |
| `point_count` | INTEGER | Points at drop time |
| `first_seen_ns` / `last_seen_ns` | INTEGER | Time span |
| `created_at` | INTEGER | When the record was written |

---

## Question types and response patterns

### Counts and summaries
```
User: "How many objects did we find?"
Tools: get_summary
LLM injects: Total aggregated objects: 345. Objects by category: Lights: 235, ...
Response: "We found 345 objects in total. The majority were lights — about 235 — along with 53 advertisement boards, 29 ticket gates, and smaller numbers of maps, TVs, and exit signs."
```

### By category
```
User: "Tell me about the lights"
Tools: get_objects_by_category(category="Lights", limit=10)
Response: "I found 235 distinct light objects. A typical example is track 1 with 4 observations and 402 points..."
```

### By track ID
```
User: "Tell me about track 218"
Tools: get_object_by_track_id(track_id=218)
Response: "Track 218 is a Ticket Gate with 12 observations and 1,847 points. It was first seen at 16:51:27 and was visible for about 1.8 seconds."
```

### Largest objects
```
User: "What are the largest objects?"
Tools: get_top_objects(n=5)
Response: "The top 5 largest objects by point count: Track 383 (TV) with 15,321 points over 105 observations..."
```

### Recent detections
```
User: "What was seen most recently?"
Tools: get_recent_objects(limit=5)
Response: "The most recently detected object was Track 1455 (Ticket Gate) at 16:53:46, followed by..."
```

### Temporal / timeline
```
User: "When did you see the lights?"
Tools: get_category_timeline(category="Lights")
Response: "Lights were detected from around 16:50:40 to 16:53:46. There are 235 distinct light objects..."
```

Image and cluster questions include inline markdown image links that the frontend renders as clickable thumbnails:
```
User: "Show me images of track 218"
Tools: get_object_image_paths(track_id=218)
Response: "Here are the frames for track 218: ![Track 218 frame](/inspection/images/1781167925584555000.jpg)"
```

### Coordinates
```
User: "What are the coordinates of the ticket gates?"
Tools: get_category_objects_coordinates(category="Ticket Gate")
Response: "Ticket gates are located around x: -18.50 to -15.20, y: 30.10 to 45.30, z: -7.10 to -5.90. For example, Track 218 at (-18.22, 32.17, -6.85)..."
```

### Proximity
```
User: "Are ticket gates close to advertisement boards?"
Tools: get_category_proximity(target_category="Ticket Gate", other_categories=["Advertisement Board", "Lights", ...])
Response: "There are 45 lights and 12 advertisement boards within 2 meters of ticket gates. For example, Track 218 at (-18.22, 32.17, -6.85) has 2 lights and 1 ad board nearby."
```

### Time-window clusters
```
User: "What were the busiest moments?"
Tools: get_temporal_clusters(window_ms=500, top_n=10)
Response: "The busiest moment was around 4:51:45 PM when 106 lights were detected in a 500ms window..."
```

### Anomalies and reports
```
User: "What anomalies did you find?"
Tools: get_report_summary (forced by anomaly keyword detection)
Response: "The inspection report shows 5 images with anomalies: 3 with cracks in the wall, 1 with a foreign object near the tracks, and 1 with a loose cable..."
```

### Mixed questions
```
User: "What was the order of ticket gates, their coordinates, and are they close to lights?"
Tools: get_category_windows(["Ticket Gate"]) + get_category_objects_coordinates("Ticket Gate") + get_category_proximity("Ticket Gate", ["Lights"])
Response: "Ticket gates were first detected at 16:51:27 and last seen at 16:53:46... Their coordinates range from ... There are 45 lights within 2m of ticket gates."
```

### Cross-source (DB + reports)
```
User: "Were there any anomalies near the ticket gates?"
Tools: get_report_summary + get_category_proximity(target_category="Ticket Gate", other_categories=["Lights", "Advertisement Board", ...])
Router: The tool router detects "anomalies" → forces get_report_summary, and also "ticket gates" + "near" → adds get_category_proximity.
LLM injects: Report context: "Found cracks in wall panels near gate 3..." + Proximity data: "45 lights and 12 ad boards within 2m of ticket gates."
Response: "The inspection report found cracks in the wall panels near gate 3. Around the ticket gate area specifically, there are 45 lights and 12 advertisement boards within 2 meters — none of which showed anomalies themselves, but the cracked panel was right next to Track 218."
```

```
User: "What's the summary of detected objects and are there any defects in the report?"
Tools: get_summary + get_report_summary
Router: Calls get_summary for the object overview and get_report_summary because "defects" is an anomaly keyword.
Response: "We found 345 objects total — 235 lights, 53 advertisement boards, 29 ticket gates, and smaller counts of maps, TVs, and exit signs. As for defects, the inspection report notes cracks in 3 wall panels near the advertisement boards on the east concourse and one loose cable near track 5."
```

```
User: "Tell me about the largest objects and whether any of them had issues"
Tools: get_top_objects(n=5) + get_report_summary
Response: "The largest object was Track 383 (TV) with 15,321 points over 105 observations. Looking at the report, none of the top 5 objects had anomalies directly associated with them, but the report does mention general issues with wall cracks and loose cabling elsewhere in the station."
```

```
User: "Show me images of the objects that had anomalies"
Tools: get_report_summary → (report text mentions specific tracks or image filenames) + get_object_image_paths(track_id=...) for each
Note: The tool router calls get_report_summary. The main LLM reads the report context (which lists anomaly images like img-003.jpg and img-007.jpg) and answers using the anomaly image URLs from the report context.
Response: "The anomalies were found in these inspection frames: ![anomaly](/reports/images/img-003.jpg) ![anomaly](/reports/images/img-007.jpg). These show cracks on the east wall panel and a loose cable near the track bed."
```

### Image annotation (via tool router)
The `annotate_image` tool lets the voice assistant itself request a vision-model analysis. When the router selects this tool, the backend fetches a frame from the inspection image directory, sends it to the Ollama vision model, draws annotations on it, and returns the annotated image as a markdown link.

```
User: "Highlight any cracks on the image of track 218"
Tools: get_object_image_paths(track_id=218) → (resolves first frame URL) → annotate_image(image_url="/inspection/images/1781167925584555000.jpg", question="Highlight any cracks")
Response: "I analyzed the frame for track 218 and highlighted the areas with cracks: ![annotated](/annotated/images/a1b2c3d4.png). The vision model found a crack running along the left edge of the panel near the ticket gate."
```

```
User: "Can you circle the anomaly on an advertisement board image?"
Tools: annotate_image(category="Advertisement Board", question="Circle the anomaly on this advertisement board")
Router picks a random sample image of an advertisement board, then calls annotate_image.
Response: "Here's a sample advertisement board frame with the anomaly circled: ![annotated](/annotated/images/e5f6g7h8.png). There appears to be a torn poster in the upper right corner of the advertisement board."
```

```
User: "Draw boxes around all the defects in the inspection report images"
Tools: get_report_summary → (reads img-003.jpg and img-007.jpg from report images) → annotate_image(image_url="/reports/images/img-003.jpg", question="Draw boxes around all defects") + annotate_image(image_url="/reports/images/img-007.jpg", question="Draw boxes around all defects")
Response: "I analyzed the anomaly images from the report. In the first frame: ![annotated](/annotated/images/b2c3d4e5.png) — a box around the wall crack on the left panel. In the second frame: ![annotated](/annotated/images/f6g7h8i9.png) — a box around the loose cable near the bottom."
```

```
User: "Mark what's wrong with this image and tell me if that area was near anything else detected"
Tools: annotate_image(image_url="/inspection/images/1781168192465731000.jpg", question="Mark what's wrong") + get_objects_near_position(x=-18.22, y=32.17, z=-6.85, radius_m=3.0)
Note: The annotate_image tool returns coordinates of the anomaly. The main LLM can use those coordinates to construct a proximity query, or the router plans both tools together.
Response: "I found a cracked panel in this frame: ![annotated](/annotated/images/j0k1l2m3.png). That location is about 2.1 meters from Track 218 (a Ticket Gate) and there are 3 lights within 3 meters of the crack site."
```

### Multi-hop temporal + spatial + cross-category
```
User: "Walk me through what happened at 4:51 PM — which objects were seen, where they were, and which ones were near each other"
Tools: get_objects_in_temporal_cluster(center_time="16:51:45", window_ms=5000) + get_category_proximity(target_category="Lights", other_categories=["Advertisement Board", "Ticket Gate", "Map", "TV", "Exit Sign"])
Router: Identifies "at 4:51 PM" as a temporal cluster query and "near each other" as a proximity query.
Response: "At around 4:51 PM, there was a cluster of 106 lights and 35 advertisement boards. The lights were concentrated along the main concourse between x: -20 to -10 and y: 30 to 45. Near those lights, there were 12 advertisement boards within 2 meters and the first ticket gate appeared around 4:51:27. It looks like the robot was passing through the main ticketing hall at that time."
```

```
User: "Compare the Lights and Ticket Gates — when were each first seen, how many are there, what area do they cover, and are there any anomalies near them?"
Tools: get_category_windows(categories=["Lights", "Ticket Gate"]) + get_objects_by_category(category="Lights") + get_objects_by_category(category="Ticket Gate") + get_category_bounding_box(category="Lights") + get_category_bounding_box(category="Ticket Gate") + get_report_summary
Router: Unpacks "compare" → windows + counts, "area" → bounding boxes, "anomalies near them" → report.
Response: "Lights were first seen at 16:50:40 and last at 16:53:46 — 235 in total — spread across the full concourse from x: -25 to -5 and y: 20 to 50. Ticket Gates appeared later at 16:51:27 through 16:53:46 — 29 gates — concentrated at x: -18 to -15 and y: 30 to 45. The inspection report does not mention anomalies near either category, but the walls near the lights at the east end had some cracks noted."
```

### Full inspection debrief
```
User: "Give me a complete debrief of this inspection run — what was detected, what were the busiest moments, what anomalies were found, and show me the relevant images"
Tools: get_summary + get_temporal_clusters(window_ms=500, top_n=5) + get_report_summary
Router: Identifies "debrief" as needing summary + clusters + report.
Response: "Here's the full inspection debrief. We detected 345 objects across 6 categories over an 8.5-minute run starting at 16:50. The busiest moment was at 4:51 PM when 106 lights were seen in under a second as the robot entered the main hall. As for anomalies, the report flags 5 frames showing wall cracks, a torn advertisement poster, and a loose cable near the track bed.
Anomaly images: ![anomaly](/reports/images/img-003.jpg) ![anomaly](/reports/images/img-007.jpg)
Sample frames from the busiest moment: ![frame](/inspection/images/1781168192465731000.jpg)"
```

---

## Configuration reference

### `backend/.env`

```bash
# Server
HOST=0.0.0.0
PORT=8000
CORS_ORIGIN=http://localhost:5173

# LLM (Ollama)
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL_NAME=gemma4:e2b
OLLAMA_THINKING=true
LLM_N_CTX=16384
LLM_MAX_TOKENS=1024
LLM_TEMPERATURE=0.5

# LLM (vLLM alternative)
VLLM_BASE_URL=http://localhost:8001
VLLM_API_KEY=EMPTY
LLM_FALLBACK_ENABLED=false

# Tool router (LLM-based DB query selection)
TOOL_ROUTER_ENABLED=true
TOOL_ROUTER_MODEL=gemma4:26b

# Vision (image annotation)
VISION_MODEL_PROVIDER=ollama
VISION_MODEL_NAME=llama3.2-vision
VISION_OLLAMA_BASE_URL=http://localhost:11434
VISION_MAX_TOKENS=1024
VISION_TEMPERATURE=0.3
VISION_REQUEST_TIMEOUT_S=120

# STT (SenseVoice)
SENSEVOICE_MODEL_DIR=./models/SenseVoiceSmall
SENSEVOICE_DEVICE=cuda:0

# Inspection DB
INSPECTION_DB_PATH=/home/.../inspection_mtr.db
INSPECTION_IMAGE_DIR=/home/.../camera/right

# Reports
REPORTS_DIR=../reports

# TTS (Piper)
PIPER_EXE_PATH=./bin/piper/piper/piper
PIPER_DEFAULT_VOICE_ID=en_US-hfc_female-medium
PIPER_CHINESE_VOICE_ID=zh_CN-xiao_ya-medium
PIPER_CANTONESE_VOICE_ID=
PIPER_CHINESE_FALLBACK_VOICE_ID=zh_CN-huayan-medium
PIPER_MODEL_PATH=./models/piper/en_US-amy-medium.onnx
PIPER_CONFIG_PATH=./models/piper/en_US-amy-medium.onnx.json
TTS_SAMPLE_RATE=22050
TTS_MIN_CHUNK_CHARS=28
TTS_FIRST_CHUNK_CHARS=16
```

### `frontend/.env.example`

```bash
VITE_WS_URL=ws://localhost:8000/ws
VITE_API_URL=http://localhost:8000
```

---

## Setup

### Docker (Recommended)

```bash
docker compose up --build
```

This starts Ollama, backend (Port 8000), and frontend (Port 3000). Models must already be pulled in Ollama.

### Local

**Backend (Linux/macOS):**
```bash
cd backend
./setup_all.sh        # Installs Python venv + downloads piper binary
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Backend (Windows):**
```powershell
cd backend
.\setup_all.ps1
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

### Required Ollama models

```bash
ollama pull gemma4:e2b       # Chat LLM (text-only)
ollama pull llama3.2-vision  # Vision model for image annotation
```

To use `gemma4:26b` for both chat and vision, set `VISION_MODEL_NAME=gemma4:26b`.

---

## Useful commands

```bash
docker compose up -d --build
docker compose logs -f backend
docker compose ps
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
curl -X POST "http://localhost:8000/annotate-image" -F "image=@frame.jpg" -F "question=What anomalies are in this image?"
sqlite3 /path/to/inspection_mtr.db "SELECT category, COUNT(*) FROM objects GROUP BY category"
```

### Inspect the DB

```bash
sqlite3 /home/wangyiming/code/object_detection_app/output/inspection_mtr.db
```

```sql
.tables
SELECT * FROM objects LIMIT 10;
SELECT category, COUNT(*) FROM objects GROUP BY category ORDER BY COUNT(*) DESC;
SELECT category, COUNT(*) FROM observations GROUP BY category ORDER BY COUNT(*) DESC;
```
