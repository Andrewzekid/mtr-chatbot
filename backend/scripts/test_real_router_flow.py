"""Full lookup flow with the real ToolRouter + FakeRerun to verify fallback logic."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

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

    ctx = await client.lookup("show me anomaly 7")
    print("Context:\n", ctx[:500], "...\n")
    print("Highlight calls:")
    for c in rerun.calls:
        print(" ", c)
    assert rerun.calls
    args = rerun.calls[-1]
    assert args.get("coordinates"), f"expected anomaly coordinates, got {args}"
    label = args["coordinates"][0].get("label", "")
    assert label.startswith("Anomaly 7:"), label
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
