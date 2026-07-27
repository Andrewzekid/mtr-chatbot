# MTR-Insight: Hong Kong MTR Inspection Robot Voice Assistant

A voice-to-voice chatbot layer for Hong Kong MTR subway station inspection. It extends the local realtime voice pipeline with a live SQLite integration, so inspectors can ask about objects detected by the automated grounding pipeline.

- STT: SenseVoice (supports English / Cantonese / Mandarin)
- LLM: `gemma4:e2b` via Ollama
- TTS: Piper
- Backend: FastAPI + WebSocket
- Frontend: React + Vite
- Inspection DB: SQLite from `inspection_grounding`
- Vision model: Ollama multimodal model for image anomaly annotation

## Quick Flow

1. Hold `Space` to record a question (e.g. "How many advertisement boards did we find?").
2. Release to send audio to backend.
3. Backend transcribes the question, queries the inspection SQLite database, and streams an LLM answer backed by the live data.
4. The assistant replies with synthesized speech.

## DB Integration

The assistant automatically detects questions about the inspection database and injects the relevant object data into the LLM prompt. Supported question types:

- Counts and summaries: "How many objects did we find?", "What did you detect?"
- By category: "Tell me about advertisement boards", "How many lights?"
- By track ID: "Tell me about track 5", "Object 542"
- Largest / most points: "What are the largest objects?"
- Recent detections: "What were the most recent objects?"
- Temporal story: "When did you see track 5?", "Tell me the timeline of lights"
- Object images: "Show me images of track 5", "Pictures of object 12"
- Time-window clusters: "Show me clusters of objects", "What objects were found together?", "What were the busiest moments?"
- Category comparison: "When were advertisement boards, lights, and ticket gates detected?", "Were ticket gates detected before or after lights?"
- Coordinates: "What are the X, Y, Z coordinates of the ticket gates?"
- Proximity: "Are ticket gates close to advertisement boards or lights? How many are close?"
- Mixed: "What was the order of ticket gates, their coordinates, and are they close to lights?"

Configure the DB path with `INSPECTION_DB_PATH` in `backend/.env` (defaults to `/home/wangyiming/code/object_detection_app/output/inspection_mtr.db`).

Source camera frames are served from `INSPECTION_IMAGE_DIR` (defaults to the rosbag `camera/right` folder) at `/inspection/images/<filename>`. The assistant returns these as markdown image links, and the chat UI renders them as clickable thumbnails.

### LLM-based tool routing

Instead of brittle keyword matching, the assistant uses the main LLM itself with Ollama's native tool calling to decide which database queries to run. This means there are two calls to the base model: one to choose the right tools (summary, timeline, cluster, image, coordinates, proximity, etc.) and a second to produce the final answer. The router falls back to rule-based routing if the model fails to return a valid tool.

Configure it with:

