"""Prepare the fixed historical case inside a bounded preparation container."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


MODULE_DIR = Path("/opt/upgrade_chamber")
if not (MODULE_DIR / "baseline.py").is_file():
    MODULE_DIR = Path(__file__).resolve().parents[1] / "src" / "upgrade_chamber"
sys.path.insert(0, str(MODULE_DIR))

from baseline import PreparationError, build_input_tar, prepare_fixed_bundles  # noqa: E402


MAX_PREPARATION_EXPORT_BYTES = 20 * 1024 * 1024


def _wait_for(path: Path) -> None:
    while not path.exists():
        time.sleep(0.05)


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare fixed offline wheel bundles")
    parser.add_argument("--handshake", action="store_true", help="Wait for runner ready/ack markers")
    parser.add_argument("--output", type=Path, default=Path("/work/prepared"))
    args = parser.parse_args(argv)
    work = Path("/work") if args.handshake else args.output.parent
    export = work / "export"
    if args.handshake:
        _wait_for(work / "ready")
    export.mkdir(parents=True, exist_ok=True)
    status = "failed"
    error = None
    summary = None
    try:
        summary = prepare_fixed_bundles(args.output)
        staged = {}
        for phase in ("baseline", "candidate"):
            phase_dir = args.output / phase
            staged[f"{phase}.tar"] = build_input_tar(phase_dir)
            staged[f"{phase}-metadata.json"] = (phase_dir / "metadata.json").read_bytes()
        if sum(map(len, staged.values())) > MAX_PREPARATION_EXPORT_BYTES:
            raise PreparationError("Prepared export exceeds byte limit")
        for name, data in staged.items():
            _write_atomic(export / name, data)
        status = "prepared"
    except (OSError, ValueError, PreparationError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    marker = {"schema_version": 1, "status": status, "error": error,
              "summary": summary if status == "prepared" else None}
    _write_atomic(export / "preparation.json", (json.dumps(marker, sort_keys=True) + "\n").encode())
    if args.handshake:
        _wait_for(work / "ack")
    else:
        print(json.dumps(marker, sort_keys=True))
    return 0 if status == "prepared" else 1


if __name__ == "__main__":
    raise SystemExit(main())
