"""Prepare a fixed, hash-checked historical reproduction without executing source."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener


PROFILE_ID = "requests-unixsocket-historical-0.3.0-research"
COMMIT_SHA = "8449bc0f76a2ce410644b5e8aab45829ddca54f7"
SOURCE_URL = f"https://codeload.github.com/msabramo/requests-unixsocket/zip/{COMMIT_SHA}"
BASELINE_VERSION = "2.31.0"
TARGET_VERSION = "2.34.2"
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_WHEEL_BYTES = 5 * 1024 * 1024
MAX_INPUT_TAR_BYTES = 128 * 1024 * 1024
SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
WHEEL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+-(?:py3|py2\.py3)-none-any\.whl\Z")

# Trial fixture pins. A profile remains disabled until both fresh attempts pass admission checks.
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PreparationError(RuntimeError):
    """An approved source could not be prepared as a reproducible wheel bundle."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class BoundedFetcher:
    """HTTPS fetcher with no environment proxy or redirect following."""

    def __init__(self) -> None:
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler())

    def fetch(self, url: str, *, max_bytes: int, body: bytes | None = None) -> bytes:
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise PreparationError(f"Approved source returned HTTP {response.status}")
                data = response.read(max_bytes + 1)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise PreparationError(f"Approved source unavailable: {type(exc).__name__}") from None
        if len(data) > max_bytes:
            raise PreparationError("Approved source exceeds byte limit")
        return data


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except ValueError:
        raise PreparationError("Approved metadata is not JSON") from None
    if not isinstance(value, dict):
        raise PreparationError("Approved metadata has an unexpected shape")
    return value


def _wheel_from_pypi(fetcher: BoundedFetcher, package: str, version: str) -> tuple[str, bytes, dict[str, Any]]:
    metadata_url = f"https://pypi.org/pypi/{package}/{version}/json"
    metadata = _json_object(fetcher.fetch(metadata_url, max_bytes=MAX_METADATA_BYTES))
    info = metadata.get("info")
    if not isinstance(info, dict) or info.get("version") != version or info.get("yanked") is not False:
        raise PreparationError(f"{package} {version} is not a confirmed non-yanked release")
    releases = metadata.get("urls")
    if not isinstance(releases, list):
        raise PreparationError(f"No release files for {package} {version}")
    wheels = [item for item in releases if isinstance(item, dict)
              and item.get("packagetype") == "bdist_wheel"
              and isinstance(item.get("filename"), str)
              and WHEEL_NAME_RE.fullmatch(item["filename"])]
    if len(wheels) != 1:
        raise PreparationError(f"Exactly one universal wheel required for {package} {version}")
    selected = wheels[0]
    filename = selected["filename"]
    expected_prefix = package.replace("-", "_").lower() + "-" + version + "-"
    if not filename.lower().startswith(expected_prefix) or selected.get("yanked") is not False:
        raise PreparationError(f"Unexpected wheel identity for {package} {version}")
    url = selected.get("url")
    if not isinstance(url, str):
        raise PreparationError("Wheel URL is missing")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "files.pythonhosted.org" or parsed.query or parsed.fragment:
        raise PreparationError("Wheel URL is outside approved PyPI host")
    if not parsed.path.startswith("/packages/") or parsed.path.rsplit("/", 1)[-1] != filename:
        raise PreparationError("Wheel URL does not match its filename")
    digests = selected.get("digests")
    expected_hash = digests.get("sha256") if isinstance(digests, dict) else None
    if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
        raise PreparationError("Wheel metadata lacks a SHA-256 digest")
    wheel_data = fetcher.fetch(url, max_bytes=MAX_WHEEL_BYTES)
    if _sha256(wheel_data) != expected_hash:
        raise PreparationError(f"Wheel hash mismatch for {package} {version}")
    return filename, wheel_data, {
        "name": package,
        "version": version,
        "filename": filename,
        "sha256": expected_hash,
        "metadata_url": metadata_url,
        "wheel_url": url,
        "requires_python": info.get("requires_python"),
        "metadata_vulnerability_ids": sorted({item["id"] for item in metadata.get("vulnerabilities", [])
                                               if isinstance(item, dict) and isinstance(item.get("id"), str)}),
    }


def _query_osv(fetcher: BoundedFetcher, version: str) -> dict[str, Any]:
    queried_at = _utc_now()
    payload = json.dumps({"package": {"name": "requests", "ecosystem": "PyPI"},
                          "version": version}, separators=(",", ":")).encode()
    try:
        response = _json_object(fetcher.fetch("https://api.osv.dev/v1/query", max_bytes=MAX_METADATA_BYTES,
                                              body=payload))
        vulnerabilities = response.get("vulns", [])
        if not isinstance(vulnerabilities, list):
            raise PreparationError("OSV response has an unexpected shape")
        ids = sorted({item["id"] for item in vulnerabilities
                      if isinstance(item, dict) and isinstance(item.get("id"), str)})
        return {"status": "available", "queried_at": queried_at, "ids": ids}
    except PreparationError as exc:
        return {"status": "unavailable", "queried_at": queried_at, "ids": [], "error": str(exc)}


