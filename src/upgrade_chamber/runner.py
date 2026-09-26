"""Controller-owned Docker policy and disposable containment probes."""

from __future__ import annotations

import io
import json
import re
import socket
import struct
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
EXEC_POLL_SECONDS = 0.05
EXEC_CHUNK_BYTES = 64 * 1024
EXEC_INIT_RACE_ATTEMPTS = 5
EXEC_INIT_RACE_SLEEP_SECONDS = 0.25


class CleanupError(RuntimeError):
    """Container removal could not be observed; new jobs must be blocked."""


class DeadlineExceeded(RuntimeError):
    """A bounded exec or socket operation outlived the run deadline."""


class ArtifactMissing(RuntimeError):
    """A polled export path does not exist in the container yet."""


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


# The container root filesystem is read-only and its tmpfs paths are unreachable
# through Docker's archive API, so the controller stages inputs and exports
# artifacts with fixed, unprivileged Python execs instead. The scripts below are
# module constants so the argv contract stays auditable and the test fakes can
# interpret it exactly.

# _WRITE_SCRIPT frames one file on stdin: an 8-byte little-endian unsigned
# length prefix followed by exactly that many bytes. The frame length is
# validated against the cap passed as the second argv value. The target is
# opened with O_EXCL so an existing file or symlink is rejected rather than
# overwritten or followed, and the bytes are flushed and fsynced before exit 0.
_WRITE_SCRIPT = """\
import os
import sys

path, cap = sys.argv[1], int(sys.argv[2])
prefix = bytearray()
while len(prefix) < 8:
    chunk = sys.stdin.buffer.read(8 - len(prefix))
    if not chunk:
        sys.exit(2)
    prefix.extend(chunk)
size = int.from_bytes(prefix, "little")
if size > cap:
    sys.exit(2)
payload = bytearray()
while len(payload) < size:
    chunk = sys.stdin.buffer.read(size - len(payload))
    if not chunk:
        sys.exit(2)
    payload.extend(chunk)
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    sys.exit(5)
with os.fdopen(descriptor, "wb") as output:
    output.write(payload)
    output.flush()
    os.fsync(output.fileno())
sys.exit(0)
"""

# _READ_SCRIPT streams one bounded file to stdout. The path is opened without
# following symlinks (O_NOFOLLOW on Linux); a missing path exits 3, another
# open error such as a symlink loop exits 6, a non-regular file exits 6, and a
# file larger than the byte limit exits 4. Otherwise the raw bytes are written
# to stdout and flushed before exit 0.
_READ_SCRIPT = """\
import os
import stat
import sys

path, limit = sys.argv[1], int(sys.argv[2])
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
except FileNotFoundError:
    sys.exit(3)
except OSError:
    sys.exit(6)
if not stat.S_ISREG(os.fstat(descriptor).st_mode):
    sys.exit(6)
if os.fstat(descriptor).st_size > limit:
    sys.exit(4)
with os.fdopen(descriptor, "rb") as source:
    data = source.read(limit + 1)
if len(data) > limit:
    sys.exit(4)
sys.stdout.buffer.write(data)
sys.stdout.buffer.flush()
sys.exit(0)
"""

# _EXTRACT_SCRIPT unpacks the staged attempt input tar directly into /work with
# the strict "data" tar filter, which rejects absolute paths, traversal outside
# the destination, symlinks, hard links, and device entries, and then unlinks
# the archive itself.
_EXTRACT_SCRIPT = """\
import os
import sys
import tarfile

with tarfile.open(sys.argv[1], mode="r:") as archive:
    archive.extractall("/work", filter="data")
os.unlink(sys.argv[1])
sys.exit(0)
"""

