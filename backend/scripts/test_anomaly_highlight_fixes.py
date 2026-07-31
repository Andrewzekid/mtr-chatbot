"""Focused regression tests for anomaly/proximity highlight behaviour."""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

# Allow importing app.services from the backend directory.
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services.db_service import InspectionDBClient

DB_PATH = BACKEND.parent / "MTR Inspection Database" / "inspection_v2_mtr_new.db"


def make_settings():
    return SimpleNamespace(tool_router_max_rounds=1)


def make_client(rerun, router):
    return InspectionDBClient(
        db_path=DB_PATH,
        router=router,
        settings=make_settings(),
        rerun_visualizer=rerun,
    )


class FakeRerun:
    def __init__(self):
        self.calls = []

    def highlight(self, **kwargs):
        self.calls.append(kwargs)
        return f"Highlighted: {kwargs}"


class AnomalySevenRouter:
    """Router that answers 'show me anomaly 7' with get_anomalies only."""

    settings = make_settings()
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            return [("get_anomalies", {"anomaly_id": 7})]
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        # Return a generic coordinate label; the backend should enrich it.
        return {
            "coordinates": [{"x": -12.93, "y": 31.65, "z": -4.35, "label": "anomaly"}],
            "keep_existing": False,
            "label": "anomaly",
        }


class ProximityBadDecisionRouter:
    """Router that runs a proximity query but decides to highlight whole categories."""

    settings = make_settings()
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            return [
                (
                    "get_category_proximity",
                    {
                        "target_category": "Lights",
                        "other_categories": ["Advertisement Board"],
                        "radius_m": 2.0,
                    },
                )
            ]
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        # This is the bug: highlighting all Lights and Advertisement Boards.
        return {
            "categories": ["Lights", "Advertisement Board"],
            "keep_existing": False,
            "label": "final",
        }


class FullAnomalyTrioRouter:
    """Router that fetches the full anomaly trio on the first query."""

    settings = make_settings()
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            return [
                ("get_anomaly_summary", {}),
                ("get_anomalies", {}),
                ("get_anomaly_locations", {}),
            ]
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        return None


class SequentialSingleAnomalyRouter:
    """Router that answers consecutive 'show me anomaly N' queries.

    The highlight decision is deliberately category-only so the backend is
    forced down the deterministic anomaly-coordinate fallback path.  That path
    previously ignored the scoped anomaly id and highlighted every location.
    """

    settings = make_settings()
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            m = re.search(r"\banomaly\s*(?:id|#)?\s*(\d+)\b", query, re.IGNORECASE)
            if m:
                return [("get_anomalies", {"anomaly_id": int(m.group(1))})]
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        # Category-only decision must be rejected for anomaly queries; the test
        # verifies the fallback highlights only the scoped anomaly.
        return {
            "categories": ["Anomaly"],
            "keep_existing": False,
            "label": "anomaly",
        }


async def test_anomaly_seven_label():
    rerun = FakeRerun()
    client = make_client(rerun, AnomalySevenRouter())
    ctx = await client.lookup("show me anomaly 7")
    assert ctx is not None
    assert "Anomaly 7" in ctx
    # The anomaly pair id should not be exposed in the user-facing text.
    assert "pair 6" not in ctx
    assert rerun.calls, "expected a Rerun highlight call"
    coords = rerun.calls[0].get("coordinates") or []
    assert len(coords) == 1, coords
    label = coords[0].get("label", "")
    assert label.startswith("Anomaly 7:"), f"unexpected label: {label!r}"
    assert "relocation" in label and "recycling machine" in label, label
    client.close()
    print("PASS: anomaly 7 highlight label is canonical")


async def test_proximity_bad_decision_overridden():
    rerun = FakeRerun()
    client = make_client(rerun, ProximityBadDecisionRouter())
    ctx = await client.lookup(
        "what advertisement boards were within 2m of lights?"
    )
    assert ctx is not None
    assert rerun.calls, "expected a Rerun highlight call"
    args = rerun.calls[0]
    # The category-only decision must be rejected in favour of specific object ids.
    assert not args.get("categories"), f"categories should not be highlighted: {args}"
    object_ids = args.get("object_ids") or []
    assert object_ids, f"expected object_ids from proximity fallback: {args}"
    # The result text mentions object ids; use it as a sanity check.
    assert "Object" in ctx
    client.close()
    print("PASS: proximity category-only decision overridden with object_ids")


