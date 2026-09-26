"""Run the fixed containment and upgrade sequence (timeout, memory, recovery,
preparation, baseline, candidate) and retain its bounded evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from upgrade_chamber.runner import (  # noqa: E402
    ATTEMPT_ARTIFACTS,
    PREPARATION_ARTIFACTS,
    DockerRunner,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_summary(output: Path, summary: dict) -> None:
    temporary = output / "summary.json.tmp"
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output / "summary.json")


def _save_result(output: Path, name: str, result: object, artifact_names: tuple[str, ...]) -> dict:
    directory = output / name
    directory.mkdir()
    artifacts = getattr(result, "artifacts", {})
    if set(artifacts) - set(artifact_names):
        raise ValueError("Runner returned an unexpected artifact name")
    record = {field.name: getattr(result, field.name) for field in fields(result) if field.name != "artifacts"}
    record["artifacts"] = {}
    for artifact_name in artifact_names:
        if artifact_name not in artifacts:
            continue
        content = artifacts[artifact_name]
        (directory / artifact_name).write_bytes(content)
        record["artifacts"][artifact_name] = {
            "path": f"{name}/{artifact_name}",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    for stream in ("stdout", "stderr"):
        content = record.pop(stream).encode("utf-8")
        filename = f"{stream}.txt"
        (directory / filename).write_bytes(content)
        record[stream] = {
            "path": f"{name}/{filename}",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return record


def run_experiment(image: str, output: Path, *, runner: DockerRunner | None = None) -> int:
    """Create a new evidence directory and run each fixed stage once."""
    output.mkdir(parents=True, exist_ok=False)
    summary = {
        "schema_version": 1,
        "image_identity": image,
        "started_utc": _utc_now(),
        "completed_utc": None,
        "status": "running",
        "orphan_cleanup": None,
        "steps": {},
        "error": None,
    }
    _save_summary(output, summary)
    current_step = "runner_initialization"

    def stop(reason: str) -> int:
        summary["status"] = "stopped"
        summary["error"] = reason
        summary["completed_utc"] = _utc_now()
        _save_summary(output, summary)
        return 1

    try:
        active_runner = runner if runner is not None else DockerRunner(image)
        current_step = "orphan_cleanup"
        summary["orphan_cleanup"] = active_runner.remove_expired_containers()
        _save_summary(output, summary)

        current_step = "timeout_probe"
        timeout = active_runner.run_probe("infinite_loop", timeout_seconds=5.0)
        summary["steps"][current_step] = _save_result(output, current_step, timeout, ())
        _save_summary(output, summary)
        if (timeout.status != "timed_out" or timeout.error is not None
                or not timeout.removal_observed or not timeout.container_id):
            return stop("Timeout probe did not prove expiry and observed container removal")

        current_step = "memory_probe"
        memory = active_runner.run_probe("memory", timeout_seconds=30.0)
        summary["steps"][current_step] = _save_result(output, current_step, memory, ())
        _save_summary(output, summary)
        if (memory.status != "oom_killed" or memory.error is not None
                or not memory.removal_observed or not memory.container_id):
            return stop("Memory probe did not demonstrate the cgroup OOM bound and observed removal")

        current_step = "success_probe"
        success = active_runner.run_probe("success", timeout_seconds=5.0)
        summary["steps"][current_step] = _save_result(output, current_step, success, ())
        _save_summary(output, summary)
        if success.status != "completed" or not success.removal_observed or not success.container_id:
            return stop("Success probe did not prove recovery and observed container removal")

        current_step = "preparation"
        preparation = active_runner.run_preparation(timeout_seconds=300.0)
        summary["steps"][current_step] = _save_result(
            output, current_step, preparation, ("preparation.json", *PREPARATION_ARTIFACTS)
        )
        _save_summary(output, summary)
        if preparation.status != "prepared" or not preparation.removal_observed:
            return stop("Preparation did not complete with observed container removal")

        for phase in ("baseline", "candidate"):
            current_step = phase
            attempt = active_runner.run_profile_attempt(
                preparation.artifacts[f"{phase}.tar"], phase=phase, timeout_seconds=300.0
            )
            summary["steps"][phase] = _save_result(
                output, phase, attempt, ("attempt.json", *ATTEMPT_ARTIFACTS)
            )
            _save_summary(output, summary)
            if not attempt.removal_observed:
                return stop(f"{phase.capitalize()} container removal was not observed")
            if phase == "baseline" and attempt.status != "passed":
                return stop("Baseline attempt did not pass")

        summary["status"] = "recorded"
        summary["completed_utc"] = _utc_now()
        _save_summary(output, summary)
        return 0
    except Exception as exc:
        return stop(f"{current_step}: {type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fixed G1 probes and historical profile attempts")
    parser.add_argument("--image", required=True, help="Full immutable Docker image ID or digest reference")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory")
    args = parser.parse_args(argv)
    try:
        result = run_experiment(args.image, args.output)
    except OSError as exc:
        parser.error(str(exc))
    print(args.output / "summary.json")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