```bash
TOOL_ROUTER_ENABLED=true
TOOL_ROUTER_MODEL=gemma4:e4b
```
                                                                                                                                                                                       
   ┌───────────────────────────────────────┬──────────────────────────────────────────────────────────────────┐                                                                                            
   │ Tool / method                         │ What it returns                                                  │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_summary                           │ Overall counts and category breakdown from the objects table.    │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_objects_by_category               │ List of objects in a category (point count, centroid, bbox).     │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_object_by_track_id                │ Full detail for one track, including its per-frame observations. │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_top_objects                       │ Largest objects by total point count.                            │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_recent_objects                    │ Most recently seen objects.                                      │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_object_timeline                   │ Every observation timestamp for a track.                         │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_object_image_paths                │ Image filenames/URLs for a track.                                │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_category_timeline                 │ First/last seen timestamps for every object in a category.       │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_category_windows                  │ First/last detection windows for one or more categories.         │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_inspection_timeline               │ Chronological log of every object detected.                      │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_temporal_clusters                 │ Time-window clusters showing which objects were seen together.   │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_category_objects_with_coordinates │ Centroid and 3D bounding-box coordinates per object.             │                                                                                            
   ├───────────────────────────────────────┼──────────────────────────────────────────────────────────────────┤                                                                                            
   │ get_category_proximity                │ Count of nearby objects from other categories within a radius.   │                                                                                            
   └───────────────────────────────────────┴────────────────────────────────────────────────────────────────

 SQL commands you can run from any bash prompt (the path to the DB is absolute):                                                                                                                         
                                                                                                                                                                                                           
   ```bash                                                                                                                                                                                                 
     sqlite3 /home/wangyiming/code/object_detection_app/output/inspection_mtr.db                                                                                                                           
   ```                                                                                                                                                                                                     
                                                                                                                                                                                                           
   Then inside sqlite3:                                                                                                                                                                                    
                                                                                                                                                                                                           
   ```sql                                                                                                                                                                                                  
     -- List tables                                                                                                                                                                                        
     .tables                                                                                                                                                                                               

    -- General select from table
    SELECT * FROM table_name LIMIT 10;
                                                                                                                                                                .mode list
    .headers off
    SELECT name FROM pragma_table_info('table_name');

     -- Distinct ad-board objects vs frame-level ad-board observations                                                                                                                                     
     SELECT COUNT(*) FROM objects WHERE category='Advertisement Board';                                                                                                                                    
     SELECT COUNT(*) FROM observations WHERE category='Advertisement Board';                                                                                                                               
                                                                                                                                                                                                           
     -- Counts per category in each table                                                                                                                                                                  
     SELECT category, COUNT(*) FROM objects GROUP BY category ORDER BY COUNT(*) DESC;                                                                                                                      
     SELECT category, COUNT(*) FROM observations GROUP BY category ORDER BY COUNT(*) DESC;                                                                                                                 
                                                                                                                                                                                                           
     -- Example: first 5 ad-board objects                                                                                                                                                                  
     SELECT track_id, category, observation_count, total_point_count, first_seen_ns, last_seen_ns                                                                                                          
     FROM objects                                                                                                                                                                                          
     WHERE category='Advertisement Board'                                                                                                                                                                  
     LIMIT 5;                                                                                                                                                                                              
                                                                                                                                                                                                           
     -- Example: first 5 ad-board observations                                                                                                                                                             
     SELECT timestamp_ns, track_id, category, image_file_name, centroid_x, centroid_y, centroid_z                                                                                                          
     FROM observations                                                                                                                                                                                     
     WHERE category='Advertisement Board'                                                                                                                                                                  
     LIMIT 5;                                                                                                                                                                                              
                                                                                                                                                                                                           
     -- Exit sqlite3                                                                                                                                                                                       
     .quit                                                                                                                                                                                                 
   ```  
### Thinking mode

The assistant is allowed to reason step by step, but any reasoning wrapped in `<think>...</think>` tags is stripped from the final response before it is shown or spoken. The user sees only the concise, direct answer.

### Text-to-speech cleanup

Before a response is spoken, the pipeline strips markdown image links, URLs, bullet markers, bold/italic syntax, and other punctuation that the TTS engine would read aloud. The original formatted text (including inline object images) is still shown in the UI.

## Anomaly Reports

The assistant also reads inspection reports from `local-voice-chatbot/reports/` (text files and PDFs generated by the VLM comparison pipeline). Questions about anomalies, findings, or the inspection summary are answered using both the object database and the report text.

Supported report question types:

- "What anomalies did you find?"
- "Tell me about the inspection report."
- "Were there any foreign objects?"
- "What are the recommendations?"

Configure the reports directory with `REPORTS_DIR` in `backend/.env` (defaults to `../reports`).

### Anomaly images

The backend extracts images embedded in `reports/inspection_report.pdf` into `reports/extracted_images/` and serves them at `/reports/images/<filename>`. The frontend displays these as a clickable gallery below the chat history, so inspectors can visually inspect the anomalous frames while listening to the assistant's summary.

Note: the default chat LLM (`gemma4:e2b`) is text-only. It answers from the report text, but it does not visually analyze the images. The image annotator uses a separate vision model (default `llama3.2-vision`). You can also point the annotator at another multimodal Ollama model such as `gemma4:26b`, `qwen2.5-vl`, or `llava` by setting `VISION_MODEL_NAME`.

