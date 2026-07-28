from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    cors_origin: str = "http://localhost:5173"

    # LLM provider: ollama (preferred) or vllm
    llm_provider: str = "ollama"
    llm_model_name: str = "gemma4:26b"
    vllm_base_url: str = "http://localhost:8001"
    vllm_api_key: str = "EMPTY"
    llm_fallback_enabled: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_model_name: str = "gemma4:26b"
    ollama_thinking: bool = True
    llm_request_timeout_s: float = 10.0
    llm_n_ctx: int = 16384
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.5
    llm_history_turns: int = 4
    llm_history_char_budget: int = 1600

    # Image anomaly annotation (annotate_image tool + /annotate-image endpoint).
    # Annotation reuses the SAME base LLM as chat — llm_model_name at ollama_base_url —
    # so the base model must be a multimodal/vision-capable Ollama model (e.g. gemma4:26b,
    # qwen2.5-vl, llava). The fields below only tune the annotation task, not the model.
    vision_max_tokens: int = 1024
    vision_temperature: float = 0.3
    vision_request_timeout_s: float = 120.0
    # Extra attempts to ask the vision model for valid JSON when its output fails to parse.
    vision_max_retries: int = 2

    # Tool router (LLM-based intent classification for DB queries)
    # Defaults to the main LLM so the base model decides which tools to call.
    tool_router_enabled: bool = True
    tool_router_model: str = "gemma4:26b"
    tool_router_temperature: float = 0.0
    tool_router_n_ctx: int = 16384

    # Speech-to-text backend: "sensevoice" (FunASR, default) or "whisper" (faster-whisper).
    stt_backend: str = "sensevoice"

    # SenseVoice (FunASR) — primary STT backend.
    # Official model is iic/SenseVoiceSmall. point sensevoice_model_dir at a different FunASR
    # model snapshot to swap in a larger ASR model.
    sensevoice_model_dir: str = "./models/SenseVoiceSmall"
    sensevoice_device: str = "cuda:0"

    # Whisper (faster-whisper / CTranslate2) — opt-in alternative STT backend (STT_BACKEND=whisper).
    # Model size or HuggingFace repo: large-v3 (best, EN/zh/yue/Cantonese), medium, small, or
    # a Systran faster-whisper repo id. large-v3 with int8_float16 fits ~1.5-2GB VRAM.
    whisper_model: str = "large-v3"
    whisper_device: str = "cuda:0"  # "cuda:0", "cpu", or "auto"
    whisper_compute_type: str = "int8_float16"  # float16 (more VRAM, slightly faster), int8, int8_float16
    whisper_language: str = ""  # "" = auto-detect; or "en", "zh", "yue" to force
    whisper_beam_size: int = 5
    whisper_vad_filter: bool = True  # Silero VAD trims silence/noise before transcription
    whisper_download_root: str = "./models/whisper-cache"  # CTranslate2 model cache dir

    # Inspection SQLite database (MTR object grounding results).
    # New multi-inspection schema: categories / inspections / images / objects / detections
    # (plus anomaly_types / abnormal_detections / abnormalities, added by the writer later).
    # Relative paths resolve against the backend/ root (see model_post_init).
    inspection_db_path: str = "../MTR_Database/inspection_v2.db"

    # Inspection anomaly reports (text + PDF summaries)
    reports_dir: str = "../reports"

    # Source camera images referenced by images.filename (served at /inspection/images).
    inspection_image_dir: str = "../MTR_Database/outputs/images"

    # Rerun 3D visualization. The chatbot pushes highlighted coordinates/objects to a
    # separately-running Rerun viewer (start it with `rerun`) over TCP. Disabled or
    # unreachable viewers degrade to a friendly status string, never a failed turn.
    rerun_enabled: bool = True
    rerun_viewer_addr: str = "127.0.0.1:9876"
    # Rerun application id. Matches the grounding pipeline's rerun_bridge_node
    # (inspection_grounding_rerun) so highlights share the grounding scene's recording
    # space and coordinate frame.
    rerun_app_id: str = "inspection_grounding_rerun"
    # Leveling rotation [roll, pitch, yaw] in degrees, mirroring the grounding pipeline's
    # rerun_bridge_node `leveling_rpy_deg` param. The DB stores object centroids/bboxes in
    # the tilted camera_init frame; pre-rotating by this matrix lands them level on the
    # grounding map (same convention the bridge uses for world/bboxes3d). "0.0,20.0,0.0"
    # matches the 2026-06-11 inspection run.
    rerun_leveling_rpy_deg: str = "0.0,20.0,0.0"
    # If true, the backend auto-launches a Rerun viewer (rr.spawn) the first time it has
    # something to visualize and no viewer is reachable on RERUN_VIEWER_ADDR. Set false to
    # require a manually-started `rerun` viewer (the earlier "connect to running viewer" mode).
    rerun_auto_spawn: bool = True

    # Cache directory for vision-annotated images served under /annotated/images
    annotated_image_cache_dir: str = "./annotated_images"

    # WebSocket keep-alive (Uvicorn/websockets protocol pings). Increase these if you
    # see reconnects behind proxies or on idle connections. The frontend also sends an
    # application-level heartbeat every 15 seconds, so these are a fallback.
    ws_ping_interval_s: float = 60.0
    ws_ping_timeout_s: float = 60.0

    # Piper TTS
    piper_exe_path: str = "./bin/piper/piper/piper.exe"
    piper_voices_dir: str = "./models/piper"
    piper_default_voice_id: str = "en_US-hfc_female-medium"
    piper_chinese_voice_id: str = "zh_CN-xiao_ya-medium"
    piper_cantonese_voice_id: str = ""
    piper_chinese_fallback_voice_id: str = "zh_CN-huayan-medium"
    piper_model_path: str = "./models/piper/en_US-amy-medium.onnx"
    piper_config_path: str = "./models/piper/en_US-amy-medium.onnx.json"
    tts_sample_rate: int = 22050
    tts_min_chunk_chars: int = 28
    tts_first_chunk_chars: int = 16

    def model_post_init(self, __context: object) -> None:
        self.sensevoice_model_dir = self._resolve_backend_path(self.sensevoice_model_dir)
        self.whisper_download_root = self._resolve_backend_path(self.whisper_download_root)
        self.reports_dir = self._resolve_backend_path(self.reports_dir)
        self.inspection_db_path = self._resolve_backend_path(self.inspection_db_path)
        self.inspection_image_dir = self._resolve_backend_path(self.inspection_image_dir)
        self.annotated_image_cache_dir = self._resolve_backend_path(self.annotated_image_cache_dir)
        self.piper_exe_path = self._resolve_backend_path(self.piper_exe_path)
        self.piper_voices_dir = self._resolve_backend_path(self.piper_voices_dir)
        self.piper_model_path = self._resolve_backend_path(self.piper_model_path)
        self.piper_config_path = self._resolve_backend_path(self.piper_config_path)

    @staticmethod
    def _resolve_backend_path(path_value: str) -> str:
        path = Path(path_value)
        if path.is_absolute():
            return str(path)
        return str((BACKEND_ROOT / path).resolve())


@lru_cache
def get_settings() -> Settings:
    return Settings()
