"""Sequential anomaly queries with real ToolRouter to verify no stale scope."""
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import Settings
from app.services.db_service import InspectionDBClient
from app.services.tool_router import ToolRouter


def make_settings():
    return Settings(tool_router_model="gemma4:e2b", tool_router_max_rounds=1)


class FakeRerun:
    def __init__(self):
        self.calls = []

    def highlight(self, **kwargs):
        self.calls.append(kwargs)
        return f"Highlighted: {kwargs}"


async def main():
    settings = make_settings()
    router = ToolRouter(settings)
    rerun = FakeRerun()
    client = InspectionDBClient(
        settings.inspection_db_path,
        router=router,
        settings=settings,
        rerun_visualizer=rerun,
    )

    for aid in (7, 8):
        rerun.calls.clear()
        ctx = await client.lookup(f"show me anomaly {aid}")
        print(f"\nAnomaly {aid} context:\n", ctx[:300], "...")
        print("Highlight args:", rerun.calls[-1] if rerun.calls else None)
        assert rerun.calls
        coords = rerun.calls[-1].get("coordinates") or []
        assert any(f"Anomaly {aid}:" in (c.get("label") or "") for c in coords), coords

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
