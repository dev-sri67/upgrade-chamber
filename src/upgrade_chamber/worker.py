"""Serial upgrade-chamber worker that executes leased runs phase by phase.

Each pass the worker requeues stale leases, leases one queued run, and drives
it through the fixed phase machine while persisting every state change before
the event that describes it. Model involvement is bounded: a single internal
inference endpoint confirms the profile-fixed selection, and at most two
bounded agent repair sessions may follow a failing candidate attempt, each
one a fixed-turn tool loop executed controller-side. Evidence artifacts are
written before the terminal update so the manifest can hash the complete
bundle except for itself, which is accepted as unhashable.
"""

from __future__ import annotations

import difflib
import io
import json
import re
import signal
import tarfile
import time
from datetime import datetime, timezone
from typing import Any, Callable

from upgrade_chamber import edits
from upgrade_chamber.agent import AgentSession
from upgrade_chamber.config import Settings
from upgrade_chamber.edits import (
    EditValidationError,
    changed_file_diffs,
    load_source_zip,
)
from upgrade_chamber.osv import query_osv
from upgrade_chamber.runner import DockerRunner
from upgrade_chamber.storage import Store


SOURCE_DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}")
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
MANIFEST_LIMITATIONS = (
    "Results prove compatibility with the executed suite under the recorded pinned "
    "environment only; they do not establish complete application correctness or "
    "absence of vulnerabilities."
)
MAX_REPAIRS = 2
MAX_INFERENCE_CALLS = 18
INTERNAL_RESPONSE_LIMIT = 1024 * 1024
INFERENCE_ERROR_MESSAGE_LIMIT = 500
INFERENCE_TIMEOUT_SECONDS = 60.0
RECORD_LIMIT = 2000
TURN_DETAIL_LIMIT = 300


class InternalInferenceError(RuntimeError):
    """One bounded failure of an internal inference endpoint call.

    Carries the provider-shaped code and message extracted from the error
    body, never the raw response object.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _error_body(data: Any, status: Any) -> tuple[str, str]:
    """Extract a bounded (code, message) pair from an error body or status."""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, str) and code:
            if not isinstance(message, str):
                message = "inference error"
            return code, message[:INFERENCE_ERROR_MESSAGE_LIMIT]
    label = f"http_{status}" if isinstance(status, int) else "invalid_response"
    return label, "internal inference request failed"


def _utc_now() -> str:
    """Current wall-clock time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def _result_error(result: Any) -> str:
    """Prefer the in-container marker error, then the runner transport error."""
    marker_error = result.marker.get("error") if isinstance(result.marker, dict) else None
    error = marker_error or result.error
    return error if error else "no error reported"


