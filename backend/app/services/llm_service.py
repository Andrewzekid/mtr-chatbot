from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import AsyncGenerator

import httpx

from app.config import Settings
from app.services.db_service import InspectionDBClient

logger = logging.getLogger(__name__)

# Markdown image links like ![alt](/annotated/images/xxx.png) or ![alt](/inspection/images/yyy.jpg).
_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Annotated-image links specifically (produced by the annotate_image tool).
_ANNOTATED_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\((/annotated/images/[^\)]+)\)")


class LocalLLM:
    def __init__(
        self,
        settings: Settings,
        db_client: InspectionDBClient | None = None,
    ) -> None:
        self.settings = settings
        self.db_client = db_client
        # Lazily fetched from the DB so the answering prompt always names the real
        # categories (prevents the model from inventing ones that do not exist).
        self._cached_categories: list[str] | None = None
        self._cached_inspections: list[dict[str, object]] | None = None

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
            "- Inspection database context contains object detections, categories, counts, timestamps, timelines, time-window clusters, 3D coordinates, proximity information, anomaly details, and image links.\n"
            "- All anomaly answers come from the structured anomaly tables in the database (abnormalities, abnormal_detections, anomaly_types). There is no separate prose report.\n"
            "- For questions about objects, categories, counts, tracks, detections, timestamps, timelines, coordinates, or proximity, use the database context.\n"
            "- For questions about anomalies, findings, issues, problems, or state changes, use the structured anomaly data from the database context.\n"
            "- Do not reveal coordinates, positions, locations, or spatial extents unless the user explicitly asks for them. "
            "When the user asks to compare the different inspections or asks for a summary of the inspection, always mention ANOMALY DATA."
            "Coordinate/location questions use words such as 'where', 'coordinates', 'position', 'location', 'spatial', 'extent', or 'area'.\n"
            "- If the provided context does not contain the requested information, say so directly and concisely. "
            "Do not infer, assume, or invent data that is not in the context. "
            "If useful, end with one brief follow-up question offering to look into it another way.\n\n"
            "Follow-up questions and feedback:\n"
            "- When the database context says it contains a REPRESENTATIVE SUBSET (e.g. 'showing 5 of 45'), say so in your answer "
            "('here is a representative subset of the lights') and end by asking the user whether they would like to see more images.\n"
            "- Each database category is a single flat category. 'Lights' is ONE category — never invent subtypes such as street lights, "
            "building lights, or traffic lights, and never ask the user to pick between subtypes that do not exist in the data.\n"
            "- When the user's question is ambiguous and several concrete options exist (especially WHICH inspection they mean), "
            "end your answer with one follow-up question that lists the options as explicit choices "
            "(e.g. 'Which inspection do you mean — inspection 1 (ground truth), inspection 2, or inspection 3?'). "
            "One short follow-up question at most.\n\n"
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
            "Answering anomaly questions (IMPORTANT):\n"
            "- When the user asks to 'tell me about the anomalies', 'what anomalies were found', 'list the anomalies', 'show me the anomalies', "
            "'give me a summary of the anomalies', or otherwise asks you to describe the anomalies, use this TWO-PART structure:\n"
            "  1. FIRST give a brief text summary — the total count of anomalies and the count by type (e.g. 'Found 7 anomalies: 3 foreign_object, "
            "2 state_change, 1 missing_object, 1 crack and structure damage.'). Keep it to one or two sentences.\n"
            "  2. THEN walk through the anomalies ONE BY ONE. Do NOT dump all the image links in bulk at the bottom of the answer.\n"
            "- For EACH anomaly in the per-anomaly walkthrough, write a short standalone entry in this shape:\n"
            "    'Anomaly <id>: <type> — <object / location / short note if available>. Ground truth: ![gt frame](...). Inspection frame: ![inspection frame](...).'\n"
            "  Put that anomaly's ground-truth and inspection image markdown links INLINE, immediately after its description, so each anomaly's "
            "description and its images sit together. The annotated result image link (when present) goes inline with that same anomaly too.\n"
            "- Use the exact anomaly id, type, image links, and notes from the database context. Never reorder anomalies or merge them.\n"
            "- Do NOT collapse the per-anomaly walkthrough into a summary — the user asked to be told about the anomalies, so after the brief "
            "summary each one must be addressed individually.\n"
            "- Keep each anomaly's prose SHORT (one or two lines) so the answer stays readable; the images carry the detail.\n"
            "- This two-part (brief summary + per-anomaly walkthrough) format is required for anomaly-listing questions. For a single-anomaly "
            "question ('tell me about anomaly 4'), skip the summary and describe just that one anomaly in the same inline format.\n"
            "- For 'how many anomalies' / counts-only questions, the per-anomaly walkthrough is not required — just give the counts.\n\n"
            "Image links:\n"
            "- The database context may include markdown image links such as ![description](/inspection/images/<filename.jpg>).\n"
            "- It may also include annotated image links from the annotate_image tool, e.g. ![annotated image](/annotated/images/<filename.png>).\n"
            "- ALWAYS include these image markdown links VERBATIM in your text response. The UI renders them as inline images for the user. "
            "Never drop, summarize, rephrase, omit, or replace them with a prose description. If the context contains an image link, your answer MUST contain that exact link.\n"
            "- When the context includes multiple image links (e.g. representative frames), include every link in your response; the UI will display them as thumbnails.\n"
            "- Do not describe the image filename or URL in words; the UI handles the image.\n"
            "- IMPORTANT: only use image links that appear VERBATIM in the provided context (tool outputs). "
            "Never invent, guess, or construct image filenames. Any image filename you show must appear verbatim in the context. "
            "If you are not given a link for an image, do not show one.\n"
            "- When the annotate_image tool ran, the annotation is ALREADY done by the system's vision model and the annotated image is shown to the user. "
            "Never say you cannot annotate, draw, or edit images. Just describe what the annotation found (object, location, anomaly) in plain language.\n\n"
            "Speech and readability:\n"
            "- Your response is also spoken aloud, so avoid heavy punctuation, markdown syntax, or lists that are hard to speak.\n"
            "- IMAGE MARKDOWN LINKS (e.g. ![...](/annotated/images/...png) or ![...](/inspection/images/...jpg)) are the ONE exception: "
            "always include them in your text. The UI extracts and displays them as images and EXCLUDES them from speech, so they do not make the spoken answer awkward. "
            "Never omit an image link because you think it should not be spoken — include it anyway.\n"
            "- Do not include raw filenames, URLs, or other non-image markdown in the spoken part of your answer.\n\n"
            "Example of a bad answer:\n"
            "1. 16:51:45 — 106 Lights. 2. 16:51:51 — 50 Lights, 35 Map...\n\n"
            "Example of a good answer:\n"
            "The busiest moment was around 4:51 PM, when over 100 Lights were detected in a five-second window. "
            "The next cluster included a mix of Lights, Maps, Advertisement Boards, and a Ticket Gate, suggesting the camera passed through a more complex area.\n\n"
            "Do not output emojis."
        )

    def _database_facts(self) -> str:
        """Real category list + schema facts, appended to the system prompt.

        Without this the model has no knowledge of the database when no tool ran and
        invents categories (people, vehicles, ...). Categories come from the DB itself,
        cached after the first successful read.
        """
        if self._cached_categories is None and self.db_client is not None:
            try:
                categories = self.db_client.get_categories()
                if categories:
                    self._cached_categories = list(categories)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not fetch category list for system prompt: %s", exc)
        if self._cached_inspections is None and self.db_client is not None:
            try:
                self._cached_inspections = self.db_client.get_inspections()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not fetch inspection list for system prompt: %s", exc)
                self._cached_inspections = []
        categories = self._cached_categories or [
            "Lights",
            "Advertisement Board",
            "Ticket Gate",
            "Map",
            "TV",
            "Exit Sign",
        ]
        inspections = self._cached_inspections or []
        inspection_facts = ""
        if len(inspections) > 1:
            listing = "; ".join(
                f"inspection {ins['id']} ({'ground truth reference' if ins['is_gt'] else 'robot inspection run'}, "
                f"started {ins['started_at']}, {ins['object_count']} objects)"
                for ins in inspections
            )
            inspection_facts = (
                f"- The database contains MULTIPLE inspections: {listing}. "
                "Each inspection is a separate run over the same station, so counts differ between them.\n"
                "- When the user asks about 'the inspection' without saying which one, and the answer depends "
                "on which inspection is meant, do NOT silently blend the numbers: give the per-inspection "
                "breakdown briefly, then end with ONE follow-up question that lists the inspections as explicit "
                "choices (e.g. 'Which inspection do you mean — inspection 1 (ground truth reference), "
                "inspection 2, or inspection 3?'). Exception: if the question is clearly about the reference "
                "scene or 'ground truth', use the ground-truth inspection without asking.\n"
                "- Whenever you state counts or object details, name the inspection they come from "
                "(or explicitly say the number combines all inspections). Never mix an object id from one "
                "inspection with counts from another.\n"
            )
        elif len(inspections) == 1:
            ins = inspections[0]
            inspection_facts = f"- The database contains a single inspection (id {ins['id']}).\n"
        return (
            "\n\nDatabase facts (always true, even when no database context is attached):\n"
            f"- The inspection database ONLY contains these object categories: {', '.join(categories)}. "
            "No other categories exist — there are no people, vehicles, buildings, or generic 'street furniture' in this data. "
            "Never invent, assume, or generalize categories outside this list.\n"
            "- 'Objects' are unique physical items tracked in 3D (each has one centroid and 3D bounding box); "
            "'detections' are per-frame sightings of those objects, so one object can have many detections. "
            "When the user asks 'how many' of something, they mean unique objects unless they explicitly ask about detections or sightings.\n"
            f"{inspection_facts}"
            "- If the user asks what was detected but NO database context is provided in this conversation, "
            "do not invent categories or counts — say the lookup returned no data and offer to try a more specific question."
        )

    @staticmethod
    def _format_tool_history(tool_history: Sequence[dict[str, object]] | None) -> str:
        """Serialize recent tool calls and their outputs for the LLM context."""
        if not tool_history:
            return ""
        lines = ["Recent tool calls and results (for reference):"]
        for entry in tool_history:
            tool_calls = entry.get("tool_calls") or []
            if not tool_calls:
                continue
            for call in tool_calls:
                name = call.get("name", "unknown")
                args = call.get("args", {})
                # Strip image links from past tool outputs so the model does not
                # re-emit annotated/inspection images from earlier turns.
                output = LocalLLM._strip_image_links(str(call.get("output", "")))
                lines.append(f"- {name}({args}): {output}")
        return "\n".join(lines)

    @staticmethod
    def _strip_image_links(text: str) -> str:
        """Remove markdown image links so they do not bleed into later turns."""
        if not text:
            return text
        cleaned = _IMAGE_LINK_RE.sub("", text)
        return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    def _build_messages(
        self,
        prompt: str,
        chat_history: Sequence[tuple[str, str]] | None,
        db_context: str | None = None,
        tool_context: str | None = None,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [{"role": "system", "content": self._system_prompt() + self._database_facts()}]

        if tool_context:
            messages.append({"role": "system", "content": f"Past tool call history:\n{tool_context}"})

        if db_context:
            messages.append({"role": "system", "content": f"Inspection database context (object detections, categories, counts, timelines, and image links):\n{db_context}"})

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
                # Strip image links from prior assistant replies so the model does
                # not re-emit annotated/inspection images in unrelated later turns.
                assistant_clean = self._strip_image_links(assistant_text)
                if assistant_clean.strip():
                    messages.append({"role": "assistant", "content": assistant_clean})

        messages.append({"role": "user", "content": prompt})
        return messages

    async def stream_reply(
        self,
        prompt: str,
        chat_history: Sequence[tuple[str, str]] | None = None,
        tool_history: Sequence[dict[str, object]] | None = None,
        tool_calls_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> AsyncGenerator[str, None]:
        db_context = None
        tool_results: list[dict[str, object]] = []
        tool_router_raw: dict[str, object] | None = None
        # Annotated-image links the backend emits itself (see yield below) so
        # the image is guaranteed to display regardless of model compliance.
        annotated_links: list[str] = []
        if self.db_client is not None:
            try:
                db_context = await self.db_client.lookup(prompt, chat_history=chat_history, tool_history=tool_history)
                if db_context:
                    logger.info("Injecting inspection DB context for prompt: %r", prompt)
                tool_results = [
                    {"name": r["name"], "args": r["args"], "output": r["output"]}
                    for r in self.db_client.last_tool_results
                ]
                highlight_called = any(
                    call.get("name") == "highlight_in_rerun" for call in self.db_client.last_tool_calls
                )
                annotate_called = any(
                    call.get("name") == "annotate_image" for call in self.db_client.last_tool_calls
                )
                image_tool_called = annotate_called or any(
                    call.get("name") in {"get_images_in_time_range", "get_category_sample_images", "get_object_image_paths"}
                    for call in self.db_client.last_tool_calls
                )
                if annotate_called and db_context:
                    # Collect the annotated-image links the tool produced. The
                    # backend emits them itself (see stream_reply) so the image
                    # is guaranteed to display even if the model would otherwise
                    # drop the link. Strip them from the context shown to the
                    # model so it cannot duplicate or paraphrase them.
                    annotated_links = []
                    for r in tool_results:
                        if r.get("name") != "annotate_image":
                            continue
                        for m in _ANNOTATED_IMAGE_LINK_RE.finditer(str(r.get("output", ""))):
                            link = m.group(0)
                            if link not in annotated_links:
                                annotated_links.append(link)
                    if annotated_links:
                        db_context = _ANNOTATED_IMAGE_LINK_RE.sub("", db_context)
                        db_context = re.sub(r"[ \t]{2,}", " ", db_context)
                        db_context = (
                            "The system's annotate_image tool has ALREADY run: a vision model analyzed the image "
                            "and drew annotation boxes, and the annotated image is displayed ABOVE your answer "
                            "(the image link is shown by the system, so do NOT re-emit it and do NOT say you cannot annotate). "
                            "You are NOT being asked to generate, edit, or draw anything — the annotation is already done. "
                            "Your ONLY job is to write a short natural-language summary of what the annotation found, "
                            "using the description and annotation details below (what object/anomaly, where in the frame, confidence if given). "
                            "Never claim you lack the ability to annotate; the annotation already happened.\n\n"
                            + db_context
                        )
                    else:
                        db_context = (
                            "IMPORTANT: The annotate_image tool just produced an annotated image. "
                            "You MUST include the annotated image markdown link at the start of your answer so the UI displays it. "
                            "Do NOT summarize it away or describe it in words instead of showing the link. "
                            "The annotation is already done by the system; just describe what it found.\n\n"
                            + db_context
                        )
                elif image_tool_called and db_context:
                    db_context = (
                        "The user asked to see images. "
                        "Preserve the image markdown links shown below in your answer EXACTLY so the UI can display them. "
                        "Do not summarize them away.\n\n"
                        + db_context
                    )
                if self.db_client.router is not None:
                    tool_router_raw = self.db_client.router.last_raw_response
                if highlight_called and db_context:
                    db_context = (
                        "The highlight_in_rerun tool pushed 3D highlights to the user's separately-running "
                        "Rerun viewer. Mention briefly that the highlighted objects/coordinates are now shown "
                        "in the Rerun viewer (the user watches it alongside this chat). Do not claim the "
                        "viewer is in this chat window.\n\n" + db_context
                    )
                elif db_context:
                    # Final-pass highlight: the router already decided (after the tools
                    # ran) which objects/coordinates to show in the Rerun viewer. If it
                    # did, tell the answerer to mention the viewer; otherwise stay quiet.
                    highlight_status = self.db_client.last_highlight_status
                    if highlight_status and highlight_status.startswith("Highlighted"):
                        db_context = (
                            "The objects/coordinates relevant to the user's question are now shown in "
                            "the Rerun viewer (chosen by a final pass after the tools ran; the user "
                            "watches it alongside this chat). Mention briefly that they are shown in "
                            "the viewer.\n\n" + db_context
                        )
                anomaly_locations_called = any(
                    call.get("name") == "get_anomaly_locations"
                    for call in self.db_client.last_tool_calls
                )
                if (
                    anomaly_locations_called
                    and db_context
                    and re.search(r"\b(where|locations?|located|coordinates?|positions?|spatial|area)\b", prompt, re.IGNORECASE)
                ):
                    db_context = (
                        "The user explicitly asked WHERE the anomalies are. The get_anomaly_locations "
                        "output below lists each anomaly pair with the 3D camera position where it was "
                        "observed. You MUST quote several of these exact (x, y, z) coordinates in your "
                        "answer (always include the z value); you may group them by area, but do not "
                        "answer with area descriptions alone.\n\n" + db_context
                    )
            except Exception as exc:
                logger.warning("DB lookup failed: %s", exc)

        if tool_calls_callback:
            debug_payload: dict[str, object] = {}
            if tool_results:
                debug_payload["tool_calls"] = tool_results
            if tool_router_raw:
                debug_payload["tool_router_raw"] = tool_router_raw
            if self.db_client is not None:
                highlight_status = self.db_client.last_highlight_status
                highlight_args = self.db_client.last_highlight_args
                if highlight_status or highlight_args:
                    debug_payload["highlight"] = {
                        "status": highlight_status,
                        "args": highlight_args,
                    }
                highlight_history = self.db_client.highlight_history
                if highlight_history:
                    debug_payload["highlight_history"] = highlight_history
                rerun_stats = self.db_client.rerun_job_stats
                if rerun_stats:
                    debug_payload["rerun_stats"] = rerun_stats
            if debug_payload:
                tool_calls_callback(debug_payload)

        logger.info("LLM request: model=%s prompt=%r", self.settings.llm_model_name, prompt)
        tool_context = self._format_tool_history(tool_history)
        messages = self._build_messages(
            prompt,
            chat_history,
            db_context=db_context,
            tool_context=tool_context,
        )

        provider = self.settings.llm_provider.lower().strip()

        # Guarantee annotated images display: emit their markdown links as the
        # first tokens of the reply, before the model's own text. The links were
        # stripped from db_context above so the model cannot duplicate them.
        if annotated_links:
            prefix = "\n\n".join(annotated_links) + "\n\n"
            yield prefix

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
                {"role": "system", "content": self._system_prompt() + self._database_facts()},
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
