"""Controller-owned SQLite persistence for runs, attempts, events, and artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Terminal alternatives from the fixed state machine in tech-stack.md. A run in
# one of these states (or with terminal_utc set) accepts no further leases,
# cancel requests, or stale-lease requeues.
TERMINAL_STATES = frozenset({
    "unsupported",
    "baseline_failed",
    "upgrade_failed",
    "timed_out",
    "cancelled",
    "infrastructure_failed",
})

_TERMINAL_LIST = tuple(sorted(TERMINAL_STATES))
_TERMINAL_SQL = ", ".join("?" for _ in _TERMINAL_LIST)

_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

_RUN_MUTABLE_COLUMNS = frozenset({
    "cleanup_state",
    "result",
    "baseline_version",
    "target_version",
    "source_sha256",
    "image_identity",
    "deadline_utc",
    "terminal_utc",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    dependency TEXT NOT NULL,
    requested_ref TEXT,
    idempotency_key TEXT UNIQUE,
    submit_ip TEXT NOT NULL,
    image_identity TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    target_version TEXT NOT NULL,
    job_deadline_seconds REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cleanup_state TEXT,
    result TEXT,
    status_detail TEXT,
    leased_by TEXT,
    lease_expires_utc TEXT,
    deadline_utc TEXT,
    terminal_utc TEXT,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    token_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    container_id TEXT,
    started_utc TEXT NOT NULL,
    finished_utc TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    exit_code INTEGER,
    marker_json TEXT,
    artifact_names_json TEXT,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    UNIQUE (run_id, name)
);

CREATE TABLE IF NOT EXISTS advisory_cache (
    package TEXT NOT NULL,
    version TEXT NOT NULL,
    response_json TEXT NOT NULL,
    fetched_utc TEXT NOT NULL,
    PRIMARY KEY (package, version)
);

CREATE INDEX IF NOT EXISTS runs_state_idx ON runs(state);
CREATE INDEX IF NOT EXISTS runs_lease_expires_idx ON runs(lease_expires_utc);
"""


