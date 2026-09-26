"""Controller-owned Docker policy and disposable containment probes."""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


IMAGE_IDENTITY = re.compile(r"^(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64})$")
WHEEL_INPUT = re.compile(r"^wheels/[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl$")
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BYTES = 20 * 1024 * 1024
MAX_PROBE_SECONDS = 300.0
MAX_ATTEMPT_INPUT_BYTES = 128 * 1024 * 1024
MAX_SOURCE_ZIP_BYTES = 10 * 1024 * 1024
MAX_SMALL_INPUT_BYTES = 64 * 1024
MAX_INPUT_FILES = 256
OWNER_LABEL = "upgrade-chamber.owner"
OWNER_VALUE = "execution"


class CleanupError(RuntimeError):
    """Container removal could not be observed; new jobs must be blocked."""


@dataclass(frozen=True)
class ProbeResult:
    kind: str
    status: str
    container_id: str | None
    elapsed_seconds: float
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    export: dict[str, str] | None
    error: str | None
    removal_observed: bool
    deadline_seconds: float


@dataclass(frozen=True)
class AttemptResult:
    phase: str
    status: str
    container_id: str | None
    elapsed_seconds: float
    exit_code: int | None
    marker: dict[str, Any] | None
    artifacts: dict[str, bytes]
    missing_artifacts: list[str]
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    error: str | None
    removal_observed: bool
    deadline_seconds: float
    image_identity: str


@dataclass(frozen=True)
class PreparationResult:
    status: str
    container_id: str | None
    elapsed_seconds: float
    exit_code: int | None
    marker: dict[str, Any] | None
    artifacts: dict[str, bytes]
    missing_artifacts: list[str]
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    error: str | None
    removal_observed: bool
    deadline_seconds: float
    image_identity: str