def _metadata_source_digest(raw: bytes | None) -> str | None:
    """Extract a well-formed source digest from baseline-metadata.json, or None."""
    if raw is None:
        return None
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError):
        return None
    digest = metadata.get("source_sha256") if isinstance(metadata, dict) else None
    if isinstance(digest, str) and SOURCE_DIGEST_PATTERN.fullmatch(digest):
        return digest
    return None


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
        inference_base_url: str = "http://127.0.0.1:8000",
        internal_token: str | None = None,
        http_client: Any | None = None,
        max_inference_calls: int = MAX_INFERENCE_CALLS,
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
        self._inference_base_url = inference_base_url
        self._internal_token = internal_token
        self._http_client = http_client
        self._max_inference_calls = max_inference_calls
        self._inference_calls = 0
        self._agent_model_id: str | None = None

    def _internal_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one JSON payload to an internal inference endpoint.

        Uses the injected HTTP client or builds a default httpx client with a
        60 second timeout, no environment proxies, and no redirects. Sends
        X-Internal-Token when configured. Non-2xx responses, oversized
        bodies, and malformed payloads become InternalInferenceError carrying
        the error body's code and message, never a raw response.
        """
        if self._http_client is None:
            import httpx

            self._http_client = httpx.Client(
                timeout=INFERENCE_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False,
            )
        headers = {"Accept": "application/json"}
        if self._internal_token is not None:
            headers["X-Internal-Token"] = self._internal_token
        url = f"{self._inference_base_url}{path}"
        try:
            response = self._http_client.post(url, json=payload, headers=headers)
        except Exception as exc:
            raise InternalInferenceError(
                "inference_unavailable",
                f"transport failed: {type(exc).__name__}: {str(exc)[:INFERENCE_ERROR_MESSAGE_LIMIT]}",
            ) from None
        content = response.content
        if len(content) > INTERNAL_RESPONSE_LIMIT:
            raise InternalInferenceError(
                "response_too_large", "Internal inference response exceeds 1 MiB")
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            code, message = _error_body(data, status)
            raise InternalInferenceError(code, message)
        if not isinstance(data, dict):
            raise InternalInferenceError(
                "invalid_response", "Internal inference response is not a JSON object")
        return data

    def _agent_model_call(self, run_id: int, messages: list[dict[str, str]]) -> dict[str, Any]:
        """POST one agent conversation turn to the internal inference endpoint.

        Sends the full worker-owned conversation with the fixed 16384 token
        cap and returns the trimmed {"message", "usage"} pair the agent
        session consumes. The latest response model_id is recorded on
        ``self._agent_model_id`` so run evidence can name the model.
        """
        response = self._internal_post("/internal/inference/agent-turn", {
            "run_id": run_id,
            "messages": messages,
            "max_tokens": 16384,
        })
        self._agent_model_id = response.get("model_id")
        return {"message": response["message"], "usage": response["usage"]}

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
        source_digest = run["source_sha256"]
        self._inference_calls = 0
        model_state: dict[str, Any] | None = None
        selection_rationale: str | None = None
        manifest_repairs: list[dict[str, Any]] = []
        repair_records: list[dict[str, Any]] = []
        repair_chain: list[tuple[int, bytes, bytes]] = []
        current_tar: bytes | None = None

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
                self._save_patch(run, baseline_tar, candidate_tar, repair_chain)
                self._save_comparison(
                    run, terminal_state, detail, baseline_summary,
                    candidate_summary, verifier_summary, repair_records,
                )
                self._save_manifest(
                    run, terminal_state, detail, cleanup_state,
                    advisory_snapshots, baseline_summary, source_digest,
                    model_state, selection_rationale, manifest_repairs,
                )
            result_payload = {
                "state": terminal_state,
                "detail": detail,
                "baseline": baseline_summary,
                "candidate": candidate_summary,
            }
            if model_state is not None:
                result_payload["model"] = {
                    "model_id": model_state["model_id"],
                    "repair_count": model_state["repair_count"],
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

        # Record the declared source digest before the baseline phase so the
        # manifest carries the same value the run row now holds.
        digest = _metadata_source_digest(
            preparation.artifacts.get("baseline-metadata.json"))
        if digest is not None:
            self._store.update_run(run_id, source_sha256=digest)
            source_digest = digest

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

        # Phase 3: the model confirms the profile-fixed target version through
        # the internal inference endpoint, then advisories are cached.
        self._transition(run_id, "selecting")
        self._inference_calls += 1
        selection_context = (
            f"{run['repo_url']} at commit {run['commit_sha']}, profile {run['profile_id']}, "
            f"upgrading {run['dependency']} {run['baseline_version']} -> "
            f"{run['target_version']}; the baseline suite passes on the baseline version."
        )
        try:
            selection_result = self._internal_post("/internal/inference/select", {
                "run_id": run_id,
                "package": run["dependency"],
                "eligible_versions": [run["target_version"]],
                "context": selection_context,
            })
        except InternalInferenceError as exc:
            finalize("infrastructure_failed", f"model selection failed: {exc.code}: {exc.message}")
            return
        selection = selection_result.get("selection")
        selected_version = (
            selection.get("target_version") if isinstance(selection, dict) else None
        )
        if not isinstance(selected_version, str) or selected_version != run["target_version"]:
            finalize(
                "infrastructure_failed",
                f"model selection failed: returned ineligible target version {selected_version!r}",
            )
            return
        selection_rationale = selection.get("rationale")
        if not isinstance(selection_rationale, str):
            selection_rationale = ""
        model_state = {
            "model_id": selection_result.get("model_id"),
            "repair_count": 0,
        }
        self._store.append_event(run_id, "selection", {
            "package": run["dependency"],
            "target_version": selected_version,
            "source": "model",
            "rationale": selection_rationale,
            "model_id": selection_result.get("model_id"),
            "attempts": selection_result.get("attempts"),
            "usage": selection_result.get("usage"),
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
        # A failing candidate whose collected IDs still match the baseline enters
        # the bounded repairing loop; everything else stays terminal as before.
        self._transition(run_id, "upgrading")
        reached_upgrade = True
        current_tar = candidate_tar
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
        baseline_ids = baseline_summary["collected_test_ids"] if baseline_summary else []
        protected = candidate.status == "passed" or candidate.status in FAILED_OUTCOME_STATUSES
        if (
            protected and candidate.marker is not None
            and candidate.marker["collected_test_ids"] != baseline_ids
        ):
            finalize("upgrade_failed", "collected test IDs changed from baseline")
            return
        repaired = False
        provider_note: tuple[str, str] | None = None
        if candidate.status == "passed":
            pass
        elif candidate.status == "test_failed":

            def budgeted_model_call(messages: list[dict[str, str]]) -> dict[str, Any]:
                """Run one agent model call under the job-wide inference budget."""
                self._inference_calls += 1
                if self._inference_calls > self._max_inference_calls:
                    raise InternalInferenceError(
                        "budget_exhausted", "per-job inference call budget exhausted")
                response = self._agent_model_call(run_id, messages)
                if isinstance(self._agent_model_id, str) and self._agent_model_id:
                    model_state["model_id"] = self._agent_model_id
                return response

            latest_failure: Any = candidate
            for repair_number in range(1, MAX_REPAIRS + 1):
                stop = pre_phase_stop()
                if stop is not None:
                    finalize(*stop)
                    return
                self._transition(run_id, "repairing", attempt=repair_number)
                try:
                    source_zip = load_source_zip(current_tar)
                except EditValidationError as exc:
                    finalize(
                        "upgrade_failed",
                        f"test_failed: {_result_error(latest_failure)};"
                        f" repair context unavailable: {exc}",
                    )
                    return
                log_bytes = latest_failure.artifacts.get("test.log")
                test_log = (
                    log_bytes.decode("utf-8", errors="replace")
                    if log_bytes is not None else None
                )
                session = AgentSession(
                    source_zip=source_zip,
                    test_log=test_log,
                    model_call=budgeted_model_call,
                )
                try:
                    session_result = session.run()
                except InternalInferenceError as exc:
                    provider_note = (exc.code, exc.message)
                    self._store.append_event(run_id, "repair", {
                        "attempt": repair_number,
                        "status": "provider_error",
                        "error_code": exc.code,
                        "message": exc.message,
                    })
                    break
                summary = (session_result.summary or "")[:RECORD_LIMIT]
                self._store.append_event(run_id, "repair", {
                    "attempt": repair_number,
                    "status": session_result.status,
                    "summary": summary,
                    "turns": [
                        {"tool": turn.tool, "ok": turn.ok,
                         "detail": turn.detail[:TURN_DETAIL_LIMIT]}
                        for turn in session_result.turns
                    ],
                    "model_calls": session_result.model_calls,
                    "usage": session_result.usage,
                    "edit_paths": [edit["path"] for edit in session_result.edits],
                })
                if session_result.status == "aborted":
                    finalize("upgrade_failed", f"repair session aborted: {summary}")
                    return
                if session_result.status in ("exhausted", "invalid"):
                    finalize("upgrade_failed", f"repair session {session_result.status}")
                    return
                if session_result.status != "finished" or not session_result.edits:
                    # validate_edits rejects empty edit lists, so a finished
                    # session always carries edits; guard the contract honestly.
                    finalize(
                        "upgrade_failed",
                        f"repair session {session_result.status}:"
                        " no validated edits to apply",
                    )
                    return
                model_state["repair_count"] = repair_number
                record = {
                    "attempt": repair_number,
                    "status": session_result.status,
                    "summary": summary,
                    "edit_paths": [edit["path"] for edit in session_result.edits],
                    "model_calls": session_result.model_calls,
                    "usage": session_result.usage,
                }
                repair_records.append(record)
                manifest_repairs.append({
                    "attempt": repair_number,
                    "status": session_result.status,
                    "summary": summary,
                    "model_calls": session_result.model_calls,
                })
                try:
                    new_zip, new_sha = edits.apply_edits(source_zip, session_result.edits)
                    new_tar = edits.rebuild_candidate_tar(current_tar, new_zip, new_sha)
                except (EditValidationError, RuntimeError, tarfile.TarError) as exc:
                    finalize(
                        "upgrade_failed",
                        f"test_failed: {_result_error(latest_failure)};"
                        f" repair application failed: {type(exc).__name__}: {exc}",
                    )
                    return
                repair_chain.append((repair_number, source_zip, new_zip))
                current_tar = new_tar
                self._transition(run_id, "upgrading")
                attempt_result, attempt_started, attempt_finished = invoke(
                    lambda: self._runner.run_profile_attempt(
                        current_tar, phase="candidate",
                        timeout_seconds=min(self._attempt_timeout, remaining()),
                        should_cancel=should_cancel,
                    )
                )
                self._record_attempt(
                    run_id, f"repair-{repair_number}", attempt_result,
                    attempt_started, attempt_finished,
                )
                self._save_prefixed_artifacts(
                    run_id, f"repair{repair_number}", attempt_result.artifacts)
                candidate_summary = _marker_summary(attempt_result.marker)
                protected = (
                    attempt_result.status == "passed"
                    or attempt_result.status in FAILED_OUTCOME_STATUSES
                )
                if (
                    protected and attempt_result.marker is not None
                    and attempt_result.marker["collected_test_ids"] != baseline_ids
                ):
                    finalize("upgrade_failed", "collected test IDs changed from baseline")
                    return
                if attempt_result.status == "passed":
                    repaired = True
                    break
                if attempt_result.status == "test_failed":
                    latest_failure = attempt_result
                    continue
                if attempt_result.status == "timed_out":
                    finalize("timed_out", f"repair-{repair_number} attempt timed out")
                    return
                if attempt_result.status == "cancelled":
                    finalize("cancelled", "cancelled by request")
                    return
                if attempt_result.status in FAILED_OUTCOME_STATUSES:
                    finalize(
                        "upgrade_failed",
                        f"{attempt_result.status}: {_result_error(attempt_result)}",
                    )
                    return
                finalize(
                    "infrastructure_failed",
                    f"repair-{repair_number} attempt {attempt_result.status}:"
                    f" {_result_error(attempt_result)}",
                )
                return
            if not repaired:
                detail = f"test_failed: {_result_error(latest_failure)}"
                if provider_note is not None:
                    note_code, note_message = provider_note
                    detail += f"; repair loop ended on provider error: {note_code}: {note_message}"
                finalize("upgrade_failed", detail)
                return
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
        # It always runs on the final accepted tar: the repaired tar when a
        # repair passed, otherwise the original candidate tar.
        self._transition(run_id, "verifying")
        verifier, started_utc, finished_utc = invoke(
            lambda: self._runner.run_profile_attempt(
                current_tar, phase="candidate",
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

    def _transition(self, run_id: int, state: str, **extra: Any) -> None:
        """Persist one non-terminal state change before its state event."""
        self._store.set_run_state(run_id, state)
        self._store.append_event(run_id, "state", {"state": state, **extra})

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

    def _save_patch(
        self, run: dict, baseline_tar: bytes | None, candidate_tar: bytes | None,
        repair_chain: list[tuple[int, bytes, bytes]],
    ) -> None:
        """Build the deterministic pin diff plus accumulated repair source diffs."""
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
            for repair_number, zip_before, zip_after in repair_chain:
                content += (
                    f"\n--- repair {repair_number} source changes ---\n"
                    + changed_file_diffs(zip_before, zip_after)
                )
            self._save_artifact(run["id"], "patch.diff", content.encode("utf-8"), kind="patch")
            return
        content = f"# patch input unavailable: {reason}\n"
        for repair_number, zip_before, zip_after in repair_chain:
            content += (
                f"\n--- repair {repair_number} source changes ---\n"
                + changed_file_diffs(zip_before, zip_after)
            )
        self._save_artifact(run["id"], "patch.diff", content.encode("utf-8"), kind="patch")

    def _save_comparison(
        self, run: dict, terminal_state: str, detail: str | None,
        baseline_summary: dict[str, Any] | None, candidate_summary: dict[str, Any] | None,
        verifier_summary: dict[str, Any] | None,
        repair_records: list[dict[str, Any]],
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
            "repairs": repair_records,
            "verifier": verifier_summary,
            "result": terminal_state,
            "detail": detail,
        }
        self._save_artifact(
            run["id"], "comparison.json", json.dumps(payload).encode("utf-8"), kind="report")

    def _save_manifest(
        self, run: dict, terminal_state: str, detail: str | None, cleanup_state: str,
        advisory_snapshots: dict[str, dict[str, Any]], baseline_summary: dict[str, Any] | None,
        source_digest: str, model_state: dict[str, Any] | None,
        selection_rationale: str | None, manifest_repairs: list[dict[str, Any]],
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
            "source_sha256": source_digest,
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
            "model": {
                "model_id": model_state["model_id"] if model_state else None,
                "selection": {"rationale": selection_rationale},
                "repairs": manifest_repairs,
            },
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
            internal_token=settings.internal_token,
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