## Image Anomaly Annotation

The assistant can analyze uploaded inspection images and visually mark anomalies for you.

1. Scroll to the **Image Anomaly Annotator** card in the chat UI.
2. Choose an image file from the inspection frames.
3. Type a question such as "What anomalies are in this image?" or "Highlight any cracks or stains."
4. Click **Annotate Image**.
5. The backend sends the image to a vision-capable LLM, parses the anomaly locations, and draws boxes, circles, or highlights directly on the image. The annotated image and a concise description are shown in the UI.

### Vision model setup

The annotation feature defaults to `llama3.2-vision` via Ollama. Pull it once:

```bash
ollama pull llama3.2-vision
```

To use `gemma4:26b` (or another multimodal model) instead, pull it and set `VISION_MODEL_NAME`:

```bash
ollama pull gemma4:26b
```

```bash
VISION_MODEL_NAME=gemma4:26b
VISION_OLLAMA_BASE_URL=http://localhost:11434
VISION_MAX_TOKENS=1024
VISION_TEMPERATURE=0.3
```

Other Ollama vision models such as `qwen2.5-vl` or `llava` can be used by changing `VISION_MODEL_NAME` after pulling them.

### API endpoint

You can also call the annotator directly:

```bash
curl -X POST "http://localhost:8000/annotate-image" \
  -F "image=@/path/to/frame.jpg" \
  -F "question=What anomalies are in this image?"
```

The response contains:

- `description` — concise explanation of the findings.
- `annotated_image_base64` — PNG of the image with drawn annotations.
- `annotations` — raw list of normalized annotations (boxes, circles, highlights).

### Inline images in the assistant response

When the text chatbot returns a markdown image link, for example:

> Here is a sample image of an advertisement board from the inspection.  
> `![Advertisement Board sample](/inspection/images/1781168192465731000.jpg)`

both the live **Assistant** streaming card and the **Chat History** card render the image as a clickable thumbnail. Click it to open the lightbox.

## Prerequisites

- Ollama installed and running
- Docker Desktop (for Docker setup)
- Local model assets available:
  - `backend/models/SenseVoiceSmall`
  - `backend/models/piper/en_US-amy-medium.onnx`
  - `backend/models/piper/en_US-amy-medium.onnx.json`
  - `backend/bin/piper/piper/piper` (Linux) or `piper.exe` (Windows)

Pull the LLM once:

```bash
ollama pull gemma4:e2b
ollama pull llama3.2-vision
```

The text model powers the voice chat; the vision model powers the image anomaly annotator. If you prefer to use `gemma4:26b` for both chat and image annotation, pull it and set `VISION_MODEL_NAME=gemma4:26b` in `backend/.env`.

## Run With Docker (Recommended)

```bash
docker compose up --build
```

Endpoints:
- Frontend: `http://localhost:3000`
- Backend: `http://localhost:8000`
- Status: `http://localhost:8000/status`

## Run Locally (Without Docker)

Backend — **Linux/macOS:**

```bash
cd backend
chmod +x setup_backend.sh
./setup_backend.sh
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Backend — **Windows:**

```powershell
cd backend
.\setup_backend.ps1
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Frontend (both platforms):

```bash
cd frontend
npm install
npm run dev
```

## Full Local Setup (models + runtime)

Linux/macOS:

```bash
cd backend
chmod +x setup_all.sh
./setup_all.sh
```

Windows:

```powershell
cd backend
.\setup_all.ps1
```

## Frontend Resilience

The WebSocket hook automatically reconnects with exponential backoff if the backend restarts or the network drops. Messages sent while disconnected are queued and flushed once the connection is restored, so pressing `Space` during a brief outage will still work after reconnect.

## Useful Commands

```bash
docker compose up -d --build
docker compose up -d --build frontend
docker compose ps
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
curl -X POST "http://127.0.0.1:8000/annotate-image" \
  -F "image=@/path/to/frame.jpg" \
  -F "question=What anomalies are in this image?"
```