def _archive_file(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.mode = 0o600
        info.uid = 10001
        info.gid = 10001
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _validate_attempt_archive(data: bytes) -> None:
    """Reject unsafe staged inputs before creating an execution container."""
    if not isinstance(data, bytes) or len(data) > MAX_ATTEMPT_INPUT_BYTES:
        raise ValueError("Attempt input exceeds byte limit")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            seen: set[str] = set()
            total_size = 0
            for member in archive:
                name = member.name
                if not member.isfile() or name in seen:
                    raise ValueError("Attempt input contains a non-file or duplicate entry")
                if name == "source.zip":
                    limit = MAX_SOURCE_ZIP_BYTES
                elif name in {"requirements.txt", "manifest.json"}:
                    limit = MAX_SMALL_INPUT_BYTES
                elif WHEEL_INPUT.fullmatch(name):
                    limit = MAX_ATTEMPT_INPUT_BYTES
                else:
                    raise ValueError("Attempt input contains an unexpected path")
                if member.size > limit:
                    raise ValueError(f"Attempt input entry exceeds byte limit: {name}")
                seen.add(name)
                total_size += member.size
                if len(seen) > MAX_INPUT_FILES or total_size > MAX_ATTEMPT_INPUT_BYTES:
                    raise ValueError("Attempt input exceeds file or byte limit")
            if not {"source.zip", "requirements.txt", "manifest.json"}.issubset(seen):
                raise ValueError("Attempt input is missing a required entry")
            if not any(name.startswith("wheels/") for name in seen):
                raise ValueError("Attempt input has no wheel files")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("Attempt input is not a valid tar archive") from exc


def _limited_log(container: Any, *, stdout: bool) -> tuple[str, bool]:
    stream = container.logs(stdout=stdout, stderr=not stdout, stream=True)
    captured = bytearray()
    truncated = False
    for chunk in stream:
        remaining = MAX_LOG_BYTES - len(captured)
        captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    return captured.decode("utf-8", errors="replace"), truncated


def _read_export(container: Any) -> dict[str, str]:
    chunks, _ = container.get_archive("/work/export/probe.json")
    buffer = bytearray()
    for chunk in chunks:
        if len(buffer) + len(chunk) > MAX_EXPORT_BYTES + 4096:
            raise ValueError("Export exceeds byte limit")
        buffer.extend(chunk)
    with tarfile.open(fileobj=io.BytesIO(buffer), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1 or members[0].name != "probe.json" or not members[0].isfile():
            raise ValueError("Unexpected export entry")
        if members[0].size > MAX_EXPORT_BYTES:
            raise ValueError("Export exceeds byte limit")
        extracted = archive.extractfile(members[0])
        if extracted is None:
            raise ValueError("Export is unreadable")
        content = extracted.read(MAX_EXPORT_BYTES + 1)
    value = json.loads(content)
    if not isinstance(value, dict) or set(value) != {"kind", "nonce"}:
        raise ValueError("Invalid probe export")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError("Invalid probe export")
    return value


ATTEMPT_ARTIFACTS = (
    "collect.txt",
    "junit.xml",
    "pip-report.json",
    "pip-check.txt",
    "installed.json",
    "install.log",
    "test.log",
)
PREPARATION_ARTIFACTS = (
    "baseline.tar", "candidate.tar", "baseline-metadata.json", "candidate-metadata.json"
)


def _read_attempt_file(container: Any, name: str, max_bytes: int) -> bytes:
    chunks, _ = container.get_archive(f"/work/export/{name}")
    buffer = bytearray()
    for chunk in chunks:
        if len(buffer) + len(chunk) > max_bytes + 4096:
            raise ValueError("Attempt export exceeds byte limit")
        buffer.extend(chunk)
    with tarfile.open(fileobj=io.BytesIO(buffer), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1 or members[0].name != name or not members[0].isfile():
            raise ValueError("Unexpected attempt export entry")
        if members[0].size > max_bytes:
            raise ValueError("Attempt export exceeds byte limit")
        extracted = archive.extractfile(members[0])
        if extracted is None:
            raise ValueError("Attempt export is unreadable")
        content = extracted.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("Attempt export exceeds byte limit")
        return content


def _validate_attempt_marker(content: bytes, phase: str) -> dict[str, Any]:
    if len(content) > MAX_SMALL_INPUT_BYTES:
        raise ValueError("Attempt marker exceeds byte limit")
    try:
        marker = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Attempt marker is invalid JSON") from exc
    expected = {
        "schema_version", "phase", "status", "steps", "collected_test_ids", "counts",
        "installed_requests_version", "source_sha256", "error",
    }
    if not isinstance(marker, dict) or set(marker) != expected:
        raise ValueError("Attempt marker has an unexpected schema")
    if type(marker["schema_version"]) is not int or marker["schema_version"] != 1 or marker["phase"] != phase:
        raise ValueError("Attempt marker has an invalid identity")
    allowed_statuses = {"passed", "install_failed", "collection_failed", "test_failed", "infrastructure_failed"}
    if marker["status"] not in allowed_statuses:
        raise ValueError("Attempt marker has an invalid status")
    steps = marker["steps"]
    if not isinstance(steps, dict) or set(steps) != {"install", "collect", "test", "pip_check"}:
        raise ValueError("Attempt marker has invalid step results")
    for step in steps.values():
        if not isinstance(step, dict) or set(step) != {"exit_code", "timed_out"}:
            raise ValueError("Attempt marker has invalid step results")
        if step["exit_code"] is not None and type(step["exit_code"]) is not int:
            raise ValueError("Attempt marker has invalid step results")
        if type(step["timed_out"]) is not bool:
            raise ValueError("Attempt marker has invalid step results")
    ids = marker["collected_test_ids"]
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Attempt marker has invalid collected tests")
    counts = marker["counts"]
    if not isinstance(counts, dict) or set(counts) != {"passed", "failed", "errors", "skipped", "xfailed", "xpassed"}:
        raise ValueError("Attempt marker has invalid test counts")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("Attempt marker has invalid test counts")
    installed = marker["installed_requests_version"]
    if installed is not None and (not isinstance(installed, str) or not installed):
        raise ValueError("Attempt marker has invalid installed version")
    if not isinstance(marker["source_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", marker["source_sha256"]):
        raise ValueError("Attempt marker has invalid source digest")
    if marker["error"] is not None and (not isinstance(marker["error"], str) or len(marker["error"]) > 2000):
        raise ValueError("Attempt marker has invalid error")
    if marker["status"] == "passed":
        if any(step != {"exit_code": 0, "timed_out": False} for step in steps.values()):
            raise ValueError("Passed attempt has a failed step")
        if not ids or counts["passed"] < 1 or counts["failed"] or counts["errors"] or not installed:
            raise ValueError("Passed attempt lacks successful tests")
    return marker


def _validate_preparation_marker(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_SMALL_INPUT_BYTES:
        raise ValueError("Preparation marker exceeds byte limit")
    try:
        marker = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Preparation marker is invalid JSON") from exc
    if not isinstance(marker, dict) or set(marker) != {"schema_version", "status", "error", "summary"}:
        raise ValueError("Preparation marker has an unexpected schema")
    if type(marker["schema_version"]) is not int or marker["schema_version"] != 1:
        raise ValueError("Preparation marker has an invalid version")
    if marker["status"] not in {"prepared", "failed"}:
        raise ValueError("Preparation marker has an invalid status")
    if marker["error"] is not None and (not isinstance(marker["error"], str) or len(marker["error"]) > 2000):
        raise ValueError("Preparation marker has an invalid error")
    if marker["summary"] is not None and not isinstance(marker["summary"], dict):
        raise ValueError("Preparation marker has an invalid summary")
    if marker["status"] == "prepared" and marker["summary"] is None:
        raise ValueError("Prepared marker lacks a summary")
    return marker


class DockerRunner:
    """Run fixed probes using a pinned operator image and a strict Docker policy."""

    def __init__(self, image: str, client: Any | None = None):
        if not IMAGE_IDENTITY.fullmatch(image):
            raise ValueError("Execution image must use a local sha256 image ID or repository digest")
        self.image = image
        if client is None:
            import docker

            client = docker.from_env(timeout=5)
        self.client = client
        self._cleanup_failed = False

    def _create_container(
        self, command: list[str], label: str, expires_at: int, *, network_mode: str = "none"
    ) -> Any:
        return self.client.containers.create(
            image=self.image,
            command=command,
            detach=True,
            user="10001:10001",
            network_mode=network_mode,
            read_only=True,
            mem_limit="1g",
            memswap_limit="1g",
            nano_cpus=1_000_000_000,
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            privileged=False,
            mounts=[],
            volumes={},
            devices=[],
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            tmpfs={
                "/work": "rw,nosuid,nodev,size=536870912,uid=10001,gid=10001,mode=0700",
                "/tmp": "rw,nosuid,nodev,size=134217728,uid=10001,gid=10001,mode=0700",
            },
            working_dir="/work",
            log_config={"type": "json-file", "config": {"max-size": "2m", "max-file": "1"}},
            labels={
                OWNER_LABEL: OWNER_VALUE,
                "upgrade-chamber.probe": label,
                "upgrade-chamber.deadline": str(expires_at),
            },
        )

    def _remove_container(self, container: Any) -> bool:
        try:
            container.remove(force=True)
            try:
                self.client.containers.get(container.id)
            except Exception as exc:
                if exc.__class__.__name__ != "NotFound":
                    raise
                return True
            raise CleanupError(f"Container {container.id} remains after removal")
        except Exception as exc:
            self._cleanup_failed = True
            raise CleanupError(f"Removal not observed for container {container.id}: {exc}") from exc

    def run_probe(self, kind: str, *, timeout_seconds: float = 5.0) -> ProbeResult:
        if self._cleanup_failed:
            raise CleanupError("Previous container removal was not observed")
        if kind not in {"infinite_loop", "success"}:
            raise ValueError("Unsupported controller-owned probe")
        if not 0 < timeout_seconds <= MAX_PROBE_SECONDS:
            raise ValueError("Probe timeout must be greater than zero and at most 300 seconds")

        started = time.monotonic()
        expires_at = int(time.time() + timeout_seconds)
        nonce = uuid4().hex
        container = None
        status = "infrastructure_failed"
        exit_code = None
        stdout = stderr = ""
        stdout_truncated = stderr_truncated = False
        exported = None
        error = None
        removed = False
        try:
            container = self._create_container(
                ["python", "-I", "/opt/upgrade_chamber/runner.py", kind], kind, expires_at
            )
            container.start()
            if not container.put_archive("/work", _archive_file("input.json", json.dumps({"nonce": nonce}).encode())):
                raise RuntimeError("Input staging failed")
            if not container.put_archive("/work", _archive_file("ready", b"")):
                raise RuntimeError("Ready marker staging failed")

            deadline = started + timeout_seconds
            acknowledged = False
            while True:
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    container.kill()
                    break
                container.reload()
                state = container.attrs.get("State", {})
                if not state.get("Running", True):
                    exit_code = state.get("ExitCode")
                    status = "completed" if exit_code == 0 and acknowledged else "infrastructure_failed"
                    break
                if kind == "success" and not acknowledged:
                    try:
                        exported = _read_export(container)
                    except Exception as exc:
                        if exc.__class__.__name__ != "NotFound":
                            raise
                    else:
                        if exported != {"kind": "success", "nonce": nonce}:
                            raise ValueError("Probe export did not match staged input")
                        if not container.put_archive("/work", _archive_file("ack", b"")):
                            raise RuntimeError("Probe acknowledgement failed")
                        acknowledged = True
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

            stdout, stdout_truncated = _limited_log(container, stdout=True)
            stderr, stderr_truncated = _limited_log(container, stdout=False)
        except Exception as exc:
            status = "infrastructure_failed" if status != "timed_out" else status
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if container is not None:
                removed = self._remove_container(container)

        return ProbeResult(
            kind=kind,
            status=status,
            container_id=container.id if container is not None else None,
            elapsed_seconds=time.monotonic() - started,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            export=exported,
            error=error,
            removal_observed=removed,
            deadline_seconds=timeout_seconds,
        )

    def run_profile_attempt(
        self, input_tar: bytes, *, phase: str, timeout_seconds: float = 300.0
    ) -> AttemptResult:
        """Run one fixed profile phase in a fresh container and retain partial evidence."""
        if self._cleanup_failed:
            raise CleanupError("Previous container removal was not observed")
        if phase not in {"baseline", "candidate"}:
            raise ValueError("Unsupported profile phase")
        if not 0 < timeout_seconds <= MAX_PROBE_SECONDS:
            raise ValueError("Attempt timeout must be greater than zero and at most 300 seconds")
        _validate_attempt_archive(input_tar)

        started = time.monotonic()
        expires_at = int(time.time() + timeout_seconds)
        container = None
        status = "infrastructure_failed"
        exit_code = None
        marker = None
        artifacts: dict[str, bytes] = {}
        missing: list[str] = []
        stdout = stderr = ""
        stdout_truncated = stderr_truncated = False
        error = None
        removed = False
        acknowledged = False
        try:
            container = self._create_container(
                ["python", "-I", "/opt/upgrade_chamber/attempt.py", phase],
                f"profile-{phase}", expires_at,
            )
            container.start()
            if not container.put_archive("/work", input_tar):
                raise RuntimeError("Attempt input staging failed")
            if not container.put_archive("/work", _archive_file("ready", b"")):
                raise RuntimeError("Ready marker staging failed")

            deadline = started + timeout_seconds
            while True:
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    container.kill()
                    break
                container.reload()
                state = container.attrs.get("State", {})
                if not state.get("Running", True):
                    exit_code = state.get("ExitCode")
                    if acknowledged and exit_code == 0 and marker is not None:
                        status = marker["status"]
                    break
                if not acknowledged:
                    try:
                        marker_bytes = _read_attempt_file(container, "attempt.json", MAX_SMALL_INPUT_BYTES)
                    except Exception as exc:
                        if exc.__class__.__name__ != "NotFound":
                            raise
                    else:
                        marker = _validate_attempt_marker(marker_bytes, phase)
                        artifacts["attempt.json"] = marker_bytes
                        total = len(marker_bytes)
                        for name in ATTEMPT_ARTIFACTS:
                            try:
                                content = _read_attempt_file(container, name, MAX_EXPORT_BYTES - total)
                            except Exception as exc:
                                if exc.__class__.__name__ != "NotFound":
                                    raise
                                missing.append(name)
                            else:
                                artifacts[name] = content
                                total += len(content)
                        if marker["status"] == "passed" and any(
                            name in missing for name in ATTEMPT_ARTIFACTS[:5]
                        ):
                            raise ValueError("Passed attempt is missing required evidence")
                        if not container.put_archive("/work", _archive_file("ack", b"")):
                            raise RuntimeError("Attempt acknowledgement failed")
                        acknowledged = True
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

            stdout, stdout_truncated = _limited_log(container, stdout=True)
            stderr, stderr_truncated = _limited_log(container, stdout=False)
        except Exception as exc:
            if status != "timed_out":
                status = "infrastructure_failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if container is not None:
                removed = self._remove_container(container)

        return AttemptResult(
            phase=phase,
            status=status,
            container_id=container.id if container is not None else None,
            elapsed_seconds=time.monotonic() - started,
            exit_code=exit_code,
            marker=marker,
            artifacts=artifacts,
            missing_artifacts=missing,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            error=error,
            removal_observed=removed,
            deadline_seconds=timeout_seconds,
            image_identity=self.image,
        )

    def run_preparation(self, *, timeout_seconds: float = 300.0) -> PreparationResult:
        """Run the fixed, trusted downloader in a bounded networked container."""
        if self._cleanup_failed:
            raise CleanupError("Previous container removal was not observed")
        if not 0 < timeout_seconds <= MAX_PROBE_SECONDS:
            raise ValueError("Preparation timeout must be greater than zero and at most 300 seconds")

        started = time.monotonic()
        expires_at = int(time.time() + timeout_seconds)
        container = None
        status = "infrastructure_failed"
        exit_code = None
        marker = None
        artifacts: dict[str, bytes] = {}
        missing: list[str] = []
        stdout = stderr = ""
        stdout_truncated = stderr_truncated = False
        error = None
        removed = False
        acknowledged = False
        try:
            container = self._create_container(
                ["python", "-I", "/opt/upgrade_chamber/prepare_baseline.py", "--handshake"],
                "preparation", expires_at, network_mode="bridge",
            )
            container.start()
            if not container.put_archive("/work", _archive_file("ready", b"")):
                raise RuntimeError("Ready marker staging failed")

            deadline = started + timeout_seconds
            while True:
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    container.kill()
                    break
                container.reload()
                state = container.attrs.get("State", {})
                if not state.get("Running", True):
                    exit_code = state.get("ExitCode")
                    if acknowledged and marker is not None and (
                        (marker["status"] == "prepared" and exit_code == 0)
                        or (marker["status"] == "failed" and exit_code == 1)
                    ):
                        status = marker["status"]
                    break
                if not acknowledged:
                    try:
                        marker_bytes = _read_attempt_file(container, "preparation.json", MAX_SMALL_INPUT_BYTES)
                    except Exception as exc:
                        if exc.__class__.__name__ != "NotFound":
                            raise
                    else:
                        marker = _validate_preparation_marker(marker_bytes)
                        artifacts["preparation.json"] = marker_bytes
                        total = len(marker_bytes)
                        for name in PREPARATION_ARTIFACTS:
                            try:
                                content = _read_attempt_file(container, name, MAX_EXPORT_BYTES - total)
                            except Exception as exc:
                                if exc.__class__.__name__ != "NotFound":
                                    raise
                                missing.append(name)
                            else:
                                artifacts[name] = content
                                total += len(content)
                        if marker["status"] == "prepared":
                            if missing:
                                raise ValueError("Prepared bundle is missing required evidence")
                            _validate_attempt_archive(artifacts["baseline.tar"])
                            _validate_attempt_archive(artifacts["candidate.tar"])
                        if not container.put_archive("/work", _archive_file("ack", b"")):
                            raise RuntimeError("Preparation acknowledgement failed")
                        acknowledged = True
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

            stdout, stdout_truncated = _limited_log(container, stdout=True)
            stderr, stderr_truncated = _limited_log(container, stdout=False)
        except Exception as exc:
            if status != "timed_out":
                status = "infrastructure_failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if container is not None:
                removed = self._remove_container(container)

        return PreparationResult(
            status=status,
            container_id=container.id if container is not None else None,
            elapsed_seconds=time.monotonic() - started,
            exit_code=exit_code,
            marker=marker,
            artifacts=artifacts,
            missing_artifacts=missing,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            error=error,
            removal_observed=removed,
            deadline_seconds=timeout_seconds,
            image_identity=self.image,
        )

    def remove_expired_containers(self, *, now: int | None = None) -> list[str]:
        """Remove only owned containers with an expired recorded deadline."""
        current = int(time.time()) if now is None else now
        removed = []
        for container in self.client.containers.list(all=True, filters={"label": f"{OWNER_LABEL}={OWNER_VALUE}"}):
            labels = container.labels or {}
            if labels.get(OWNER_LABEL) != OWNER_VALUE:
                continue
            try:
                deadline = int(labels["upgrade-chamber.deadline"])
            except (KeyError, TypeError, ValueError):
                continue
            if deadline >= current:
                continue
            try:
                container.remove(force=True)
                self.client.containers.get(container.id)
            except Exception as exc:
                if exc.__class__.__name__ != "NotFound":
                    self._cleanup_failed = True
                    raise CleanupError(f"Removal not observed for container {container.id}: {exc}") from exc
            else:
                self._cleanup_failed = True
                raise CleanupError(f"Container {container.id} remains after removal")
            removed.append(container.id)
        return removed
