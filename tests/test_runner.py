"""Policy and lifecycle checks with an explicitly fake Docker client."""

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

from upgrade_chamber.runner import (
    MAX_EXPORT_BYTES,
    MAX_LOG_BYTES,
    CleanupError,
    DockerRunner,
    _EXTRACT_SCRIPT,
    _READ_SCRIPT,
    _WRITE_SCRIPT,
    _validate_attempt_archive,
)


IMAGE = "python-runner@sha256:" + "a" * 64
LOCAL_IMAGE = "sha256:" + "b" * 64


class NotFound(Exception):
    pass


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


class FakeExecRecord:
    """One interpreted exec: the fake parses the fixed argv contract."""

    def __init__(self, exec_id: str, cmd: list, container: "FakeContainer"):
        self.id = exec_id
        self.cmd = cmd
        self.container = container
        self.running = True
        self.exit_code = None
        self.chunks: list[bytes] = []
        self.buffer = bytearray()
        self.timeouts: list[float] = []
        self.hangs = False

    def finalize_write(self) -> None:
        """Interpret a framed write once the 8-byte prefix and payload arrived."""
        if self.hangs or self.exit_code is not None:
            return
        if len(self.buffer) < 8:
            return
        size = int.from_bytes(self.buffer[:8], "little")
        cap = int(self.cmd[5])
        if size > cap:
            self.exit_code = 2
            self.running = False
            return
        if len(self.buffer) < 8 + size:
            return
        payload = bytes(self.buffer[8:8 + size])
        path = self.cmd[4]
        if path in self.container.files:
            self.exit_code = 5
        else:
            self.container.files[path] = payload
            if path == "/work/input.json":
                self.container.nonce = json.loads(payload)["nonce"]
            if path == "/work/ack":
                self.container.acknowledged = True
            self.container.events.append(f"write:{path}")
            self.exit_code = 0
        self.running = False


class FakeRawSocket:
    def __init__(self, record: FakeExecRecord):
        self.record = record
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.record.timeouts.append(value)
        self.timeout = value

    def sendall(self, data):
        assert not self.closed
        assert self.record.cmd[3] is _WRITE_SCRIPT
        self.record.buffer.extend(data)
        self.record.finalize_write()

    def recv(self, size):
        if self.record.chunks:
            return self.record.chunks.pop(0)
        return b""

    def close(self):
        self.closed = True


class FakeSocketIO:
    def __init__(self, record: FakeExecRecord):
        self._sock = FakeRawSocket(record)

    def close(self):
        self._sock.close()


class FakeExecAPI:
    def __init__(self, container: "FakeContainer"):
        self.container = container
        self.execs: dict[str, FakeExecRecord] = {}
        self.counter = 0

    def exec_create(self, container, cmd, stdout=True, stderr=True, stdin=False, tty=False, **_):
        assert container == self.container.id
        assert not tty
        script = cmd[3]
        if script is _WRITE_SCRIPT:
            assert stdin
        else:
            assert script is _READ_SCRIPT or script is _EXTRACT_SCRIPT
            assert not stdin
        self.counter += 1
        record = FakeExecRecord(f"exec-{self.counter}", list(cmd), self.container)
        if script is _WRITE_SCRIPT:
            record.hangs = self.container.hang_execs
        elif script is _READ_SCRIPT:
            self._finalize_read(record)
        else:
            self._finalize_extract(record)
        self.execs[record.id] = record
        return {"Id": record.id}

    def exec_start(self, exec_id, detach=False, tty=False, stream=False, socket=False, demux=False):
        assert socket and not detach and not stream and not demux and not tty
        return FakeSocketIO(self.execs[exec_id])

    def exec_inspect(self, exec_id):
        record = self.execs[exec_id]
        return {"Running": record.running, "ExitCode": record.exit_code}

    def _finalize_read(self, record: FakeExecRecord) -> None:
        container = self.container
        path, limit = record.cmd[4], int(record.cmd[5])
        if container.attrs["State"]["Running"]:
            container.export_read_while_running = True
        if (container.kind == "success" and path == "/work/export/probe.json"
                and path not in container.files and path not in container.symlinks
                and container.nonce is not None):
            container.files[path] = json.dumps({"kind": "success", "nonce": container.nonce}).encode()
        if path in container.symlinks:
            record.exit_code = 6
        elif path not in container.files:
            record.exit_code = 3
        elif len(container.files[path]) > limit:
            record.exit_code = 4
        else:
            record.chunks = [container.files[path]]
            record.exit_code = 0
        record.running = False

    def _finalize_extract(self, record: FakeExecRecord) -> None:
        container = self.container
        archive_path = record.cmd[4]
        data = container.files.get(archive_path)
        if data is None:
            record.exit_code = 1
        else:
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                    for member in archive.getmembers():
                        name = member.name
                        if not member.isfile() or name.startswith("/") or ".." in name.split("/"):
                            raise ValueError("unsafe tar member")
                        container.files[f"/work/{name}"] = archive.extractfile(member).read()
            except (tarfile.TarError, ValueError):
                record.exit_code = 1
            else:
                container.files.pop(archive_path, None)
                container.events.append(f"extract:{archive_path}")
                record.exit_code = 0
        record.running = False


