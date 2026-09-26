"""Fixed offline install and historical test command for one disposable container."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath


PROFILE_ID = "requests-unixsocket-historical-0.3.0-research"
COMMIT_SHA = "8449bc0f76a2ce410644b5e8aab45829ddca54f7"
VERSIONS = {"baseline": "2.31.0", "candidate": "2.34.2"}
COMMON_PINS = {
    "urllib3": "1.26.20",
    "charset-normalizer": "3.4.4",
    "idna": "3.10",
    "certifi": "2026.7.22",
    "pytest": "9.1.1",
    "pluggy": "1.6.0",
    "iniconfig": "2.3.0",
    "packaging": "26.3",
    "pygments": "2.21.0",
    "waitress": "3.0.2",
}
SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
WHEEL_RE = re.compile(r"[A-Za-z0-9_.-]+-(?:py3|py2\.py3)-none-any\.whl\Z")
TEST_ID_RE = re.compile(r"requests_unixsocket/tests/[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_\[\].-]+\Z")
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
MAX_FILES = 5000
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024


class AttemptError(RuntimeError):
    """The staged input or a generated report did not satisfy the fixed contract."""


def _sha256_file(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > limit:
                raise AttemptError(f"Input exceeds byte limit: {path.name}")
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(work: Path, phase: str) -> dict:
    manifest_path = work / "manifest.json"
    if manifest_path.stat().st_size > 65536:
        raise AttemptError("Manifest exceeds byte limit")
    manifest = json.loads(manifest_path.read_bytes())
    expected = {"schema_version", "profile_id", "commit_sha", "phase", "requests_version",
                "source_sha256", "requirements_sha256", "wheels"}
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise AttemptError("Manifest has an unexpected schema")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["profile_id"] != PROFILE_ID
            or manifest["commit_sha"] != COMMIT_SHA or manifest["phase"] != phase
            or manifest["requests_version"] != VERSIONS[phase]):
        raise AttemptError("Manifest has an unexpected identity")
    source_hash = manifest["source_sha256"]
    requirements_hash = manifest["requirements_sha256"]
    if not all(isinstance(item, str) and SHA256_RE.fullmatch(item)
               for item in (source_hash, requirements_hash)):
        raise AttemptError("Manifest has an invalid digest")
    wheel_hashes = manifest["wheels"]
    if not isinstance(wheel_hashes, dict) or len(wheel_hashes) != len(COMMON_PINS) + 1:
        raise AttemptError("Manifest has an unexpected wheel set")
    for filename, digest in wheel_hashes.items():
        if (not isinstance(filename, str) or not WHEEL_RE.fullmatch(filename)
                or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
            raise AttemptError("Manifest has an invalid wheel entry")
    if _sha256_file(work / "source.zip", MAX_SOURCE_BYTES) != source_hash:
        raise AttemptError("Source archive hash mismatch")
    requirements_path = work / "requirements.txt"
    if _sha256_file(requirements_path, 65536) != requirements_hash:
        raise AttemptError("Requirements hash mismatch")
    pins = {"requests": VERSIONS[phase], **COMMON_PINS}
    lines = requirements_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != len(pins):
        raise AttemptError("Requirements do not match fixed pins")
    expected_lines = []
    for package, version in pins.items():
        prefix = f"{package.replace('-', '_')}-{version}-"
        matching = [(filename, digest) for filename, digest in wheel_hashes.items()
                    if filename.lower().startswith(prefix.lower())]
        if len(matching) != 1:
            raise AttemptError("Wheel set does not match fixed pins")
        filename, digest = matching[0]
        if _sha256_file(work / "wheels" / filename, 5 * 1024 * 1024) != digest:
            raise AttemptError(f"Wheel hash mismatch: {filename}")
        expected_lines.append(f"{package}=={version} --hash=sha256:{digest}")
    if lines != expected_lines:
        raise AttemptError("Requirements do not match fixed pins")
    if set(path.name for path in (work / "wheels").iterdir()) != set(wheel_hashes):
        raise AttemptError("Unexpected wheel file")
    return manifest


def _extract_source(archive_path: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, exist_ok=False)
    total = 0
    seen = set()
    roots = set()
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise AttemptError("Source archive has too many entries")
        for entry in entries:
            name = entry.filename
            if "\\" in name or "\x00" in name or name.startswith("/"):
                raise AttemptError("Source archive has an unsafe path")
            parts = PurePosixPath(name).parts
            if not parts or any(part in {".", ".."} for part in parts):
                raise AttemptError("Source archive has an unsafe path")
            roots.add(parts[0])
            if len(parts) == 1 and entry.is_dir():
                continue
            if len(parts) < 2:
                raise AttemptError("Source archive has an unsafe path")
            relative = PurePosixPath(*parts[1:])
            if relative.as_posix() in seen:
                raise AttemptError("Source archive has duplicate paths")
            seen.add(relative.as_posix())
            mode = (entry.external_attr >> 16) & 0o170000
            if mode not in {0, 0o040000, 0o100000}:
                raise AttemptError("Source archive has a link or special file")
            total += entry.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise AttemptError("Source archive exceeds extracted byte limit")
            target = destination.joinpath(*parts[1:])
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if entry.file_size > MAX_EXTRACTED_BYTES:
                raise AttemptError("Source file exceeds byte limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("xb") as output:
                copied = 0
                while chunk := source.read(65536):
                    copied += len(chunk)
                    if copied > entry.file_size:
                        raise AttemptError("Source archive entry size mismatch")
                    output.write(chunk)
                if copied != entry.file_size:
                    raise AttemptError("Source archive entry size mismatch")
    if roots != {f"requests-unixsocket-{COMMIT_SHA}"}:
        raise AttemptError("Source archive root does not match fixed repository")
    if not (destination / "requests_unixsocket/tests/test_requests_unixsocket.py").is_file():
        raise AttemptError("Historical test module is missing")


def _run_command(command: list[str], log_path: Path, *, timeout_seconds: float,
                 cwd: Path, env: dict[str, str]) -> tuple[int | None, bool, bool]:
    started = time.monotonic()
    captured = bytearray()
    timed_out = truncated = False
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(timeout=min(remaining, 0.1)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    break
                room = MAX_LOG_BYTES - len(captured)
                captured.extend(chunk[:room])
                if len(chunk) > room:
                    truncated = True
                    break
            if truncated:
                break
        if timed_out or truncated:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        exit_code = process.wait(timeout=5)
    finally:
        selector.close()
        process.stdout.close()
        log_path.write_bytes(captured)
    return exit_code, timed_out, truncated


def _collect_ids(content: bytes) -> list[str]:
    text = content.decode("utf-8", errors="replace")
    ids = [line.strip() for line in text.splitlines() if TEST_ID_RE.fullmatch(line.strip())]
    if not ids or len(ids) != len(set(ids)):
        raise AttemptError("Pytest collected zero or duplicate historical tests")
    return ids


def _parse_junit(path: Path) -> tuple[dict[str, int], list[str]]:
    content = path.read_bytes()
    if len(content) > MAX_REPORT_BYTES or b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise AttemptError("JUnit report exceeds limits or contains entities")
    root = ET.fromstring(content)
    cases = list(root.iter("testcase"))
    if not cases:
        raise AttemptError("JUnit report contains zero tests")
    names = [case.get("name") for case in cases]
    if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise AttemptError("JUnit report has missing or duplicate test names")
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
    for case in cases:
        if case.find("error") is not None:
            counts["errors"] += 1
        elif (failure := case.find("failure")) is not None:
            counts["failed"] += 1
            if failure.get("type") == "pytest.xpass":
                counts["xpassed"] += 1
        elif (skipped := case.find("skipped")) is not None:
            counts["skipped"] += 1
            if skipped.get("type") == "pytest.xfail":
                counts["xfailed"] += 1
        else:
            counts["passed"] += 1
    return counts, names


def _empty_steps() -> dict[str, dict[str, int | bool | None]]:
    return {name: {"exit_code": None, "timed_out": False}
            for name in ("install", "collect", "test", "pip_check")}


def run_attempt(work: Path, phase: str) -> dict:
    if phase not in VERSIONS:
        raise ValueError("Unsupported fixed phase")
    export = work / "export"
    export.mkdir(mode=0o700, exist_ok=True)
    marker = {
        "schema_version": 1, "phase": phase, "status": "infrastructure_failed",
        "steps": _empty_steps(), "collected_test_ids": [],
        "counts": {name: 0 for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")},
        "installed_requests_version": None, "source_sha256": "0" * 64, "error": None,
    }
    try:
        manifest = _load_manifest(work, phase)
        marker["source_sha256"] = manifest["source_sha256"]
        source = work / "source"
        _extract_source(work / "source.zip", source)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/work", "TMPDIR": "/tmp",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
               "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        install_started = time.monotonic()
        install_log = export / "install.log"
        code, timed_out, truncated = _run_command(
            [sys.executable, "-m", "venv", "/work/venv"], install_log,
            timeout_seconds=120, cwd=work, env=env)
        if code == 0 and not truncated and not timed_out:
            remaining = max(0.1, 120 - (time.monotonic() - install_started))
            code, timed_out, truncated = _run_command(
                ["/work/venv/bin/python", "-m", "pip", "install", "--no-index",
                 "--find-links=/work/wheels", "--only-binary=:all:", "--require-hashes",
                 "--report=/work/export/pip-report.json", "-r", "/work/requirements.txt"],
                install_log, timeout_seconds=remaining, cwd=work, env=env)
        marker["steps"]["install"] = {"exit_code": code, "timed_out": timed_out}
        if code != 0 or timed_out or truncated:
            marker["status"] = "install_failed"
            marker["error"] = "Offline installation failed or exceeded its limit"
            return marker

        check_code, check_timeout, check_truncated = _run_command(
            ["/work/venv/bin/python", "-m", "pip", "check"], export / "pip-check.txt",
            timeout_seconds=30, cwd=work, env=env)
        marker["steps"]["pip_check"] = {"exit_code": check_code, "timed_out": check_timeout}
        if check_code != 0 or check_timeout or check_truncated:
            marker["status"] = "install_failed"
            marker["error"] = "Installed dependency check failed"
            return marker

        list_code, list_timeout, list_truncated = _run_command(
            ["/work/venv/bin/python", "-m", "pip", "list", "--format=json"],
            export / "installed.json", timeout_seconds=30, cwd=work, env=env)
        if list_code != 0 or list_timeout or list_truncated:
            raise AttemptError("Installed inventory could not be recorded")
        installed = json.loads((export / "installed.json").read_bytes())
        if not isinstance(installed, list):
            raise AttemptError("Installed inventory has an unexpected shape")
        found = [item.get("version") for item in installed
                 if isinstance(item, dict) and str(item.get("name", "")).lower() == "requests"]
        if found != [VERSIONS[phase]]:
            raise AttemptError("Actual installed Requests version does not match phase")
        marker["installed_requests_version"] = found[0]

        test_env = {**env, "PYTHONPATH": str(source)}
        collect_code, collect_timeout, collect_truncated = _run_command(
            ["/work/venv/bin/python", "-m", "pytest", "--collect-only", "-q",
             "requests_unixsocket/tests"], export / "collect.txt",
            timeout_seconds=120, cwd=source, env=test_env)
        marker["steps"]["collect"] = {"exit_code": collect_code, "timed_out": collect_timeout}
        if collect_code != 0 or collect_timeout or collect_truncated:
            marker["status"] = "collection_failed"
            marker["error"] = "Historical test collection failed or exceeded its limit"
            return marker
        ids = _collect_ids((export / "collect.txt").read_bytes())
        marker["collected_test_ids"] = ids

        test_code, test_timeout, test_truncated = _run_command(
            ["/work/venv/bin/python", "-m", "pytest", "requests_unixsocket/tests",
             "--junitxml=/work/export/junit.xml"], export / "test.log",
            timeout_seconds=120, cwd=source, env=test_env)
        marker["steps"]["test"] = {"exit_code": test_code, "timed_out": test_timeout}
        if (export / "junit.xml").is_file():
            marker["counts"], report_names = _parse_junit(export / "junit.xml")
            if set(report_names) != {item.rsplit("::", 1)[-1] for item in ids}:
                raise AttemptError("JUnit test names differ from collected tests")
        if test_code != 0 or test_timeout or test_truncated:
            marker["status"] = "test_failed"
            marker["error"] = "Historical tests failed or exceeded their limit"
            return marker
        case_count = sum(marker["counts"][name] for name in ("passed", "failed", "errors", "skipped"))
        if (case_count != len(ids) or marker["counts"]["passed"] < 1
                or marker["counts"]["failed"] or marker["counts"]["errors"]):
            raise AttemptError("JUnit report does not cover collected tests")
        marker["status"] = "passed"
        return marker
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, AttemptError) as exc:
        marker["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        return marker


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in VERSIONS:
        return 2
    work = Path("/work")
    while not (work / "ready").exists():
        time.sleep(0.05)
    marker = run_attempt(work, sys.argv[1])
    export = work / "export"
    temporary = export / "attempt.json.tmp"
    temporary.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(export / "attempt.json")
    while not (work / "ack").exists():
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
