"""Fixed in-container containment probes. No repository supplied command is accepted."""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"infinite_loop", "success"}:
        return 2

    ready = Path("/work/ready")
    while not ready.exists():
        time.sleep(0.02)

    payload = json.loads(Path("/work/input.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("nonce"), str):
        return 2

    if sys.argv[1] == "infinite_loop":
        while True:
            time.sleep(0.1)

    output = Path("/work/export")
    output.mkdir(mode=0o700)
    staged = output / "probe.json.tmp"
    staged.write_text(
        json.dumps({"kind": "success", "nonce": payload["nonce"]}), encoding="utf-8"
    )
    staged.replace(output / "probe.json")
    while not Path("/work/ack").exists():
        time.sleep(0.02)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