# _MEMORY_SCRIPT is a deliberate containment fixture, not behavior under test:
# it allocates and zero-fills memory without bound (one 2 GiB block first, then
# repeated 256 MiB chunks) until the 1 GiB cgroup limit OOM-kills the process.
# The 2 GiB attempt is guarded so the chunk loop still runs when a single large
# allocation is refused early with MemoryError instead of an OOM kill. The
# script never exits on its own: a live run must end in an OOM kill, and a run
# that survives to the deadline is reported as "timed_out" so the missing
# memory bound is surfaced honestly.
_MEMORY_SCRIPT = """\
import time

try:
    data = bytearray(2 * 1024 * 1024 * 1024)
except MemoryError:
    pass
chunks = []
while True:
    try:
        chunks.append(bytearray(256 * 1024 * 1024))
    except MemoryError:
        time.sleep(0.01)
"""


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("Deadline exceeded before the bounded exec completed")
    return remaining


def _start_exec(
    client: Any, container_id: str, cmd: list[str], deadline: float,
    *, stdin: bool = False, **create_kwargs: Any,
) -> tuple[str, Any]:
    """Create and start one exec, retrying the containerd startup race.

    An exec issued immediately after container.start() can reach the daemon
    before the containerd init process of the target container is running,
    which docker-py reports as an APIError whose text contains
    "FailedPrecondition" and "container init process is not running". The
    exec_create/exec_start pair is retried up to EXEC_INIT_RACE_ATTEMPTS
    times with a short fixed sleep between attempts, and every retry is
    checked against the run deadline so a persistently unhealthy container
    fails bounded instead of retrying forever. Only that precondition-shaped
    failure is retried (matched by exception type name, without importing
    docker, the same way NotFound is matched elsewhere); any other exception
    propagates unchanged so genuine staging errors keep their original type
    and message.
    """
    attempts = 0
    while True:
        attempts += 1
        stream = None
        try:
            exec_id = client.api.exec_create(
                container=container_id, cmd=list(cmd),
                stdin=stdin, tty=False, **create_kwargs,
            )["Id"]
            stream = client.api.exec_start(exec_id, socket=True, tty=False)
            return exec_id, stream
        except Exception as exc:
            if stream is not None:
                stream.close()
            text = str(exc)
            retryable = (
                exc.__class__.__name__ == "APIError"
                and ("FailedPrecondition" in text or "not running" in text)
            )
            if not retryable:
                raise
            if attempts >= EXEC_INIT_RACE_ATTEMPTS:
                raise RuntimeError(
                    "Exec still raced the containerd init process after "
                    f"{attempts} attempts: {exc}"
                ) from exc
            _remaining_seconds(deadline)
            time.sleep(EXEC_INIT_RACE_SLEEP_SECONDS)


def _poll_exec(client: Any, exec_id: str, deadline: float) -> int | None:
    """Wait for one exec to finish and return its exit code within the deadline."""
    while True:
        remaining = _remaining_seconds(deadline)
        state = client.api.exec_inspect(exec_id)
        if not state.get("Running", False):
            return state.get("ExitCode")
        time.sleep(min(EXEC_POLL_SECONDS, remaining))


