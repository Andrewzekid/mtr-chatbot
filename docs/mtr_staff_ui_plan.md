# MTR Staff Inspection Console — UI Implementation Plan

**Status:** Draft for review
**Target users:** Hong Kong MTR station staff (station controller, maintenance/inspection technician, duty officer)
**Existing system:** LLM voice/text chatbot over an inspection database (objects, detections, images, anomalies) with a Rerun 3D viewer

---

## 1. Background

The current frontend is a push-to-talk voice assistant. Every query — including deterministic lookups such as *"show me the lights"* or *"how many anomalies are there?"* — is routed through the LLM tool router (`ToolRouter`) → tool execution (`InspectionDBClient`) → answering LLM → TTS. This is slow, token-hungry, and error-prone for tasks that are really just structured filters over a known schema.

**Goal:** Build a companion **button-driven inspection console** that lets MTR staff answer the 80% of routine questions with one or two taps (fast, deterministic, bilingual, touch-friendly), and falls back to the voice/text LLM assistant only for genuinely free-form questions.

The plan is split into:
- **Section 5 — Feature catalog**: the extensive feature list, each mapped to an existing `db_service` tool or a new capability.
- **Section 6 — API layer**: new backend JSON endpoints that mirror the tools.
- **Section 7 — Frontend architecture**: new page map, components, state.
- **Section 8 — Backend changes**: JSON serialization, new endpoints, new queries.
- **Section 9–10 — Wireframe description and phased implementation.**

---

## 2. Goals & Non-Goals

### Goals
- One-tap answers for all routine inspection questions currently served by the LLM.
- Reduce reliance on the LLM router for deterministic queries (cost/latency/reliability).
- Touch-first UI usable with gloves on a tablet or a desk monitor with mouse/keyboard.
- Bilingual labels (English + Traditional Chinese; Cantonese-appropriate vocabulary used in HK MTR).
- MTR visual identity (MTR red `#E3002C`, clear hierarchy, high contrast).
- Keep the existing voice assistant intact as a fallback and for open-ended questions.
- Reuse all existing backend tool logic — no duplication of query logic.

### Non-goals (v1)
- Live telemetry / robot control (no commanding the robot from the UI).
- Full multi-user auth/RBAC (single staff workstation; a simple PIN/role selector is enough).
- Predictive ML analytics beyond what the DB already exposes.
- Replacing the Rerun viewer (we push highlights to it, not embed it).

---

## 3. Users & Tasks

| Persona | Primary tasks | Speed requirement |
|---|---|---|
| **Station Controller** | "What's the overall condition?" — counts, anomaly summary, busy periods | < 5 s glance |
| **Inspection / maintenance technician** | "Show me the lights", "where is object 9?", "what's near ticket gate 2?" | < 2 taps |
| **Duty officer / shift handover** | "What anomalies were found after 4 PM?", "print/export the defect list" | < 5 taps |
| **Engineer (rare)** | arbitrary SQL, raw coordinates, cross-inspection comparison | search + expert mode |

---

## 4. Design Principles

1. **Buttons over text; LLM as fallback.** Every routine question has a labelled control. The existing voice/text LLM assistant is **kept intact** at `/assistant` (F90) and is the recommended path only for open-ended, ambiguous, or cross-cutting questions that no single button covers (e.g. "summarise what changed vs the ground truth"). This is a **buttons-first, LLM-optional** split: ~most queries never touch the LLM router; the assistant is always reachable (top-bar quick link) rather than removed.
2. **Big targets.** Minimum 48×48 px touch targets, prominent category tiles.
3. **Zero-input defaults.** Landing dashboard is already a useful answer (no empty states requiring a search).
4. **Progressive disclosure.** Overview → drill-down; filters stay visible but collapsed until needed.
5. **Bilingual, Cantonese-aware.** `EN 中文` labels (e.g. "Lights / 燈光", "Anomalies / 異常"). Voice replies keep the existing EN/Cantonese TTS.
6. **Consistent 3D integration.** Any list that is spatial gets a "Show in 3D" action that pushes highlights to Rerun.
7. **Deterministic, no dead ends.** Every action maps to a known endpoint; errors show a friendly retry, never "ask the bot".
8. **Offline-first reads.** Reads hit SQLite directly; the UI should feel instant (< 100 ms).

