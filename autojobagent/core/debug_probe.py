from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEBUG_MODE_LOG_PATH = Path(
    "/Users/xingyuchen/Documents/Cursor Projects/Job Autopilot_Auto Application Agent/.cursor/debug.log"
)


def append_debug_log(
    *,
    location: str,
    message: str,
    data: dict[str, Any],
    run_id: str,
    hypothesis_id: str,
) -> None:
    payload = {
        "id": f"log_{int(time.time() * 1000)}_{hypothesis_id}",
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "data": data,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
    }
    try:
        DEBUG_MODE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_MODE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