def prepare_fixed_bundles(output: Path, *, fetcher: BoundedFetcher | None = None) -> dict[str, Any]:
    """Download opaque source and approved wheels; never import or extract the source."""
    chosen_fetcher = fetcher or BoundedFetcher()
    source = chosen_fetcher.fetch(SOURCE_URL, max_bytes=MAX_SOURCE_BYTES)
    if not source.startswith(b"PK\x03\x04"):
        raise PreparationError("Historical archive is not a ZIP file")
    source_hash = _sha256(source)
    wheel_data: dict[tuple[str, str], tuple[str, bytes, dict[str, Any]]] = {}
    for name, version in (("requests", BASELINE_VERSION), ("requests", TARGET_VERSION), *COMMON_PINS.items()):
        wheel_data[name, version] = _wheel_from_pypi(chosen_fetcher, name, version)

    output.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for phase, requests_version in (("baseline", BASELINE_VERSION), ("candidate", TARGET_VERSION)):
        phase_dir = output / phase
        wheel_dir = phase_dir / "wheels"
        wheel_dir.mkdir(parents=True, exist_ok=True)
        (phase_dir / "source.zip").write_bytes(source)
        pins = {"requests": requests_version, **COMMON_PINS}
        wheel_hashes = {}
        requirement_lines = []
        selected_metadata = []
        for package, version in pins.items():
            filename, content, metadata = wheel_data[package, version]
            (wheel_dir / filename).write_bytes(content)
            wheel_hashes[filename] = metadata["sha256"]
            requirement_lines.append(f"{package}=={version} --hash=sha256:{metadata['sha256']}")
            selected_metadata.append(metadata)
        requirements = ("\n".join(requirement_lines) + "\n").encode()
        (phase_dir / "requirements.txt").write_bytes(requirements)
        manifest = {
            "schema_version": 1,
            "profile_id": PROFILE_ID,
            "commit_sha": COMMIT_SHA,
            "phase": phase,
            "requests_version": requests_version,
            "source_sha256": source_hash,
            "requirements_sha256": _sha256(requirements),
            "wheels": wheel_hashes,
        }
        (phase_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        advisory = _query_osv(chosen_fetcher, requests_version)
        metadata_record = {
            "prepared_at": _utc_now(),
            "source_url": SOURCE_URL,
            "source_sha256": source_hash,
            "packages": selected_metadata,
            "advisory": advisory,
        }
        (phase_dir / "metadata.json").write_text(json.dumps(metadata_record, indent=2) + "\n", encoding="utf-8")
        summaries[phase] = {"requests_version": requests_version, "source_sha256": source_hash,
                            "wheel_count": len(wheel_hashes), "advisory_status": advisory["status"]}
    return summaries


def build_input_tar(phase_dir: Path) -> bytes:
    """Package only verified opaque inputs for runner staging; never extract source here."""
    manifest = _json_object((phase_dir / "manifest.json").read_bytes())
    if manifest.get("profile_id") != PROFILE_ID or manifest.get("commit_sha") != COMMIT_SHA:
        raise PreparationError("Prepared profile identity does not match fixed case")
    phase = phase_dir.name
    if phase not in {"baseline", "candidate"} or manifest.get("phase") != phase:
        raise PreparationError("Prepared phase does not match directory")
    expected_version = BASELINE_VERSION if phase == "baseline" else TARGET_VERSION
    if manifest.get("requests_version") != expected_version:
        raise PreparationError("Prepared Requests version does not match phase")
    wheel_hashes = manifest.get("wheels")
    if not isinstance(wheel_hashes, dict) or len(wheel_hashes) != len(COMMON_PINS) + 1:
        raise PreparationError("Prepared wheel set has an unexpected shape")
    file_names = ["source.zip", "requirements.txt", "manifest.json"]
    for filename, digest in wheel_hashes.items():
        if not isinstance(filename, str) or not WHEEL_NAME_RE.fullmatch(filename):
            raise PreparationError("Prepared wheel has an invalid filename")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise PreparationError("Prepared wheel has an invalid hash")
        file_names.append(f"wheels/{filename}")
    files: list[tuple[str, bytes]] = []
    for name in file_names:
        content = (phase_dir / name).read_bytes()
        expected_hash = manifest.get("source_sha256") if name == "source.zip" else (
            manifest.get("requirements_sha256") if name == "requirements.txt" else
            wheel_hashes[name.removeprefix("wheels/")] if name.startswith("wheels/") else None
        )
        if expected_hash is not None and _sha256(content) != expected_hash:
            raise PreparationError(f"Prepared input hash mismatch: {name}")
        if name == "source.zip" and len(content) > MAX_SOURCE_BYTES:
            raise PreparationError("Historical archive exceeds byte limit")
        if name.startswith("wheels/") and len(content) > MAX_WHEEL_BYTES:
            raise PreparationError("Wheel exceeds byte limit")
        files.append((name, content))
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        for name, content in files:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.uid = entry.gid = 10001
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    result = archive_buffer.getvalue()
    if len(result) > MAX_INPUT_TAR_BYTES:
        raise PreparationError("Prepared input tar exceeds byte limit")
    return result
