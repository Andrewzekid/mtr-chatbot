"""Whole-pipeline regression tests for the MTR-Insight voice assistant.

These tests exercise the deterministic backend pipeline end-to-end against the
real SQLite inspection database:

    user question
        -> InspectionDBClient.lookup()        (router + tools + final-pass highlight)
            -> db_context string               (the formatted text the answerer sees)
            -> last_tool_calls / last_highlight_status / last_highlight_args

The Ollama LLM is NOT called. The ToolRouter is replaced with deterministic
stub routers that return scripted tool calls, so the tests are fast, hermetic,
and reproducible. The RerunVisualizer is replaced with a FakeRerun that records
every push.

Run as a script::

    python scripts/test_pipeline.py
    python -m unittest scripts.test_pipeline -v

The tests assert on the REAL formatted output that the answerer LLM would
receive, so they catch regressions in tool formatting, anomaly top-up, the
safety net, the cached anomaly trio, dedup, and the final-pass highlight
decision (including the deterministic fallback).
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Allow importing app.services from the backend directory.
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

DB_PATH = BACKEND.parent / "MTR Inspection Database" / "inspection_v2_mtr_new.db"

from app.services.db_service import InspectionDBClient  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeRerun:
    """Records every highlight() call so tests can assert on it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def highlight(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"Highlighted: {kwargs}"


class StubRouter:
    """Configurable router stub.

    ``calls_per_round`` is a list whose entries correspond to router rounds.
    Each entry is a list of (tool_name, args) tuples the router "selects" in
    that round. After the list is exhausted the router returns [] (stops).
    """

    def __init__(self, calls_per_round: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.calls_per_round = calls_per_round
        self._round = 0
        self.settings = SimpleNamespace(tool_router_max_rounds=max(1, len(calls_per_round)))
        self.last_raw_response: dict[str, Any] | None = None
        self.decide_highlights_return: dict[str, Any] | None = None
        self.decide_highlights_calls: list[tuple[str, str, Any]] = []

    def select_tool(
        self,
        query: str,
        chat_history: Any,
        tool_history: Any,
        prior_results: Any,
        current_turn_calls: Any = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if self._round >= len(self.calls_per_round):
            return []
        out = self.calls_per_round[self._round]
        self._round += 1
        return out

    def decide_highlights(
        self,
        query: str,
        tool_results_text: str,
        chat_history: Any = None,
        tool_history: Any = None,
    ) -> dict[str, Any] | None:
        self.decide_highlights_calls.append((query, tool_results_text, tool_history))
        return self.decide_highlights_return


def make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        tool_router_max_rounds=3,
        tool_router_enabled=True,
        rerun_enabled=True,
    )


def make_client(router: StubRouter, rerun: FakeRerun) -> InspectionDBClient:
    return InspectionDBClient(
        db_path=DB_PATH,
        router=router,
        settings=make_settings(),
        rerun_visualizer=rerun,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
COORD_RE = re.compile(r"\(-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?\)")
BRACKET_COORD_RE = re.compile(r"\[-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?\]")


def extract_tool_names(calls: list[dict[str, Any]]) -> list[str]:
    """Extract tool names from InspectionDBClient.last_tool_calls (list of dicts)."""
    return [c["name"] for c in calls]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests of InspectionDBClient.lookup() with stub routers."""

    # --- summary / counts ----------------------------------------------------

    async def test_summary_question_returns_total_counts(self) -> None:
        """'Give me a summary of the inspection' -> get_summary + get_categories."""
        router = StubRouter([[("get_summary", {})]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Give me a summary of the inspection")

        self.assertIsNotNone(ctx, "db_context must not be None for a summary question")
        assert ctx is not None  # for mypy
        self.assertIn("Total objects: 195", ctx)
        self.assertIn("Lights", ctx)
        self.assertIn("Ticket Gate", ctx)
        # The router called exactly one tool (no top-ups for non-anomaly queries).
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_summary"])
        # get_summary is a broad-summary query, so the deterministic fallback
        # highlight lights up every category (all categories are relevant).
        self.assertIsNotNone(client.last_highlight_status,
                             "get_summary fallback should highlight all categories")
        last = rerun.calls[-1]
        self.assertTrue(last.get("categories"),
                        "broad summary fallback must highlight all categories")
        client.close()

    # --- category questions --------------------------------------------------

    async def test_ticket_gate_coordinates_question(self) -> None:
        """'Coordinates of the ticket gates?' -> coordinates in db_context."""
        router = StubRouter([[("get_category_objects_coordinates", {"category": "Ticket Gate"})]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("What are the coordinates of the ticket gates?")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertIn("Ticket Gate", ctx)
        # Coordinates tuples appear in the formatted output.
        self.assertTrue(COORD_RE.search(ctx) or BRACKET_COORD_RE.search(ctx),
                        f"expected coordinates in output, got: {ctx[:300]}")
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_category_objects_coordinates"])
        client.close()

    async def test_show_me_lights_calls_image_tool(self) -> None:
        """'Show me some lights' -> get_category_sample_images + 3D highlight."""
        router = StubRouter([[("get_category_sample_images", {"category": "Lights", "limit": 5})]])
        # Final-pass decider says highlight the Lights category.
        router.decide_highlights_return = {"category": "Lights", "keep_existing": False}
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Show me some lights")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        # The sample-images formatter returns markdown image links.
        self.assertTrue(IMAGE_LINK_RE.search(ctx),
                        f"expected image links in output, got: {ctx[:300]}")
        # Final-pass highlight was applied.
        self.assertIsNotNone(client.last_highlight_status)
        self.assertTrue(rerun.calls, "FakeRerun should have received a highlight")
        self.assertEqual(rerun.calls[-1].get("category"), "Lights")
        client.close()

    # --- anomalies: the key test for this codebase --------------------------

    async def test_anomaly_summary_question_has_summary_then_per_anomaly(self) -> None:
        """'Give me a summary of the anomalies' -> get_anomaly_summary + top-up to get_anomalies + get_anomaly_locations.

        The formatted db_context must contain:
          - the brief counts-by-type summary, AND
          - a per-anomaly block for every abnormality (13 total) with image links.
        """
        router = StubRouter([[("get_anomaly_summary", {})]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Give me a summary of the anomalies")

        self.assertIsNotNone(ctx, "anomaly question must produce db_context")
        assert ctx is not None
        # The router only called get_anomaly_summary; the backend's anomaly
        # completeness top-up must add get_anomaly_locations and get_anomalies.
        names = extract_tool_names(client.last_tool_calls)
        self.assertIn("get_anomaly_summary", names)
        self.assertIn("get_anomalies", names, "anomaly top-up must add get_anomalies")
        self.assertIn("get_anomaly_locations", names,
                      "anomaly top-up must add get_anomaly_locations")
        # Brief summary: total count and per-type counts.
        self.assertIn("13 abnormalit", ctx)
        self.assertIn("foreign_object: 3", ctx)
        self.assertIn("state_change: 2", ctx)
        self.assertIn("relocation: 1", ctx)
        # Per-anomaly walkthrough: every abnormality id 1..13 must appear.
        for anomaly_id in range(1, 14):
            self.assertIn(f"Anomaly {anomaly_id} (inspection 2)", ctx,
                          f"Anomaly {anomaly_id} missing from per-anomaly block")
        # Each anomaly entry should include image links inline.
        image_links = IMAGE_LINK_RE.findall(ctx)
        # 13 abnormalities * 2 images (gt + inspection) = at least 26 links.
        self.assertGreaterEqual(len(image_links), 26,
                                f"expected >=26 image links (13 anomalies * 2), got {len(image_links)}")
        # Image URLs reference /inspection/images/ (the source camera frames).
        for url in image_links:
            self.assertTrue(url.startswith("/inspection/images/"),
                            f"unexpected image URL: {url}")
        # Anomaly locations were pushed to the Rerun viewer (always-highlight).
        self.assertIsNotNone(client.last_highlight_status,
                             "anomaly questions must always push highlights")
        self.assertTrue(rerun.calls, "FakeRerun must receive anomaly coordinate push")
        last = rerun.calls[-1]
        self.assertTrue(last.get("coordinates"),
                        "anomaly highlight must use coordinates, not category")
        client.close()

    async def test_anomaly_locations_question_includes_xyz(self) -> None:
        """'Where are the anomalies?' -> get_anomaly_locations + (x, y, z) in output."""
        router = StubRouter([[("get_anomaly_locations", {})]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Where are the anomalies located?")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertIn("get_anomaly_locations", names)
        # The anomaly top-up adds get_anomalies too.
        self.assertIn("get_anomalies", names)
        # Camera positions are formatted as bracketed [x, y, z] tuples.
        self.assertTrue(BRACKET_COORD_RE.search(ctx),
                        "expected [x, y, z] camera positions in output")
        # Anomaly 7 (recycling machine) camera position must appear.
        self.assertIn("Anomaly 7", ctx)
        self.assertIn("relocation", ctx)
        self.assertIn("recycling machine", ctx)
        # Always-highlight triggered for anomaly queries.
        self.assertIsNotNone(client.last_highlight_status)
        client.close()

    async def test_single_anomaly_question_returns_just_that_anomaly(self) -> None:
        """'Tell me about anomaly 7' -> only Anomaly 7 in the per-anomaly block."""
        router = StubRouter([[("get_anomalies", {"anomaly_id": 7})]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Tell me about anomaly 7")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertIn("Anomaly 7", ctx)
        self.assertIn("relocation", ctx)
        self.assertIn("recycling machine", ctx)
        # Anomaly 7's image links must be present.
        self.assertIn("/inspection/images/235.jpg", ctx)  # gt frame
        self.assertIn("/inspection/images/502.jpg", ctx)  # inspection frame
        # Anomaly 6 must NOT appear (single-anomaly query is scoped).
        self.assertNotIn("Anomaly 6 (inspection", ctx)
        self.assertNotIn("Anomaly 8 (inspection", ctx)
        # The anomaly top-up still adds get_anomaly_locations scoped to anomaly 7.
        names = extract_tool_names(client.last_tool_calls)
        self.assertIn("get_anomaly_locations", names)
        # Highlight is scoped to anomaly 7 only.
        self.assertIsNotNone(client.last_highlight_status)
        last = rerun.calls[-1]
        coords = last.get("coordinates") or []
        # Only anomaly 7's camera position (-12.93, 31.65, -4.35) should be marked.
        self.assertEqual(len(coords), 1, f"expected 1 anomaly coord, got {len(coords)}")
        client.close()

    # --- proximity + final-pass highlight decision --------------------------

    async def test_proximity_question_final_pass_uses_object_ids(self) -> None:
        """'Lights near ticket gates within 2m?' -> final pass highlights specific ids, not categories."""
        router = StubRouter([[
            ("get_category_proximity", {
                "target_category": "Ticket Gate",
                "other_categories": ["Lights"],
                "radius_m": 2.0,
            }),
        ]])
        # The LLM decides to highlight specific object ids returned by the
        # proximity tool (the correct behavior for proximity queries).
        router.decide_highlights_return = {
            "object_ids": [109, 110],  # example nearby ids
            "keep_existing": False,
        }
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("What lights are within 2 meters of the ticket gates?")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertIn("Ticket Gate", ctx)
        self.assertIn("Lights", ctx)
        # Final-pass highlight used object_ids (validated as a proximity query).
        self.assertIsNotNone(client.last_highlight_status)
        last = rerun.calls[-1]
        self.assertTrue(last.get("object_ids"),
                        "proximity highlight must use object_ids, not categories")
        self.assertFalse(last.get("category"),
                         "proximity highlight must not set a single category")
        self.assertFalse(last.get("categories"),
                         "proximity highlight must not set categories")
        client.close()

    async def test_proximity_question_rejects_category_highlight(self) -> None:
        """A buggy final-pass decision (highlight whole categories for a proximity
        query) must be rejected by _decision_matches_tool_results and fall back to
        the deterministic proximity-id highlight."""
        router = StubRouter([[
            ("get_category_proximity", {
                "target_category": "Ticket Gate",
                "other_categories": ["Lights"],
                "radius_m": 2.0,
            }),
        ]])
        # Buggy LLM decision: highlight whole categories instead of nearby ids.
        router.decide_highlights_return = {
            "categories": ["Ticket Gate", "Lights"],
            "keep_existing": False,
        }
        rerun = FakeRerun()
        client = make_client(router, rerun)

        await client.lookup("What is near the ticket gates?")

        # The deterministic fallback should have run. For proximity queries the
        # fallback re-runs get_category_proximity_with_images and highlights the
        # specific nearby object ids, so the highlight must carry object_ids.
        self.assertIsNotNone(client.last_highlight_status)
        last = rerun.calls[-1]
        # Either object_ids or coordinates are acceptable (proximity-specific);
        # categories alone are NOT.
        self.assertTrue(
            last.get("object_ids") or last.get("coordinates"),
            f"proximity fallback must use ids/coords, not categories: {last}",
        )
        self.assertFalse(last.get("categories"),
                         "category highlight must be rejected for proximity queries")
        client.close()

    # --- safety net ----------------------------------------------------------

    async def test_safety_net_runs_when_router_returns_nothing(self) -> None:
        """If the router returns no tools for a category question, the safety net
        deterministically fetches that category so the answerer never invents data."""
        router = StubRouter([[]])  # router returns nothing
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Tell me about the lights")

        self.assertIsNotNone(ctx, "safety net must produce db_context for category queries")
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertTrue(names, "safety net must run at least one tool")
        # For a non-show-intent category query the safety net calls get_objects_by_category.
        self.assertIn("get_objects_by_category", names)
        self.assertIn("Lights", ctx)
        client.close()

    async def test_safety_net_anomaly_question_runs_anomaly_tools(self) -> None:
        """An anomaly question with no router calls triggers the anomaly safety net."""
        router = StubRouter([[]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("What anomalies were found?")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        # Safety net for anomaly queries runs get_anomaly_summary + get_anomalies(limit=5).
        self.assertIn("get_anomaly_summary", names)
        self.assertIn("get_anomalies", names)
        # Anomaly top-up then adds get_anomaly_locations.
        self.assertIn("get_anomaly_locations", names)
        self.assertIn("13 abnormalit", ctx)
        # Always-highlight triggered.
        self.assertIsNotNone(client.last_highlight_status)
        client.close()

    async def test_safety_net_leaves_chit_chat_untouched(self) -> None:
        """Greetings / chit-chat with no router calls stay empty (no db_context)."""
        router = StubRouter([[]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Hello, how are you?")

        self.assertIsNone(ctx, "chit-chat must not produce db_context")
        self.assertEqual(client.last_tool_calls, [])
        client.close()

    # --- dedup --------------------------------------------------------------

    async def test_repeated_tool_calls_are_deduplicated(self) -> None:
        """If the router returns the same (name, args) in two rounds, the second
        call is skipped (no infinite loop, no duplicate output)."""
        router = StubRouter([
            [("get_summary", {})],
            [("get_summary", {})],  # duplicate -> must be dropped
        ])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Tell me about the inspection")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        # get_summary must appear exactly once despite the router returning it twice.
        self.assertEqual(names.count("get_summary"), 1,
                         f"expected get_summary exactly once, got {names}")
        client.close()

    # --- cached anomaly trio follow-up --------------------------------------

    async def test_cached_anomaly_trio_reuses_context(self) -> None:
        """A generic anomaly follow-up ('tell me more about them') reuses the cached
        anomaly trio context without re-querying the database, but still runs the
        final-pass highlight decision so the viewer can be refined."""
        # First turn: router fetches the full anomaly trio.
        router1 = StubRouter([[
            ("get_anomaly_summary", {}),
            ("get_anomalies", {}),
            ("get_anomaly_locations", {}),
        ]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        first_ctx = await client.lookup("What anomalies were found?")
        self.assertIsNotNone(first_ctx)
        assert first_ctx is not None
        first_calls = list(client.last_tool_calls)

        # Second turn: generic follow-up with no new anomaly scope.
        # The cached path should be taken, so a fresh stub router with NO calls
        # still yields the SAME db_context (the cached one).
        router2 = StubRouter([[]])  # would normally produce no context
        client.router = router2

        second_ctx = await client.lookup("Tell me more about them")

        self.assertIsNotNone(second_ctx, "cached anomaly trio must produce db_context")
        assert second_ctx is not None
        self.assertEqual(second_ctx, first_ctx,
                         "follow-up must reuse the exact cached anomaly context")
        # The tool calls are replayed from the cache (not re-executed).
        self.assertEqual(client.last_tool_calls, first_calls)
        # The cached path still runs the final-pass highlight decision so
        # follow-ups like "highlight anomaly 4" can refine the viewer.
        self.assertEqual(len(router2.decide_highlights_calls), 1,
                         "cached path must still invoke decide_highlights")
        client.close()

    async def test_cached_anomaly_trio_bypassed_when_scope_changes(self) -> None:
        """A follow-up that names a specific anomaly id is NOT a cached follow-up —
        it must re-query the database scoped to that anomaly."""
        router1 = StubRouter([[
            ("get_anomaly_summary", {}),
            ("get_anomalies", {}),
            ("get_anomaly_locations", {}),
        ]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What anomalies were found?")

        # New turn: user names anomaly 7 specifically.
        router2 = StubRouter([[("get_anomalies", {"anomaly_id": 7})]])
        client.router = router2

        ctx = await client.lookup("Tell me about anomaly 7")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertIn("Anomaly 7", ctx)
        self.assertIn("recycling machine", ctx)
        # The cached trio is NOT used — the new scoped query ran.
        self.assertNotIn("Anomaly 6 (inspection", ctx)
        self.assertNotIn("Anomaly 8 (inspection", ctx)
        client.close()

    # --- off-topic / no-tools path -----------------------------------------

    async def test_off_topic_question_with_no_tools_returns_none(self) -> None:
        """When the router returns nothing and the query is not object/anomaly-related,
        lookup() returns None and the answerer replies from general knowledge."""
        router = StubRouter([[]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("What is the weather today?")

        self.assertIsNone(ctx, "off-topic query with no tools must yield no db_context")
        self.assertEqual(client.last_tool_calls, [])
        client.close()

    # --- multi-round --------------------------------------------------------

    async def test_multi_round_router_chains_tools(self) -> None:
        """The router can call get_summary in round 1 and get_categories in round 2."""
        router = StubRouter([
            [("get_summary", {})],
            [("get_categories", {})],
        ])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("Tell me about the inspection and what categories exist")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_summary", "get_categories"])
        self.assertIn("Total objects: 195", ctx)
        self.assertIn("Advertisement Board", ctx)
        client.close()

    # --- changed-radius re-call (deterministic fallback) -------------------

    async def test_changed_radius_recall_re_calls_with_new_radius(self) -> None:
        """A follow-up 'how about within 5m' after a 2m get_objects_near_position
        query must re-call get_objects_near_position with radius_m=5.0, even when
        the router returns no tools. The other args (x, y, z) are preserved."""
        # Turn 1: router calls get_objects_near_position with radius_m=2.0.
        router1 = StubRouter([[(
            "get_objects_near_position",
            {"x": 0.02, "y": 1.46, "z": 1.12, "radius_m": 2.0},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        first_ctx = await client.lookup("What's within 2 meters of (0.02, 1.46, 1.12)?")
        self.assertIsNotNone(first_ctx)
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        # Turn 2: user asks "how about within 5m". Router returns NO tools
        # (the bug we're fixing — the router thinks the question was answered).
        router2 = StubRouter([[]])
        client.router = router2

        second_ctx = await client.lookup(
            "how about within 5m",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(second_ctx,
                             "changed-radius re-call must produce db_context even "
                             "when the router returned no tools")
        assert second_ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_objects_near_position"],
                         f"expected a single re-call of get_objects_near_position, got {names}")
        # The re-called tool must use radius_m=5.0 and preserve x/y/z.
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["radius_m"], 5.0)
        self.assertEqual(args["x"], 0.02)
        self.assertEqual(args["y"], 1.46)
        self.assertEqual(args["z"], 1.12)
        client.close()

    async def test_changed_radius_recall_works_for_proximity_tool(self) -> None:
        """The changed-radius re-call also covers get_category_proximity: a follow-up
        'try 5 meters' after a 2m proximity query re-runs it with radius_m=5.0."""
        router1 = StubRouter([[
            ("get_category_proximity", {
                "target_category": "Ticket Gate",
                "other_categories": ["Lights"],
                "radius_m": 2.0,
            }),
        ]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What lights are within 2 meters of the ticket gates?")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "try 5 meters",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_category_proximity"])
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["radius_m"], 5.0)
        self.assertEqual(args["target_category"], "Ticket Gate")
        self.assertEqual(args["other_categories"], ["Lights"])
        client.close()

    async def test_changed_radius_recall_skips_when_radius_unchanged(self) -> None:
        """If the user names the SAME radius as the prior call, no re-call happens
        (the question is genuinely already answered)."""
        router1 = StubRouter([[
            ("get_objects_near_position",
             {"x": 0.02, "y": 1.46, "z": 1.12, "radius_m": 2.0}),
        ]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What's within 2 meters of (0.02, 1.46, 1.12)?")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        # Same radius -> no re-call; query is also not object/anomaly-related
        # so the generic safety net does nothing either -> None.
        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "how about within 2m",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNone(ctx,
                          "same-radius follow-up must not trigger a re-call")
        self.assertEqual(client.last_tool_calls, [])
        client.close()

    async def test_changed_radius_recall_does_not_fire_without_history(self) -> None:
        """Without any prior tool history, a radius mention does not trigger a re-call."""
        router = StubRouter([[]])
        rerun = FakeRerun()
        client = make_client(router, rerun)

        ctx = await client.lookup("how about within 5m")

        self.assertIsNone(ctx,
                          "radius mention without prior radius-bearing call must "
                          "not produce db_context")
        self.assertEqual(client.last_tool_calls, [])
        client.close()

    # --- flexible changed-parameter re-call (beyond radius) ----------------

    async def test_changed_limit_recall_doubles_on_more(self) -> None:
        """A follow-up 'show me more' after a capped get_objects_by_category(limit=5)
        re-runs the tool with a doubled limit (10)."""
        router1 = StubRouter([[(
            "get_objects_by_category",
            {"category": "Lights", "limit": 5},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("Show me 5 lights")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "show me more",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_objects_by_category"])
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["limit"], 10, f"limit should double 5->10, got {args}")
        self.assertEqual(args["category"], "Lights")
        client.close()

    async def test_changed_limit_recall_all_keyword(self) -> None:
        """A follow-up 'show me all of them' after a capped call sets limit to 'all'."""
        router1 = StubRouter([[(
            "get_objects_by_category",
            {"category": "Lights", "limit": 5},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("Show me 5 lights")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "show me all of them",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["limit"], "all")
        self.assertEqual(args["category"], "Lights")
        client.close()

    async def test_changed_coord_recall_re_calls_near_position(self) -> None:
        """A follow-up giving a new (x, y, z) tuple re-calls get_objects_near_position
        with the new coordinates, preserving radius_m and category."""
        router1 = StubRouter([[(
            "get_objects_near_position",
            {"x": 0.02, "y": 1.46, "z": 1.12, "radius_m": 2.0},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What's near (0.02, 1.46, 1.12)?")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "how about (-12.93, 31.65, -4.35)",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_objects_near_position"])
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["x"], -12.93)
        self.assertEqual(args["y"], 31.65)
        self.assertEqual(args["z"], -4.35)
        # radius_m preserved from the prior call.
        self.assertEqual(args["radius_m"], 2.0)
        client.close()

    async def test_changed_radius_and_coord_recall_updates_both(self) -> None:
        """A follow-up naming both a new radius AND new coordinates updates both."""
        router1 = StubRouter([[(
            "get_objects_near_position",
            {"x": 0.02, "y": 1.46, "z": 1.12, "radius_m": 2.0},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What's near (0.02, 1.46, 1.12)?")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "how about within 5m of (-12.93, 31.65, -4.35)",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["radius_m"], 5.0)
        self.assertEqual(args["x"], -12.93)
        self.assertEqual(args["y"], 31.65)
        self.assertEqual(args["z"], -4.35)
        client.close()

    # --- action-intent re-call + highlight of nearby object ids ------------

    async def test_action_recall_re_calls_near_position_with_unchanged_radius(self) -> None:
        """The reported failure: 'highlight all objects detected within 5m of a crack
        in rerun' repeats the previous 5m query. The router returns nothing; the
        action-intent re-call re-runs get_objects_near_position with the SAME args,
        and the deterministic fallback highlights the returned object ids (not just
        the reference point). decide_highlights also receives the tool history."""
        router1 = StubRouter([[(
            "get_objects_near_position",
            {"x": 0.02, "y": 1.46, "z": 1.12, "radius_m": 5.0},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What's within 5m of (0.02, 1.46, 1.12)?")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "highlight all objects detected within 5m of a crack in rerun",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(
            names,
            ["get_objects_near_position"],
            "action intent must re-call the near-position tool even when the radius is unchanged",
        )
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["radius_m"], 5.0)
        self.assertEqual(args["x"], 0.02)
        self.assertEqual(args["y"], 1.46)
        self.assertEqual(args["z"], 1.12)

        # decide_highlights must have seen the past tool history.
        self.assertTrue(router2.decide_highlights_calls,
                        "final-pass must call decide_highlights")
        _, _, hist = router2.decide_highlights_calls[-1]
        self.assertEqual(hist, [{"tool_calls": first_payload}],
                         "decide_highlights must receive the cross-turn tool history")

        # The deterministic fallback must highlight the 12 nearby objects.
        self.assertTrue(rerun.calls, "fallback highlight must have run")
        last_hl = rerun.calls[-1]
        self.assertEqual(
            sorted(last_hl.get("object_ids") or []),
            [9, 12, 15, 307, 309, 311, 312, 316, 607, 611, 613, 617],
            "near-position branch must highlight the nearby object ids",
        )
        client.close()

    async def test_action_recall_works_without_radius_mention(self) -> None:
        """A highlight request that does NOT repeat the radius ('highlight all
        objects near the crack in rerun') still re-calls the prior near-position
        tool via action intent, preserving its unchanged arguments."""
        router1 = StubRouter([[(
            "get_objects_near_position",
            {"x": 0.02, "y": 1.46, "z": 1.12, "radius_m": 2.0},
        )]])
        rerun = FakeRerun()
        client = make_client(router1, rerun)

        await client.lookup("What's within 2m of (0.02, 1.46, 1.12)?")
        first_payload = [{"name": c["name"], "args": c["args"], "output": ""}
                          for c in client.last_tool_calls]

        router2 = StubRouter([[]])
        client.router = router2

        ctx = await client.lookup(
            "highlight all objects near the crack in rerun",
            tool_history=[{"tool_calls": first_payload}],
        )

        self.assertIsNotNone(ctx)
        assert ctx is not None
        names = extract_tool_names(client.last_tool_calls)
        self.assertEqual(names, ["get_objects_near_position"])
        args = client.last_tool_calls[0]["args"]
        self.assertEqual(args["radius_m"], 2.0,
                         "unchanged arguments must be preserved on action re-call")
        client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)