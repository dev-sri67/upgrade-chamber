"""Worker phase-machine checks against a real store and a programmable fake runner."""

import copy
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Callable

from upgrade_chamber.edits import member_name
from upgrade_chamber.runner import AttemptResult, PreparationResult
from upgrade_chamber.storage import Store
from upgrade_chamber.worker import Worker


IMAGE = "python-runner@sha256:" + "b" * 64
RUNNER_SOURCE_DIGEST = "c" * 64
METADATA_SOURCE_DIGEST = "d" * 64
BASELINE_IDS = [f"tests/test_example.py::test_{index}" for index in range(5)]
MODEL_ID = "vultr-test-model"
ADAPTERS_PATH = "requests_unixsocket/adapters.py"
ADAPTERS_ORIGINAL = "HTTPAdapter = object\nUNIXSocketAdapter = object\n"
ADAPTERS_REPAIRED = "HTTPAdapter = object\nUNIXSocketAdapter = object  # repaired\n"
REPAIR_FILES = {
    "requests_unixsocket/__init__.py": "from . import adapters\n",
    ADAPTERS_PATH: ADAPTERS_ORIGINAL,
    "requests_unixsocket/tests/test_requests_unixsocket.py": "def test_placeholder():\n    assert True\n",
    "setup.py": "from setuptools import setup\nsetup()\n",
}
FAILURE_LOG = (
    b"collected 5 items\n"
    b"tests/test_example.py::test_0 FAILED socket path changed\n"
    b"1 failed, 4 passed\n"
)
JUNIT_WITH_FAILURE = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<testsuite tests="2" failures="1" errors="0">\n'
    b'<testcase classname="tests.test_example" name="test_0">'
    b'<failure message="boom">traceback</failure></testcase>\n'
    b'<testcase classname="tests.test_example" name="test_1"/>\n'
    b"</testsuite>\n"
)
ATTEMPT_ARTIFACT_NAMES = (
    "attempt.json",
    "collect.txt",
    "junit.xml",
    "pip-report.json",
    "pip-check.txt",
    "installed.json",
    "install.log",
    "test.log",
)

RUN_PARAMS = {
    "profile_id": "requests-unixsocket",
    "repo_url": "https://github.com/msabramo/requests-unixsocket",
    "commit_sha": "a" * 40,
    "dependency": "requests",
    "requested_ref": None,
    "idempotency_key": None,
    "submit_ip": "127.0.0.1",
    "image_identity": IMAGE,
    "source_sha256": RUNNER_SOURCE_DIGEST,
    "baseline_version": "2.31.0",
    "target_version": "2.32.2",
    "job_deadline_seconds": 900.0,
}


def requirements_bundle(content: bytes) -> bytes:
    """Build a minimal valid attempt bundle carrying one requirements.txt."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        info = tarfile.TarInfo("requirements.txt")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _tar_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """Build one uncompressed tar from (name, content) members."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def sha256_text(text: str) -> str:
    """SHA-256 of one UTF-8 text, matching the edit hash contract."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def repair_source_zip() -> bytes:
    """Build a real small source.zip over the fixed edit-allowed files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in REPAIR_FILES.items():
            archive.writestr(
                zipfile.ZipInfo(member_name(path), date_time=(1980, 1, 1, 0, 0, 0)),
                text.encode("utf-8"),
            )
    return buffer.getvalue()


def repair_candidate_tar(source: bytes) -> bytes:
    """Build a valid candidate attempt input tar around one source.zip."""
    manifest = {
        "schema_version": 1,
        "phase": "candidate",
        "source_sha256": hashlib.sha256(source).hexdigest(),
    }
    return _tar_bytes([
        ("source.zip", source),
        ("requirements.txt", b"requests==2.32.2\n"),
        ("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8")),
        ("wheels/requests.whl", b"wheel"),
    ])


def adapters_edit(*, digest: str | None = None,
                  replacement: str = ADAPTERS_REPAIRED) -> dict:
    """Build one adapters.py edit; a wrong digest forces semantic rejection."""
    return {
        "path": ADAPTERS_PATH,
        "original_sha256": digest or sha256_text(ADAPTERS_ORIGINAL),
        "replacement_text": replacement,
    }


