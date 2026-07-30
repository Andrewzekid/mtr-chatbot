"""End-to-end smoke test: anomaly-location question through the real pipeline.

Uses the real DB, real tool router (Ollama), and real answering LLM (Ollama).
Rerun pushes are disabled so the test has no viewer side effects. The old
failure mode was the assistant answering "coordinates not provided" even though
get_anomaly_locations returned coordinates.
"""
import asyncio
import re
from pathlib import Path

from app.config import Settings
from app.services.db_service import InspectionDBClient
from app.services.llm_service import LocalLLM
from app.services.tool_router import ToolRouter

COORD_RE = re.compile(r"\(-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?\)")

QUERIES = [
    "where are the anomalies located?",
    "tell me about the anomalies found during this inspection",
]


async def run_query(llm: LocalLLM, db_client: InspectionDBClient, query: str) -> str:
    reply = ""
    async for token in llm.stream_reply(query):
        reply += token
    calls = [(c["name"], c["args"]) for c in db_client.last_tool_calls]
    print(f"\n=== QUERY: {query!r}")
    print(f"TOOL CALLS: {calls}")
    print(f"HIGHLIGHT: {db_client.last_highlight_status}")
    print(f"REPLY:\n{reply}\n")
    return reply


async def main() -> None:
    settings = Settings()
    settings.rerun_enabled = False

    router = ToolRouter(settings) if settings.tool_router_enabled else None
    db_client = InspectionDBClient(
        Path(settings.inspection_db_path), router=router, settings=settings
    )
    llm = LocalLLM(settings, db_client=db_client)

    failures = 0
    for query in QUERIES:
        reply = await run_query(llm, db_client, query)
        if "not provided" in reply.lower() or "not available" in reply.lower():
            print(f"FAIL: {query!r} -> answer claims data not provided")
            failures += 1
        elif query.startswith("where") and not COORD_RE.search(reply):
            print(f"FAIL: {query!r} -> location question answered without any (x, y, z)")
            failures += 1
        else:
            print(f"PASS: {query!r}")

    db_client.close()
    if failures:
        raise SystemExit(f"{failures} query(ies) FAILED")
    print("ALL E2E QUERIES PASSED")


asyncio.run(main())
