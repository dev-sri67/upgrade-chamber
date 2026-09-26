"""Worker phase-machine checks against a real store and a programmable fake runner."""

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from upgrade_chamber.runner import AttemptResult, PreparationResult
from upgrade_chamber.storage import Store
from upgrade_chamber.worker import Worker


IMAGE = "python-runner@sha256:" + "b" * 64
BASELINE_IDS = [f"tests/test_example.py::test_{index}" for index in range(5)]
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
    "source_sha256": "c" * 64,
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


def preparation_result(status: str = "prepared", *, error=None) -> PreparationResult:
    """Build one PreparationResult carrying both pinned offline bundles."""
    marker = {
        "schema_version": 1,
        "status": status,
        "error": None if status == "prepared" else (error or "Download failed"),
        "summary": {} if status == "prepared" else None,
    }
    artifacts = {"preparation.json": json.dumps(marker).encode()}
    if status == "prepared":
        artifacts.update({
            "baseline-metadata.json": b"{}",
            "candidate-metadata.json": b"{}",
            "baseline.tar": requirements_bundle(b"requests==2.31.0\n"),
            "candidate.tar": requirements_bundle(b"requests==2.32.2\n"),
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
        self.calls: list[tuple[str, float]] = []
        self.cancel_checks: list[tuple[str, bool]] = []

    def run_preparation(self, *, timeout_seconds=300.0, should_cancel=None) -> PreparationResult:
        self.calls.append(("preparation", timeout_seconds))
        return self._preparation

    def run_profile_attempt(self, input_tar: bytes, *, phase: str,
                            timeout_seconds=300.0, should_cancel=None) -> AttemptResult:
        self.calls.append((phase, timeout_seconds))
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

    def execute(self, runner: FakeRunner, osv_query=fake_osv) -> int:
        """Create, lease, and execute one run; return its id."""
        run_id, _ = self.store.create_run(**RUN_PARAMS)
        run = self.store.lease_next_run("test-worker", 1000.0)
        Worker(self.store, runner, osv_query=osv_query).execute_run(run)
        return run_id

    def artifact_names(self, run_id: int) -> set[str]:
        return {row["name"] for row in self.store.list_artifacts(run_id)}

    def test_happy_path_completes_with_full_evidence(self):
        queries = []

        def counting_osv(package, version):
            queries.append((package, version))
            return fake_osv(package, version)

        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", installed="2.32.2"),
             attempt_result("candidate", installed="2.32.2")])
        run_id = self.execute(runner, osv_query=counting_osv)

        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "completed")
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

        manifest = json.loads(self.store.get_artifact(run_id, "manifest.json"))
        self.assertEqual(manifest["result"]["state"], "completed")
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

        advisory = json.loads(self.store.get_artifact(run_id, "advisory-baseline.json"))
        self.assertEqual(advisory["status"], "available")
        self.assertEqual(queries, [("requests", "2.31.0"), ("requests", "2.32.2")])

        events = self.store.events_after(run_id, 0)
        self.assertEqual({event["kind"] for event in events},
                         {"state", "attempt", "artifact", "selection", "advisory", "terminal"})
        selection = [event for event in events if event["kind"] == "selection"]
        self.assertEqual(selection[0]["data"]["source"], "profile")
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

    def test_candidate_test_failure_produces_evidence_without_verifier(self):
        runner = FakeRunner(
            preparation_result(),
            [attempt_result("baseline", installed="2.31.0"),
             attempt_result("candidate", status="test_failed", ids=list(BASELINE_IDS),
                            error="1 test failed", complete=False)])
        run_id = self.execute(runner)

        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "upgrade_failed")
        self.assertIn("test_failed", run["status_detail"])
        phases = [attempt["phase"] for attempt in self.store.attempts(run_id)]
        self.assertEqual(phases, ["preparation", "baseline", "candidate"])
        self.assertEqual([call[0] for call in runner.calls],
                         ["preparation", "baseline", "candidate"])
        names = self.artifact_names(run_id)
        self.assertLessEqual({"patch.diff", "comparison.json", "manifest.json"}, names)
        comparison = json.loads(self.store.get_artifact(run_id, "comparison.json"))
        self.assertEqual(comparison["candidate"]["status"], "test_failed")
        self.assertIsNone(comparison["verifier"])
        self.assertTrue(comparison["collected_ids_match"])

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

        Worker(self.store, runner).run_forever(should_stop_run)
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
        Worker(self.store, runner).execute_run(run)

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
