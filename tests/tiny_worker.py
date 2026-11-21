"""Stand-in worker for supervisor tests: `ok` beats every 0.2 s until asked to stop, `hang` beats once."""

import json
import os
import sys
import time
from pathlib import Path

state = Path(os.environ["LIVE_CLIPPER_HOME"]) / "state"
mode = sys.argv[1]
while True:
    (state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    if mode == "hang":
        time.sleep(3600)
    if (state / "worker.stop").exists():
        sys.exit(0)
    time.sleep(0.2)
