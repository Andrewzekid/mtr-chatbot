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

    # Vision model for image anomaly annotation (Ollama preferred)
    vision_model_provider: str = "ollama"
    vision_model_name: str = "llama3.2-vision"
    vision_ollama_base_url: str = "http://localhost:11434"
    vision_max_tokens: int = 1024
    vision_temperature: float = 0.3
    vision_request_timeout_s: float = 120.0

    # Tool router (LLM-based intent classification for DB queries)
    # Defaults to the main LLM so the base model decides which tools to call.
    tool_router_enabled: bool = True
    tool_router_model: str = "gemma4:26b"
    tool_router_temperature: float = 0.0
    tool_router_n_ctx: int = 16384

    # SenseVoice (FunASR)
    sensevoice_model_dir: str = "./models/SenseVoiceSmall"
    sensevoice_device: str = "cuda:0"

    # Inspection SQLite database (MTR object grounding results)
    inspection_db_path: str = "/home/wangyiming/code/object_detection_app/output/inspection_mtr.db"

    # Inspection anomaly reports (text + PDF summaries)
    reports_dir: str = "../reports"

    # Source camera images referenced by observations.image_path
    inspection_image_dir: str = "/home/wangyiming/code/object_detection_app/Datasets/MTR/rosbags/2026-06-11_16-50-08_rosbag/camera/right"

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
        self.reports_dir = self._resolve_backend_path(self.reports_dir)
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
