from __future__ import annotations

import asyncio
import base64
import logging
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import AsyncGenerator

from app.config import Settings
from app.models import ServerMessage
from app.services.db_service import InspectionDBClient
from app.services.llm_service import LocalLLM
from app.services.report_service import InspectionReportClient
from app.services.rerun_service import RerunVisualizer
from app.services.stt_service import STTResult, build_stt
from app.services.tool_router import ToolRouter
from app.services.tts_service import PiperTTS
from app.services.vision_service import VisionAnnotator

logger = logging.getLogger(__name__)


class VoicePipeline:
    _SENTENCE_ENDERS = set(".。！？!?;；\n")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stt = build_stt(settings)

        db_client = None
        if settings.inspection_db_path:
            db_path = Path(settings.inspection_db_path)
            if db_path.exists():
                router = ToolRouter(settings) if settings.tool_router_enabled else None
                vision_annotator = VisionAnnotator(settings)
                rerun_visualizer = RerunVisualizer(settings) if settings.rerun_enabled else None
                db_client = InspectionDBClient(
                    db_path,
                    router=router,
                    settings=settings,
                    vision_annotator=vision_annotator,
                    rerun_visualizer=rerun_visualizer,
                )
            else:
                logger.warning("Inspection DB not found at %s; DB lookups disabled", db_path)

        report_client = None
        if settings.reports_dir:
            reports_dir = Path(settings.reports_dir)
            if reports_dir.exists():
                report_client = InspectionReportClient(reports_dir)
            else:
                logger.warning("Reports directory not found at %s; report lookups disabled", reports_dir)

        self.llm = LocalLLM(settings, db_client=db_client, report_client=report_client)
        self.tts = PiperTTS(settings)

    async def _stream_tts_segments(
        self,
        segments: list[str],
        voice_model_path: str | None,
        language_tag: str | None,
        request_id: str,
        mark_last_final: bool,
    ) -> AsyncGenerator[tuple[ServerMessage, int], None]:
        """Synthesize a list of text segments and yield (ServerMessage, chunk_bytes_len) pairs.

        When *mark_last_final* is True the very last chunk receives ``is_final_chunk=True``;
        otherwise every chunk is marked non-final (used for mid-stream flushes).
        """
        for seg_idx, segment in enumerate(segments, start=1):
            stream_chunks = await asyncio.to_thread(
                lambda seg=segment: list(self.tts.stream_synthesize(seg, voice_model_path, language_tag))
            )
            if not stream_chunks:
                continue
            tts_status = self.tts.runtime_status()
            is_last_segment = mark_last_final and seg_idx == len(segments)
            for chunk_idx, audio_chunk in enumerate(stream_chunks, start=1):
                is_final = is_last_segment and chunk_idx == len(stream_chunks)
                yield (
                    ServerMessage(
                        type="tts_audio_chunk",
                        audio_base64=base64.b64encode(audio_chunk).decode("ascii"),
                        sample_rate=self.settings.tts_sample_rate,
                        request_id=request_id,
                        is_final_chunk=is_final,
                        tts_voice_id=tts_status.get("tts_last_voice_id"),
                        tts_voice_reason=tts_status.get("tts_last_voice_reason"),
                        tts_text_language=tts_status.get("tts_last_text_language"),
                    ),
                    len(audio_chunk),
                )

    def _extract_flushable_segments(self, text: str, keep_last_complete: bool) -> tuple[list[str], str]:
        segments: list[str] = []
        start = 0
        for idx, ch in enumerate(text):
            if ch in self._SENTENCE_ENDERS:
                segment = text[start : idx + 1].strip()
                if segment:
                    segments.append(segment)
                start = idx + 1

        remainder = text[start:]
        if keep_last_complete and segments:
            # Keep one completed sentence buffered to avoid wrongly marking a non-final chunk as final.
            remainder = segments.pop() + remainder

        return segments, remainder

    @staticmethod
    def _clean_for_tts(text: str) -> str:
        """Strip markdown, image links, bullets, and other punctuation that TTS should not read."""
        # Protect decimal numbers (including negatives) so their punctuation is preserved.
        decimals: list[str] = []

        def _protect_decimal(match: re.Match) -> str:
            decimals.append(match.group(0))
            return f"DECIMAL{len(decimals) - 1}"

        text = re.sub(r"-?\d+\.\d+", _protect_decimal, text)

        # Remove markdown image tags entirely.
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
        # Replace markdown links with just their link text.
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # Remove standalone URLs.
        text = re.sub(r"https?://\S+", "", text)
        # Remove bold/italic/heading/backtick markers.
        text = re.sub(r"[*_`#]+", "", text)
        # Remove bullet markers at the start of a line.
        text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
        # Remove numbered list prefixes at the start of a line.
        text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
        # Replace multiplication markers used in cluster counts (e.g. "10 x Lights").
        text = re.sub(r"\s+x\s+", " ", text)
        # Replace em/en dashes with a comma pause.
        text = text.replace("—", ",").replace("–", ",")
        # Remove parentheses and brackets.
        text = text.replace("(", " ").replace(")", " ")
        text = text.replace("[", " ").replace("]", " ")
        # Remove empty parentheses.
        text = re.sub(r"\(\s*\)", "", text)
        # Normalize whitespace.
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n+", " ", text)
        text = re.sub(r"\s+([.,;:!?)])", r"\1", text)
        # Collapse repeated punctuation.
        text = re.sub(r"([.,;:!?]){2,}", r"\1", text)

        # Restore decimal numbers.
        for i, value in enumerate(decimals):
            text = text.replace(f"DECIMAL{i}", value)

        return text.strip()

    async def handle_audio(
        self,
        audio_bytes: bytes,
        suffix: str = ".webm",
        voice_model_path: str | None = None,
        chat_history: list[tuple[str, str]] | None = None,
        tool_history: Sequence[dict[str, object]] | None = None,
        tool_calls_callback: Callable[[list[dict[str, object]]], None] | None = None,
    ) -> AsyncGenerator[ServerMessage, None]:
        request_id = str(uuid.uuid4())
        logger.info("Pipeline start: request_id=%s audio_bytes=%d suffix=%s", request_id, len(audio_bytes), suffix)

        stt_result: STTResult = await asyncio.to_thread(self.stt.transcribe_with_metadata, audio_bytes, suffix)
        transcript = stt_result.text
        logger.info(
            "STT transcript: %r language_tag=%s emotion_tag=%s raw=%r",
            transcript,
            stt_result.language_tag,
            stt_result.emotion_tag,
            stt_result.raw_text,
        )
        yield ServerMessage(
            type="transcript",
            transcript=transcript,
            transcript_emotion=stt_result.emotion_tag,
            transcript_raw=stt_result.raw_text,
            request_id=request_id,
        )
        async for msg in self._stream_reply(
            stt_result=stt_result,
            voice_model_path=voice_model_path,
            chat_history=chat_history,
            tool_history=tool_history,
            tool_calls_callback=tool_calls_callback,
            request_id=request_id,
        ):
            yield msg

    async def handle_text(
        self,
        text: str,
        voice_model_path: str | None = None,
        chat_history: list[tuple[str, str]] | None = None,
        tool_history: Sequence[dict[str, object]] | None = None,
        tool_calls_callback: Callable[[list[dict[str, object]]], None] | None = None,
    ) -> AsyncGenerator[ServerMessage, None]:
        """Text input path: skip STT and run the reply/TTS stream directly.

        Treats *text* as the transcript via a synthetic ``STTResult`` (no emotion
        or raw SenseVoice payload), so the rest of the pipeline is identical to the
        spoken path. TTS language is detected from the text itself.
        """
        request_id = str(uuid.uuid4())
        text = (text or "").strip()
        logger.info("Pipeline (text) start: request_id=%s chars=%d", request_id, len(text))
        stt_result = STTResult(text=text, language_tag=None, emotion_tag=None, raw_text=None)
        yield ServerMessage(
            type="transcript",
            transcript=text,
            transcript_emotion=None,
            transcript_raw=None,
            request_id=request_id,
        )
        async for msg in self._stream_reply(
            stt_result=stt_result,
            voice_model_path=voice_model_path,
            chat_history=chat_history,
            tool_history=tool_history,
            tool_calls_callback=tool_calls_callback,
            request_id=request_id,
        ):
            yield msg

    async def _stream_reply(
        self,
        *,
        stt_result: STTResult,
        voice_model_path: str | None,
        chat_history: list[tuple[str, str]] | None,
        tool_history: Sequence[dict[str, object]] | None,
        tool_calls_callback: Callable[[list[dict[str, object]]], None] | None,
        request_id: str,
    ) -> AsyncGenerator[ServerMessage, None]:
        """Shared LLM + TTS streaming for a transcribed/typed request (post-STT)."""
        full_reply = ""
        tts_pending_text = ""
        llm_token_count = 0
        tts_chunk_count = 0

        try:
            async for token in self.llm.stream_reply(
                stt_result.text,
                chat_history=chat_history,
                tool_history=tool_history,
                tool_calls_callback=tool_calls_callback,
            ):
                full_reply += token
                llm_token_count += 1
                if llm_token_count <= 5 or llm_token_count % 50 == 0:
                    logger.info(
                        "LLM token stream: request_id=%s count=%d token=%r",
                        request_id,
                        llm_token_count,
                        token[:80],
                    )
                yield ServerMessage(type="llm_token", token=token, request_id=request_id)

                tts_pending_text += token
                flushable_segments, tts_pending_text = self._extract_flushable_segments(
                    tts_pending_text,
                    keep_last_complete=True,
                )

                for segment in flushable_segments:
                    cleaned_segment = self._clean_for_tts(segment)
                    if not cleaned_segment:
                        continue
                    async for msg, chunk_bytes in self._stream_tts_segments(
                        [cleaned_segment], voice_model_path, stt_result.language_tag, request_id, False
                    ):
                        tts_chunk_count += 1
                        logger.info(
                            "Streaming audio chunk: request_id=%s count=%d final=%s",
                            request_id,
                            tts_chunk_count,
                            msg.is_final_chunk,
                        )
                        yield msg
                        logger.info(
                            "TTS audio ready: request_id=%s bytes=%d final=%s",
                            request_id,
                            chunk_bytes,
                            msg.is_final_chunk,
                        )

            final_text = full_reply.strip()
            final_tts_language = self.tts.resolve_tts_language(final_text, stt_result.language_tag)
            if final_text:
                final_segments, _ = self._extract_flushable_segments(tts_pending_text, keep_last_complete=False)
                if not final_segments and tts_pending_text.strip():
                    final_segments = [tts_pending_text.strip()]

                final_segments = [self._clean_for_tts(seg) for seg in final_segments]
                final_segments = [seg for seg in final_segments if seg]

                if final_segments:
                    async for msg, chunk_bytes in self._stream_tts_segments(
                        final_segments, voice_model_path, stt_result.language_tag, request_id, True
                    ):
                        tts_chunk_count += 1
                        logger.info(
                            "Streaming audio chunk: request_id=%s count=%d final=%s",
                            request_id,
                            tts_chunk_count,
                            msg.is_final_chunk,
                        )
                        yield msg
                        logger.info(
                            "TTS audio ready: request_id=%s bytes=%d final=%s",
                            request_id,
                            chunk_bytes,
                            msg.is_final_chunk,
                        )
                elif tts_chunk_count == 0:
                    logger.info(
                        "TTS audio not generated: request_id=%s chars=%d resolved_language=%s",
                        request_id,
                        len(final_text),
                        final_tts_language,
                    )

            logger.info(
                "Pipeline done: request_id=%s llm_tokens=%d tts_chunks=%d reply_chars=%d",
                request_id,
                llm_token_count,
                tts_chunk_count,
                len(final_text),
            )
            llm_done_reason = "browser_tts_fallback" if (tts_chunk_count == 0 and final_tts_language == "cantonese") else None
            yield ServerMessage(
                type="llm_done",
                text=final_text,
                request_id=request_id,
                tts_text_language=final_tts_language,
                tts_voice_reason=llm_done_reason,
            )
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled: request_id=%s", request_id)
            raise