---

## 5. Feature Catalog

Each feature: **ID · name · UI · backing tool/endpoint · notes.** Tools refer to `InspectionDBClient` / `ToolRouter.TOOLS`.

### A. Dashboard & Overview

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F01 | **Landing summary** | KPI cards: total objects, total detections, # inspections, # anomalies; category breakdown bar list | `get_summary`, `get_categories`, `get_detection_counts_by_category`, `get_anomaly_summary` | Auto-loaded on app open |
| F02 | **Inspection selector** | Top bar dropdown/chips: "Inspection 1 (Ground truth) · Inspection 2 · All" | `get_inspections` | All other pages scope to this selection; label `is_gt` clearly |
| F03 | **Busiest moments** | List of temporal clusters with category counts | `get_temporal_clusters` | "Top busy moments" card |
| F04 | **Recent activity** | Most recently seen objects list | `get_recent_objects` | |
| F05 | **Category windows** | Which categories appeared when (first/last) | `get_category_windows` | |

### B. Category Explorer (the "show me the lights" replacement)

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F10 | **Category tiles** | Big tiles: Lights, Advertisement Board, Ticket Gate, Map, TV, Exit Sign — each with object count badge | `get_objects_by_category` (count) + `get_summary` | Tiles are the main navigation |
| F11 | **Category object grid** | Grid of object cards: sample image, object id, category, detection count, first/last seen, coordinates | `get_category_objects_with_images` | Click card → object detail (F20) |
| F12 | **Category coordinates view** | Table of object id → centroid (x,y,z) + 3D bbox, sortable | `get_category_objects_coordinates` | "Coordinates" toggle on category page |
| F13 | **Category sample gallery** | Photo strip of random frames for the category | `get_category_sample_images` | |
| F14 | **Category detection timeline** | Bar/line chart of detections per time bucket | `get_category_detection_timeline` | Bucket selector 15s/60s/5m |
| F15 | **Category spatial extent** | Bounding box of the whole category | `get_category_bounding_box` | shown in 3D |

### C. Object Detail (drill-down)

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F20 | **Object detail panel** | Hero image, meta (id, category, inspection, centroid, first/last seen, detection count) | `get_object_by_id` | Opened from any grid |
| F21 | **Object frames gallery** | All frames object appears in, thumbnail strip | `get_object_image_paths` | |
| F22 | **Object timeline** | First/last seen, duration, key moments | `get_object_timeline` | |
| F23 | **Movement path** | Start/end/waypoints, displacement | `get_object_movement` | shown in 3D as path |
| F24 | **Nearby objects** | "Objects within X m of this object" list with distances + images | `get_nearest_objects_to_object` | radius stepper |
| F25 | **Jump to similar** | Same-category objects quick links | `get_objects_by_category` | |

### D. Time & Timeline Exploration

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F30 | **Global time range filter** | Start/end time pickers + presets (All / 4–5 PM / last 15 min / busy peak) | `get_inspection_poses` (bounds) | Applied to any list page |
| F31 | **Objects in time range** | Objects whose span overlaps the window | `get_objects_in_time_range` | |
| F32 | **Detections in time range** | Per-frame detection log | `get_detections_in_time_range` | |
| F33 | **Images in time range** | Sampled frames in window (optionally by category) | `get_images_in_time_range` | |
| F34 | **Category × time** | Objects of one category within the window | `get_objects_by_category_in_time_range` | |
| F35 | **What happened at time T** | Objects/detections around a chosen moment | `get_objects_in_temporal_cluster` | click a busy-moment row → this |

