"""Smoke test: category-mention safety net, anomaly highlight fallback, subset limits."""
import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.services.db_service import InspectionDBClient


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE inspections (id INTEGER PRIMARY KEY, started_at TEXT, is_gt INTEGER);
        CREATE TABLE images (id INTEGER PRIMARY KEY, inspection_id INTEGER, timestamp_ns INTEGER,
                             tf_translation_x REAL, tf_translation_y REAL, tf_translation_z REAL,
                             tf_rotation_x REAL, tf_rotation_y REAL, tf_rotation_z REAL, tf_rotation_w REAL,
                             filename TEXT);
        CREATE TABLE objects (id INTEGER PRIMARY KEY, category_id INTEGER, centroid_x REAL, centroid_y REAL,
                              centroid_z REAL, min_x REAL, min_y REAL, min_z REAL,
                              max_x REAL, max_y REAL, max_z REAL, is_gt INTEGER, created_at TEXT);
        CREATE TABLE detections (id INTEGER PRIMARY KEY, image_id INTEGER, object_id INTEGER,
                                 centroid_x REAL, centroid_y REAL, centroid_z REAL,
                                 min_x REAL, min_y REAL, min_z REAL, max_x REAL, max_y REAL, max_z REAL);
        CREATE TABLE anomaly_types (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE abnormal_detections (id INTEGER PRIMARY KEY, gt_image INTEGER,
                                          inspection_image INTEGER, summary TEXT);
        CREATE TABLE abnormalities (id INTEGER PRIMARY KEY, pair INTEGER, type INTEGER,
                                    object TEXT, location TEXT, note TEXT,
                                    min_x REAL, min_y REAL, max_x REAL, max_y REAL);
        INSERT INTO categories (id, name) VALUES (1, 'Lights'), (2, 'Advertisement Board');
        INSERT INTO inspections (id, started_at, is_gt) VALUES (1, '2026-01-01 16:00:00', 1),
            (2, '2026-01-02 16:00:00', 0);
        INSERT INTO images (id, inspection_id, timestamp_ns, filename,
                            tf_translation_x, tf_translation_y, tf_translation_z) VALUES
            (1, 1, 1700000000000000000, '1.jpg', 0.0, 0.0, 0.0),
            (2, 2, 1700000001000000000, '2.jpg', 9.0, 8.0, 7.0);
        INSERT INTO objects (id, category_id, centroid_x, centroid_y, centroid_z) VALUES
            (10, 1, 0.0, 4.5, 2.9), (11, 1, 1.0, 4.5, 2.9), (12, 1, 2.0, 4.5, 2.9),
            (13, 1, 3.0, 4.5, 2.9), (14, 1, 4.0, 4.5, 2.9), (15, 1, 5.0, 4.5, 2.9),
            (16, 1, 6.0, 4.5, 2.9),
            (20, 2, -3.0, 34.0, 0.9);
        INSERT INTO detections (id, image_id, object_id) VALUES
            (1, 1, 10), (2, 1, 11), (3, 1, 12), (4, 1, 13), (5, 1, 14), (6, 1, 15),
            (7, 1, 16), (8, 1, 20);
        INSERT INTO anomaly_types (id, name) VALUES (1, 'missing object');
        INSERT INTO abnormal_detections (id, gt_image, inspection_image, summary) VALUES
            (1, 1, 2, 'ticket gate missing');
        INSERT INTO abnormalities (id, pair, type, object, location) VALUES
            (1, 1, 1, 'Ticket Gate', 'concourse');
        """
    )
    conn.commit()
    conn.close()


class FakeRerun:
    def __init__(self):
        self.calls = []

    def highlight(self, **kwargs):
        self.calls.append(kwargs)
        return f"Highlighted: {kwargs}"


def make_client(db_path, rerun, router):
    settings = SimpleNamespace(tool_router_max_rounds=1)
    return InspectionDBClient(db_path, router=router, settings=settings, rerun_visualizer=rerun)


class EmptyRouter:
    """Router that calls nothing and decides no highlights (worst case)."""

    settings = SimpleNamespace(tool_router_max_rounds=1)
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        return None


class SummaryOnlyRouter(EmptyRouter):
    """Router that answers anomaly questions with get_anomaly_summary only."""

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            return [("get_anomaly_summary", {})]
        return []


class LocationsOnlyRouter(EmptyRouter):
    """Router that answers anomaly questions with get_anomaly_locations only."""

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        if not prior_results:
            return [("get_anomaly_locations", {})]
        return []


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        make_db(db_path)

        # (a) Anomaly question answered via get_anomaly_summary only -> top-up adds
        # locations + details; anomaly locations highlighted once, with larger markers.
        rerun = FakeRerun()
        client = make_client(db_path, rerun, SummaryOnlyRouter())
        ctx = await client.lookup("tell me about the anomalies found during this inspection")
        names = [c["name"] for c in client.last_tool_calls]
        assert names == ["get_anomaly_summary", "get_anomaly_locations", "get_anomalies"], names
        assert ctx is not None and "missing object" in ctx, ctx
        assert rerun.calls, "anomaly fallback highlight did not fire"
        coords = rerun.calls[0].get("coordinates")
        assert coords and len(coords) == 1, rerun.calls[0]
        assert abs(coords[0]["x"] - 9.0) < 1e-6 and abs(coords[0]["z"] - 7.0) < 1e-6, coords
        assert coords[0].get("radius") == 0.35, coords
        print("PASS a: summary-only router -> top-up + anomaly highlight (radius 0.35)")

        # (b) "show me the lights" with a silent router -> image tool with limit 5,
        # subset note in context, and the Lights category highlighted.
        rerun.calls.clear()
        client = make_client(db_path, rerun, EmptyRouter())
        ctx = await client.lookup("show me the lights")
        assert ctx is not None, "safety net did not produce context"
        names = [c["name"] for c in client.last_tool_calls]
        assert names == ["get_category_objects_with_images"], names
        args = client.last_tool_calls[0]["args"]
        assert args["category"] == "Lights" and args["limit"] == 5, args
        assert "/inspection/images/" in ctx, ctx
        assert "showing 5 of 7" in ctx, ctx
        assert "representative subset" in ctx, ctx
        assert rerun.calls and rerun.calls[0].get("categories") == ["Lights"], rerun.calls
        print("PASS b: 'show me the lights' -> 5-image subset + Lights highlight")

        # (c) "how many lights were detected" -> object list, not detections.
        rerun.calls.clear()
        client = make_client(db_path, rerun, EmptyRouter())
        ctx = await client.lookup("how many lights were detected?")
        names = [c["name"] for c in client.last_tool_calls]
        assert names == ["get_objects_by_category"], names
        assert "Object 10" in ctx and "Object 16" in ctx, ctx
        assert rerun.calls and rerun.calls[0].get("categories") == ["Lights"], rerun.calls
        print("PASS c: 'how many lights' -> get_objects_by_category + highlight")

        # (d) Multi-category show query -> both categories fetched and highlighted.
        rerun.calls.clear()
        client = make_client(db_path, rerun, EmptyRouter())
        ctx = await client.lookup("show me all lights and advertisement boards")
        names = [c["name"] for c in client.last_tool_calls]
        assert names == ["get_category_objects_with_images"] * 2, names
        cats = rerun.calls[0].get("categories") if rerun.calls else None
        assert set(cats or []) == {"Lights", "Advertisement Board"}, cats
        print("PASS d: multi-category show query -> both categories highlighted")

        # (f) Anomaly question with a silent router -> anomaly safety net plus the
        # anomaly top-up (locations + details), and the anomaly locations highlighted
        # exactly once despite several anomaly tools running.
        rerun.calls.clear()
        client = make_client(db_path, rerun, EmptyRouter())
        ctx = await client.lookup("tell me about the anomalies found during this inspection")
        names = [c["name"] for c in client.last_tool_calls]
        assert names == [
            "get_anomaly_summary",
            "get_anomalies",
            "get_anomaly_locations",
        ], names
        assert "missing object" in ctx, ctx
        assert rerun.calls and rerun.calls[0].get("coordinates"), rerun.calls
        coords = rerun.calls[0]["coordinates"]
        assert len(coords) == 1, f"anomaly coordinates duplicated: {len(coords)}"
        assert coords[0].get("radius") == 0.35, rerun.calls[0]
        print("PASS f: silent-router anomaly question -> full anomaly pair + highlight")

        # (g) Router calls only get_anomaly_locations -> top-up adds the anomaly
        # details, so the answerer always has the full picture.
        rerun.calls.clear()
        client = make_client(db_path, rerun, LocationsOnlyRouter())
        ctx = await client.lookup("where are the anomalies located?")
        names = [c["name"] for c in client.last_tool_calls]
        assert names == ["get_anomaly_locations", "get_anomalies"], names
        assert "missing object" in ctx and "Anomaly 1" in ctx, ctx
        coords = rerun.calls[0]["coordinates"] if rerun.calls else []
        assert len(coords) == 1, f"anomaly coordinates duplicated: {len(coords)}"
        print("PASS g: locations-only router -> top-up adds anomaly details")

        # (e) Off-topic chat untouched.
        rerun.calls.clear()
        client = make_client(db_path, rerun, EmptyRouter())
        ctx = await client.lookup("hello, how are you today?")
        assert ctx is None, ctx
        assert not rerun.calls
        print("PASS e: off-topic query untouched")

        client.close()


asyncio.run(main())
print("ALL SMOKE TESTS PASSED")