async def test_cached_anomaly_follow_up_highlights():
    rerun = FakeRerun()
    router = FullAnomalyTrioRouter()
    client = make_client(rerun, router)

    # First query: fills the cached anomaly trio.
    ctx1 = await client.lookup("tell me about the anomalies")
    assert ctx1 is not None
    first_calls = list(rerun.calls)
    assert first_calls and first_calls[0].get("coordinates"), first_calls
    rerun.calls.clear()

    # Follow-up that should reuse the cache and still push a highlight.
    ctx2 = await client.lookup("show me all anomaly locations")
    assert ctx2 is not None
    assert rerun.calls, "cached follow-up should still produce a Rerun highlight"
    coords = rerun.calls[0].get("coordinates") or []
    assert coords, rerun.calls
    # All anomaly locations should be highlighted.
    assert len(coords) >= 10, f"expected all anomaly locations, got {len(coords)}"
    client.close()
    print("PASS: cached anomaly follow-up highlights all anomaly locations")


async def test_highlight_history_recorded():
    rerun = FakeRerun()
    client = make_client(rerun, AnomalySevenRouter())
    await client.lookup("show me anomaly 7")
    history = client.highlight_history
    assert history, "expected highlight history to be populated"
    # Two calls: explicit? Actually anomaly 7 path uses final-pass decision -> _apply_highlight_decision.
    # That should record one entry.
    assert any("Anomaly 7" in str(entry.get("args")) for entry in history), history
    client.close()
    print("PASS: highlight history records anomaly highlights")


async def test_anomaly_locations_formatter_no_pair():
    rerun = FakeRerun()
    client = make_client(rerun, FullAnomalyTrioRouter())
    text = client._format_anomaly_locations()
    # No "Pair N" lines; only "Anomaly N".
    pair_lines = [line for line in text.splitlines() if re.search(r"\bPair\s+\d+", line, re.IGNORECASE)]
    assert not pair_lines, f"unexpected Pair lines: {pair_lines}"
    assert "Anomaly 1" in text
    client.close()
    print("PASS: get_anomaly_locations formatter omits pair numbers")


async def test_sequential_single_anomaly_scoped():
    rerun = FakeRerun()
    client = make_client(rerun, SequentialSingleAnomalyRouter())

    ctx1 = await client.lookup("show me anomaly 7")
    assert ctx1 is not None and "Anomaly 7" in ctx1
    assert rerun.calls, "expected highlight for anomaly 7"
    coords7 = rerun.calls[0].get("coordinates") or []
    assert len(coords7) == 1, f"expected one anomaly 7 coordinate, got {len(coords7)}"
    assert coords7[0].get("label", "").startswith("Anomaly 7:"), coords7[0]
    rerun.calls.clear()

    ctx2 = await client.lookup("show me anomaly 8")
    assert ctx2 is not None and "Anomaly 8" in ctx2
    assert rerun.calls, "expected highlight for anomaly 8"
    coords8 = rerun.calls[0].get("coordinates") or []
    assert len(coords8) == 1, f"expected one anomaly 8 coordinate, got {len(coords8)}"
    assert coords8[0].get("label", "").startswith("Anomaly 8:"), coords8[0]
    client.close()
    print("PASS: sequential single-anomaly queries stay scoped")


class ObjectsProximityRouter:
    """Router that answers 'what lights are near objects 9 and 11?'"""

    settings = make_settings()
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            return [
                (
                    "get_objects_proximity_with_images",
                    {"object_ids": [9, 11], "target_category": "Lights", "radius_m": 5.0},
                )
            ]
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        return None


async def test_objects_proximity_highlight():
    rerun = FakeRerun()
    client = make_client(rerun, ObjectsProximityRouter())
    ctx = await client.lookup("what lights are near objects 9 and 11")
    assert ctx is not None
    assert "Object 9" in ctx or "objects [9, 11]" in ctx
    assert rerun.calls, "expected a Rerun highlight call"
    args = rerun.calls[0]
    # The fallback should highlight specific object ids, not whole categories.
    assert not args.get("categories"), f"categories should not be highlighted: {args}"
    object_ids = args.get("object_ids") or []
    assert 9 in object_ids, f"source object 9 should be highlighted: {object_ids}"
    assert 11 in object_ids, f"source object 11 should be highlighted: {object_ids}"
    # At least one nearby light object should also be highlighted.
    light_ids = {oid for oid in object_ids if oid not in (9, 11)}
    assert light_ids, f"expected nearby light object ids to be highlighted: {object_ids}"
    client.close()
    print("PASS: object-id proximity fallback highlights source + nearby objects")


async def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(DB_PATH)
    await test_anomaly_seven_label()
    await test_proximity_bad_decision_overridden()
    await test_cached_anomaly_follow_up_highlights()
    await test_highlight_history_recorded()
    await test_anomaly_locations_formatter_no_pair()
    await test_sequential_single_anomaly_scoped()
    await test_objects_proximity_highlight()
    print("\nALL ANOMALY HIGHLIGHT FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
