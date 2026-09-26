"""Policy and lifecycle checks with an explicitly fake Docker client."""

import io
import json
import tarfile
import unittest

from upgrade_chamber.runner import CleanupError, DockerRunner, MAX_LOG_BYTES, _validate_attempt_archive


IMAGE = "python-runner@sha256:" + "a" * 64
LOCAL_IMAGE = "sha256:" + "b" * 64


class NotFound(Exception):
    pass


def archive_file(name: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def attempt_bundle() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in (
            ("source.zip", b"zip"),
            ("requirements.txt", b"requests==2.31.0"),
            ("manifest.json", b"{}"),
            ("wheels/requests.whl", b"wheel"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def attempt_marker(status: str = "passed") -> dict:
    steps = {name: {"exit_code": 0, "timed_out": False} for name in ("install", "collect", "test", "pip_check")}
    if status != "passed":
        steps["install"]["exit_code"] = 1
        for name in ("collect", "test", "pip_check"):
            steps[name]["exit_code"] = None
    return {
        "schema_version": 1,
        "phase": "baseline",
        "status": status,
        "steps": steps,
        "collected_test_ids": ["tests/test_example.py::test_example"] if status == "passed" else [],
        "counts": {"passed": 1 if status == "passed" else 0, "failed": 0, "errors": 0,
                   "skipped": 0, "xfailed": 0, "xpassed": 0},
        "installed_requests_version": "2.31.0" if status == "passed" else None,
        "source_sha256": "a" * 64,
        "error": None if status == "passed" else "Installation failed",
    }


class FakeContainer:
    def __init__(self, kind: str, *, stage_fails: bool = False, remove_fails: bool = False, flood: bool = False):
        self.id = "fake-1"
        self.kind = kind
        self.stage_fails = stage_fails
        self.remove_fails = remove_fails
        self.flood = flood
        self.labels = {}
        self.started = False
        self.killed = False
        self.removed = False
        self.nonce = None
        self.acknowledged = False
        self.export_read_while_running = False
        self.attrs = {"State": {"Running": True, "ExitCode": None}}

    def start(self):
        self.started = True

    def put_archive(self, path, data):
        if self.stage_fails:
            return False
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            member = archive.getmembers()[0]
            if member.name == "input.json":
                self.nonce = json.load(archive.extractfile(member))["nonce"]
            if member.name == "ack":
                self.acknowledged = True
        return True

    def reload(self):
        if self.kind == "success" and self.acknowledged:
            self.attrs = {"State": {"Running": False, "ExitCode": 0}}

    def kill(self):
        self.killed = True
        self.attrs = {"State": {"Running": False, "ExitCode": 137}}

    def logs(self, *, stdout, stderr, stream):
        assert stream
        if self.flood and stdout:
            return iter([b"a" * (MAX_LOG_BYTES + 100)])
        return iter([b"fake output"] if stdout else [])

    def get_archive(self, path):
        assert path == "/work/export/probe.json"
        self.export_read_while_running = self.attrs["State"]["Running"]
        return iter([archive_file("probe.json", json.dumps({"kind": "success", "nonce": self.nonce}).encode())]), {}

    def remove(self, *, force):
        assert force
        if self.remove_fails:
            raise RuntimeError("fake removal failed")
        self.removed = True


class FakeContainers:
    def __init__(self, container):
        self.container = container
        self.options = None

    def create(self, **options):
        self.options = options
        self.container.labels = options["labels"]
        return self.container

    def get(self, ident):
        if self.container.removed:
            raise NotFound()
        return self.container

    def list(self, *, all, filters):
        assert all
        assert filters == {"label": "upgrade-chamber.owner=execution"}
        return [self.container]


class FakeDocker:
    def __init__(self, container):
        self.containers = FakeContainers(container)


class FakeAttemptContainer(FakeContainer):
    def __init__(self, marker: dict, files: dict[str, bytes]):
        super().__init__("success")
        self.files = {"attempt.json": json.dumps(marker).encode(), **files}

    def get_archive(self, path):
        name = path.rsplit("/", 1)[-1]
        if name not in self.files:
            raise NotFound()
        self.export_read_while_running = self.attrs["State"]["Running"]
        return iter([archive_file(name, self.files[name])]), {}


class FakePreparationContainer(FakeAttemptContainer):
    def __init__(self, status: str = "prepared", files: dict[str, bytes] | None = None):
        FakeContainer.__init__(self, "success")
        self.preparation_status = status
        marker = {"schema_version": 1, "status": status, "error": None if status == "prepared" else "Download failed",
                  "summary": {} if status == "prepared" else None}
        self.files = {"preparation.json": json.dumps(marker).encode(), **(files or {})}

    def reload(self):
        if self.acknowledged:
            self.attrs = {"State": {"Running": False, "ExitCode": 0 if self.preparation_status == "prepared" else 1}}


class RunnerTests(unittest.TestCase):
    def test_attempt_archive_accepts_only_bounded_regular_inputs(self):
        def bundle(*entries):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                for name, content, kind in entries:
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    if kind == "symlink":
                        info.type = tarfile.SYMTYPE
                        info.linkname = "source.zip"
                    archive.addfile(info, io.BytesIO(content) if kind == "file" else None)
            return output.getvalue()

        required = (("source.zip", b"zip", "file"), ("requirements.txt", b"requests==2.31.0", "file"),
                    ("manifest.json", b"{}", "file"), ("wheels/requests.whl", b"wheel", "file"))
        _validate_attempt_archive(bundle(*required))
        for bad in (
            bundle(*required, ("../escape", b"x", "file")),
            bundle(*required, ("wheels/evil.whl", b"", "symlink")),
            bundle(*required, required[0]),
            bundle(*required[:-1]),
            bundle(*required, ("manifest.json", b"x", "file")),
        ):
            with self.assertRaises(ValueError):
                _validate_attempt_archive(bad)
        with self.assertRaises(ValueError):
            _validate_attempt_archive(bundle(("source.zip", b"x" * (10 * 1024 * 1024 + 1), "file"),
                                             *required[1:]))

    def test_rejects_unpinned_image_and_unknown_probe(self):
        with self.assertRaises(ValueError):
            DockerRunner("python-runner:latest", FakeDocker(FakeContainer("success")))
        with self.assertRaises(ValueError):
            DockerRunner("sha256:short", FakeDocker(FakeContainer("success")))
        self.assertEqual(DockerRunner(LOCAL_IMAGE, FakeDocker(FakeContainer("success"))).image, LOCAL_IMAGE)
        runner = DockerRunner(IMAGE, FakeDocker(FakeContainer("success")))
        with self.assertRaises(ValueError):
            runner.run_probe("python -c 'anything'")
        with self.assertRaises(ValueError):
            runner.run_probe("success", timeout_seconds=301)

    def test_profile_attempt_preserves_passed_evidence_and_cleanup(self):
        files = {name: b"evidence" for name in
                 ("collect.txt", "junit.xml", "pip-report.json", "pip-check.txt", "installed.json")}
        container = FakeAttemptContainer(attempt_marker(), files)
        docker = FakeDocker(container)
        result = DockerRunner(LOCAL_IMAGE, docker).run_profile_attempt(attempt_bundle(), phase="baseline")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.marker["counts"]["passed"], 1)
        self.assertTrue(result.removal_observed)
        self.assertTrue(container.export_read_while_running)
        self.assertEqual(result.image_identity, LOCAL_IMAGE)
        self.assertEqual(docker.containers.options["command"],
                         ["python", "-I", "/opt/upgrade_chamber/attempt.py", "baseline"])
        self.assertEqual(docker.containers.options["network_mode"], "none")
        self.assertEqual(result.missing_artifacts, ["install.log", "test.log"])

    def test_profile_attempt_keeps_partial_install_failure(self):
        container = FakeAttemptContainer(attempt_marker("install_failed"), {"install.log": b"pip failed"})
        result = DockerRunner(IMAGE, FakeDocker(container)).run_profile_attempt(attempt_bundle(), phase="baseline")
        self.assertEqual(result.status, "install_failed")
        self.assertEqual(result.artifacts["install.log"], b"pip failed")
        self.assertIn("junit.xml", result.missing_artifacts)
        self.assertTrue(result.removal_observed)

    def test_profile_attempt_rejects_false_pass_and_invalid_input_before_create(self):
        marker = attempt_marker()
        marker["collected_test_ids"] = []
        container = FakeAttemptContainer(marker, {})
        result = DockerRunner(IMAGE, FakeDocker(container)).run_profile_attempt(attempt_bundle(), phase="baseline")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertTrue(result.removal_observed)
        runner = DockerRunner(IMAGE, FakeDocker(FakeContainer("success")))
        with self.assertRaises(ValueError):
            runner.run_profile_attempt(b"not a tar", phase="baseline")
        with self.assertRaises(ValueError):
            runner.run_profile_attempt(attempt_bundle(), phase="other")

    def test_preparation_exports_two_valid_bundles_and_removes_container(self):
        files = {"baseline.tar": attempt_bundle(), "candidate.tar": attempt_bundle(),
                 "baseline-metadata.json": b"{}", "candidate-metadata.json": b"{}"}
        container = FakePreparationContainer(files=files)
        docker = FakeDocker(container)
        result = DockerRunner(LOCAL_IMAGE, docker).run_preparation()
        self.assertEqual(result.status, "prepared")
        self.assertTrue(result.removal_observed)
        self.assertEqual(result.missing_artifacts, [])
        self.assertEqual(docker.containers.options["network_mode"], "bridge")
        self.assertEqual(docker.containers.options["environment"], {"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(docker.containers.options["command"],
                         ["python", "-I", "/opt/upgrade_chamber/prepare_baseline.py", "--handshake"])

    def test_preparation_failed_marker_preserves_missing_exports(self):
        container = FakePreparationContainer(status="failed")
        result = DockerRunner(IMAGE, FakeDocker(container)).run_preparation()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("baseline.tar", result.missing_artifacts)
        self.assertTrue(result.removal_observed)

    def test_preparation_rejects_false_prepared_bundle(self):
        container = FakePreparationContainer(files={"baseline.tar": attempt_bundle()})
        result = DockerRunner(IMAGE, FakeDocker(container)).run_preparation()
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertTrue(result.removal_observed)

    def test_success_policy_and_removal(self):
        container = FakeContainer("success")
        docker = FakeDocker(container)
        result = DockerRunner(IMAGE, docker).run_probe("success")
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.removal_observed)
        self.assertEqual(result.export["kind"], "success")
        self.assertEqual(result.export["nonce"], container.nonce)
        self.assertTrue(container.export_read_while_running)
        self.assertTrue(container.acknowledged)
        options = docker.containers.options
        self.assertEqual(options["network_mode"], "none")
        self.assertEqual(options["user"], "10001:10001")
        self.assertTrue(options["read_only"])
        self.assertFalse(options["privileged"])
        self.assertEqual(options["cap_drop"], ["ALL"])
        self.assertEqual(options["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(options["mem_limit"], "1g")
        self.assertEqual(options["memswap_limit"], "1g")
        self.assertEqual(options["nano_cpus"], 1_000_000_000)
        self.assertEqual(options["pids_limit"], 128)
        self.assertEqual(options["mounts"], [])
        self.assertEqual(options["volumes"], {})
        self.assertEqual(options["devices"], [])
        self.assertIn("size=536870912", options["tmpfs"]["/work"])

    def test_timeout_kills_and_follow_up_succeeds(self):
        loop = FakeContainer("infinite_loop")
        timed_out = DockerRunner(IMAGE, FakeDocker(loop)).run_probe("infinite_loop", timeout_seconds=0.01)
        self.assertEqual(timed_out.status, "timed_out")
        self.assertTrue(loop.killed)
        self.assertTrue(timed_out.removal_observed)
        following = DockerRunner(IMAGE, FakeDocker(FakeContainer("success"))).run_probe("success")
        self.assertEqual(following.status, "completed")

    def test_staging_failure_still_removes_container(self):
        container = FakeContainer("success", stage_fails=True)
        result = DockerRunner(IMAGE, FakeDocker(container)).run_probe("success")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertTrue(result.removal_observed)
        self.assertIn("Input staging failed", result.error)

    def test_cleanup_failure_blocks_result(self):
        container = FakeContainer("success", remove_fails=True)
        runner = DockerRunner(IMAGE, FakeDocker(container))
        with self.assertRaises(CleanupError):
            runner.run_probe("success")
        with self.assertRaises(CleanupError):
            runner.run_probe("success")

    def test_logs_are_bounded_and_marked(self):
        result = DockerRunner(IMAGE, FakeDocker(FakeContainer("success", flood=True))).run_probe("success")
        self.assertEqual(len(result.stdout), MAX_LOG_BYTES)
        self.assertTrue(result.stdout_truncated)

    def test_expired_orphan_removed_only_after_deadline(self):
        container = FakeContainer("infinite_loop")
        container.labels = {"upgrade-chamber.owner": "execution", "upgrade-chamber.deadline": "10"}
        runner = DockerRunner(IMAGE, FakeDocker(container))
        self.assertEqual(runner.remove_expired_containers(now=10), [])
        self.assertFalse(container.removed)
        self.assertEqual(runner.remove_expired_containers(now=11), [container.id])
        self.assertTrue(container.removed)

    def test_unowned_container_is_not_removed_even_if_listed(self):
        container = FakeContainer("infinite_loop")
        container.labels = {"upgrade-chamber.owner": "another-service", "upgrade-chamber.deadline": "10"}
        runner = DockerRunner(IMAGE, FakeDocker(container))
        self.assertEqual(runner.remove_expired_containers(now=11), [])
        self.assertFalse(container.removed)

    def test_export_rejects_extra_tar_entry_and_removes_container(self):
        class BadExportContainer(FakeContainer):
            def get_archive(self, path):
                self.export_read_while_running = self.attrs["State"]["Running"]
                return iter([archive_file("unexpected.json", b"{}")]), {}

        container = BadExportContainer("success")
        result = DockerRunner(IMAGE, FakeDocker(container)).run_probe("success")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertIn("Unexpected export entry", result.error)
        self.assertTrue(result.removal_observed)


if __name__ == "__main__":
    unittest.main()