### E. Proximity Explorer

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F40 | **Category proximity wizard** | Pick target category + other categories + radius slider (1–10 m) | `get_category_proximity_with_images` | shows nearby pairs with distances + images |
| F41 | **Proximity counts only** | Same wizard, counts table | `get_category_proximity` | |
| F42 | **Near specific objects** | Select object ids, find target-category objects near them | `get_objects_proximity_with_images` | object id multi-select |
| F43 | **Near a coordinate** | x,y,z inputs + radius → objects within radius | `get_objects_near_position` | |
| F44 | **Co-occurrence insight** | Category pairs seen together most | `get_category_cooccurrence` | |

### F. Anomaly / Defect Review

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F50 | **Anomaly summary** | Cards per anomaly type (missing_object, foreign_object, relocation, state_change, crack & structure damage, stain/graffiti, content_change) + per-inspection counts | `get_anomaly_summary`, `get_anomaly_types` | Red-badged section |
| F51 | **Anomaly list** | Filter by type / inspection / id; each row: type, object, location, note, GT vs inspection image side-by-side | `get_anomalies` | **primary maintenance screen** |
| F52 | **Anomaly map** | "Show anomalies in 3D" → highlights anomaly locations in Rerun | `get_anomaly_locations`, `anomaly_location_points`, highlight | |
| F53 | **Anomaly annotation** | "Annotate this image" → runs vision annotation on selected frame | `annotate_image` / `/annotate-image` | reuses existing vision service |
| F54 | **New-anomaly detection flag** | "Anomalies not in ground truth run" filter | `get_anomalies` + `get_inspections` (is_gt) | cross-inspection filter |

### G. 3D Viewer Integration (Rerun)

The UI talks to the Rerun viewer exclusively through the backend's existing
`RerunVisualizer` (`rerun_service.py`), exposed via three thin endpoints (§6.4).
The viewer itself runs as a separate window/process; the UI never speaks Rerun
directly — it sends highlight/clear commands and *reports viewer health* back to
staff.

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F60 | **Show in 3D** | Action button on any object card, category tile, anomaly row, proximity result, or "objects in time range" list → pushes highlight to Rerun | `RerunVisualizer.highlight` via POST `/api/rerun/highlight` | The one-click "see it in the station map" affordance, reused everywhere spatial data is listed |
| F61 | **Clear markings** | "Clear 3D" button in the top bar + on the anomaly map page | `RerunVisualizer.clear` via POST `/api/rerun/clear` | |
| F62 | **Highlight keep/add mode** | Global toggle: **Replace** (default) vs **Add to current** → sets `keep_existing` on every highlight call | `keep_existing` arg | Staff often compare two sets (e.g. lights, then add exits); toggle is sticky |
| F63 | **Station map base** | Highlights composite over the station map in the viewer | `rerun_map_points_path` / `rerun_map_enabled` (existing) | no frontend change, just ensure enabled |
| F64 | **Viewer connection chip** | Live chip in the top bar: **Connected · Auto-launching · Disconnected** — driven by the backend's TCP probe on `rerun_viewer_addr` | new `RerunVisualizer.status()` → GET `/api/rerun/status` | Polled like `/status`; green/amber/red dot |
| F65 | **Launch viewer** | "Launch 3D viewer" button when disconnected (only if `rerun_auto_spawn`); after click the chip flips to "auto-launching…" | reuses `rerun_auto_spawn` + a probe-backed poll | no double-spawn: backend re-probes before spawning |
| F66 | **Highlight feedback** | After any highlight/clear, a toast/snackbar shows the returned status string; a small **Highlight log** panel lists recent commands (what was highlighted, when, ok/failed) | `RerunVisualizer.job_stats` (`jobs_ok`/`jobs_failed`/`last_ok_at`) + returned status strings | staff see "lights highlighted in viewer" and can tell if the push failed |
| F67 | **3D for time/proximity results** | "Highlight all objects in this window" / "highlight these N nearby objects" buttons on F33/F35/F40 result lists | object_ids + `inspection_id` from the list payloads | bridges time & proximity views into 3D |

### H. Search & Lookup

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F70 | **Object id search** | Big numeric input "Go to object #…" | `get_object_by_id` | number pad friendly |
| F71 | **Image filename search** | "What's in this frame?" filename input | `get_objects_in_image` | |
| F72 | **Category text search** | type-ahead across category tiles | `get_categories` | |
| F73 | **Distance between two objects** | object_id_a/b pickers | `get_object_distance` | |

