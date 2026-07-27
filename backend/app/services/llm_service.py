from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from typing import AsyncGenerator

import httpx

from app.config import Settings
from app.services.db_service import InspectionDBClient
from app.services.report_service import InspectionReportClient

logger = logging.getLogger(__name__)


class LocalLLM:
    def __init__(
        self,
        settings: Settings,
        db_client: InspectionDBClient | None = None,
        report_client: InspectionReportClient | None = None,
    ) -> None:
        self.settings = settings
        self.db_client = db_client
        self.report_client = report_client

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are MTR-Insight, a Hong Kong MTR subway station inspection assistant. "
            "You help station inspectors understand what the robot detected during its inspection. "
            "Speak in a clear, conversational, helpful tone, as if you are briefing a station engineer over a radio.\n\n"
            "Style and formatting:\n"
            "- Be concise. Answer in one or two short paragraphs unless the user asks for detail.\n"
            "- Use complete sentences and short paragraphs. Avoid robotic recitation of raw data tables or long bulleted lists.\n"
            "- Synthesize the data: highlight patterns, busiest moments, unusual combinations, or changes over time.\n"
            "- Do not narrow the answer to a single category unless the user explicitly asks for that category. "
            "If the provided data contains multiple categories, describe them together.\n"
            "- Round or summarize large numbers when precision is not critical (e.g., 'about 90 observations' instead of listing every exact count).\n"
            "- If the user explicitly asks for a list or table, you may provide one, but keep it concise.\n"
            "- Do not output self-correction, disclaimers about the prompt, or meta-commentary in the final answer.\n"
            "- Never ask the user to provide data that is already in the inspection context.\n\n"
            "Thinking:\n"
            "- You may reason step by step before answering, but put all reasoning inside <think>...</think> tags.\n"
            "- Only the text outside <think>...</think> tags is shown to the user, so keep the final answer clean and direct.\n\n"
            "Information sources:\n"
            "- Inspection database context contains object detections, categories, counts, timestamps, timelines, time-window clusters, 3D coordinates, proximity information, and image links.\n"
            "- Inspection report context contains anomaly findings, issues, problems, state changes, and recommendations. It is only included when the user asks about anomalies or findings.\n"
            "- For questions about objects, categories, counts, tracks, detections, timestamps, timelines, coordinates, or proximity, prioritize the database context.\n"
            "- For questions about anomalies, findings, issues, problems, state changes, or recommendations, prioritize the report context.\n"
            "- Do not reveal coordinates, positions, locations, or spatial extents unless the user explicitly asks for them. "
            "Coordinate/location questions use words such as 'where', 'coordinates', 'position', 'location', 'spatial', 'extent', or 'area'.\n"
            "- When a question touches on both sources (for example, anomalies in a specific category or object), synthesize them into one coherent answer rather than two separate sections.\n"
            "- If the provided context does not contain the requested information, say so directly and concisely. "
            "Do not infer, assume, or invent data that is not in the context. "
            "If useful, end with one brief follow-up question offering to look into it another way.\n\n"
            "Answering temporal and spatial questions:\n"
            "- When the user asks about order, sequence, before/after, or clusters, describe the pattern in plain language.\n"
            "- Do not dump raw timestamps. Convert them to readable clock times (e.g., 'around 4:51 PM').\n"
            "- Do NOT mention coordinates, positions, locations, or spatial extents unless the user explicitly asks for them. "
            "If the user only asks for a summary, counts, or timestamps, leave coordinates out of the answer entirely.\n"
            "- For coordinate/location questions (when the user says 'where', 'coordinates', 'position', 'location', 'spatial', 'extent', or 'area'), quote a few representative positions for the objects the user asked about, then summarize the overall spatial pattern. "
            "Coordinates are always 3D; you MUST include the z coordinate and present them as (x, y, z) tuples (e.g., 'around (-18.2, 32.2, -6.9)'). "
            "Never drop the z value. Never say you do not have coordinate data when coordinates are present in the context. "
            "Use the exact coordinates from the context; never invent or average coordinates unless you are explicitly summarizing an aggregate value.\n"
            "- When the user asks about the location of a cluster or 'what was at time T', the database tools already return object coordinates for that time window. "
            "Use those coordinates directly to describe where the cluster was. Do not say the location is unknown. "
            "For example, if get_objects_in_temporal_cluster returns objects around 4:51 PM, answer with representative (x, y, z) positions and describe the overall area.\n"
            "- When the user asks about movement, displacement, or a path, describe the start and end positions and the total displacement in plain language. Only give (x, y, z) coordinates if the user asked for them.\n"
            "- When the user asks which objects are close to each other, give both the count and a few representative nearby positions as (x, y, z) tuples.\n\n"
            "Summarization:\n"
            "- When the context contains many objects (for example, dozens of advertisement boards or lights), do NOT list every individual object. "
            "Summarize by category and time period.\n"
            "- Give totals first, then a handful of representative examples, then the overall pattern.\n"
            "- If the context is already aggregated (e.g., SQL GROUP BY results), describe the aggregates directly: counts, averages, ranges, and time windows.\n"
            "- Never invent individual object names like 'Board 1', 'Board 2', or 'Light 47'. The database does not assign such labels.\n"
            "- Do not include coordinates, positions, or spatial areas in a summary unless the user explicitly asked for location information.\n"
            "- If coordinate or location data is NOT in the provided context, do not invent coordinates. "
            "Report only the counts, time spans, and categories you actually have. "
            "Do not say objects are 'concentrated at (0.01, 0.01)' or any other made-up coordinate.\n\n"
            "Image links:\n"
            "- The database context may include markdown image links such as ![description](/inspection/images/<filename.jpg>).\n"
            "- It may also include annotated image links from the annotate_image tool, e.g. ![annotated image](/annotated/images/<filename.png>).\n"
            "- Always preserve these links in your text response so the UI can display the object frames or annotated result to the user.\n"
            "- Do not describe the image filename or URL in words; the UI handles the image.\n\n"
            "Speech and readability:\n"
            "- Your response is also spoken aloud, so avoid heavy punctuation, markdown syntax, or lists that are hard to speak.\n"
            "- Do not include raw filenames, URLs, or image markdown in the spoken part of your answer.\n\n"
            "Example of a bad answer:\n"
            "1. 16:51:45 — 106 Lights. 2. 16:51:51 — 50 Lights, 35 Map...\n\n"
            "Example of a good answer:\n"
            "The busiest moment was around 4:51 PM, when over 100 Lights were detected in a five-second window. "
            "The next cluster included a mix of Lights, Maps, Advertisement Boards, and a Ticket Gate, suggesting the camera passed through a more complex area.\n\n"
            "Do not output emojis."
        )

    def _build_messages(
        self,
        prompt: str,
        chat_history: Sequence[tuple[str, str]] | None,
        db_context: str | None = None,
        report_context: str | None = None,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [{"role": "system", "content": self._system_prompt()}]

        if db_context:
            messages.append({"role": "system", "content": f"Inspection database context (object detections, categories, counts, timelines, and image links):\n{db_context}"})

        if report_context:
            messages.append({"role": "system", "content": f"Inspection report context (anomaly findings, issues, and recommendations):\n{report_context}"})

        if chat_history:
            recent_turns = list(chat_history)[-self.settings.llm_history_turns :]
            accumulated = 0
            kept: list[tuple[str, str]] = []
            for user_text, assistant_text in reversed(recent_turns):
                turn_size = len(user_text) + len(assistant_text)
                if kept and accumulated + turn_size > self.settings.llm_history_char_budget:
                    break
                kept.append((user_text, assistant_text))
                accumulated += turn_size

            for user_text, assistant_text in reversed(kept):
                if user_text.strip():
                    messages.append({"role": "user", "content": user_text})
                if assistant_text.strip():
                    messages.append({"role": "assistant", "content": assistant_text})

        messages.append({"role": "user", "content": prompt})
        return messages

    async def stream_reply(
        self,
        prompt: str,
        chat_history: Sequence[tuple[str, str]] | None = None,
        tool_calls_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> AsyncGenerator[str, None]:
        db_context = None
        tool_results: list[dict[str, object]] = []
        report_needed = False
        tool_router_raw: dict[str, object] | None = None
        if self.db_client is not None:
            try:
                db_context = await self.db_client.lookup(prompt)
                if db_context:
                    logger.info("Injecting inspection DB context for prompt: %r", prompt)
                tool_results = [
                    {"name": r["name"], "args": r["args"], "output": r["output"]}
                    for r in self.db_client.last_tool_results
                ]
                report_needed = any(call.get("name") == "get_report_summary" for call in self.db_client.last_tool_calls)
                annotated_image_called = any(call.get("name") == "annotate_image" for call in self.db_client.last_tool_calls)
                if annotated_image_called and db_context:
                    db_context = (
                        "The user asked to annotate/highlight an image. "
                        "Include the annotated image markdown link shown below in your answer so the UI can display it.\n\n"
                        + db_context
                    )
                if self.db_client.router is not None:
                    tool_router_raw = self.db_client.router.last_raw_response
            except Exception as exc:
                logger.warning("DB lookup failed: %s", exc)

        report_context = None
        if report_needed and self.report_client is not None:
            try:
                report_context = self.report_client.get_context()
                image_urls = self.report_client.get_image_urls()
                if image_urls:
                    report_context += "\n\n--- Extracted anomaly images ---\n"
                    report_context += "The following images are available for reference:\n"
                    for url in image_urls:
                        report_context += f"- {url}\n"
                if report_context:
                    logger.info("Injecting inspection report context for prompt: %r", prompt)
                    # Attach the real report output to the debug payload.
                    for r in tool_results:
                        if r["name"] == "get_report_summary":
                            r["output"] = report_context
            except Exception as exc:
                logger.warning("Report lookup failed: %s", exc)

        if tool_calls_callback:
            debug_payload: dict[str, object] = {}
            if tool_results:
                debug_payload["tool_calls"] = tool_results
            if tool_router_raw:
                debug_payload["tool_router_raw"] = tool_router_raw
            if debug_payload:
                tool_calls_callback(debug_payload)

        logger.info("LLM request: model=%s prompt=%r", self.settings.llm_model_name, prompt)
        messages = self._build_messages(
            prompt,
            chat_history,
            db_context=db_context,
            report_context=report_context,
        )

        provider = self.settings.llm_provider.lower().strip()

        if provider == "ollama":
            try:
                async for token in self._stream_ollama(messages, model_name=self.settings.llm_model_name):
                    yield token
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ollama stream failed: %s", exc)
                reason = f"Ollama: {exc}"
        else:
            try:
                async for token in self._stream_vllm_with_retries(messages):
                    yield token
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("vLLM stream failed: %s", exc)

                if self.settings.llm_fallback_enabled:
                    try:
                        async for token in self._stream_ollama(messages, model_name=self.settings.ollama_model_name):
                            yield token
                        return
                    except Exception as fallback_exc:  # noqa: BLE001
                        logger.exception("Ollama fallback stream failed: %s", fallback_exc)
                        reason = f"vLLM: {exc} | Ollama: {fallback_exc}"
                else:
                    reason = f"vLLM: {exc}"

        fallback = f"[LLM request failed] {reason}. You said: {prompt}"
        for token in fallback.split(" "):
            yield token + " "
            await asyncio.sleep(0.01)

    async def _stream_vllm_with_retries(self, messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
        attempts = 6
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                async for token in self._stream_vllm(messages):
                    yield token
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == attempts:
                    break
                delay = min(2 * attempt, 10)
                logger.info("vLLM not ready (attempt %d/%d): %s; retrying in %ss", attempt, attempts, exc, delay)
                await asyncio.sleep(delay)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("vLLM request failed without exception")

    async def _stream_vllm(self, messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.vllm_api_key}",
        }

        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(
            base_url=self.settings.vllm_base_url.rstrip("/"),
            timeout=timeout,
        ) as client:
            request_candidates: list[tuple[str, int]] = [
                (self.settings.llm_model_name, self.settings.llm_max_tokens),
            ]
            attempted_requests: set[tuple[str, int]] = set()

            while request_candidates:
                model_name, max_tokens = request_candidates.pop(0)
                attempted_requests.add((model_name.lower(), max_tokens))
                token_count = 0
                raw_buffer = ""

                payload = {
                    "model": model_name,
                    "messages": messages,
                    "stream": True,
                    "temperature": self.settings.llm_temperature,
                    "max_tokens": max_tokens,
                }

                async with client.stream("POST", "/v1/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body_bytes = await resp.aread()
                        body = body_bytes.decode("utf-8", errors="ignore")[:500]
                        status_code = resp.status_code
                        logger.warning("vLLM HTTP error: status=%s model=%s body=%r", status_code, model_name, body)

                        if status_code in {400, 404}:
                            discovered_model = await self._discover_vllm_model(client, headers)
                            if discovered_model and (discovered_model.lower(), max_tokens) not in attempted_requests and (
                                discovered_model,
                                max_tokens,
                            ) not in request_candidates:
                                logger.warning(
                                    "Retrying vLLM stream with discovered model=%s (configured=%s)",
                                    discovered_model,
                                    self.settings.llm_model_name,
                                )
                                request_candidates.append((discovered_model, max_tokens))
                                continue

                        # If prompt + output budget exceeds model context, reduce output tokens and retry.
                        if status_code == 400 and (
                            "maximum context length" in body.lower() or '"param":"input_text"' in body.lower()
                        ):
                            reduced = max(64, max_tokens // 2)
                            if reduced < max_tokens and (model_name.lower(), reduced) not in attempted_requests and (
                                model_name,
                                reduced,
                            ) not in request_candidates:
                                logger.warning(
                                    "Retrying vLLM stream with reduced max_tokens=%s (previous=%s)",
                                    reduced,
                                    max_tokens,
                                )
                                request_candidates.append((model_name, reduced))
                                continue

                        detail = f"status={status_code} model={model_name}"
                        if body:
                            detail += f" body={body}"
                        raise RuntimeError(f"vLLM chat failed: {detail}")

                    async for raw_line in resp.aiter_lines():
                        if not raw_line or not raw_line.startswith("data:"):
                            continue

                        data = raw_line[5:].strip()
                        if data == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            logger.debug("vLLM non-json stream chunk: %r", data)
                            continue

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue

                        delta = choices[0].get("delta") or {}
                        text = delta.get("content") or ""
                        if not text:
                            continue

                        if len(raw_buffer) < 300:
                            raw_buffer += text
                            if len(raw_buffer) >= 300:
                                logger.info("vLLM raw output (first 300): %r", raw_buffer[:300])

                        if token_count == 0:
                            logger.info("vLLM first token received: %r", text[:80])
                        token_count += 1
                        yield text

                logger.info(
                    "vLLM stream done: model=%s max_tokens=%s tokens=%d | raw_start=%r",
                    model_name,
                    max_tokens,
                    token_count,
                    raw_buffer[:200],
                )
                return

        raise RuntimeError("vLLM chat failed: no model candidates left")

    async def _discover_vllm_model(self, client: httpx.AsyncClient, headers: dict[str, str]) -> str | None:
        try:
            models_resp = await client.get("/v1/models", headers=headers)
            if models_resp.status_code >= 400:
                return None

            payload = models_resp.json()
            models = payload.get("data") or []
            for model in models:
                model_id = model.get("id")
                if isinstance(model_id, str) and model_id:
                    return model_id
        except Exception:
            return None

        return None

    async def _stream_ollama(
        self,
        messages: list[dict[str, str]],
        model_name: str | None = None,
    ) -> AsyncGenerator[str, None]:
        token_count = 0

        payload = {
            "model": model_name or self.settings.ollama_model_name,
            "messages": messages,
            "stream": True,
            "think": self.settings.ollama_thinking,
            "options": {
                "temperature": self.settings.llm_temperature,
                "num_ctx": self.settings.llm_n_ctx,
            },
        }

        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(
            base_url=self.settings.ollama_base_url.rstrip("/"),
            timeout=timeout,
        ) as client:
            async with client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                buffer = ""
                inside_think = False
                think_open_len = len("<think")
                think_close_len = len("</think>")

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("Ollama non-json stream chunk: %r", line)
                        continue

                    msg = chunk.get("message") or {}
                    text = msg.get("content") or ""
                    if not text:
                        continue

                    token_count += 1
                    if token_count == 1:
                        logger.info("Ollama first token received: %r", text[:80])

                    buffer += text
                    while True:
                        if not inside_think:
                            open_idx = buffer.find("<think")
                            if open_idx != -1:
                                before = buffer[:open_idx]
                                if before:
                                    yield before
                                inside_think = True
                                buffer = buffer[open_idx + think_open_len :]
                                continue
                            # Yield everything except a possible incomplete opening tag suffix.
                            keep = min(len(buffer), think_open_len - 1)
                            out = buffer[:-keep] if keep else buffer
                            if out:
                                yield out
                            buffer = buffer[-keep:] if keep else ""
                            break
                        else:
                            close_idx = buffer.find("</think>")
                            if close_idx != -1:
                                inside_think = False
                                buffer = buffer[close_idx + think_close_len :]
                                continue
                            # Keep a possible incomplete closing tag suffix.
                            keep = min(len(buffer), think_close_len - 1)
                            buffer = buffer[-keep:] if keep else ""
                            break

                # Flush any remaining non-thinking text.
                if not inside_think and buffer:
                    yield buffer

        logger.info("Ollama stream done: %d visible tokens", token_count)

    async def runtime_status(self) -> dict[str, object]:
        provider = self.settings.llm_provider.lower().strip()
        timeout = self.settings.llm_request_timeout_s

        if provider == "ollama":
            ollama_models: list[str] = []
            ollama_ok = False
            try:
                async with httpx.AsyncClient(base_url=self.settings.ollama_base_url.rstrip("/"), timeout=timeout) as client:
                    tags_resp = await client.get("/api/tags")
                    ollama_ok = tags_resp.status_code < 400
                    if ollama_ok:
                        payload = tags_resp.json()
                        for model in payload.get("models", []):
                            name = model.get("name")
                            if isinstance(name, str):
                                ollama_models.append(name)
            except Exception:
                ollama_ok = False

            target = self.settings.llm_model_name.lower()
            llm_running = any(name.lower() == target for name in ollama_models)
            return {
                "configured_model": self.settings.llm_model_name,
                "active_backend": "ollama" if ollama_ok else "none",
                "llm_reachable": ollama_ok,
                "llm_running": llm_running,
                "running_models": ollama_models,
                "ollama_reachable": ollama_ok,
            }

        vllm_models: list[str] = []
        vllm_ok = False

        try:
            headers = {"Authorization": f"Bearer {self.settings.vllm_api_key}"}
            async with httpx.AsyncClient(base_url=self.settings.vllm_base_url.rstrip("/"), timeout=timeout) as client:
                health_resp = await client.get("/health")
                vllm_ok = health_resp.status_code < 400
                if vllm_ok:
                    models_resp = await client.get("/v1/models", headers=headers)
                    if models_resp.status_code < 400:
                        payload = models_resp.json()
                        for model in payload.get("data", []):
                            model_id = model.get("id")
                            if isinstance(model_id, str):
                                vllm_models.append(model_id)
        except Exception:
            vllm_ok = False

        if vllm_ok:
            target = self.settings.llm_model_name.lower()
            llm_running = any(name.lower() == target for name in vllm_models)
            return {
                "configured_model": self.settings.llm_model_name,
                "active_backend": "vllm",
                "llm_reachable": True,
                "llm_running": llm_running,
                "running_models": vllm_models,
                "ollama_reachable": False,
            }

        return {
            "configured_model": self.settings.llm_model_name,
            "active_backend": "none",
            "llm_reachable": False,
            "llm_running": False,
            "running_models": [],
            "ollama_reachable": False,
        }

    async def preload_model(self) -> bool:
        """Preload the LLM model by sending a minimal request to warm it up."""
        try:
            logger.info("Preloading LLM model: %s via %s", self.settings.llm_model_name, self.settings.llm_provider)
            preload_messages = [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": "Hi"},
            ]
            provider = self.settings.llm_provider.lower().strip()
            stream = (
                self._stream_ollama(preload_messages, model_name=self.settings.llm_model_name)
                if provider == "ollama"
                else self._stream_vllm_with_retries(preload_messages)
            )
            token_count = 0
            async for _ in stream:
                token_count += 1
                if token_count > 3:
                    break
            logger.info("LLM model preloaded successfully")
            return True
        except Exception as exc:
            logger.warning("LLM model preload failed: %s", exc)
            return False
