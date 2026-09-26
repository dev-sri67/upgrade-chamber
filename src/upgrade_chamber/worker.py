"""Serial upgrade-chamber worker that executes leased runs phase by phase.

The worker owns no inference path. Each pass it requeues stale leases, leases
one queued run, and drives it through the fixed phase machine while persisting
every state change before the event that describes it. Evidence artifacts are
written before the terminal update so the manifest can hash the complete
bundle except for itself, which is accepted as unhashable.
"""

from __future__ import annotations

import difflib
import io
import json
import signal
import tarfile
import time
from datetime import datetime, timezone
from typing import Any, Callable

from upgrade_chamber.config import Settings
from upgrade_chamber.osv import query_osv
from upgrade_chamber.runner import DockerRunner
from upgrade_chamber.storage import Store


MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
PYTHON_VERSION_LABEL = "3.11"
ATTEMPT_EXPORT_NAMES = (
    "attempt.json",
    "collect.txt",
    "junit.xml",
    "pip-report.json",
    "pip-check.txt",
    "installed.json",
    "install.log",
    "test.log",
)
PREPARATION_REPORT_NAMES = (
    "preparation.json",
    "baseline-metadata.json",
    "candidate-metadata.json",
)
VERIFIER_REQUIRED_ARTIFACTS = (
    "collect.txt",
    "junit.xml",
    "pip-report.json",
    "pip-check.txt",
    "installed.json",
)
FAILED_OUTCOME_STATUSES = frozenset({"install_failed", "collection_failed", "test_failed"})
SELECTION_RATIONALE = (
    "Fixed target version from the validated execution profile; model selection "
    "is not part of this phase"
)
MANIFEST_LIMITATIONS = (
    "Results prove compatibility with the executed suite under the recorded pinned "
    "environment only; they do not establish complete application correctness or "
    "absence of vulnerabilities."
)
def _utc_now() -> str:
    """Current wall-clock time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def _result_error(result: Any) -> str:
    """Prefer the in-container marker error, then the runner transport error."""
    marker_error = result.marker.get("error") if isinstance(result.marker, dict) else None
    error = marker_error or result.error
    return error if error else "no error reported"


def _marker_summary(marker: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract the comparable attempt summary recorded in comparison.json."""
    if not isinstance(marker, dict):
        return None
    return {
        "status": marker["status"],
        "counts": marker["counts"],
        "collected_test_ids": marker["collected_test_ids"],
        "installed_requests_version": marker["installed_requests_version"],
    }


