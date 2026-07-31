"""Quick sanity check of the real ToolRouter for anomaly/proximity queries."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import Settings
from app.services.tool_router import ToolRouter


def main():
    settings = Settings(
        tool_router_model="gemma4:e2b",
        tool_router_max_rounds=1,
    )
    router = ToolRouter(settings)

    queries = [
        "show me anomaly 7",
        "show me all anomaly locations",
        "what advertisement boards were within 2m of lights?",
    ]
    for q in queries:
        print(f"\nQuery: {q!r}")
        calls = router.select_tool(q, None, None, None, [])
        print("Selected tools:")
        for name, args in calls:
            print(f"  {name}({args})")


if __name__ == "__main__":
    main()