class FakeContainer:
    def __init__(self, kind: str, *, remove_fails: bool = False, flood: bool = False,
                 hang_execs: bool = False, preset_files: dict | None = None,
                 preset_symlinks: set | None = None):
        self.id = "fake-1"
        self.kind = kind
        self.remove_fails = remove_fails
        self.flood = flood
        self.hang_execs = hang_execs
        self.labels = {}
        self.started = False
        self.killed = False
        self.removed = False
        self.nonce = None
        self.acknowledged = False
        self.export_read_while_running = False
        self.files = dict(preset_files or {})
        self.symlinks = set(preset_symlinks or ())
        self.events: list[str] = []
        self.attrs = {"State": {"Running": True, "ExitCode": None}}

    def start(self):
        self.started = True

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
        self.api = FakeExecAPI(container)


class FakeAttemptContainer(FakeContainer):
    def __init__(self, marker: dict, files: dict[str, bytes]):
        super().__init__("success")
        self.files.update({f"/work/export/{name}": data for name, data in files.items()})
        self.files["/work/export/attempt.json"] = json.dumps(marker).encode()


class FakePreparationContainer(FakeContainer):
    def __init__(self, status: str = "prepared", files: dict[str, bytes] | None = None):
        super().__init__("success")
        self.preparation_status = status
        marker = {"schema_version": 1, "status": status, "error": None if status == "prepared" else "Download failed",
                  "summary": {} if status == "prepared" else None}
        self.files.update({f"/work/export/{name}": data for name, data in (files or {}).items()})
        self.files["/work/export/preparation.json"] = json.dumps(marker).encode()

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

    def test_overwrite_rejection_still_removes_container(self):
        container = FakeContainer("success", preset_files={"/work/input.json": b"stale"})
        result = DockerRunner(IMAGE, FakeDocker(container)).run_probe("success")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertIn("Overwrite rejected", result.error)
        self.assertTrue(result.removal_observed)

    def test_ready_overwrite_rejected_in_attempt(self):
        container = FakeAttemptContainer(attempt_marker(), {})
        container.files["/work/ready"] = b"stale"
        result = DockerRunner(IMAGE, FakeDocker(container)).run_profile_attempt(attempt_bundle(), phase="baseline")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertIn("Overwrite rejected", result.error)
        self.assertTrue(result.removal_observed)

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

    def test_symlinked_export_rejected_and_removed(self):
        container = FakeContainer("success", preset_symlinks={"/work/export/probe.json"})
        result = DockerRunner(IMAGE, FakeDocker(container)).run_probe("success")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertIn("not a regular file or is a symlink", result.error)
        self.assertTrue(result.removal_observed)

    def test_oversize_export_rejected_and_removed(self):
        container = FakeContainer("success", preset_files={"/work/export/probe.json": b"x" * (MAX_EXPORT_BYTES + 1)})
        result = DockerRunner(IMAGE, FakeDocker(container)).run_probe("success")
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertIn("exceeds byte limit", result.error)
        self.assertTrue(result.removal_observed)

    def test_attempt_input_lands_as_work_files_before_ready(self):
        files = {name: b"evidence" for name in
                 ("collect.txt", "junit.xml", "pip-report.json", "pip-check.txt", "installed.json")}
        container = FakeAttemptContainer(attempt_marker(), files)
        result = DockerRunner(IMAGE, FakeDocker(container)).run_profile_attempt(attempt_bundle(), phase="baseline")
        self.assertEqual(result.status, "passed")
        for name in ("/work/source.zip", "/work/requirements.txt", "/work/manifest.json", "/work/wheels/requests.whl"):
            self.assertIn(name, container.files)
        self.assertNotIn("/work/input.tar", container.files)
        self.assertIn("/work/ready", container.files)
        self.assertIn("/work/ack", container.files)
        staged = container.events.index("write:/work/input.tar")
        extracted = container.events.index("extract:/work/input.tar")
        ready = container.events.index("write:/work/ready")
        self.assertLess(staged, extracted)
        self.assertLess(extracted, ready)

    def test_exec_deadline_marks_timeout_kills_and_removes(self):
        container = FakeContainer("success", hang_execs=True)
        result = DockerRunner(IMAGE, FakeDocker(container)).run_probe("success", timeout_seconds=0.3)
        self.assertEqual(result.status, "timed_out")
        self.assertTrue(container.killed)
        self.assertTrue(result.removal_observed)
        self.assertIn("DeadlineExceeded", result.error)

    def test_write_script_frames_payload_and_rejects_overwrite(self):
        with tempfile.TemporaryDirectory() as base:
            target = os.path.join(base, "staged.bin")
            payload = b"frame-payload"
            argv = [sys.executable, "-I", "-c", _WRITE_SCRIPT, target, str(len(payload))]
            framed = len(payload).to_bytes(8, "little") + payload
            first = subprocess.run(argv, input=framed, capture_output=True)
            self.assertEqual(first.returncode, 0)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), payload)
            second = subprocess.run(argv, input=framed, capture_output=True)
            self.assertEqual(second.returncode, 5)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), payload)
            short = subprocess.run(argv, input=framed[:-1], capture_output=True)
            self.assertEqual(short.returncode, 2)
            oversize = subprocess.run(argv, input=(len(payload) + 1).to_bytes(8, "little"), capture_output=True)
            self.assertEqual(oversize.returncode, 2)

    def test_read_script_serves_bounded_regular_files(self):
        with tempfile.TemporaryDirectory() as base:
            target = os.path.join(base, "export.bin")
            payload = b"export-bytes"
            with open(target, "wb") as handle:
                handle.write(payload)

            def run(path, limit):
                return subprocess.run(
                    [sys.executable, "-I", "-c", _READ_SCRIPT, path, str(limit)],
                    capture_output=True,
                )

            served = run(target, len(payload))
            self.assertEqual(served.returncode, 0)
            self.assertEqual(served.stdout, payload)
            missing = run(os.path.join(base, "absent.bin"), 10)
            self.assertEqual(missing.returncode, 3)
            oversize = run(target, len(payload) - 1)
            self.assertEqual(oversize.returncode, 4)
            directory = run(base, 10)
            self.assertEqual(directory.returncode, 6)
            if hasattr(os, "O_NOFOLLOW"):
                link = None
                try:
                    link = os.path.join(base, "link.bin")
                    os.symlink(target, link)
                except (NotImplementedError, OSError):
                    link = None
                if link is not None:
                    rejected = run(link, 100)
                    self.assertEqual(rejected.returncode, 6)


if __name__ == "__main__":
    unittest.main()
