from __future__ import annotations

import io
import logging
import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


class WhisperSTT:
    """Speech-to-text via faster-whisper (CTranslate2 Whisper).

    Drop-in replacement for SenseVoiceSTT. Supports English, Mandarin (zh) and
    Cantonese (yue) and returns the detected language code so the TTS layer can
    route to the matching voice. Whisper has no emotion detection, so
    ``emotion_tag`` is always None.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None
        self._fallback = False
        self._fallback_reason = "Whisper is unavailable"
        self._init_model()

    def _init_model(self) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:
            self._fallback = True
            self._fallback_reason = f"faster-whisper not installed: {exc}"
            return

        device, device_index = self._resolve_device()
        compute_type = (self.settings.whisper_compute_type or "int8_float16").strip()
        if device == "cpu" and compute_type not in {"int8", "float32"}:
            # float16 is not supported on CPU; fall back to int8.
            compute_type = "int8"
        download_root = self.settings.whisper_download_root or None
        if download_root:
            Path(download_root).mkdir(parents=True, exist_ok=True)

        try:
            logger.info(
                "Loading Whisper model=%s device=%s index=%s compute_type=%s cache=%s",
                self.settings.whisper_model, device, device_index, compute_type, download_root,
            )
            self._model = WhisperModel(
                self.settings.whisper_model,
                device=device,
                device_index=device_index,
                compute_type=compute_type,
                download_root=download_root,
            )
            logger.info("Whisper model loaded: %s", self.settings.whisper_model)
        except Exception as exc:
            self._fallback = True
            self._fallback_reason = f"Whisper init failed: {exc}"
            logger.exception("Whisper init failed: %s", exc)

    def _resolve_device(self) -> tuple[str, int]:
        desired = (self.settings.whisper_device or "auto").lower().strip()
        if desired in {"auto", "gpu", "cuda"}:
            device, idx = "cuda", 0
        elif desired.startswith("cuda"):
            device, idx = "cuda", 0
            if ":" in desired:
                try:
                    idx = int(desired.split(":", 1)[1])
                except ValueError:
                    idx = 0
        else:
            device, idx = "cpu", 0

        if device == "cuda":
            try:
                import torch  # type: ignore

                if not torch.cuda.is_available():
                    logger.warning("CUDA not available for Whisper; falling back to CPU")
                    return "cpu", 0
            except Exception:
                logger.warning("torch not available to verify CUDA; assuming CPU for Whisper")
                return "cpu", 0
        return device, idx

    def transcribe_with_metadata(self, audio_bytes: bytes, suffix: str = ".webm") -> STTResult:
        if self._fallback or self._model is None:
            fallback_text = f"[{self._fallback_reason}]"
            return STTResult(text=fallback_text, language_tag=None, emotion_tag=None, raw_text=fallback_text)

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_bytes)
            tmp_path = Path(tmp.name)

        try:
            language = (self.settings.whisper_language or "").strip().lower() or None
            segments, info = self._model.transcribe(
                str(tmp_path),
                language=language,
                beam_size=int(self.settings.whisper_beam_size or 5),
                vad_filter=bool(self.settings.whisper_vad_filter),
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            detected = (info.language or "").strip().lower() if info and info.language else None
            return STTResult(
                text=text,
                language_tag=detected,
                emotion_tag=None,
                raw_text=text,
            )
        except Exception as exc:
            logger.exception("Whisper transcription failed: %s", exc)
            fallback_text = f"[Whisper transcription failed: {exc}]"
            return STTResult(text=fallback_text, language_tag=None, emotion_tag=None, raw_text=fallback_text)
        finally:
            tmp_path.unlink(missing_ok=True)

    def transcribe_bytes(self, audio_bytes: bytes, suffix: str = ".webm") -> str:
        return self.transcribe_with_metadata(audio_bytes, suffix).text


def build_stt(settings: Settings) -> Any:
    """Construct the configured STT backend (whisper or sensevoice)."""
    backend = (settings.stt_backend or "whisper").lower().strip()
    if backend == "sensevoice":
        return SenseVoiceSTT(settings)
    return WhisperSTT(settings)


class SenseVoiceSTT:
    _TAG_PATTERN = re.compile(r"<\|[^>]+\|>|<[^>]+>")
    _LANG_TAG_PATTERN = re.compile(r"<\|([a-zA-Z0-9_-]+)\|>")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None
        self._fallback = False
        self._fallback_reason = "SenseVoice is unavailable"
        self._init_model()

    def _init_model(self) -> None:
        try:
            model_dir = Path(self.settings.sensevoice_model_dir)
            if not model_dir.exists():
                self._fallback = True
                self._fallback_reason = f"SenseVoice model path not found: {model_dir}"
                return

            from funasr import AutoModel  # type: ignore

            device = self._resolve_device()

            self._model = AutoModel(
                model=self.settings.sensevoice_model_dir,
                trust_remote_code=True,
                device=device,
                disable_update=True,
            )
        except Exception as exc:
            # Keeps development flow running before model dependencies are installed.
            self._fallback = True
            self._fallback_reason = f"SenseVoice init failed: {exc}"

    def _resolve_device(self) -> str:
        desired = (self.settings.sensevoice_device or "cpu").lower()
        if not desired.startswith("cuda"):
            return desired

        try:
            import torch  # type: ignore

            if not torch.cuda.is_available():
                return "cpu"

            major, minor = torch.cuda.get_device_capability(0)
            current_arch = f"sm_{major}{minor}"
            supported_arches = set(torch.cuda.get_arch_list())
            if current_arch not in supported_arches:
                return "cpu"
            return desired
        except Exception:
            return "cpu"

    def transcribe_with_metadata(self, audio_bytes: bytes, suffix: str = ".webm") -> STTResult:
        if self._fallback or self._model is None:
            fallback_text = f"[{self._fallback_reason}]"
            return STTResult(text=fallback_text, language_tag=None, emotion_tag=None, raw_text=fallback_text)

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_bytes)
            tmp_path = Path(tmp.name)

        try:
            # FunASR may emit tqdm/progress output to stdio; capture it to keep service logs clean.
            sink = io.StringIO()
            with redirect_stdout(sink), redirect_stderr(sink):
                result = self._model.generate(input=str(tmp_path), language="auto", use_itn=True)
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict) and "text" in first:
                    raw_text = str(first["text"])
                    return STTResult(
                        text=self._clean_transcript(raw_text),
                        language_tag=self._extract_language_tag(raw_text, first),
                        emotion_tag=self._extract_emotion_tag(raw_text, first),
                        raw_text=raw_text,
                    )

            raw_text = str(result)
            return STTResult(
                text=self._clean_transcript(raw_text),
                language_tag=self._extract_language_tag(raw_text, None),
                emotion_tag=self._extract_emotion_tag(raw_text, None),
                raw_text=raw_text,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    def transcribe_bytes(self, audio_bytes: bytes, suffix: str = ".webm") -> str:
        return self.transcribe_with_metadata(audio_bytes, suffix).text

    def _extract_language_tag(self, raw_text: str, payload: dict[str, Any] | None) -> str | None:
        if payload:
            payload_lang = payload.get("language") or payload.get("lang")
            if isinstance(payload_lang, str) and payload_lang.strip():
                return payload_lang.strip().lower()

        for tag in self._LANG_TAG_PATTERN.findall(raw_text):
            lower_tag = tag.lower().strip()
            if lower_tag in {"nospeech", "event", "itn", "transcribe", "asr"}:
                continue
            if lower_tag.startswith("event"):
                continue
            return lower_tag

        return None

    def _extract_emotion_tag(self, raw_text: str, payload: dict[str, Any] | None) -> str | None:
        if payload:
            payload_emotion = payload.get("emotion") or payload.get("emo") or payload.get("mood")
            if isinstance(payload_emotion, str):
                mapped = self._normalize_emotion(payload_emotion)
                if mapped:
                    return mapped

        for tag in self._LANG_TAG_PATTERN.findall(raw_text):
            mapped = self._normalize_emotion(tag)
            if mapped:
                return mapped

        return None

    @staticmethod
    def _normalize_emotion(value: str) -> str | None:
        normalized = value.lower().strip()
        if normalized.startswith("<|") and normalized.endswith("|>"):
            normalized = normalized[2:-2]
        if normalized.startswith("<") and normalized.endswith(">"):
            normalized = normalized[1:-1]
        normalized = normalized.strip().replace("-", "_").replace(" ", "_")
        mapping = {
            "happy": "happy",
            "joy": "happy",
            "excited": "happy",
            "sad": "sad",
            "angry": "angry",
            "neutral": "neutral",
            "calm": "neutral",
            "fear": "fear",
            "fearful": "fear",
            "surprise": "surprised",
            "surprised": "surprised",
            "disgust": "disgust",
            "disgusted": "disgust",
        }
        return mapping.get(normalized)

    def _clean_transcript(self, text: str) -> str:
        without_tags = self._TAG_PATTERN.sub(" ", text)
        cleaned = re.sub(r"\s+", " ", without_tags).strip()
        return cleaned


@dataclass
class STTResult:
    text: str
    language_tag: str | None
    emotion_tag: str | None = None
    raw_text: str | None = None
