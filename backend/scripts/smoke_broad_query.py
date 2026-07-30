"""Smoke test: no-tool safety net + answering-prompt category injection."""
import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        INSERT INTO categories (id, name) VALUES (1, 'Lights'), (2, 'Advertisement Board'), (3, 'Ticket Gate');
        INSERT INTO inspections (id, started_at, is_gt) VALUES (1, '2026-01-01 16:00:00', 0);
        INSERT INTO images (id, inspection_id, timestamp_ns, filename) VALUES (1, 1, 1700000000000000000, '1.jpg');
        INSERT INTO objects (id, category_id, centroid_x, centroid_y, centroid_z) VALUES
            (10, 1, 0.0, 4.5, 2.9), (11, 1, 1.0, 4.5, 2.9), (20, 2, -3.0, 34.0, 0.9), (30, 3, 5.0, 10.0, 1.0);
        INSERT INTO detections (id, image_id, object_id) VALUES (1, 1, 10), (2, 1, 10), (3, 1, 11), (4, 1, 20);
        """
    )
    conn.commit()
    conn.close()


class DummyRouter:
    """Router that calls nothing and decides no highlights (worst case)."""

    settings = SimpleNamespace(tool_router_max_rounds=1)
    last_raw_response = None

    def select_tool(self, query, chat_history, tool_history, prior_results, current_turn_calls=None):
        return []

    def decide_highlights(self, query, tool_results_text, chat_history=None):
        return None


class FakeRerun:
    def __init__(self):
        self.calls = []

    def highlight(self, **kwargs):
        self.calls.append(kwargs)
        return f"Highlighted: {kwargs}"


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        make_db(db_path)

        rerun = FakeRerun()
        settings = SimpleNamespace(tool_router_max_rounds=1)
        client = InspectionDBClient(
            db_path, router=DummyRouter(), settings=settings, rerun_visualizer=rerun
        )

        # 1. Broad object question, router calls nothing -> safety net must fire.
        ctx = await client.lookup("tell me about all the objects")
        assert ctx is not None, "safety net did not produce context"
        assert "Lights" in ctx and "Advertisement Board" in ctx and "Ticket Gate" in ctx, ctx
        names = [c["name"] for c in client.last_tool_calls]
        assert names == ["get_summary", "get_categories"], names
        assert rerun.calls, "fallback highlight did not fire"
        cats = rerun.calls[0].get("categories")
        assert cats == ["Advertisement Board", "Lights", "Ticket Gate"] or set(cats) == {
            "Advertisement Board",
            "Lights",
            "Ticket Gate",
        }, cats
        print("PASS 1: safety net fired, real categories in context, all categories highlighted")

        # 2. Off-topic chat -> no safety net.
        rerun.calls.clear()
        ctx2 = await client.lookup("hello, how are you today?")
        assert ctx2 is None, ctx2
        assert not rerun.calls
        print("PASS 2: off-topic query untouched")

        # 3. Answering LLM system prompt contains the real categories + schema facts.
        from app.config import Settings
        from app.services.llm_service import LocalLLM

        real_settings = Settings()
        llm = LocalLLM(real_settings, db_client=client)
        facts = llm._database_facts()
        assert "Lights" in facts and "Advertisement Board" in facts, facts
        assert "unique physical items" in facts
        msgs = llm._build_messages("tell me about all the objects", None)
        assert "ONLY contains these object categories" in msgs[0]["content"]
        print("PASS 3: answering system prompt carries real categories + schema facts")

        client.close()


asyncio.run(main())
print("ALL SMOKE TESTS PASSED")
