"""Honest OSV advisory snapshots for exact installed versions.

Provider failures are recorded as an explicit ``unavailable`` snapshot and are
never fabricated; only caller input bugs raise.
"""

import json
from datetime import datetime, timezone
from typing import Any

import httpx


OSV_API_ROOT = "https://api.osv.dev/v1"
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_INPUT_LENGTH = 200


class OSVError(RuntimeError):
    """The advisory service failed in a way safe to record as unavailable."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated(label: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_INPUT_LENGTH:
        raise ValueError(f"OSV {label} must be a non-empty string of at most {MAX_INPUT_LENGTH} characters")
    return value


def _parsed_vulnerability(item: Any) -> dict[str, Any]:
    """Keep only id, aliases, and summary, each defaulted safely."""
    aliases = item.get("aliases")
    return {
        "id": item["id"] if isinstance(item.get("id"), str) else "",
        "aliases": [alias for alias in aliases if isinstance(alias, str)]
        if isinstance(aliases, list) else [],
        "summary": item["summary"] if isinstance(item.get("summary"), str) else "",
    }


def _requested_vulnerabilities(chosen: httpx.Client, payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        with chosen.stream("POST", f"{OSV_API_ROOT}/query", json=payload, follow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise OSVError(f"OSV returned HTTP {response.status_code}")
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise OSVError("OSV response exceeds byte limit")
                chunks.append(chunk)
    except httpx.RequestError:
        raise OSVError("OSV transport failed") from None
    try:
        data = json.loads(b"".join(chunks))
    except ValueError:
        raise OSVError("OSV returned invalid JSON") from None
    if not isinstance(data, dict):
        raise OSVError("OSV response has an unexpected shape")
    # Live probing of requests 2.34.2 showed OSV answering HTTP 200 with an
    # empty object when there are zero known vulnerabilities; treating that
    # as an error recorded a dishonest "unavailable". A body without a
    # "vulns" key is an honest empty result.
    if "vulns" not in data:
        return []
    vulnerabilities = data["vulns"]
    if not isinstance(vulnerabilities, list):
        raise OSVError("OSV response has an unexpected shape")
    # Malformed entries are skipped; a snapshot never invents a vulnerability.
    return [_parsed_vulnerability(item) for item in vulnerabilities if isinstance(item, dict)]


def query_osv(package: str, version: str, *, client: httpx.Client | None = None,
              now_utc: str | None = None) -> dict:
    """Return a self-contained advisory snapshot for one exact package version."""
    _validated("package", package)
    _validated("version", version)
    fetched_utc = now_utc if now_utc is not None else _utc_now()
    payload = {"package": {"name": package, "ecosystem": "PyPI"}, "version": version}
    owns_client = client is None
    chosen = client or httpx.Client(
        follow_redirects=False, trust_env=False, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        vulnerabilities = _requested_vulnerabilities(chosen, payload)
    except OSVError as exc:
        return {
            "schema_version": 1,
            "package": package,
            "version": version,
            "status": "unavailable",
            "fetched_utc": fetched_utc,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if owns_client:
            chosen.close()
    return {
        "schema_version": 1,
        "package": package,
        "version": version,
        "status": "available",
        "fetched_utc": fetched_utc,
        "vulnerabilities": vulnerabilities,
    }
