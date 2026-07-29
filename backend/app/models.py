from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ClientAudioMessage(BaseModel):
    type: Literal["user_audio"]
    audio_base64: str
    mime_type: str = "audio/webm"


class ClientInterruptMessage(BaseModel):
    type: Literal["interrupt"]
    request_id: Optional[str] = None


class ClientTextMessage(BaseModel):
    type: Literal["user_text"]
    text: str


class ServerMessage(BaseModel):
    type: str
    text: Optional[str] = None
    token: Optional[str] = None
    transcript: Optional[str] = None
    transcript_emotion: Optional[str] = None
    transcript_raw: Optional[str] = None
    request_id: Optional[str] = None
    audio_base64: Optional[str] = None
    sample_rate: Optional[int] = Field(default=None)
    is_final_chunk: Optional[bool] = None
    error: Optional[str] = None
    reason: Optional[str] = None
    runtime: Optional[dict[str, object]] = None
    tts_voice_id: Optional[str] = None
    tts_voice_reason: Optional[str] = None
    tts_text_language: Optional[str] = None
    tool_calls: Optional[list[dict[str, object]]] = None
    tool_router_raw: Optional[dict[str, object]] = None


class ImageAnnotationRequest(BaseModel):
    question: str = "What anomalies are in this image?"


class ImageAnnotationResponse(BaseModel):
    description: str
    annotated_image_base64: str
    mime_type: str = "image/png"
    annotations: list[dict[str, Any]] = Field(default_factory=list)