### I. Export & Reporting

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F80 | **Export current view (CSV)** | Download the active table/grid as CSV | new endpoint wrapping query result | |
| F81 | **Export anomalies (CSV/JSON)** | Download anomaly list with current filters | `get_anomalies` → CSV | shift handover use |
| F82 | **Print-friendly inspection report** | Aggregated page (summary + category counts + anomaly table + busy moments) that `window.print()` renders cleanly | F01/F50/F03 data | |
| F83 | **Share/deep-link state** | URL encodes inspection, category, time range so views are bookmarkable | frontend only | |

### J. Assistant & System

| ID | Feature | UI | Backing tool(s) | Notes |
|---|---|---|---|---|
| F90 | **Ask the assistant (fallback)** | Existing voice + text chat panel; prefilled with current context ("on inspection 2, lights") | existing `pipeline.handle_text/audio` | keeps open-ended coverage |
| F91 | **Runtime status** | Model/GPU/STT/TTS health chips | `/status`, `/health` | reuse `StatusPanel` |
| F92 | **Connection state** | WS + DB reachability banner | `/health`, `/api/info` | |
| F93 | **Live refresh** | Poll "DB updated" heartbeat (file mtime / row counts) and auto-refresh dashboard | new `/api/info` | inspection writes are external |

---

## 6. New Backend API Layer

The chat path currently produces markdown text. The console needs JSON. We add a **read-only REST API** that returns the tool methods' dicts directly. Recommended: a new module `app/api.py` with an `APIRouter`, mounting structured endpoints that call the existing `InspectionDBClient` methods unchanged (no LLM).

### 6.1 Endpoint map (feature → endpoint)

| Method | Path | Feature(s) | Returns |
|---|---|---|---|
| GET | `/api/info` | F92/F93 | db path, mtime, counts, inspections |
| GET | `/api/inspections` | F02 | inspection list (`get_inspections`) |
| GET | `/api/summary?inspection_id=` | F01 | summary dict |
| GET | `/api/categories` | F01/F10 | category list + counts |
| GET | `/api/detection-counts?inspection_id=` | F01 | per-category detection counts |
| GET | `/api/temporal-clusters?window_ms=&top_n=&inspection_id=` | F03 | clusters |
| GET | `/api/recent-objects?limit=&inspection_id=` | F04 | recent objects |
| GET | `/api/category/{name}/objects?limit=&inspection_id=` | F11 | objects with sample image |
| GET | `/api/category/{name}/coordinates?inspection_id=` | F12 | centroids + bbox |
| GET | `/api/category/{name}/images?limit=&inspection_id=` | F13 | sample image URLs |
| GET | `/api/category/{name}/timeline?bucket_seconds=&inspection_id=` | F14 | per-bucket counts |
| GET | `/api/category/{name}/windows?inspection_id=` | F05 | first/last windows |
| GET | `/api/category/{name}/extent?inspection_id=` | F15 | category bounding box |
| GET | `/api/objects/{id}` | F20 | object detail |
| GET | `/api/objects/{id}/frames` | F21 | frame URLs |
| GET | `/api/objects/{id}/timeline` | F22 | detections timeline |
| GET | `/api/objects/{id}/movement` | F23 | centroid path |
| GET | `/api/objects/{id}/nearby?radius_m=&inspection_id=` | F24 | nearby objects + distances |
| GET | `/api/time-range/objects?start=&end=&inspection_id=` | F31 | objects in range |
| GET | `/api/time-range/detections?start=&end=&inspection_id=` | F32 | detections in range |
| GET | `/api/time-range/images?start=&end=&category=&limit=` | F33 | images in range |
| GET | `/api/time-range/category/{name}?start=&end=&inspection_id=` | F34 | category objects in range |
| GET | `/api/temporal-cluster?center_time=&window_ms=&inspection_id=` | F35 | around-time snapshot |
| GET | `/api/proximity?target=&others=&radius_m=&inspection_id=` | F40/F41 | proximity (counts or with images) |
| GET | `/api/proximity/objects?object_ids=&target=&radius_m=` | F42 | near specific objects |
| GET | `/api/near-position?x=&y=&z=&radius_m=&category=` | F43 | objects near coordinate |
| GET | `/api/cooccurrence?window_ms=&top_n=` | F44 | co-occurrence pairs |
| GET | `/api/anomalies?type=&inspection_id=&limit=` | F51/F54 | anomaly rows (with image URLs) |
| GET | `/api/anomalies/summary?inspection_id=` | F50 | anomaly summary |
| GET | `/api/anomalies/locations?inspection_id=` | F52 | anomaly 3D locations |
| GET | `/api/anomalies/types` | F50 | anomaly type names |
| GET | `/api/images/{filename}/objects` | F71 | objects in one frame |
| GET | `/api/objects/distance?a=&b=` | F73 | distance between two objects |
| GET | `/api/rerun/status` | F64/F66 | `{listening, viewer_addr, auto_spawn, app_id, job_stats}` |
| POST | `/api/rerun/highlight` | F60/F62/F67 | body `{object_ids?, categories?, coordinates?, inspection_id?, label?, keep_existing?}` → calls `RerunVisualizer.highlight`, returns `{status}` |
| POST | `/api/rerun/clear` | F61 | → calls `RerunVisualizer.clear`, returns `{status}` |
| POST | `/api/export` | F80/F81 | `{queryName, filters}` → CSV (Content-Disposition download) |
| POST | `/annotate-image` | F53 | existing endpoint (no change) |

