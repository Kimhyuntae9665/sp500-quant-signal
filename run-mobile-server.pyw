"""Console-free entry point for the private Tailscale mobile server."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import sys
import traceback


PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_ROOT = PROJECT_ROOT / ".cache"
TOKEN_PATH = CACHE_ROOT / "mobile-access-token.txt"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

stdout_handle = (CACHE_ROOT / "mobile-server.stdout.log").open(
    "a", encoding="utf-8", buffering=1
)
stderr_handle = (CACHE_ROOT / "mobile-server.stderr.log").open(
    "a", encoding="utf-8", buffering=1
)
sys.stdout = stdout_handle
sys.stderr = stderr_handle
os.chdir(PROJECT_ROOT)

access_token = TOKEN_PATH.read_text(encoding="ascii").strip() if TOKEN_PATH.exists() else ""
if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", access_token):
    raise RuntimeError(f"Invalid or missing mobile token: {TOKEN_PATH}")
os.environ["QUANT_ACCESS_TOKEN"] = access_token

print(f"[{datetime.now().astimezone().isoformat()}] private mobile task started")

exit_code = 1
try:
    from server import main

    exit_code = main(sys.argv[1:])
except BaseException:
    traceback.print_exc(file=sys.stderr)
finally:
    stdout_handle.flush()
    stderr_handle.flush()

raise SystemExit(exit_code)
