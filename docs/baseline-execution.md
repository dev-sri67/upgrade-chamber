# G1 historical baseline execution contract

Status: implementation contract, not an executed baseline. The historical source, tests, and fixture remain unmodified. The only reproduction override is a disclosed pinned dependency set. The baseline uses Requests 2.31.0; the proposed target is Requests 2.34.2. Both retain the same urllib3 and fixture versions so the Requests API change can be inspected separately from resolver drift.

## Preparation

Run `scripts/prepare_baseline.py` only in a bounded preparation container. Its fixed fetcher requests GitHub's approved archive host, PyPI metadata, PyPI wheel files, and OSV, rejects redirects, and disables environment proxies. Container-level egress filtering remains unimplemented; do not claim network isolation for preparation. The script downloads the exact commit archive as opaque bytes, never extracts or executes repository code, and writes `baseline` and `candidate` bundles. Downloads have byte and time limits. Each wheel must be a non-yanked `py3-none-any` or `py2.py3-none-any` release file whose SHA-256 matches current PyPI metadata. Every package and transitive dependency is pinned in a hash-bearing requirements file. Record metadata URLs, fetched UTC time, source digest, wheel names/digests, and advisory query outcome. An unavailable OSV response must remain explicit.

The supported source is `https://codeload.github.com/msabramo/requests-unixsocket/zip/8449bc0f76a2ce410644b5e8aab45829ddca54f7`. The archive is at most 10 MiB compressed. The repository's `requirements.txt` says `requests>=1.1`; the Requests 2.31.0 baseline pin is therefore a labeled reproduction snapshot, not a historical upstream pin. The fixed test scope is `requests_unixsocket/tests`. Obsolete historical pytest plugins are excluded because the fixed test command does not request them; the source and tests remain unchanged.

## Input and runner seam

The controller creates one bounded tar per phase. The runner accepts only regular entries with exact relative names `source.zip`, `requirements.txt`, `manifest.json`, and `wheels/<filename>.whl`. It rejects symlinks, links, devices, traversal, unknown files, duplicate names, and archives above 128 MiB. It does not extract the source archive on the host. Its fixed call is `run_profile_attempt(input_tar, phase="baseline" | "candidate", timeout_seconds=300)`. Each call uses a fresh digest-pinned container and the probe runner's non-root, no-network, no-mount, resource-limited policy. The fixed command is `python -I /opt/upgrade_chamber/attempt.py <phase>`. After staging input, the runner stages `/work/ready`.

`manifest.json` schema version 1 has exactly these fields:

```json
{
  "schema_version": 1,
  "profile_id": "requests-unixsocket-historical-0.3.0-research",
  "commit_sha": "8449bc0f76a2ce410644b5e8aab45829ddca54f7",
  "phase": "baseline",
  "requests_version": "2.31.0",
  "source_sha256": "<64 lowercase hex digits>",
  "requirements_sha256": "<64 lowercase hex digits>",
  "wheels": {"<wheel filename>": "<64 lowercase hex digits>"}
}
```

Candidate phase changes only `phase`, `requests_version`, and the Requests wheel/hash and requirement line. The prepared bundle records exact file digests separately from the immutable Git commit. The image digest belongs in the runner result and later evidence manifest.

## Fixed in-container work

The fixed script verifies staged hashes and safely extracts the GitHub ZIP under `/work/source`. It rejects absolute or traversal paths, links, devices, over 5,000 entries, and more than 50 MiB extracted. It creates `/work/venv` and installs only from `/work/wheels` using `pip --no-index --find-links --only-binary=:all: --require-hashes --report` and the pinned requirements. It uses `PYTHONPATH=/work/source` to import the historical project without executing `setup.py`. It runs collection and the historical pytest scope as separate fixed steps, writes JUnit XML, runs `pip check`, and records actual installed package versions. Installation and tests each have 120-second inner deadlines; the runner's external deadline remains authoritative.

Six fixed exports are `attempt.json`, `collect.txt`, `junit.xml`, `pip-report.json`, `pip-check.txt`, and `installed.json`. `install.log` and `test.log` may be exported if each and the total remain bounded by the runner's 20 MiB cap. Missing files after an early failure are reported as missing, never synthesized as successful results. `attempt.json` is written last through atomic replacement and is the completion marker. The script waits for `/work/ack` after writing it, even when install or tests fail, so the runner can export evidence before removal. The runner observes container removal separately from the test status.

`attempt.json` schema version 1:

```json
{
  "schema_version": 1,
  "phase": "baseline",
  "status": "passed",
  "steps": {
    "install": {"exit_code": 0, "timed_out": false},
    "collect": {"exit_code": 0, "timed_out": false},
    "test": {"exit_code": 0, "timed_out": false},
    "pip_check": {"exit_code": 0, "timed_out": false}
  },
  "collected_test_ids": ["requests_unixsocket/tests/test_requests_unixsocket.py::test_unix_domain_adapter_ok"],
  "counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0},
  "installed_requests_version": "2.31.0",
  "source_sha256": "<64 lowercase hex digits>",
  "error": null
}
```

Allowed statuses are `passed`, `install_failed`, `collection_failed`, `test_failed`, and `infrastructure_failed`. The sample values above show only the shape; they are not observed test evidence. A verifier must compare the actual collected IDs, skips, xfails, source digest, and installed inventory across fresh attempts. No profile is enabled until that check and an actual passing baseline exist.

## Evidence still required

The local computer has no Docker daemon. This contract and its mocked tests cannot establish G1. On Vultr, record source and image digests, exact wheel hashes, pip reports, collected IDs, JUnit counts, process exit codes, container IDs/removal, an actual dependency-only candidate run, and a live OSV response or explicit failure. Retain [research provenance](baseline-feasibility.md) and the prepared bundle manifest with the results.