### 6.2 Response conventions
- All responses wrap tool dicts directly (`{...}` or `{items: [...]}`) with snake_case keys matching the DB methods.
- Timestamps returned as ISO 8601 in addition to raw `*_ns` so the frontend doesn't need to convert.
- `inspection_id` query param optional everywhere; omitted = all inspections.
- Errors: `{"error": "..."}` with 400/404/500 as appropriate; never leak SQL.

### 6.3 Concurrency/refresh
- `InspectionDBClient` holds one connection; for the console we reuse the same client but add a `refresh()` that reopens the connection when the DB file mtime changes (the writer updates the DB externally).

### 6.4 Rerun viewer communication (mechanics)
Communication is **one-way backend → viewer**; the UI → backend → viewer. This matches the existing `RerunVisualizer` design:
- **Highlight** = `POST /api/rerun/highlight` with `{object_ids, categories, coordinates, inspection_id, label, keep_existing}`. The handler normalizes args exactly like `_highlight_in_rerun` (extract that normalization into a shared helper so the LLM path and the API path stay identical), calls `rerun_visualizer.highlight(**args)`, and returns the status string. It is **non-blocking**: `RerunVisualizer.highlight` only does a fast TCP probe and enqueues the work on its background daemon thread, so the UI gets an instant response.
- **Clear** = `POST /api/rerun/clear` → `rerun_visualizer.clear()`.
- **Status / health** = `GET /api/rerun/status`. Add a small `RerunVisualizer.status()` method returning `{enabled, listening, viewer_addr, auto_spawn, app_id, job_stats}` where `listening = self._viewer_listening()` (a 0.5 s TCP probe on `rerun_viewer_addr`). The UI polls this to drive the F64 connection chip and F66 highlight-feedback log. This uses only the already-existing `_viewer_listening()` and `job_stats()`.
- **Launch** = rely on existing auto-spawn: `RerunVisualizer` spawns a viewer (`rerun_auto_spawn`) when a highlight is requested while `_viewer_listening()` is false. The F65 button simply issues a no-op highlight (e.g. the current selection) or a probe-trigger; the backend's own probe prevents double-spawn.
- **keep_existing / "add to current"** = passed straight through as the `keep_existing` flag (`RerunVisualizer` clears previous markings before a highlight unless `keep_existing=true`).
- Because every highlight result carries `jobs_ok` / `jobs_failed` via `job_stats()`, the F66 feedback log distinguishes "pushed to viewer OK" from "failed" — important since the viewer may be closed/unreachable.