class _FrameDemuxer:
    """Demultiplex the framed exec output stream the daemon sends for tty=False.

    Every frame is an 8-byte header (one stream-type byte, three zero bytes,
    and a four-byte big-endian payload length) followed by exactly that many
    payload bytes; recv may split a frame anywhere, so partial headers and
    payloads are buffered until a complete frame can be parsed. Only stdout
    frames (type 1) are collected; stderr frames (type 2) are drained and
    discarded, and any other stream type is a protocol violation.
    """

    HEADER_SIZE = 8
    DRAIN_SLACK = 1024 * 1024

    def __init__(self, sock: Any, deadline: float, max_bytes: int):
        self._sock = sock
        self._deadline = deadline
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._eof = False
        self.output = bytearray()
        self.drained = 0

    def _recv_chunk(self) -> None:
        """Read one bounded chunk, refreshing the timeout against the deadline."""
        self._sock.settimeout(_remaining_seconds(self._deadline))
        chunk = self._sock.recv(EXEC_CHUNK_BYTES)
        if not chunk:
            self._eof = True
            return
        self._buffer.extend(chunk)
        self.drained += len(chunk)
        if self.drained > self._max_bytes + self.DRAIN_SLACK:
            raise RuntimeError("Exec output exceeded the framed drain guard")

    def read_stdout(self) -> bytes:
        """Collect bounded stdout payload bytes until the stream ends."""
        guard = self._max_bytes + self.DRAIN_SLACK
        while not self._eof:
            if len(self._buffer) < self.HEADER_SIZE:
                self._recv_chunk()
                continue
            stream_type, length = struct.unpack(">BxxxL", bytes(self._buffer[:self.HEADER_SIZE]))
            if stream_type not in (1, 2):
                raise RuntimeError(f"Exec output frame used unexpected stream type {stream_type}")
            if length == 0:
                del self._buffer[:self.HEADER_SIZE]
                continue
            if len(self._buffer) < self.HEADER_SIZE + length:
                self._recv_chunk()
                continue
            payload = bytes(self._buffer[self.HEADER_SIZE:self.HEADER_SIZE + length])
            del self._buffer[:self.HEADER_SIZE + length]
            if stream_type == 1 and len(self.output) < self._max_bytes:
                room = self._max_bytes - len(self.output)
                self.output.extend(payload[:room])
        if self.drained > guard:
            raise RuntimeError("Exec output exceeded the framed drain guard")
        return bytes(self.output)


def _exec_write_file(client: Any, container_id: str, path: str, data: bytes, deadline: float) -> None:
    """Stage one file at a fixed path through an unprivileged stdin-framed exec."""
    _remaining_seconds(deadline)
    exec_id, stream = _start_exec(
        client, container_id,
        ["python", "-I", "-c", _WRITE_SCRIPT, path, str(len(data))],
        deadline, stdin=True,
    )
    try:
        sock = stream._sock
        try:
            sock.settimeout(_remaining_seconds(deadline))
            sock.sendall(len(data).to_bytes(8, "little"))
            for offset in range(0, len(data), EXEC_CHUNK_BYTES):
                sock.settimeout(_remaining_seconds(deadline))
                sock.sendall(data[offset:offset + EXEC_CHUNK_BYTES])
        except socket.timeout as exc:
            raise DeadlineExceeded(f"Deadline exceeded while staging {path}") from exc
        exit_code = _poll_exec(client, exec_id, deadline)
    finally:
        stream.close()
    if exit_code == 5:
        raise RuntimeError(f"Overwrite rejected for {path}")
    if exit_code != 0:
        raise RuntimeError(f"Staging exec failed with exit code {exit_code} for {path}")


def _exec_extract_tar(client: Any, container_id: str, archive_path: str, deadline: float) -> None:
    """Extract the staged attempt input tar into /work through a fixed exec."""
    _remaining_seconds(deadline)
    exec_id, stream = _start_exec(
        client, container_id,
        ["python", "-I", "-c", _EXTRACT_SCRIPT, archive_path],
        deadline,
    )
    try:
        exit_code = _poll_exec(client, exec_id, deadline)
    finally:
        stream.close()
    if exit_code != 0:
        raise RuntimeError(f"Input extraction exec failed with exit code {exit_code}")


def _exec_read_file(client: Any, container_id: str, path: str, max_bytes: int, deadline: float) -> bytes:
    """Read one bounded export file through an exec that frames bytes on stdout."""
    _remaining_seconds(deadline)
    exec_id, stream = _start_exec(
        client, container_id,
        ["python", "-I", "-c", _READ_SCRIPT, path, str(max_bytes)],
        deadline, stdout=True,
    )
    collected: bytes
    try:
        collected = _FrameDemuxer(stream._sock, deadline, max_bytes).read_stdout()
        exit_code = _poll_exec(client, exec_id, deadline)
    except socket.timeout as exc:
        raise DeadlineExceeded(f"Deadline exceeded while reading {path}") from exc
    finally:
        stream.close()
    if exit_code == 3:
        raise ArtifactMissing(f"Export is missing: {path}")
    if exit_code == 4 or len(collected) > max_bytes:
        raise ValueError(f"Export exceeds byte limit: {path}")
    if exit_code == 6:
        raise ValueError(f"Export path is not a regular file or is a symlink: {path}")
    if exit_code != 0:
        raise RuntimeError(f"Read exec failed with exit code {exit_code} for {path}")
    return bytes(collected)


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