def _utc_now() -> str:
    """Current wall-clock time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def _utc_offset(seconds: float) -> str:
    """ISO-8601 UTC timestamp shifted by signed seconds from now."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class Store:
    """SQLite record store shared by the API and the serial worker.

    One connection serves every caller and a lock serializes all database
    work, so statements from different threads never interleave. Timestamps
    are ISO-8601 UTC strings produced by one formatter, which makes the text
    comparisons used for lease expiry and pruning reliable.
    """

    def __init__(self, db_path: str | Path, artifact_dir: str | Path) -> None:
        self._artifact_dir = Path(artifact_dir)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.isolation_level = None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # The schema write in WAL mode forces the -wal/-shm sidecar files to exist so the creating process can chmod its own files.
        # This heals cross-service sharing by keeping the shared files group-writable.
        # Only the file's owner can chmod it; the other service sets the mode when it creates the file.
        for target in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
            if target.exists():
                try:
                    os.chmod(target, 0o660)
                except OSError:
                    pass

    @staticmethod
    def _rows(cursor: sqlite3.Cursor) -> list[dict]:
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # --- runs ---

    def create_run(self, *, profile_id: str, repo_url: str, commit_sha: str, dependency: str,
                   requested_ref: str | None, idempotency_key: str | None, submit_ip: str,
                   image_identity: str, source_sha256: str, baseline_version: str,
                   target_version: str, job_deadline_seconds: float) -> tuple[int, str]:
        """Insert a queued run and return its id with the plaintext access token."""
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO runs (profile_id, repo_url, commit_sha, dependency, requested_ref,"
                " idempotency_key, submit_ip, image_identity, source_sha256, baseline_version,"
                " target_version, job_deadline_seconds, state, cancel_requested, token_hash,"
                " created_utc, updated_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)",
                (profile_id, repo_url, commit_sha, dependency, requested_ref, idempotency_key,
                 submit_ip, image_identity, source_sha256, baseline_version, target_version,
                 job_deadline_seconds, token_hash, now, now),
            )
            run_id = cursor.lastrowid
        return run_id, token

    def get_run(self, run_id: int) -> dict | None:
        with self._lock:
            rows = self._rows(self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)))
        return rows[0] if rows else None

    def find_run_by_idempotency(self, key: str) -> dict | None:
        with self._lock:
            rows = self._rows(
                self._conn.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)))
        return rows[0] if rows else None

    def set_run_state(self, run_id: int, state: str, *, detail: str | None = None) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET state = ?, status_detail = ?, updated_utc = ? WHERE id = ?",
                (state, detail, now, run_id),
            )

    def update_run(self, run_id: int, **columns) -> None:
        unknown = sorted(set(columns) - _RUN_MUTABLE_COLUMNS)
        if unknown:
            raise ValueError(f"Unknown run columns: {', '.join(unknown)}")
        now = _utc_now()
        with self._lock:
            if columns:
                assignments = ", ".join(f"{name} = ?" for name in sorted(columns))
                parameters = [columns[name] for name in sorted(columns)]
                self._conn.execute(
                    f"UPDATE runs SET {assignments}, updated_utc = ? WHERE id = ?",
                    (*parameters, now, run_id),
                )
            else:
                self._conn.execute(
                    "UPDATE runs SET updated_utc = ? WHERE id = ?", (now, run_id))

    def request_cancel(self, run_id: int) -> bool:
        """Flag a non-terminal run for cancellation; report whether it took effect."""
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT state, terminal_utc FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row[0] in TERMINAL_STATES or row[1] is not None:
                return False
            self._conn.execute(
                "UPDATE runs SET cancel_requested = 1, updated_utc = ? WHERE id = ?",
                (now, run_id),
            )
            return True

    def cancel_requested(self, run_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
        return bool(row and row[0])

    def verify_token(self, run_id: int, token: str) -> bool:
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._conn.execute(
                "SELECT token_hash FROM runs WHERE id = ?", (run_id,)).fetchone()
        return bool(row) and hmac.compare_digest(row[0], candidate)

    def lease_next_run(self, worker_id: str, lease_seconds: float) -> dict | None:
        """Atomically claim the oldest queued run that has never been leased.

        The SELECT and UPDATE run inside one BEGIN IMMEDIATE transaction, so
        concurrent workers can never receive the same run. Runs whose lease
        expired do not re-enter the queue here; requeue_stale_leases turns
        them into an explicit infrastructure failure instead of silently
        resuming an interrupted worker's job.
        """
        now = _utc_now()
        expires = _utc_offset(lease_seconds)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._rows(self._conn.execute(
                    "SELECT * FROM runs WHERE state = 'queued' AND lease_expires_utc IS NULL"
                    " ORDER BY id LIMIT 1"))
                if not rows:
                    self._conn.execute("ROLLBACK")
                    return None
                run = rows[0]
                self._conn.execute(
                    "UPDATE runs SET leased_by = ?, lease_expires_utc = ?, updated_utc = ?"
                    " WHERE id = ?",
                    (worker_id, expires, now, run["id"]),
                )
                run = self._rows(self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run["id"],)))[0]
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return run

    def heartbeat(self, run_id: int, worker_id: str, lease_seconds: float) -> None:
        """Extend the lease only when the caller owns it and the run is not terminal."""
        now = _utc_now()
        expires = _utc_offset(lease_seconds)
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET lease_expires_utc = ?, updated_utc = ?"
                f" WHERE id = ? AND leased_by = ? AND state NOT IN ({_TERMINAL_SQL})",
                (expires, now, run_id, worker_id, *_TERMINAL_LIST),
            )

    def requeue_stale_leases(self, *, now_utc: str | None = None) -> list[int]:
        """Mark expired non-terminal leases as infrastructure failures and return their ids.

        The affected runs never return to the queue; a worker or operator must
        start fresh work rather than silently resume the interrupted job.
        """
        now = now_utc or _utc_now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM runs WHERE lease_expires_utc IS NOT NULL"
                f" AND lease_expires_utc < ? AND state NOT IN ({_TERMINAL_SQL})",
                (now, *_TERMINAL_LIST),
            ).fetchall()
            run_ids = [row[0] for row in rows]
            for run_id in run_ids:
                self._conn.execute(
                    "UPDATE runs SET state = 'infrastructure_failed',"
                    " status_detail = 'worker interrupted before completion',"
                    " updated_utc = ? WHERE id = ?",
                    (now, run_id),
                )
        return run_ids

    def count_state(self, state: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE state = ?", (state,)).fetchone()
        return int(row[0])

    def recent_submissions_from_ip(self, submit_ip: str, within_seconds: int) -> int:
        cutoff = _utc_offset(-within_seconds)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE submit_ip = ? AND created_utc >= ?",
                (submit_ip, cutoff),
            ).fetchone()
        return int(row[0])

    def terminal_runs_older_than(self, cutoff_utc: str) -> list[dict]:
        """Terminal runs whose terminal time (or last update) precedes the cutoff."""
        with self._lock:
            rows = self._rows(self._conn.execute(
                f"SELECT * FROM runs WHERE (state IN ({_TERMINAL_SQL}) OR terminal_utc IS NOT NULL)"
                " AND COALESCE(terminal_utc, updated_utc) < ? ORDER BY id",
                (*_TERMINAL_LIST, cutoff_utc),
            ))
        return rows

    def delete_run(self, run_id: int) -> None:
        """Remove a run's rows through the cascades and its artifact files from disk."""
        with self._lock:
            names = [row[0] for row in self._conn.execute(
                "SELECT name FROM artifacts WHERE run_id = ?", (run_id,))]
            self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        run_dir = self._artifact_dir / str(run_id)
        for name in names:
            (run_dir / name).unlink(missing_ok=True)
        try:
            run_dir.rmdir()
        except OSError:
            pass

    def storage_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM artifacts").fetchone()
        return int(row[0])

    # --- attempts ---

    def add_attempt(self, run_id: int, *, phase: str, status: str, container_id: str | None,
                    started_utc: str, finished_utc: str, elapsed_seconds: float,
                    exit_code: int | None, marker: dict | None, artifact_names: list[str],
                    stdout: str, stderr: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO attempts (run_id, phase, status, container_id, started_utc,"
                " finished_utc, elapsed_seconds, exit_code, marker_json, artifact_names_json,"
                " stdout, stderr)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, phase, status, container_id, started_utc, finished_utc, elapsed_seconds,
                 exit_code, json.dumps(marker) if marker is not None else None,
                 json.dumps(artifact_names), stdout, stderr),
            )
            return cursor.lastrowid

    def attempts(self, run_id: int) -> list[dict]:
        with self._lock:
            rows = self._rows(
                self._conn.execute("SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)))
        decoded = []
        for row in rows:
            row["marker"] = json.loads(row.pop("marker_json")) if row["marker_json"] is not None else None
            row["artifact_names"] = json.loads(row.pop("artifact_names_json"))
            decoded.append(row)
        return decoded

    # --- events ---

    def append_event(self, run_id: int, kind: str, data: dict) -> int:
        now = _utc_now()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events (run_id, kind, data_json, created_utc) VALUES (?, ?, ?, ?)",
                (run_id, kind, json.dumps(data), now),
            )
            return cursor.lastrowid

    def events_after(self, run_id: int, after: int, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._rows(self._conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
                (run_id, after, limit),
            ))
        return [{**row, "data": json.loads(row.pop("data_json"))} for row in rows]

    # --- artifacts ---

    def save_artifact(self, run_id: int, name: str, content: bytes, *, kind: str) -> dict:
        """Store one artifact file and its record, replacing any same-name artifact.

        The name whitelist blocks separators and traversal, and the content is
        written to a temporary file in the run's directory and moved into place
        with an atomic replace so readers never observe a partial file.
        """
        if not _ARTIFACT_NAME.fullmatch(name):
            raise ValueError(f"Invalid artifact name: {name!r}")
        digest = hashlib.sha256(content).hexdigest()
        now = _utc_now()
        run_dir = self._artifact_dir / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / name
        tmp = run_dir / f".{name}.tmp-{secrets.token_hex(8)}"
        tmp.write_bytes(content)
        try:
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifacts (run_id, name, bytes, sha256, kind, created_utc)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, name) DO UPDATE SET bytes = excluded.bytes,"
                " sha256 = excluded.sha256, kind = excluded.kind, created_utc = excluded.created_utc",
                (run_id, name, len(content), digest, kind, now),
            )
            rows = self._rows(self._conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? AND name = ?", (run_id, name)))
        return rows[0]

    def get_artifact(self, run_id: int, name: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM artifacts WHERE run_id = ? AND name = ?", (run_id, name)).fetchone()
        if row is None:
            return None
        try:
            return (self._artifact_dir / str(run_id) / name).read_bytes()
        except FileNotFoundError:
            return None

    def list_artifacts(self, run_id: int) -> list[dict]:
        with self._lock:
            rows = self._rows(self._conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY name", (run_id,)))
        return rows

    # --- advisory cache ---

    def get_advisory(self, package: str, version: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response_json, fetched_utc FROM advisory_cache"
                " WHERE package = ? AND version = ?",
                (package, version),
            ).fetchone()
        if row is None:
            return None
        return {"response": json.loads(row[0]), "fetched_utc": row[1]}

    def put_advisory(self, package: str, version: str, response: dict) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO advisory_cache (package, version, response_json, fetched_utc)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(package, version) DO UPDATE SET response_json = excluded.response_json,"
                " fetched_utc = excluded.fetched_utc",
                (package, version, json.dumps(response), now),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