---

## 7. Frontend Architecture

### 7.1 Stack decisions
- **Keep** React 18 + Vite (existing repo). No new framework.
- **Routing:** add `react-router-dom` (single new dep) OR use a lightweight tab/state router. Recommendation: `react-router-dom` with hash history (works from `file://`-style previews and doesn't need server rewrites).
- **Styling:** extend the existing `styles.css` with a BEM-ish component layer; CSS variables for MTR palette. No Tailwind (avoid a big new toolchain); no component library (hand-rolled components stay lightweight and fully branded).
- **Charts:** `get_category_detection_timeline` and busy moments render best as simple bars. Use pure CSS/SVG bars for v1 (zero deps); add `recharts` only if line/area charts are requested.
- **Data layer:** a `useApi` hook (fetch + abort + cache) wrapping the REST endpoints; small in-memory cache keyed by URL with 30 s TTL + manual refresh.

### 7.2 Page map (routes)

```
/                     Dashboard (F01–F05) + time filter bar (F30)
/categories           Category tiles (F10)
/categories/:name     Category page: grid (F11), toggles for coordinates(F12)/gallery(F13)/timeline(F14)
/objects/:id          Object detail (F20–F25)
/anomalies            Anomaly review (F50–F54)
/explore/proximity    Proximity wizard (F40–F44)
/explore/time         Time explorer (F30–F35)
/search               Object id / frame / distance lookup (F70–F73)
/assistant            Voice/text assistant fallback (F90) + runtime status (F91–F92)
```

A **global top bar** carries: MTR brand mark, inspection selector (F02), global time-range filter (F30), the **Rerun connection chip + Clear-3D button** (F64/F61), "Ask assistant" quick link (F90), and connection status chip (F92). Footer: DB mtime + highlight feedback log (F66).

### 7.3 Component tree (new files under `frontend/src/`)

```
src/
  api/client.js            fetch wrapper + cache + error normalization
  state/ConsoleState.jsx   context: selected inspection_id, time range, db info, refresh tick
  components/
    TopBar.jsx             brand, inspection selector, time filter, connection chip
    KpiCards.jsx           F01 summary cards
    CategoryTiles.jsx      F10 big tiles
    ObjectGrid.jsx         F11/F25 object cards grid (shared)
    ObjectCard.jsx         image + id + meta; onClick → /objects/:id
    CoordsTable.jsx        F12 sortable coordinate table
    FrameStrip.jsx         F13/F21/F33 thumbnail strips
    TimeFilterBar.jsx      F30 presets + pickers (shared global control)
    RadiusStepper.jsx      F24/F40 radius control
    ProximityWizard.jsx    F40–F42
    AnomalyList.jsx        F51 rows w/ GT-vs-inspection images
    AnomalyTypeBadges.jsx  F50 cards
    TimelineBars.jsx       F14/F03 CSS bar charts
    SearchPanel.jsx        F70–F73
    AssistantPanel.jsx     embeds existing TranscriptCards + ChatHistory + PTT (F90)
    RerunStatusChip.jsx    F64/F65 viewer connection chip (polls /api/rerun/status)
    ShowIn3DButton.jsx     F60/F62 → POST /api/rerun/highlight; variant buttons carry
                           the current row's object_ids / categories / coordinates
    HighlightLog.jsx       F66 recent highlight/clear commands + ok/failed status
    EmptyState.jsx         friendly no-data / error states
  hooks/
    useApi.js              GET hook w/ cache + abort
    useDbRefresh.js        polls /api/info mtime, bumps refresh tick
    useUrlState.js         syncs inspection/time filters to URL query params (F83)
```

`App.jsx` becomes a router shell; the existing WebSocket chat hooks move into `AssistantPanel` unchanged.

### 7.4 Interaction patterns
- **Category → grid → object → nearby:** 3 taps max to any spatial fact.
- Every object card / anomaly row has a **"3D" icon button** (ShowIn3DButton). Tapping it highlights in the viewer **without leaving the page**, and the top-bar chip toasts the result (F66) so staff see "lights highlighted in viewer".
- The **Add-to-current** toggle (F62) changes the button semantics from "replace viewer marks" to "append"; the chip label flips to "Add mode".
- Time filter is global and sticky: pages read it from `ConsoleState`.
- Busy-moment rows are clickable → `/explore/time?center_time=…` (F35).
- Tables have a persistent **Export CSV** button (F80) in the page header.

---

## 8. Backend Changes (beyond the API layer)

1. **`app/api.py`** (new): `APIRouter` with all endpoints in §6.1. Each handler: parse `inspection_id`, call the existing method, normalize timestamps, return JSON. Reuse `InspectionDBClient` instance (same singleton the pipeline builds).
2. **`main.py`**: `app.include_router(api.router)`; add static mount for annotated images already exists; expose `/api/info`.
3. **`InspectionDBClient.refresh()`** (new): if `db_path.stat().st_mtime` differs from the last-seen, close + reopen connection. Called lazily at the start of each API call.
4. **New DB methods (small, additive):**
   - `count_rows_per_table()` or reuse `run_sql_query` internally for `/api/info`.
   - `get_objects_by_category(..., offset=)` pagination support (categories can be large; F11 needs paging).
   - A `to_json()`-friendly timestamp normalizer: convert `*_ns` → ISO string inline in API layer.
5. **Highlight/Rerun endpoints** (`/api/rerun/status|highlight|clear`) call the existing `RerunVisualizer`. Extract the arg-normalization from `_highlight_in_rerun` into a reusable helper so the API and LLM paths share it. Add a small `RerunVisualizer.status()` method returning `{enabled, listening, viewer_addr, auto_spawn, app_id, job_stats}` (reuses `_viewer_listening()` and `job_stats()`).
6. **CSV export** (F80/F81): small helper serializing a list of dicts → CSV bytes; set `Content-Disposition`.
7. **CORS**: already allow `settings.cors_origin`; add the Vite dev origin for the new app if different.
8. **No LLM in the loop** for any console endpoint — deterministic, fast, and testable.

---

## 9. UI Design & Wireframe Notes (v1)

- **Layout:** 12-column grid; dashboard = 4 KPI cards top row + category breakdown left + busy moments right.
- **Category tiles:** 6 large rectangular cards with icon, EN/中文 name, live count badge. Red-tinted hover; active page highlighted MTR red.
- **Object card:** ~220×160 image (object-fit cover) + caption `#12 · Lights · 34 detections` + small "3D" chip.
- **Anomaly row:** left = inspection frame, right = GT frame, middle = type badge (color-coded by type), object, location, note, "3D" chip, "Annotate" chip.
- **Time filter:** compact bar — [All ▾ presets] [start time] [end time] [Apply] [Clear]. Collapses to a chip on narrow screens.
- **Color system (CSS vars):** `--mtr-red:#E3002C; --ink:#0d0d0d; --paper:#f5f6f7; --ok:#00843D; --warn:#F5A623;` type badges use distinct hues.
- **Fonts:** system stack + `Noto Sans TC` for Chinese; large base (16–18 px), min touch target 48 px.
- **Responsive:** tablet portrait (primary) → single column stacks; desktop → dense grids.

---

## 10. Phased Implementation Plan

### Phase 0 — Foundation (0.5–1 day)
- [ ] `app/api.py` skeleton + `/api/info`, `/api/inspections`, `/api/categories`, `/api/summary`
- [ ] `RerunVisualizer.status()` + `/api/rerun/status` + `/api/rerun/highlight` + `/api/rerun/clear`
- [ ] `InspectionDBClient.refresh()` + mtime detection
- [ ] `useApi` hook + `ConsoleState` context + `TopBar` (with RerunStatusChip) + router shell
- [ ] Dashboard page (F01/F02/F92) with KPI cards

**Exit criteria:** dashboard renders live counts from the DB without the LLM; top-bar chip shows viewer Connected/Disconnected.

### Phase 1 — Category & object browsing (1–1.5 days)
- [ ] `/api/category/{name}/objects|coordinates|images|timeline` + pagination
- [ ] Category tiles page (F10), category page (F11–F15), object detail page (F20–F25)
- [ ] Frame strips, coords table, radius stepper, ShowIn3D buttons

**Exit criteria:** "show me the lights" is exactly 1 tap; object detail reachable in ≤ 3 taps.

### Phase 2 — Time & proximity explorers (1 day)
- [ ] Time-range endpoints + global TimeFilterBar (F30–F35)
- [ ] Proximity endpoints + wizard UI (F40–F44)
- [ ] URL state binding (F83)

### Phase 3 — Anomalies + export (1 day)
- [ ] Anomaly endpoints (F50–F54), anomaly review page, type badges, GT-vs-inspection comparison
- [ ] Wire `/api/rerun/highlight` + `/api/rerun/clear` to the Shared normalization helper; anomaly map (F52), object/category 3D (F60/F62), time/proximity 3D (F67), HighlightLog + feedback toasts (F66)
- [ ] CSV export (F80/F81), print report (F82)

### Phase 4 — Assistant fallback + polish (0.5–1 day)
- [ ] `AssistantPanel` embedding existing chat/PTT (F90)
- [ ] Runtime status chips (F91), live refresh (F93), empty/error states, bilingual audit
- [ ] E2E walkthrough of every feature vs §5 table

### Phase 5 — Testing & hardening (0.5–1 day)
- [ ] Backend: pytest for API layer (compare JSON against tool dicts)
- [ ] Frontend: manual checklist per feature; touch-target audit
- [ ] Performance pass (cached queries, big-category pagination)

**Total estimate: ~5–7 working days.**

---

## 11. Testing Plan

- **API unit tests:** each endpoint returns the same data as the corresponding `InspectionDBClient` method (golden tests against the real DB fixture).
- **Determinism check:** a fixed query (`/api/category/Lights/objects`) returns identical JSON across calls — proving no LLM in the loop.
- **Frontend manual checklist:** every row in the §5 feature table has a test step (tap → expected result → 3D action works).
- **Touch audit:** all interactive elements ≥ 48×48 px; keyboard navigable for desk use.
- **Resilience:** DB file replaced mid-session (writer running) → `refresh()` picks it up; WS down → assistant page shows banner, console pages unaffected.

---

## 12. Risks & Open Questions

| Risk / question | Mitigation / decision needed |
|---|---|
| Coordinate frame vs station map legibility for non-experts | Show "nearby objects" lists first; coordinates secondary; rely on 3D highlights for spatial intuition |
| Multi-inspection blending (per § system prompt warnings) | Default selector = "All" but every page labels per-inspection counts; comparison view explicit |
| Large categories → slow image grids | Server-side pagination + lazy `<img loading="lazy">`; sample limit default 50 |
| DB writer concurrency | Single-writer assumption; mtime-based reconnect; document "stop writer before schema changes" |
| Anomaly tables optional (`_anomaly_tables_exist`) | UI hides anomaly section with "No anomaly data" empty state when absent |
| Bilingual vocabulary | Confirm preferred terms with HK MTR staff: 燈光/廣告牌/閘機/地圖/電視/出口標誌, 異常/缺陷 |
| New deps policy | Only `react-router-dom` added; charts hand-rolled in v1 |
| Should the console replace the chat app or sit beside it? | Recommended: same SPA, `/assistant` route keeps chat; keep old build reachable during migration |

---

## 13. Future Enhancements (post-v1)

- **Live inspection monitor:** tail new detections as the robot writes (poll + WebSocket push).
- **Anomaly workflow:** assign/acknowledge/close defects, status history (needs new DB tables).
- **Station zone map:** cluster objects into zones (platform/concourses) from 3D coords for "zone" filters.
- **Trending/drift analytics:** cross-inspection diff (objects moved, added, missing) using existing multi-inspection data.
- **Role-based landing:** controller vs technician dashboards.
- **Embedded Rerun panel:** iframe/WebView of the viewer in-app instead of a separate window.
- **PDF report generation** with MTR stationery header.