def selection_response(*, target: str = "2.32.2", model_id: str = MODEL_ID) -> tuple[int, bytes]:
    """Canned 200 body for the internal select endpoint."""
    body = {
        "selection": {
            "package": "requests",
            "target_version": target,
            "rationale": "target version fixed by the validated profile",
        },
        "model_id": model_id,
        "attempts": 1,
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return (200, json.dumps(body).encode("utf-8"))


def agent_turn_response(message: dict, *, usage: dict | None = None,
                        model_id: str = MODEL_ID) -> tuple[int, bytes]:
    """Canned 200 body for the internal agent-turn endpoint."""
    body = {
        "message": message,
        "usage": {"prompt_tokens": 21, "completion_tokens": 9} if usage is None else usage,
        "attempts": 1,
        "model_id": model_id,
    }
    return (200, json.dumps(body).encode("utf-8"))


def turn_list_repo_files() -> dict:
    """One agent-turn message invoking list_repo_files."""
    return {"tool": "list_repo_files", "args": {}}


def turn_read_file(path: str) -> dict:
    """One agent-turn message invoking read_file."""
    return {"tool": "read_file", "args": {"path": path}}


def turn_propose_edits(edits: list[dict]) -> dict:
    """One agent-turn message invoking propose_edits."""
    return {"tool": "propose_edits", "args": {"edits": edits}}


def turn_finish(summary: str, edits: list[dict]) -> dict:
    """One agent-turn message finishing with a validated repair."""
    return {"tool": "finish", "args": {"summary": summary, "edits": edits}}


def turn_abort(reason: str) -> dict:
    """One agent-turn message aborting the session."""
    return {"tool": "abort", "args": {"reason": reason}}


def happy_repair_script() -> list:
    """Script one full successful repair session after model selection."""
    edit = adapters_edit()
    return [
        selection_response(),
        agent_turn_response(turn_list_repo_files()),
        agent_turn_response(turn_read_file(ADAPTERS_PATH)),
        agent_turn_response(turn_propose_edits([edit])),
        agent_turn_response(turn_finish("adjust adapter compatibility", [edit])),
    ]


def error_response(code: str, message: str,
                   status: int = 502) -> tuple[int, bytes]:
    """Canned error body shaped like the frozen internal error contract."""
    return (status, json.dumps({"error": {"code": code, "message": message}}).encode("utf-8"))


class FakeInternalResponse:
    """Minimal httpx-like response carrying only status and body bytes."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class FakeInternalClient:
    """Stub HTTP client recording internal POSTs and returning canned responses.

    Each scripted entry is either an exception to raise or a
    (status, body-bytes) pair. Exhausting the script fails the call loudly so
    tests prove exactly how many internal POSTs the worker made.
    """

    def __init__(self, responses: list) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)

    def post(self, url: str, *, json: Any = None, headers: Any = None) -> FakeInternalResponse:
        # The agent session mutates its conversation list in place between
        # turns, so each recorded POST must snapshot the payload as it stood
        # at call time, the way a real HTTP serialization would.
        self.calls.append({
            "url": url,
            "payload": copy.deepcopy(json),
            "headers": dict(headers) if headers is not None else None,
        })
        if not self._responses:
            raise AssertionError(f"unexpected internal inference POST: {url}")
        entry = self._responses.pop(0)
        if isinstance(entry, Exception):
            raise entry
        status, content = entry
        return FakeInternalResponse(status, content)


def attempt_marker(phase: str, status: str, *, ids=None, installed=None, error=None) -> dict:
    """Build a valid marker copying the runner's documented attempt schema."""
    passed = status == "passed"
    steps = {name: {"exit_code": 0, "timed_out": False}
             for name in ("install", "collect", "test", "pip_check")}
    if not passed:
        steps["install"]["exit_code"] = 1
        for name in ("collect", "test", "pip_check"):
            steps[name]["exit_code"] = None
    if ids is None:
        ids = list(BASELINE_IDS) if passed else []
    return {
        "schema_version": 1,
        "phase": phase,
        "status": status,
        "steps": steps,
        "collected_test_ids": list(ids),
        "counts": {"passed": len(ids) if passed else 0, "failed": 0, "errors": 0,
                   "skipped": 0, "xfailed": 0, "xpassed": 0},
        "installed_requests_version": installed if passed else None,
        "source_sha256": "c" * 64,
        "error": None if passed else (error or "Attempt failed"),
    }


def attempt_result(phase: str, status: str = "passed", *, ids=None, installed=None,
                   error=None, complete=True) -> AttemptResult:
    """Build one AttemptResult with programmable status, IDs, and artifacts."""
    marker = attempt_marker(phase, status, ids=ids, installed=installed, error=error)
    if complete:
        artifacts = {name: b"evidence:" + name.encode() for name in ATTEMPT_ARTIFACT_NAMES}
    else:
        artifacts = {"attempt.json": json.dumps(marker).encode(),
                     "install.log": b"pip failed", "test.log": b"test failed"}
    return AttemptResult(
        phase=phase,
        status=status,
        container_id=f"{phase}-container",
        elapsed_seconds=2.0,
        exit_code=0 if status == "passed" else 1,
        marker=marker,
        artifacts=artifacts,
        missing_artifacts=[],
        stdout="out",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        error=None,
        removal_observed=True,
        deadline_seconds=300.0,
        image_identity=IMAGE,
    )


def failing_candidate(*, error: str = "1 test failed", log: bytes = FAILURE_LOG,
                      junit: bytes | None = JUNIT_WITH_FAILURE,
                      ids: list[str] | None = None) -> AttemptResult:
    """Build one candidate test_failed result carrying real log and junit bytes."""
    result = attempt_result(
        "candidate", status="test_failed",
        ids=list(BASELINE_IDS) if ids is None else ids, error=error)
    result.artifacts["test.log"] = log
    if junit is not None:
        result.artifacts["junit.xml"] = junit
    return result


def preparation_result(status: str = "prepared", *, error=None,
                       source_digest=None, candidate: bytes | None = None) -> PreparationResult:
    """Build one PreparationResult carrying both pinned offline bundles.

    ``candidate`` replaces the default requirements-only candidate.tar with a
    full attempt input tar (used by the repair-loop tests).
    """
    marker = {
        "schema_version": 1,
        "status": status,
        "error": None if status == "prepared" else (error or "Download failed"),
        "summary": {} if status == "prepared" else None,
    }
    artifacts = {"preparation.json": json.dumps(marker).encode()}
    if status == "prepared":
        metadata = {} if source_digest is None else {"source_sha256": source_digest}
        artifacts.update({
            "baseline-metadata.json": json.dumps(metadata).encode(),
            "candidate-metadata.json": b"{}",
            "baseline.tar": requirements_bundle(b"requests==2.31.0\n"),
            "candidate.tar": candidate or requirements_bundle(b"requests==2.32.2\n"),
        })
    return PreparationResult(
        status=status,
        container_id="prep-container" if status == "prepared" else None,
        elapsed_seconds=3.0,
        exit_code=0 if status == "prepared" else 1,
        marker=marker,
        artifacts=artifacts,
        missing_artifacts=[],
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        error=None,
        removal_observed=True,
        deadline_seconds=300.0,
        image_identity=IMAGE,
    )


def fake_osv(package: str, version: str) -> dict:
    """Return one available advisory snapshot without touching the network."""
    return {
        "schema_version": 1,
        "package": package,
        "version": version,
        "status": "available",
        "fetched_utc": "2026-01-01T00:00:00+00:00",
        "vulnerabilities": [{"id": f"GHSA-{version}", "aliases": [], "summary": "x"}],
    }


def unavailable_osv(package: str, version: str) -> dict:
    """Return one unavailable advisory snapshot as the OSV client would on failure."""
    return {
        "schema_version": 1,
        "package": package,
        "version": version,
        "status": "unavailable",
        "fetched_utc": "2026-01-01T00:00:00+00:00",
        "error": "OSVError: OSV API unreachable",
    }


class FakeRunner:
    """Programmable runner stand-in that records every call the worker makes.

    Optional hooks fire inside the matching runner phase label (the verifier
    rerun also uses the "candidate" phase label) before the canned result is
    returned, which lets a test mutate store state mid-call. Every attempt
    call also evaluates the should_cancel callback once and records the
    observation so tests can prove the worker passed a live check.
    """

    def __init__(self, preparation: PreparationResult, attempts: list[AttemptResult], *,
                 hooks: dict[str, Callable[[], None]] | None = None):
        self.image = IMAGE
        self._preparation = preparation
        self._attempts = list(attempts)
        self._hooks = dict(hooks or {})
        self.calls: list[tuple] = []
        self.cancel_checks: list[tuple[str, bool]] = []

    def run_preparation(self, *, timeout_seconds=300.0, should_cancel=None) -> PreparationResult:
        self.calls.append(("preparation", timeout_seconds))
        return self._preparation

    def run_profile_attempt(self, input_tar: bytes, *, phase: str,
                            timeout_seconds: float = 300.0,
                            should_cancel: Callable[[], bool] | None = None) -> AttemptResult:
        self.calls.append((phase, timeout_seconds, input_tar))
        hook = self._hooks.get(phase)
        if hook is not None:
            hook()
        observed = bool(should_cancel()) if should_cancel is not None else False
        self.cancel_checks.append((phase, observed))
        return self._attempts.pop(0)

    def remove_expired_containers(self, *, now=None) -> list[str]:
        return []


class WorkerTests(unittest.TestCase):
    def setUp(self):
        context = tempfile.TemporaryDirectory()
        self.addCleanup(context.cleanup)
        base = Path(context.name)
        self.store = Store(base / "runs.db", base / "artifacts")
        self.addCleanup(self.store.close)

    def execute(self, runner: FakeRunner, osv_query=fake_osv, *,
                internal: FakeInternalClient | None = None,
                **worker_kwargs: Any) -> int:
        """Create, lease, and execute one run; return its id."""
        if internal is None:
            internal = FakeInternalClient([selection_response()])
        run_id, _ = self.store.create_run(**RUN_PARAMS)
        run = self.store.lease_next_run("test-worker", 1000.0)
        Worker(self.store, runner, osv_query=osv_query,
               http_client=internal, **worker_kwargs).execute_run(run)
        return run_id

    def artifact_names(self, run_id: int) -> set[str]:
        return {row["name"] for row in self.store.list_artifacts(run_id)}

    def test_happy_path_completes_with_full_evidence(self):
        queries = []

        def counting_osv(package, version):
            queries.append((package, version))
            return fake_osv(package, version)

        runner = FakeRunner(
            preparation_result(source_digest=METADATA_SOURCE_DIGEST),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        run_id = self.execute(runner, osv_query=counting_osv)

        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["source_sha256"], METADATA_SOURCE_DIGEST)
        self.assertIsNotNone(run["terminal_utc"])
        self.assertEqual(run["cleanup_state"], "observed")
        self.assertEqual(
            [attempt["phase"] for attempt in self.store.attempts(run_id)],
            ["preparation", "baseline", "candidate", "verifier"])
        # The runner only accepts baseline/candidate phases; the verifier rerun
        # also runs as "candidate" but is recorded under the "verifier" phase.
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate", "candidate"])
        self.assertEqual([call[1] for call in runner.calls], [300.0, 300.0, 300.0, 300.0])

        names = self.artifact_names(run_id)
        self.assertLessEqual(
            {"preparation.json", "baseline-metadata.json", "candidate-metadata.json",
             "baseline-attempt.json", "baseline-collect.txt", "baseline-install.log",
             "candidate-attempt.json", "verifier-attempt.json",
             "advisory-baseline.json", "advisory-target.json",
             "patch.diff", "comparison.json", "manifest.json"},
            names)

        patch = self.store.get_artifact(run_id, "patch.diff")
        self.assertIn(b"2.31.0", patch)
        self.assertIn(b"2.32.2", patch)

        comparison = json.loads(self.store.get_artifact(run_id, "comparison.json"))
        self.assertEqual(comparison["baseline"]["status"], "passed")
        self.assertEqual(comparison["candidate"]["status"], "passed")
        self.assertEqual(comparison["verifier"]["status"], "passed")
        self.assertTrue(comparison["collected_ids_match"])
        self.assertEqual(comparison["result"], "completed")
        self.assertEqual(comparison["repairs"], [])

        manifest = json.loads(self.store.get_artifact(run_id, "manifest.json"))
        self.assertEqual(manifest["result"]["state"], "completed")
        self.assertEqual(manifest["source_sha256"], METADATA_SOURCE_DIGEST)
        self.assertEqual(len(manifest["attempts"]), 4)
        self.assertNotIn("manifest.json", manifest["artifacts"])
        records = {row["name"]: row for row in self.store.list_artifacts(run_id)}
        for name, entry in manifest["artifacts"].items():
            self.assertEqual(entry["sha256"], records[name]["sha256"])
            self.assertEqual(entry["bytes"], records[name]["bytes"])
        self.assertEqual(manifest["dependency"],
                         {"name": "requests", "baseline_version": "2.31.0",
                          "target_version": "2.32.2"})
        self.assertEqual(manifest["test_scope"],
                         {"collected": 5, "first": BASELINE_IDS[0], "last": BASELINE_IDS[-1]})
        self.assertEqual(manifest["cleanup_state"], "observed")
        self.assertEqual(manifest["advisories"]["baseline"]["status"], "available")
        self.assertEqual(manifest["model"]["repairs"], [])

        advisory = json.loads(self.store.get_artifact(run_id, "advisory-baseline.json"))
        self.assertEqual(advisory["status"], "available")
        self.assertEqual(queries, [("requests", "2.31.0"), ("requests", "2.32.2")])

        events = self.store.events_after(run_id, 0)
        self.assertEqual({event["kind"] for event in events},
                         {"state", "attempt", "artifact", "selection", "advisory", "terminal"})
        selection = [event for event in events if event["kind"] == "selection"]
        self.assertEqual(selection[0]["data"]["source"], "model")
        self.assertEqual(selection[0]["data"]["model_id"], MODEL_ID)
        self.assertEqual(selection[0]["data"]["target_version"], "2.32.2")
        terminal = [event for event in events if event["kind"] == "terminal"][-1]
        self.assertEqual(terminal["data"]["state"], "completed")
        self.assertEqual(terminal["data"]["cleanup_state"], "observed")
        self.assertEqual(self.store.get_run(run_id)["state"], terminal["data"]["state"])

    def test_baseline_failure_stops_before_selection_and_patch(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", status="test_failed", error="2 tests failed",
                            complete=False)])
        run_id = self.execute(runner)

        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "baseline_failed")
        self.assertEqual(run["source_sha256"], RUNNER_SOURCE_DIGEST)
        self.assertIn("test_failed", run["status_detail"])
        self.assertIsNotNone(run["terminal_utc"])
        self.assertEqual([call[0] for call in runner.calls if call[0] != "preparation"],
                         ["baseline"])
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline"])
        names = self.artifact_names(run_id)
        self.assertNotIn("patch.diff", names)
        self.assertNotIn("manifest.json", names)
        self.assertNotIn("comparison.json", names)
        events = self.store.events_after(run_id, 0)
        self.assertFalse([event for event in events if event["kind"] == "selection"])

    def test_candidate_changed_ids_fail_protected_scope_before_verifier(self):
        changed = ["tests/test_other.py::test_renamed"] + BASELINE_IDS[:4]
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2", ids=changed)])
        run_id = self.execute(runner)

        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "upgrade_failed")
        self.assertEqual(run["status_detail"], "collected test IDs changed from baseline")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate"])
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate"])

    # --- model selection and the bounded agent repair sessions ---

    def test_model_selection_uses_internal_endpoint(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        internal = FakeInternalClient([selection_response()])
        run_id = self.execute(runner, internal=internal)

        self.assertEqual(self.store.get_run(run_id)["state"], "completed")
        self.assertEqual(len(internal.calls), 1)
        call = internal.calls[0]
        self.assertTrue(call["url"].endswith("/internal/inference/select"))
        payload = call["payload"]
        self.assertEqual(set(payload), {"run_id", "package", "eligible_versions", "context"})
        self.assertEqual(payload["package"], "requests")
        self.assertEqual(payload["eligible_versions"], ["2.32.2"])
        self.assertIn("https://github.com/msabramo/requests-unixsocket", payload["context"])
        self.assertIn("the baseline suite passes on the baseline version", payload["context"])
        events = self.store.events_after(run_id, 0)
        selection = [event for event in events if event["kind"] == "selection"][0]
        self.assertEqual(selection["data"]["source"], "model")
        self.assertEqual(selection["data"]["model_id"], MODEL_ID)
        self.assertNotIn("repair", {event["kind"] for event in events})

    def test_model_selection_provider_failure_is_terminal(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0")])
        internal = FakeInternalClient(
            [error_response("inference_unavailable", "provider offline")])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "infrastructure_failed")
        self.assertEqual(
            record["status_detail"],
            "model selection failed: inference_unavailable: provider offline")
        self.assertIsNotNone(record["terminal_utc"])
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline"])
        names = self.artifact_names(run_id)
        self.assertNotIn("patch.diff", names)
        self.assertNotIn("manifest.json", names)
        events = self.store.events_after(run_id, 0)
        self.assertFalse([event for event in events if event["kind"] == "advisory"])

    def test_agentic_repair_happy_path_records_session_evidence(self):
        prepared = preparation_result(candidate=repair_candidate_tar(repair_source_zip()))
        runner = FakeRunner(
            prepared,
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate(),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        internal = FakeInternalClient(happy_repair_script())
        run_id = self.execute(runner, internal=internal)

        self.assertEqual(self.store.get_run(run_id)["state"], "completed")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(
            phases, ["preparation", "baseline", "candidate", "repair-1", "verifier"])
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate", "candidate", "candidate"])
        self.assertNotEqual(runner.calls[3][2], prepared.artifacts["candidate.tar"])
        self.assertEqual(runner.calls[4][2], runner.calls[3][2])
        self.assertIn("repair1-test.log", self.artifact_names(run_id))

        # Every model call went to the agent-turn endpoint with the bounded
        # worker-owned conversation.
        self.assertEqual(len(internal.calls), 5)
        turn_calls = internal.calls[1:]
        for call in turn_calls:
            self.assertTrue(call["url"].endswith("/internal/inference/agent-turn"))
            payload = call["payload"]
            self.assertEqual(set(payload), {"run_id", "messages", "max_tokens"})
            self.assertEqual(payload["run_id"], run_id)
            self.assertEqual(payload["max_tokens"], 16384)
            self.assertLessEqual(len(payload["messages"]), 24)
            self.assertEqual(payload["messages"][0]["role"], "system")
            for message in payload["messages"]:
                self.assertIn(message["role"], {"system", "user", "assistant"})
        opening = turn_calls[0]["payload"]["messages"]
        self.assertEqual([message["role"] for message in opening], ["system", "user"])
        self.assertIn("requests_unixsocket/adapters.py", opening[1]["content"])
        self.assertIn("socket path changed", opening[1]["content"])

        patch = self.store.get_artifact(run_id, "patch.diff")
        self.assertIn(b"--- repair 1 source changes ---", patch)
        self.assertIn(b"+UNIXSocketAdapter = object  # repaired", patch)
        self.assertIn(b"2.31.0", patch)
        self.assertIn(b"2.32.2", patch)

        comparison = json.loads(self.store.get_artifact(run_id, "comparison.json"))
        self.assertEqual(comparison["candidate"]["status"], "passed")
        self.assertEqual(comparison["repairs"], [{
            "attempt": 1,
            "status": "finished",
            "summary": "adjust adapter compatibility",
            "edit_paths": [ADAPTERS_PATH],
            "model_calls": 4,
            "usage": {"prompt_tokens": 84, "completion_tokens": 36},
        }])
        self.assertTrue(comparison["collected_ids_match"])

        manifest = json.loads(self.store.get_artifact(run_id, "manifest.json"))
        self.assertEqual(
            manifest["model"],
            {"model_id": MODEL_ID,
             "selection": {"rationale": "target version fixed by the validated profile"},
             "repairs": [{"attempt": 1, "status": "finished",
                          "summary": "adjust adapter compatibility", "model_calls": 4}]})
        self.assertEqual(
            [attempt["phase"] for attempt in manifest["attempts"]],
            ["preparation", "baseline", "candidate", "repair-1", "verifier"])

        result_payload = json.loads(self.store.get_run(run_id)["result"])
        self.assertEqual(result_payload["model"], {"model_id": MODEL_ID, "repair_count": 1})

        events = self.store.events_after(run_id, 0)
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual(len(repair_events), 1)
        data = repair_events[0]["data"]
        self.assertEqual(data["status"], "finished")
        self.assertEqual(data["summary"], "adjust adapter compatibility")
        self.assertEqual(
            [turn["tool"] for turn in data["turns"]],
            ["list_repo_files", "read_file", "propose_edits", "finish"])
        self.assertTrue(all(turn["ok"] for turn in data["turns"]))
        self.assertTrue(all(len(turn["detail"]) <= 300 for turn in data["turns"]))
        self.assertEqual(data["model_calls"], 4)
        self.assertEqual(data["usage"], {"prompt_tokens": 84, "completion_tokens": 36})
        self.assertEqual(data["edit_paths"], [ADAPTERS_PATH])

    def test_repair_proposal_rejected_then_abort_is_terminal_without_attempt(self):
        runner = FakeRunner(
            preparation_result(candidate=repair_candidate_tar(repair_source_zip())),
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate()])
        internal = FakeInternalClient([
            selection_response(),
            agent_turn_response(turn_propose_edits([adapters_edit(digest="0" * 64)])),
            agent_turn_response(turn_abort("cannot fix this failure")),
        ])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(
            record["status_detail"], "repair session aborted: cannot fix this failure")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate"])
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate"])
        self.assertEqual(len(internal.calls), 3)
        events = self.store.events_after(run_id, 0)
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual(len(repair_events), 1)
        data = repair_events[0]["data"]
        self.assertEqual(data["status"], "aborted")
        rejected = data["turns"][0]
        self.assertEqual(rejected["tool"], "propose_edits")
        self.assertFalse(rejected["ok"])
        self.assertIn("does not match", rejected["detail"])
        self.assertEqual(data["turns"][1]["tool"], "abort")
        self.assertEqual(data["edit_paths"], [])
        result_payload = json.loads(record["result"])
        self.assertEqual(result_payload["model"], {"model_id": MODEL_ID, "repair_count": 0})

    def test_repair_session_exhausted_is_terminal_without_attempt(self):
        runner = FakeRunner(
            preparation_result(candidate=repair_candidate_tar(repair_source_zip())),
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate()])
        internal = FakeInternalClient(
            [selection_response()]
            + [agent_turn_response(turn_list_repo_files()) for _ in range(8)])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(record["status_detail"], "repair session exhausted")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate"])
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate"])
        # Eight session turns plus selection, and nothing else: the exhausted
        # session stopped on its own turn budget.
        self.assertEqual(len(internal.calls), 9)
        events = self.store.events_after(run_id, 0)
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual(len(repair_events), 1)
        data = repair_events[0]["data"]
        self.assertEqual(data["status"], "exhausted")
        self.assertEqual(data["model_calls"], 8)
        self.assertEqual([turn["tool"] for turn in data["turns"]],
                         ["list_repo_files"] * 8)
        self.assertEqual(data["edit_paths"], [])

    def test_inference_budget_limits_agent_turn_posts(self):
        runner = FakeRunner(
            preparation_result(candidate=repair_candidate_tar(repair_source_zip())),
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate()])
        internal = FakeInternalClient([
            selection_response(),
            agent_turn_response(turn_list_repo_files()),
            agent_turn_response(turn_read_file(ADAPTERS_PATH)),
        ])
        run_id = self.execute(runner, internal=internal, max_inference_calls=3)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertIn("test_failed: 1 test failed", record["status_detail"])
        self.assertIn(
            "repair loop ended on provider error: budget_exhausted:"
            " per-job inference call budget exhausted",
            record["status_detail"])
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate"])
        # Only the budgeted number of posts happened: selection plus two
        # agent turns; the third turn was refused before any POST.
        self.assertEqual(len(internal.calls), 3)
        self.assertTrue(internal.calls[1]["url"].endswith("/internal/inference/agent-turn"))
        self.assertTrue(internal.calls[2]["url"].endswith("/internal/inference/agent-turn"))
        events = self.store.events_after(run_id, 0)
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual(len(repair_events), 1)
        data = repair_events[0]["data"]
        self.assertEqual(data["status"], "provider_error")
        self.assertEqual(data["error_code"], "budget_exhausted")
        self.assertEqual(data["attempt"], 1)
        result_payload = json.loads(record["result"])
        self.assertEqual(result_payload["model"], {"model_id": MODEL_ID, "repair_count": 0})

    def test_two_repair_cycles_use_prior_repaired_tar_and_new_log(self):
        prepared = preparation_result(candidate=repair_candidate_tar(repair_source_zip()))
        first_edit = adapters_edit()
        second_edit = adapters_edit(
            digest=sha256_text(ADAPTERS_REPAIRED),
            replacement=ADAPTERS_REPAIRED + "FIXED = True\n")
        runner = FakeRunner(
            prepared,
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate(),
             failing_candidate(error="still failing",
                               log=b"collected 5 items\nstill failing\n"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        internal = FakeInternalClient([
            selection_response(),
            agent_turn_response(turn_finish("first fix", [first_edit])),
            agent_turn_response(turn_read_file(ADAPTERS_PATH)),
            agent_turn_response(turn_finish("second fix", [second_edit])),
        ])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "completed")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(
            phases,
            ["preparation", "baseline", "candidate", "repair-1", "repair-2", "verifier"])
        self.assertEqual(
            [call[0] for call in runner.calls],
            ["preparation", "baseline", "candidate", "candidate", "candidate", "candidate"])
        self.assertNotEqual(runner.calls[3][2], prepared.artifacts["candidate.tar"])
        self.assertNotEqual(runner.calls[4][2], runner.calls[3][2])
        self.assertEqual(runner.calls[5][2], runner.calls[4][2])

        # The second session opened on the post-repair zip and the new log:
        # its opening message carries the fresh failure, and its read_file
        # turn observes the already-repaired adapter content.
        second_opening = internal.calls[2]["payload"]["messages"]
        self.assertIn("still failing", second_opening[1]["content"])
        second_reply = internal.calls[3]["payload"]["messages"][-1]["content"]
        self.assertIn("UNIXSocketAdapter = object  # repaired", second_reply)

        patch = self.store.get_artifact(run_id, "patch.diff")
        self.assertIn(b"--- repair 1 source changes ---", patch)
        self.assertIn(b"--- repair 2 source changes ---", patch)
        self.assertIn(b"+UNIXSocketAdapter = object  # repaired", patch)
        self.assertIn(b"+FIXED = True", patch)

        comparison = json.loads(self.store.get_artifact(run_id, "comparison.json"))
        self.assertEqual(
            [(entry["attempt"], entry["status"], entry["model_calls"])
             for entry in comparison["repairs"]],
            [(1, "finished", 1), (2, "finished", 2)])
        self.assertEqual(
            [entry["summary"] for entry in comparison["repairs"]],
            ["first fix", "second fix"])
        self.assertTrue(comparison["collected_ids_match"])

        manifest = json.loads(self.store.get_artifact(run_id, "manifest.json"))
        self.assertEqual(
            [(entry["attempt"], entry["status"], entry["model_calls"])
             for entry in manifest["model"]["repairs"]],
            [(1, "finished", 1), (2, "finished", 2)])
        self.assertEqual(
            [attempt["phase"] for attempt in manifest["attempts"]],
            ["preparation", "baseline", "candidate", "repair-1", "repair-2", "verifier"])
        result_payload = json.loads(record["result"])
        self.assertEqual(result_payload["model"], {"model_id": MODEL_ID, "repair_count": 2})

    def test_repair_provider_error_is_honest_terminal(self):
        runner = FakeRunner(
            preparation_result(candidate=repair_candidate_tar(repair_source_zip())),
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate()])
        internal = FakeInternalClient([
            selection_response(),
            error_response("inference_unavailable", "model offline"),
        ])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(
            record["status_detail"],
            "test_failed: 1 test failed;"
            " repair loop ended on provider error: inference_unavailable: model offline")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate"])
        self.assertEqual(len(runner.calls), 3)
        events = self.store.events_after(run_id, 0)
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual([event["data"]["status"] for event in repair_events],
                         ["provider_error"])
        self.assertEqual(repair_events[0]["data"]["error_code"], "inference_unavailable")
        self.assertEqual(repair_events[0]["data"]["attempt"], 1)

    def test_both_repair_cycles_fail_is_honest_upgrade_failed(self):
        prepared = preparation_result(candidate=repair_candidate_tar(repair_source_zip()))
        first_edit = adapters_edit()
        second_edit = adapters_edit(
            digest=sha256_text(ADAPTERS_REPAIRED),
            replacement=ADAPTERS_REPAIRED + "FIXED = True\n")
        runner = FakeRunner(
            prepared,
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate(),
             failing_candidate(error="still failing"),
             failing_candidate(error="still failing again")])
        internal = FakeInternalClient([
            selection_response(),
            agent_turn_response(turn_finish("first fix", [first_edit])),
            agent_turn_response(turn_finish("second fix", [second_edit])),
        ])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(record["status_detail"], "test_failed: still failing again")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(
            phases, ["preparation", "baseline", "candidate", "repair-1", "repair-2"])
        self.assertNotIn("verifier", phases)
        self.assertEqual(
            [call[0] for call in runner.calls],
            ["preparation", "baseline", "candidate", "candidate", "candidate"])
        events = self.store.events_after(run_id, 0)
        repair_events = [event for event in events if event["kind"] == "repair"]
        self.assertEqual([event["data"]["status"] for event in repair_events],
                         ["finished", "finished"])
        result_payload = json.loads(record["result"])
        self.assertEqual(result_payload["model"]["repair_count"], 2)

    def test_repaired_attempt_changed_ids_fail_protected_scope(self):
        changed = ["tests/test_other.py::test_renamed"] + BASELINE_IDS[:4]
        runner = FakeRunner(
            preparation_result(candidate=repair_candidate_tar(repair_source_zip())),
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate(),
             attempt_result("candidate", installed="2.32.2", ids=changed)])
        internal = FakeInternalClient([
            selection_response(),
            agent_turn_response(turn_finish(
                "adjust adapter compatibility", [adapters_edit()])),
        ])
        run_id = self.execute(runner, internal=internal)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(record["status_detail"], "collected test IDs changed from baseline")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate", "repair-1"])
        self.assertEqual(len(internal.calls), 2)

    def test_candidate_passes_directly_skips_repair_and_reuses_tar(self):
        prepared = preparation_result()
        runner = FakeRunner(
            prepared,
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        internal = FakeInternalClient([selection_response()])
        run_id = self.execute(runner, internal=internal)

        self.assertEqual(self.store.get_run(run_id)["state"], "completed")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate", "verifier"])
        self.assertEqual(len(internal.calls), 1)
        self.assertTrue(internal.calls[0]["url"].endswith("/internal/inference/select"))
        # The verifier rerun must use the original candidate tar untouched.
        self.assertEqual(runner.calls[2][2], prepared.artifacts["candidate.tar"])
        self.assertEqual(runner.calls[3][2], runner.calls[2][2])

    def test_internal_token_header_sent_when_configured(self):
        runner = FakeRunner(
            preparation_result(candidate=repair_candidate_tar(repair_source_zip())),
            [attempt_result("baseline", installed="2.31.0"),
             failing_candidate(),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        internal = FakeInternalClient(happy_repair_script())
        run_id = self.execute(runner, internal=internal, internal_token="secret-token")

        self.assertEqual(self.store.get_run(run_id)["state"], "completed")
        self.assertEqual(len(internal.calls), 5)
        for call in internal.calls:
            self.assertEqual(call["headers"]["X-Internal-Token"], "secret-token")

    def test_verifier_failure_names_the_failed_check(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", status="test_failed", error="verifier broke")])
        run_id = self.execute(runner)

        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "upgrade_failed")
        self.assertEqual(run["status_detail"], "verifier status was test_failed")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate", "verifier"])

    def test_run_forever_executes_leased_run_then_polls_idle_and_stops(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        run_id, _ = self.store.create_run(**RUN_PARAMS)
        checks = {"count": 0}

        def should_stop_run() -> bool:
            checks["count"] += 1
            return checks["count"] > 1

        Worker(self.store, runner,
               http_client=FakeInternalClient([selection_response()])).run_forever(should_stop_run)
        self.assertEqual(self.store.count_state("completed"), 1)
        self.assertEqual(runner.calls[0][0], "preparation")
        self.assertEqual(runner.calls[0][1], 300.0)

        sleeps = []
        idle_checks = {"count": 0}

        def should_stop_idle() -> bool:
            idle_checks["count"] += 1
            return idle_checks["count"] > 1

        idle = Worker(self.store, FakeRunner(preparation_result(), []),
                      sleep=sleeps.append, poll_seconds=0.25)
        idle.run_forever(should_stop_idle)
        self.assertEqual(sleeps, [0.25])

    def test_cancel_during_candidate_returns_cancelled_terminal(self):
        run_id, _ = self.store.create_run(**RUN_PARAMS)
        run = self.store.lease_next_run("test-worker", 1000.0)
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", status="cancelled")],
            hooks={"candidate": lambda: self.store.request_cancel(run_id)})
        Worker(self.store, runner,
               http_client=FakeInternalClient([selection_response()])).execute_run(run)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["status_detail"], "cancelled by request")
        self.assertIsNotNone(record["terminal_utc"])
        self.assertEqual(
            [attempt["phase"] for attempt in self.store.attempts(run_id)],
            ["preparation", "baseline", "candidate"])
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate"])
        # The cancel flag only flipped during the candidate call, so the
        # baseline callback must have observed False and the candidate
        # callback must have observed True through the live store check.
        self.assertEqual(runner.cancel_checks, [("baseline", False), ("candidate", True)])

    def test_job_deadline_exceeded_times_out(self):
        run_id, _ = self.store.create_run(**RUN_PARAMS)
        run = self.store.lease_next_run("test-worker", 1000.0)
        runner = FakeRunner(preparation_result(), [])
        Worker(self.store, runner, job_deadline_seconds=0.0).execute_run(run)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "timed_out")
        self.assertEqual(record["status_detail"], "job deadline exceeded")
        self.assertIsNotNone(record["terminal_utc"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.artifact_names(run_id), set())

    def test_osv_unavailable_snapshot_is_persisted_and_run_completes(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        run_id = self.execute(runner, osv_query=unavailable_osv)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "completed")
        self.assertIsNotNone(record["terminal_utc"])
        for name in ("advisory-baseline.json", "advisory-target.json"):
            advisory = json.loads(self.store.get_artifact(run_id, name))
            self.assertEqual(advisory["status"], "unavailable")
            self.assertTrue(advisory["error"])
        manifest = json.loads(self.store.get_artifact(run_id, "manifest.json"))
        self.assertEqual(manifest["advisories"]["baseline"]["status"], "unavailable")
        self.assertEqual(manifest["advisories"]["target"]["status"], "unavailable")
        self.assertEqual(manifest["advisories"]["baseline"]["vulnerability_ids"], [])
        self.assertEqual(manifest["advisories"]["target"]["vulnerability_ids"], [])

    def test_preparation_infrastructure_failure_is_terminal(self):
        runner = FakeRunner(
            preparation_result("infrastructure_failed", error="Registry unreachable"), [])
        run_id = self.execute(runner)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "infrastructure_failed")
        self.assertIn("Registry unreachable", record["status_detail"])
        self.assertIsNotNone(record["terminal_utc"])
        self.assertEqual([call for call in runner.calls if call[0] != "preparation"], [])
        self.assertEqual(self.artifact_names(run_id), {"preparation.json"})
        events = self.store.events_after(run_id, 0)
        self.assertFalse([event for event in events if event["kind"] == "selection"])

    def test_verifier_pip_check_failure_rejects(self):
        verifier = attempt_result("candidate", installed="2.32.2")
        verifier.marker["steps"]["pip_check"]["exit_code"] = 1
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             verifier])
        run_id = self.execute(runner)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(record["status_detail"], "verifier pip check failed")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate", "verifier"])

    def test_verifier_new_skips_rejects(self):
        verifier = attempt_result("candidate", installed="2.32.2")
        verifier.marker["counts"] = {"passed": 4, "failed": 0, "errors": 0,
                                     "skipped": 1, "xfailed": 0, "xpassed": 0}
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             verifier])
        run_id = self.execute(runner)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "upgrade_failed")
        self.assertEqual(record["status_detail"], "verifier reported new skips or xfails")
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate", "verifier"])

    def test_run_forever_marks_stale_lease_and_idles(self):
        run_id, _ = self.store.create_run(**RUN_PARAMS)
        # The store exposes no direct lease-column write, so the lease is
        # granted for negative seconds, which makes lease_expires_utc already
        # expired; set_run_state then leaves the run mid-flight, non-terminal.
        self.store.lease_next_run("lost-worker", -1.0)
        self.store.set_run_state(run_id, "preparing")
        runner = FakeRunner(preparation_result(), [])
        sleeps = []
        passes = {"count": 0}

        def should_stop() -> bool:
            passes["count"] += 1
            return passes["count"] > 1

        Worker(self.store, runner, sleep=sleeps.append, poll_seconds=0.25).run_forever(should_stop)

        record = self.store.get_run(run_id)
        self.assertEqual(record["state"], "infrastructure_failed")
        self.assertEqual(record["status_detail"], "worker interrupted before completion")
        self.assertEqual(runner.calls, [])
        self.assertEqual(sleeps, [0.25])

    def test_events_have_increasing_ids_and_states_precede_events(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        run_id = self.execute(runner)

        events = self.store.events_after(run_id, 0)
        ids = [event["id"] for event in events]
        self.assertTrue(ids)
        self.assertTrue(all(later > earlier for earlier, later in zip(ids, ids[1:])))
        terminal = [event for event in events if event["kind"] == "terminal"][-1]
        self.assertEqual(terminal["data"]["state"], "completed")
        self.assertEqual(self.store.get_run(run_id)["state"], terminal["data"]["state"])


if __name__ == "__main__":
    unittest.main()