def _advisory_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce one advisory snapshot to its status and vulnerability IDs."""
    if not isinstance(snapshot, dict):
        return {"status": "unavailable", "vulnerability_ids": []}
    vulnerabilities = snapshot.get("vulnerabilities")
    ids = []
    if isinstance(vulnerabilities, list):
        ids = [
            item["id"] for item in vulnerabilities
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    return {"status": snapshot.get("status", "unavailable"), "vulnerability_ids": ids}


def _requirements_text(bundle: bytes | None) -> str | None:
    """Read the requirements.txt member of an attempt bundle, or None."""
    if bundle is None:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:") as archive:
            member = archive.extractfile("requirements.txt")
            if member is None:
                return None
            return member.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, KeyError, EOFError, OSError):
        return None


def _verifier_rejection(
    result: Any, baseline_summary: dict[str, Any] | None, target_version: str
) -> str | None:
    """Return the first failed verifier check, or None when every check holds."""
    if result.status != "passed":
        return f"verifier status was {result.status}"
    marker = result.marker if isinstance(result.marker, dict) else {}
    baseline = baseline_summary if isinstance(baseline_summary, dict) else {}
    baseline_ids = baseline.get("collected_test_ids", [])
    if marker.get("collected_test_ids") != baseline_ids:
        return "verifier collected IDs differ from baseline"
    baseline_counts = baseline.get("counts", {})
    counts = marker.get("counts", {})
    tolerated = sum(baseline_counts.get(name, 0) for name in ("skipped", "xfailed", "xpassed"))
    observed = sum(counts.get(name, 0) for name in ("skipped", "xfailed", "xpassed"))
    if observed > tolerated:
        return "verifier reported new skips or xfails"
    if marker.get("installed_requests_version") != target_version:
        return "verifier installed requests version differs"
    pip_check = (marker.get("steps") or {}).get("pip_check") or {}
    if pip_check.get("exit_code") != 0:
        return "verifier pip check failed"
    if any(name not in result.artifacts for name in VERIFIER_REQUIRED_ARTIFACTS):
        return "verifier evidence incomplete"
    return None


class Worker:
    """Execute leased runs through the fixed phase machine with honest evidence."""

    def __init__(
        self, store: Store, runner: Any, *, worker_id: str = "upgrade-chamber-worker-1",
        lease_seconds: float = 1000.0, poll_seconds: float = 0.5,
        job_deadline_seconds: float = 900.0, preparation_timeout: float = 300.0,
        attempt_timeout: float = 300.0, osv_query: Callable[[str, str], dict] = query_osv,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._runner = runner
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._job_deadline_seconds = job_deadline_seconds
        self._preparation_timeout = preparation_timeout
        self._attempt_timeout = attempt_timeout
        self._osv_query = osv_query
        self._sleep = sleep
        self._monotonic = monotonic

    def run_forever(self, should_stop: Callable[[], bool]) -> None:
        """Lease and execute runs until should_stop() turns true."""
        while not should_stop():
            self._store.requeue_stale_leases()
            run = self._store.lease_next_run(self._worker_id, self._lease_seconds)
            if run is not None:
                self.execute_run(run)
            else:
                self._sleep(self._poll_seconds)

    def execute_run(self, run: dict) -> None:
        """Execute one leased run through every phase until a terminal state."""
        run_id = run["id"]
        job_deadline = self._monotonic() + self._job_deadline_seconds

        def remaining() -> float:
            """Bounded positive time left for one runner call."""
            return max(0.1, job_deadline - self._monotonic())

        def should_cancel() -> bool:
            return self._store.cancel_requested(run_id)

        def pre_phase_stop() -> tuple[str, str] | None:
            """Cancellation and job-deadline checks that run before every phase."""
            if should_cancel():
                return ("cancelled", "cancelled by request")
            if job_deadline - self._monotonic() <= 0:
                return ("timed_out", "job deadline exceeded")
            return None

        cleanup_observed = True
        reached_upgrade = False
        baseline_tar: bytes | None = None
        candidate_tar: bytes | None = None
        baseline_summary: dict[str, Any] | None = None
        candidate_summary: dict[str, Any] | None = None
        verifier_summary: dict[str, Any] | None = None
        advisory_snapshots: dict[str, dict[str, Any]] = {}

        def invoke(call: Callable[[], Any]) -> tuple[Any, str, str]:
            """Run one runner call, bracket it with timestamps, and track cleanup."""
            nonlocal cleanup_observed
            started_utc = _utc_now()
            result = call()
            finished_utc = _utc_now()
            if not result.removal_observed:
                cleanup_observed = False
            return result, started_utc, finished_utc

        def finalize(terminal_state: str, detail: str | None) -> None:
            """Persist evidence, the run record, the state, and the terminal event."""
            cleanup_state = "observed" if cleanup_observed else "unobserved"
            if reached_upgrade:
                self._save_patch(run, baseline_tar, candidate_tar)
                self._save_comparison(
                    run, terminal_state, detail, baseline_summary,
                    candidate_summary, verifier_summary,
                )
                self._save_manifest(
                    run, terminal_state, detail, cleanup_state,
                    advisory_snapshots, baseline_summary,
                )
            result_payload = {
                "state": terminal_state,
                "detail": detail,
                "baseline": baseline_summary,
                "candidate": candidate_summary,
            }
            self._store.update_run(
                run_id,
                terminal_utc=_utc_now(),
                cleanup_state=cleanup_state,
                result=json.dumps(result_payload, separators=(",", ":")),
            )
            self._store.set_run_state(run_id, terminal_state, detail=detail)
            self._store.append_event(run_id, "terminal", {
                "state": terminal_state,
                "detail": detail,
                "cleanup_state": cleanup_state,
            })

        stop = pre_phase_stop()
        if stop is not None:
            finalize(*stop)
            return

        # Phase 1: prepare the two pinned offline bundles in a networked container.
        self._transition(run_id, "preparing")
        preparation, started_utc, finished_utc = invoke(
            lambda: self._runner.run_preparation(
                timeout_seconds=min(self._preparation_timeout, remaining()),
                should_cancel=should_cancel,
            )
        )
        self._record_attempt(run_id, "preparation", preparation, started_utc, finished_utc)
        for name in PREPARATION_REPORT_NAMES:
            if name in preparation.artifacts:
                self._save_artifact(run_id, name, preparation.artifacts[name], kind="report")
        if preparation.status != "prepared":
            if preparation.status == "timed_out":
                finalize("timed_out", "preparation timed out")
            elif preparation.status == "cancelled":
                finalize("cancelled", "cancelled by request")
            else:
                finalize(
                    "infrastructure_failed",
                    f"preparation {preparation.status}: {_result_error(preparation)}",
                )
            return
        baseline_tar = preparation.artifacts["baseline.tar"]
        candidate_tar = preparation.artifacts["candidate.tar"]

        stop = pre_phase_stop()
        if stop is not None:
            finalize(*stop)
            return

        # Phase 2: the baseline must pass before any mutation is considered.
        self._transition(run_id, "baseline")
        baseline, started_utc, finished_utc = invoke(
            lambda: self._runner.run_profile_attempt(
                baseline_tar, phase="baseline",
                timeout_seconds=min(self._attempt_timeout, remaining()),
                should_cancel=should_cancel,
            )
        )
        self._record_attempt(run_id, "baseline", baseline, started_utc, finished_utc)
        self._save_prefixed_artifacts(run_id, "baseline", baseline.artifacts)
        baseline_summary = _marker_summary(baseline.marker)
        if baseline.status != "passed":
            if baseline.status == "timed_out":
                finalize("timed_out", "baseline attempt timed out")
            elif baseline.status == "cancelled":
                finalize("cancelled", "cancelled by request")
            elif baseline.status in FAILED_OUTCOME_STATUSES:
                finalize("baseline_failed", f"{baseline.status}: {_result_error(baseline)}")
            else:
                finalize(
                    "infrastructure_failed",
                    f"baseline attempt {baseline.status}: {_result_error(baseline)}",
                )
            return

        stop = pre_phase_stop()
        if stop is not None:
            finalize(*stop)
            return

        # Phase 3: record the fixed selection and cache advisory snapshots.
        self._transition(run_id, "selecting")
        self._store.append_event(run_id, "selection", {
            "package": run["dependency"],
            "target_version": run["target_version"],
            "source": "profile",
            "rationale": SELECTION_RATIONALE,
        })
        for position, version in (
            ("baseline", run["baseline_version"]),
            ("target", run["target_version"]),
        ):
            cached = self._store.get_advisory(run["dependency"], version)
            if cached is not None:
                snapshot = cached["response"]
            else:
                snapshot = self._osv_query(run["dependency"], version)
                self._store.put_advisory(run["dependency"], version, snapshot)
            self._save_artifact(
                run_id, f"advisory-{position}.json",
                json.dumps(snapshot).encode("utf-8"), kind="advisory",
            )
            self._store.append_event(run_id, "advisory", {
                "position": position,
                "status": snapshot.get("status", "unavailable"),
            })
            advisory_snapshots[position] = snapshot

        stop = pre_phase_stop()
        if stop is not None:
            finalize(*stop)
            return

        # Phase 4: install the candidate and run the suite under the same bounds.
        self._transition(run_id, "upgrading")
        reached_upgrade = True
        candidate, started_utc, finished_utc = invoke(
            lambda: self._runner.run_profile_attempt(
                candidate_tar, phase="candidate",
                timeout_seconds=min(self._attempt_timeout, remaining()),
                should_cancel=should_cancel,
            )
        )
        self._record_attempt(run_id, "candidate", candidate, started_utc, finished_utc)
        self._save_prefixed_artifacts(run_id, "candidate", candidate.artifacts)
        candidate_summary = _marker_summary(candidate.marker)
        protected = candidate.status == "passed" or candidate.status in FAILED_OUTCOME_STATUSES
        baseline_ids = baseline_summary["collected_test_ids"] if baseline_summary else []
        if (
            protected and candidate.marker is not None
            and candidate.marker["collected_test_ids"] != baseline_ids
        ):
            finalize("upgrade_failed", "collected test IDs changed from baseline")
            return
        if candidate.status == "passed":
            pass
        elif candidate.status in FAILED_OUTCOME_STATUSES:
            finalize("upgrade_failed", f"{candidate.status}: {_result_error(candidate)}")
            return
        elif candidate.status == "timed_out":
            finalize("timed_out", "candidate attempt timed out")
            return
        elif candidate.status == "cancelled":
            finalize("cancelled", "cancelled by request")
            return
        else:
            finalize(
                "infrastructure_failed",
                f"candidate attempt {candidate.status}: {_result_error(candidate)}",
            )
            return

        stop = pre_phase_stop()
        if stop is not None:
            finalize(*stop)
            return

        # Phase 5: a fresh verifier rerun must confirm every protected property.
        self._transition(run_id, "verifying")
        verifier, started_utc, finished_utc = invoke(
            lambda: self._runner.run_profile_attempt(
                candidate_tar, phase="candidate",
                timeout_seconds=min(self._attempt_timeout, remaining()),
                should_cancel=should_cancel,
            )
        )
        self._record_attempt(run_id, "verifier", verifier, started_utc, finished_utc)
        self._save_prefixed_artifacts(run_id, "verifier", verifier.artifacts)
        verifier_summary = _marker_summary(verifier.marker)
        rejection = _verifier_rejection(verifier, baseline_summary, run["target_version"])
        if rejection is not None:
            finalize("upgrade_failed", rejection)
            return
        finalize("completed", None)

    def _transition(self, run_id: int, state: str) -> None:
        """Persist one non-terminal state change before its state event."""
        self._store.set_run_state(run_id, state)
        self._store.append_event(run_id, "state", {"state": state})

    def _record_attempt(
        self, run_id: int, phase: str, result: Any, started_utc: str, finished_utc: str
    ) -> None:
        """Persist one attempt row and its attempt event in insertion order."""
        self._store.add_attempt(
            run_id,
            phase=phase,
            status=result.status,
            container_id=result.container_id,
            started_utc=started_utc,
            finished_utc=finished_utc,
            elapsed_seconds=result.elapsed_seconds,
            exit_code=result.exit_code,
            marker=result.marker,
            artifact_names=sorted(result.artifacts),
            stdout=result.stdout,
            stderr=result.stderr,
        )
        data: dict[str, Any] = {
            "phase": phase,
            "status": result.status,
            "container_id": result.container_id,
            "elapsed_seconds": result.elapsed_seconds,
        }
        if isinstance(result.marker, dict) and "counts" in result.marker:
            data["counts"] = result.marker["counts"]
            data["collected_test_count"] = len(result.marker["collected_test_ids"])
        self._store.append_event(run_id, "attempt", data)

    def _save_artifact(self, run_id: int, name: str, content: bytes, *, kind: str) -> None:
        """Store one artifact and emit its artifact event with the byte count."""
        self._store.save_artifact(run_id, name, content, kind=kind)
        self._store.append_event(run_id, "artifact", {"name": name, "bytes": len(content)})

    def _save_prefixed_artifacts(self, run_id: int, prefix: str, artifacts: dict[str, bytes]) -> None:
        """Save every exported attempt artifact under one phase prefix."""
        for name in ATTEMPT_EXPORT_NAMES:
            if name in artifacts:
                self._save_artifact(run_id, f"{prefix}-{name}", artifacts[name], kind="report")

    def _save_patch(self, run: dict, baseline_tar: bytes | None, candidate_tar: bytes | None) -> None:
        """Build the deterministic requirements diff, never fabricating content."""
        baseline_text = _requirements_text(baseline_tar)
        candidate_text = _requirements_text(candidate_tar)
        if baseline_text is None and candidate_text is None:
            reason = "requirements.txt is missing from both bundles"
        elif baseline_text is None:
            reason = "requirements.txt is missing from the baseline bundle"
        elif candidate_text is None:
            reason = "requirements.txt is missing from the candidate bundle"
        else:
            diff = difflib.unified_diff(
                baseline_text.splitlines(keepends=True),
                candidate_text.splitlines(keepends=True),
                fromfile=f"requirements.txt@{run['baseline_version']}",
                tofile=f"requirements.txt@{run['target_version']}",
            )
            content = "".join(diff)
            self._save_artifact(run["id"], "patch.diff", content.encode("utf-8"), kind="patch")
            return
        content = f"# patch input unavailable: {reason}\n"
        self._save_artifact(run["id"], "patch.diff", content.encode("utf-8"), kind="patch")

    def _save_comparison(
        self, run: dict, terminal_state: str, detail: str | None,
        baseline_summary: dict[str, Any] | None, candidate_summary: dict[str, Any] | None,
        verifier_summary: dict[str, Any] | None,
    ) -> None:
        """Persist the machine-readable before/after comparison artifact."""
        baseline_ids = baseline_summary["collected_test_ids"] if baseline_summary else None
        candidate_ids = candidate_summary["collected_test_ids"] if candidate_summary else None
        payload = {
            "baseline": baseline_summary,
            "candidate": candidate_summary,
            "collected_ids_match": bool(
                baseline_ids is not None and candidate_ids is not None
                and baseline_ids == candidate_ids
            ),
            "verifier": verifier_summary,
            "result": terminal_state,
            "detail": detail,
        }
        self._save_artifact(
            run["id"], "comparison.json", json.dumps(payload).encode("utf-8"), kind="report")

    def _save_manifest(
        self, run: dict, terminal_state: str, detail: str | None, cleanup_state: str,
        advisory_snapshots: dict[str, dict[str, Any]], baseline_summary: dict[str, Any] | None,
    ) -> None:
        """Persist the manifest last, hashing every artifact except itself."""
        run_id = run["id"]
        artifacts: dict[str, dict[str, Any]] = {}
        for row in self._store.list_artifacts(run_id):
            if row["name"] == MANIFEST_NAME:
                continue
            artifacts[row["name"]] = {"sha256": row["sha256"], "bytes": row["bytes"]}
        attempts = [
            {
                "phase": row["phase"],
                "status": row["status"],
                "container_id": row["container_id"],
                "elapsed_seconds": row["elapsed_seconds"],
            }
            for row in self._store.attempts(run_id)
        ]
        baseline_ids = baseline_summary["collected_test_ids"] if baseline_summary else []
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": run_id,
            "created_utc": _utc_now(),
            "profile_id": run["profile_id"],
            "repository_url": run["repo_url"],
            "commit_sha": run["commit_sha"],
            "source_sha256": run["source_sha256"],
            "image_identity": self._runner.image,
            "python": PYTHON_VERSION_LABEL,
            "dependency": {
                "name": run["dependency"],
                "baseline_version": run["baseline_version"],
                "target_version": run["target_version"],
            },
            "test_scope": {
                "collected": len(baseline_ids),
                "first": baseline_ids[0] if baseline_ids else None,
                "last": baseline_ids[-1] if baseline_ids else None,
            },
            "attempts": attempts,
            "advisories": {
                "baseline": _advisory_summary(advisory_snapshots.get("baseline")),
                "target": _advisory_summary(advisory_snapshots.get("target")),
            },
            "result": {"state": terminal_state, "detail": detail},
            "cleanup_state": cleanup_state,
            "limitations": MANIFEST_LIMITATIONS,
            "artifacts": artifacts,
        }
        self._save_artifact(
            run_id, MANIFEST_NAME, json.dumps(payload).encode("utf-8"), kind="report")


def _request_stop(_signum: object, _frame: object) -> None:
    """Signal handler that only flips the module stop flag."""
    global STOP_REQUESTED
    STOP_REQUESTED = True


STOP_REQUESTED = False


def main() -> int:
    """Run the serial worker until SIGTERM or SIGINT arrives."""
    global STOP_REQUESTED
    STOP_REQUESTED = False
    settings = Settings()
    settings.require_worker_settings()
    store = Store(settings.database_path, settings.artifact_dir)
    try:
        runner = DockerRunner(settings.worker_image)
        runner.remove_expired_containers()
        worker = Worker(
            store,
            runner,
            worker_id=settings.worker_id,
            job_deadline_seconds=settings.job_deadline_seconds,
            lease_seconds=settings.lease_seconds,
            poll_seconds=settings.poll_seconds,
        )
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        print(f"upgrade-chamber worker {settings.worker_id} started")
        worker.run_forever(lambda: STOP_REQUESTED)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
