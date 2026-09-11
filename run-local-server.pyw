"""Console-free entry point for the always-on localhost scheduled task."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys
import traceback


PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_ROOT = PROJECT_ROOT / ".cache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

# pythonw has no console streams. Persist them so request logs and fatal errors
# remain inspectable without allowing console Ctrl+C events to stop the task.
stdout_handle = (CACHE_ROOT / "local-server.stdout.log").open(
    "a", encoding="utf-8", buffering=1
)
stderr_handle = (CACHE_ROOT / "local-server.stderr.log").open(
    "a", encoding="utf-8", buffering=1
)
sys.stdout = stdout_handle
sys.stderr = stderr_handle
os.chdir(PROJECT_ROOT)
os.environ["QUANT_ACCESS_TOKEN"] = ""

print(f"[{datetime.now().astimezone().isoformat()}] pythonw localhost task started")

exit_code = 1
try:
    from server import main

    exit_code = main(sys.argv[1:])
except BaseException:  # Task Scheduler must observe a non-zero exit and restart.
    traceback.print_exc(file=sys.stderr)
finally:
    stdout_handle.flush()
    stderr_handle.flush()

raise SystemExit(exit_code)