def _read_export(client: Any, container_id: str, deadline: float) -> dict[str, str]:
    content = _exec_read_file(
        client, container_id, "/work/export/probe.json", MAX_EXPORT_BYTES, deadline
    )
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


def _read_attempt_file(client: Any, container_id: str, name: str, max_bytes: int, deadline: float) -> bytes:
    return _exec_read_file(client, container_id, f"/work/export/{name}", max_bytes, deadline)


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
        if kind not in {"infinite_loop", "success", "memory"}:
            raise ValueError("Unsupported controller-owned probe")
        if not 0 < timeout_seconds <= MAX_PROBE_SECONDS:
            raise ValueError("Probe timeout must be greater than zero and at most 300 seconds")

        started = time.monotonic()
        expires_at = int(time.time() + timeout_seconds)
        deadline = started + timeout_seconds
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
            if kind == "memory":
                command = ["python", "-I", "-c", _MEMORY_SCRIPT]
            else:
                command = ["python", "-I", "/opt/upgrade_chamber/runner.py", kind]
            container = self._create_container(command, kind, expires_at)
            container.start()
            if kind != "memory":
                _exec_write_file(
                    self.client, container.id, "/work/input.json",
                    json.dumps({"nonce": nonce}).encode(), deadline,
                )
                _exec_write_file(self.client, container.id, "/work/ready", b"", deadline)

            acknowledged = False
            poll_interval = 0.05
            while True:
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    container.kill()
                    break
                container.reload()
                state = container.attrs.get("State", {})
                if not state.get("Running", True):
                    exit_code = state.get("ExitCode")
                    if kind == "memory":
                        if state.get("OOMKilled"):
                            status = "oom_killed"
                        else:
                            status = "infrastructure_failed"
                            error = (
                                "Memory probe ended without an OOM kill "
                                f"(OOMKilled was false, exit code {exit_code})"
                            )
                    else:
                        status = "completed" if exit_code == 0 and acknowledged else "infrastructure_failed"
                    break
                if kind == "success" and not acknowledged:
                    try:
                        exported = _read_export(self.client, container.id, deadline)
                    except ArtifactMissing:
                        pass
                    else:
                        if exported != {"kind": "success", "nonce": nonce}:
                            raise ValueError("Probe export did not match staged input")
                        _exec_write_file(self.client, container.id, "/work/ack", b"", deadline)
                        acknowledged = True
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                poll_interval = min(poll_interval * 2, 0.5)

            stdout, stdout_truncated = _limited_log(container, stdout=True)
            stderr, stderr_truncated = _limited_log(container, stdout=False)
        except DeadlineExceeded as exc:
            status = "timed_out"
            error = f"{type(exc).__name__}: {exc}"
            if container is not None:
                try:
                    container.kill()
                except Exception as kill_exc:
                    error = f"{error}; kill failed: {type(kill_exc).__name__}: {kill_exc}"
                try:
                    stdout, stdout_truncated = _limited_log(container, stdout=True)
                    stderr, stderr_truncated = _limited_log(container, stdout=False)
                except Exception as log_exc:
                    error = f"{error}; log capture failed: {type(log_exc).__name__}: {log_exc}"
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
        deadline = started + timeout_seconds
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
            _exec_write_file(self.client, container.id, "/work/input.tar", input_tar, deadline)
            _exec_extract_tar(self.client, container.id, "/work/input.tar", deadline)
            _exec_write_file(self.client, container.id, "/work/ready", b"", deadline)

            poll_interval = 0.25
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
                        marker_bytes = _read_attempt_file(
                            self.client, container.id, "attempt.json", MAX_SMALL_INPUT_BYTES, deadline
                        )
                    except ArtifactMissing:
                        pass
                    else:
                        marker = _validate_attempt_marker(marker_bytes, phase)
                        artifacts["attempt.json"] = marker_bytes
                        total = len(marker_bytes)
                        for name in ATTEMPT_ARTIFACTS:
                            try:
                                content = _read_attempt_file(
                                    self.client, container.id, name, MAX_EXPORT_BYTES - total, deadline
                                )
                            except ArtifactMissing:
                                missing.append(name)
                            else:
                                artifacts[name] = content
                                total += len(content)
                        if marker["status"] == "passed" and any(
                            name in missing for name in ATTEMPT_ARTIFACTS[:5]
                        ):
                            raise ValueError("Passed attempt is missing required evidence")
                        _exec_write_file(self.client, container.id, "/work/ack", b"", deadline)
                        acknowledged = True
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                poll_interval = min(poll_interval * 2, 1.0)

            stdout, stdout_truncated = _limited_log(container, stdout=True)
            stderr, stderr_truncated = _limited_log(container, stdout=False)
        except DeadlineExceeded as exc:
            status = "timed_out"
            error = f"{type(exc).__name__}: {exc}"
            if container is not None:
                try:
                    container.kill()
                except Exception as kill_exc:
                    error = f"{error}; kill failed: {type(kill_exc).__name__}: {kill_exc}"
                try:
                    stdout, stdout_truncated = _limited_log(container, stdout=True)
                    stderr, stderr_truncated = _limited_log(container, stdout=False)
                except Exception as log_exc:
                    error = f"{error}; log capture failed: {type(log_exc).__name__}: {log_exc}"
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
        deadline = started + timeout_seconds
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
            _exec_write_file(self.client, container.id, "/work/ready", b"", deadline)

            poll_interval = 0.25
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
                        marker_bytes = _read_attempt_file(
                            self.client, container.id, "preparation.json", MAX_SMALL_INPUT_BYTES, deadline
                        )
                    except ArtifactMissing:
                        pass
                    else:
                        marker = _validate_preparation_marker(marker_bytes)
                        artifacts["preparation.json"] = marker_bytes
                        total = len(marker_bytes)
                        for name in PREPARATION_ARTIFACTS:
                            try:
                                content = _read_attempt_file(
                                    self.client, container.id, name, MAX_EXPORT_BYTES - total, deadline
                                )
                            except ArtifactMissing:
                                missing.append(name)
                            else:
                                artifacts[name] = content
                                total += len(content)
                        if marker["status"] == "prepared":
                            if missing:
                                raise ValueError("Prepared bundle is missing required evidence")
                            _validate_attempt_archive(artifacts["baseline.tar"])
                            _validate_attempt_archive(artifacts["candidate.tar"])
                        _exec_write_file(self.client, container.id, "/work/ack", b"", deadline)
                        acknowledged = True
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                poll_interval = min(poll_interval * 2, 1.0)

            stdout, stdout_truncated = _limited_log(container, stdout=True)
            stderr, stderr_truncated = _limited_log(container, stdout=False)
        except DeadlineExceeded as exc:
            status = "timed_out"
            error = f"{type(exc).__name__}: {exc}"
            if container is not None:
                try:
                    container.kill()
                except Exception as kill_exc:
                    error = f"{error}; kill failed: {type(kill_exc).__name__}: {kill_exc}"
                try:
                    stdout, stdout_truncated = _limited_log(container, stdout=True)
                    stderr, stderr_truncated = _limited_log(container, stdout=False)
                except Exception as log_exc:
                    error = f"{error}; log capture failed: {type(log_exc).__name__}: {log_exc}"
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
