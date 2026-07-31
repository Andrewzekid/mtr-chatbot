"""Check the final-pass decider for anomaly and proximity queries."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import Settings
from app.services.db_service import InspectionDBClient
from app.services.tool_router import ToolRouter


def main():
    settings = Settings(tool_router_model="gemma4:e2b", tool_router_max_rounds=1)
    router = ToolRouter(settings)
    db = InspectionDBClient(settings.inspection_db_path, router=router, settings=settings)

    # Anomaly 7 tool result
    anomaly_text = db._format_anomalies(anomaly_id=7)
    print("Anomaly 7 tool result:\n", anomaly_text)
    decision = router.decide_highlights("show me anomaly 7", anomaly_text)
    print("\nDecide highlights:", decision)

    # Proximity tool result
    prox = db.get_category_proximity_with_images(
        "Lights", ["Advertisement Board"], radius_m=2.0, limit=5, nearby_limit=5
    )
    prox_text = "\n".join(
        f"Object {r['object_id']} at ({r['centroid_x']}, {r['centroid_y']}, {r['centroid_z']}): nearby "
        + ", ".join(
            f"{n['category']} {n['object_id']} ({n['distance_m']}m)" for n in r.get("nearby", [])
        )
        for r in prox
    )
    print("\nProximity tool result:\n", prox_text[:1000])
    decision2 = router.decide_highlights(
        "what advertisement boards were within 2m of lights?", prox_text
    )
    print("\nProximity decide highlights:", decision2)

    db.close()


if __name__ == "__main__":
    main()
