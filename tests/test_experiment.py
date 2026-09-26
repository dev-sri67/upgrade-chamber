"""The operator sequence records fixed evidence and stops at failed gates."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.run_experiment import run_experiment
from upgrade_chamber.runner import AttemptResult, PreparationResult, ProbeResult


IMAGE = "sha256:" + "a" * 64


def probe(kind, status):
    return ProbeResult(kind, status, f"container-{kind}", 0.5, None, "out", "", False,
                       False, {"kind": "success", "nonce": "fixed"} if kind == "success" else None,
                       None, True, 5.0)


def preparation(status="prepared"):
    artifacts = {
        "preparation.json": b'{"status":"prepared"}',
        "baseline.tar": b"baseline input",
        "candidate.tar": b"candidate input",
        "baseline-metadata.json": b'{"source_sha256":"source"}',
        "candidate-metadata.json": b'{"source_sha256":"source"}',
    } if status == "prepared" else {"preparation.json": b'{"status":"failed"}'}
    return PreparationResult(status, "container-preparation", 1.0, 0 if status == "prepared" else 1,
                             {"status": status}, artifacts, [], "", "", False, False, None,
                             True, 300.0, IMAGE)


def attempt(phase, status="passed"):
    marker = {
        "phase": phase,
        "status": status,
        "source_sha256": "b" * 64,
        "installed_requests_version": "2.31.0" if phase == "baseline" else "2.34.2",
        "collected_test_ids": ["requests_unixsocket/tests/test_case.py::test_case"],
        "counts": {"passed": 1 if status == "passed" else 0, "failed": int(status != "passed")},
    }
    artifacts = {"attempt.json": json.dumps(marker).encode(), "installed.json": b'{"requests":"2.31.0"}'}
    return AttemptResult(phase, status, f"container-{phase}", 2.0, 0, marker, artifacts,
                         ["junit.xml"] if status != "passed" else [], "", "", False,
                         False, None, True, 300.0, IMAGE)


class FakeRunner:
    def __init__(self, *, timeout_status="timed_out", success_status="completed",
                 preparation_status="prepared", baseline_status="passed", candidate_status="test_failed",
                 cleanup_error=None, timeout_error=None):
        self.calls = []
        self.timeout_status = timeout_status
        self.success_status = success_status
        self.preparation_status = preparation_status
        self.baseline_status = baseline_status
        self.candidate_status = candidate_status
        self.cleanup_error = cleanup_error
        self.timeout_error = timeout_error

    def remove_expired_containers(self):
        self.calls.append("cleanup")
        if self.cleanup_error:
            raise self.cleanup_error
        return ["expired-container"]

    def run_probe(self, kind, *, timeout_seconds):
        self.calls.append(kind)
        assert timeout_seconds == 5.0
        result = probe(kind, self.timeout_status if kind == "infinite_loop" else self.success_status)
        return replace(result, error=self.timeout_error) if kind == "infinite_loop" else result

    def run_preparation(self, *, timeout_seconds):
        self.calls.append("preparation")
        assert timeout_seconds == 300.0
        return preparation(self.preparation_status)

    def run_profile_attempt(self, input_tar, *, phase, timeout_seconds):
        self.calls.append(phase)
        assert timeout_seconds == 300.0
        assert input_tar == f"{phase} input".encode()
        return attempt(phase, self.baseline_status if phase == "baseline" else self.candidate_status)


def load(output: Path):
    return json.loads((output / "summary.json").read_text(encoding="utf-8"))


def test_records_all_steps_and_failed_candidate_as_result(tmp_path):
    runner = FakeRunner()
    output = tmp_path / "evidence"
    assert run_experiment(IMAGE, output, runner=runner) == 0
    assert runner.calls == ["cleanup", "infinite_loop", "success", "preparation", "baseline", "candidate"]
    summary = load(output)
    assert summary["status"] == "recorded"
    assert summary["image_identity"] == IMAGE
    assert summary["orphan_cleanup"] == ["expired-container"]
    assert summary["steps"]["timeout_probe"]["container_id"] == "container-infinite_loop"
    assert summary["steps"]["candidate"]["status"] == "test_failed"
    assert summary["steps"]["candidate"]["marker"]["collected_test_ids"]
    assert summary["steps"]["baseline"]["marker"]["installed_requests_version"] == "2.31.0"
    assert (output / "preparation" / "baseline.tar").read_bytes() == b"baseline input"
    assert (output / "candidate" / "attempt.json").is_file()
    assert summary["steps"]["candidate"]["artifacts"]["attempt.json"]["sha256"]


@pytest.mark.parametrize(("settings", "expected_calls"), [
    ({"timeout_status": "infrastructure_failed"}, ["cleanup", "infinite_loop"]),
    ({"timeout_error": "Kill failed"}, ["cleanup", "infinite_loop"]),
    ({"success_status": "infrastructure_failed"}, ["cleanup", "infinite_loop", "success"]),
    ({"preparation_status": "failed"}, ["cleanup", "infinite_loop", "success", "preparation"]),
    ({"baseline_status": "test_failed"}, ["cleanup", "infinite_loop", "success", "preparation", "baseline"]),
])
def test_failed_gate_records_result_and_blocks_later_steps(tmp_path, settings, expected_calls):
    runner = FakeRunner(**settings)
    output = tmp_path / "evidence"
    assert run_experiment(IMAGE, output, runner=runner) == 1
    assert runner.calls == expected_calls
    summary = load(output)
    assert summary["status"] == "stopped"
    assert summary["error"]
    assert len(summary["steps"]) == len(expected_calls) - 1


def test_cleanup_exception_is_recorded_and_blocks_probes(tmp_path):
    runner = FakeRunner(cleanup_error=RuntimeError("removal not observed"))
    output = tmp_path / "evidence"
    assert run_experiment(IMAGE, output, runner=runner) == 1
    assert runner.calls == ["cleanup"]
    assert "removal not observed" in load(output)["error"]


def test_existing_evidence_directory_is_never_reused(tmp_path):
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "summary.json").write_text("previous evidence", encoding="utf-8")
    runner = FakeRunner()
    with pytest.raises(FileExistsError):
        run_experiment(IMAGE, output, runner=runner)
    assert (output / "summary.json").read_text(encoding="utf-8") == "previous evidence"
    assert runner.calls == []
