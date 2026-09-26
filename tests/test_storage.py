"""Storage contract checks for the SQLite run, attempt, event, and artifact records."""

import hashlib
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from upgrade_chamber.storage import Store


def utc_offset(seconds: float) -> str:
    """ISO-8601 UTC timestamp shifted from now, matching the store's format."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def run_params(**overrides) -> dict:
    params = {
        "profile_id": "requests-unixsocket",
        "repo_url": "https://github.com/msabramo/requests-unixsocket",
        "commit_sha": "a" * 40,
        "dependency": "requests",
        "requested_ref": None,
        "idempotency_key": None,
        "submit_ip": "127.0.0.1",
        "image_identity": "python-runner@sha256:" + "b" * 64,
        "source_sha256": "c" * 64,
        "baseline_version": "2.31.0",
        "target_version": "2.32.2",
        "job_deadline_seconds": 600.0,
    }
    params.update(overrides)
    return params


class StorageContractTests(unittest.TestCase):
    def setUp(self):
        self._context = tempfile.TemporaryDirectory()
        self.addCleanup(self._context.cleanup)
        base = Path(self._context.name)
        self.artifact_dir = base / "artifacts"
        self.store = Store(base / "runs.db", self.artifact_dir)
        self.addCleanup(self.store.close)

    def create_run(self, **overrides) -> tuple[int, str]:
        return self.store.create_run(**run_params(**overrides))

    def test_store_chmods_database_group_writable(self):
        base = Path(self._context.name)
        db_path = base / "shared.db"
        with mock.patch("upgrade_chamber.storage.os.chmod") as chmod:
            store = Store(db_path, base / "shared-artifacts")
        self.addCleanup(store.close)
        calls = {args[0]: args[1] for args, _ in chmod.call_args_list}
        self.assertIn(db_path, calls)
        self.assertEqual(calls[db_path], 0o660)

    def test_create_run_returns_token_and_stores_only_hash(self):
        run_id, token = self.create_run()
        self.assertIsInstance(run_id, int)
        self.assertIsInstance(token, str)
        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "queued")
        self.assertEqual(run["cancel_requested"], 0)
        self.assertEqual(run["profile_id"], "requests-unixsocket")
        self.assertEqual(run["dependency"], "requests")
        self.assertIsNone(run["requested_ref"])
        self.assertEqual(run["job_deadline_seconds"], 600.0)
        self.assertEqual(run["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertTrue(run["created_utc"].endswith("+00:00"))
        self.assertEqual(run["created_utc"], run["updated_utc"])
        self.assertTrue(self.store.verify_token(run_id, token))
        self.assertFalse(self.store.verify_token(run_id, token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertFalse(self.store.verify_token(run_id, ""))
        self.assertFalse(self.store.verify_token(run_id + 1000, token))
        self.assertIsNone(self.store.get_run(run_id + 1000))

    def test_idempotency_duplicate_raises_integrity_error(self):
        run_id, _ = self.create_run(idempotency_key="submit-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_run(idempotency_key="submit-1")
        other_id, _ = self.create_run(idempotency_key="submit-2")
        self.assertNotEqual(run_id, other_id)

    def test_find_run_by_idempotency_round_trip(self):
        run_id, _ = self.create_run(idempotency_key="find-me")
        found = self.store.find_run_by_idempotency("find-me")
        self.assertEqual(found["id"], run_id)
        self.assertEqual(found["repo_url"], run_params()["repo_url"])
        self.assertIsNone(self.store.find_run_by_idempotency("absent"))

    def test_lease_next_run_orders_and_skips_non_queued(self):
        first, second, third = (self.create_run()[0] for _ in range(3))
        lease = self.store.lease_next_run("worker-1", 300.0)
        self.assertEqual(lease["id"], first)
        self.assertEqual(lease["leased_by"], "worker-1")
        self.assertIsNotNone(lease["lease_expires_utc"])
        self.assertEqual(lease["state"], "queued")
        self.assertEqual(self.store.lease_next_run("worker-1", 300.0)["id"], second)
        self.store.set_run_state(third, "cancelled")
        self.assertIsNone(self.store.lease_next_run("worker-1", 300.0))
        failed = self.create_run()[0]
        self.store.set_run_state(failed, "baseline_failed")
        queued = self.create_run()[0]
        self.assertEqual(self.store.lease_next_run("worker-2", 300.0)["id"], queued)

    def test_heartbeat_extends_lease_only_for_owner_and_live_run(self):
        run_id, _ = self.create_run()
        self.store.lease_next_run("worker-1", 60.0)
        expiry = self.store.get_run(run_id)["lease_expires_utc"]
        time.sleep(0.002)
        self.store.heartbeat(run_id, "worker-2", 600.0)
        self.assertEqual(self.store.get_run(run_id)["lease_expires_utc"], expiry)
        self.store.heartbeat(run_id, "worker-1", 600.0)
        extended = self.store.get_run(run_id)["lease_expires_utc"]
        self.assertGreater(extended, expiry)
        self.store.set_run_state(run_id, "cancelled")
        self.store.heartbeat(run_id, "worker-1", 900.0)
        self.assertEqual(self.store.get_run(run_id)["lease_expires_utc"], extended)

    def test_requeue_stale_leases_fails_only_expired_non_terminal_runs(self):
        stale, terminal, fresh = (self.create_run()[0] for _ in range(3))
        self.store.lease_next_run("worker-1", 60.0)
        self.store.lease_next_run("worker-2", 60.0)
        self.store.set_run_state(terminal, "cancelled")
        requeued = self.store.requeue_stale_leases(now_utc=utc_offset(3600))
        self.assertEqual(requeued, [stale])
        failed = self.store.get_run(stale)
        self.assertEqual(failed["state"], "infrastructure_failed")
        self.assertEqual(failed["status_detail"], "worker interrupted before completion")
        self.assertEqual(self.store.get_run(terminal)["state"], "cancelled")
        untouched = self.store.get_run(fresh)
        self.assertEqual(untouched["state"], "queued")
        self.assertIsNone(untouched["lease_expires_utc"])
        self.assertEqual(self.store.requeue_stale_leases(), [])

    def test_set_run_state_updates_state_detail_and_updated_utc(self):
        run_id, _ = self.create_run()
        before = self.store.get_run(run_id)["updated_utc"]
        time.sleep(0.002)
        self.store.set_run_state(run_id, "baseline_failed", detail="install exited 1")
        run = self.store.get_run(run_id)
        self.assertEqual(run["state"], "baseline_failed")
        self.assertEqual(run["status_detail"], "install exited 1")
        self.assertGreater(run["updated_utc"], before)

    def test_update_run_whitelist_refresh_and_unknown_rejection(self):
        run_id, _ = self.create_run()
        self.store.set_run_state(run_id, "upgrade_failed")
        with self.assertRaises(ValueError):
            self.store.update_run(run_id, state="queued")
        with self.assertRaises(ValueError):
            self.store.update_run(run_id, token_hash="forged")
        with self.assertRaises(ValueError):
            self.store.update_run(run_id, nonsense=1)
        self.assertEqual(self.store.get_run(run_id)["state"], "upgrade_failed")
        before = self.store.get_run(run_id)["updated_utc"]
        time.sleep(0.002)
        self.store.update_run(
            run_id, cleanup_state="removed", result="passed",
            deadline_utc="2026-09-27T00:00:00+00:00", terminal_utc="2026-09-26T23:00:00+00:00")
        run = self.store.get_run(run_id)
        self.assertEqual(run["cleanup_state"], "removed")
        self.assertEqual(run["result"], "passed")
        self.assertEqual(run["deadline_utc"], "2026-09-27T00:00:00+00:00")
        self.assertEqual(run["terminal_utc"], "2026-09-26T23:00:00+00:00")
        self.assertGreater(run["updated_utc"], before)

    def test_request_cancel_semantics(self):
        live, done = (self.create_run()[0] for _ in range(2))
        self.assertTrue(self.store.request_cancel(live))
        self.assertTrue(self.store.cancel_requested(live))
        self.assertTrue(self.store.request_cancel(live))
        self.store.set_run_state(done, "cancelled")
        self.assertFalse(self.store.request_cancel(done))
        self.assertFalse(self.store.cancel_requested(done))
        self.assertFalse(self.store.request_cancel(done + 1000))
        self.assertFalse(self.store.cancel_requested(done + 1000))

    def test_attempt_round_trip_preserves_marker_and_names(self):
        run_id, _ = self.create_run()
        marker = {"schema_version": 1, "phase": "baseline", "status": "passed",
                  "counts": {"passed": 5, "failed": 0}}
        first = self.store.add_attempt(
            run_id, phase="baseline", status="passed", container_id="container-1",
            started_utc="2026-09-26T00:00:00+00:00", finished_utc="2026-09-26T00:00:10+00:00",
            elapsed_seconds=10.0, exit_code=0, marker=marker,
            artifact_names=["install.log", "junit.xml"], stdout="ok", stderr="")
        second = self.store.add_attempt(
            run_id, phase="candidate", status="test_failed", container_id=None,
            started_utc="2026-09-26T00:01:00+00:00", finished_utc="2026-09-26T00:01:20+00:00",
            elapsed_seconds=20.0, exit_code=None, marker=None, artifact_names=[],
            stdout="", stderr="boom")
        records = self.store.attempts(run_id)
        self.assertEqual([record["id"] for record in records], [first, second])
        self.assertEqual([record["phase"] for record in records], ["baseline", "candidate"])
        baseline = records[0]
        self.assertEqual(baseline["run_id"], run_id)
        self.assertEqual(baseline["status"], "passed")
        self.assertEqual(baseline["container_id"], "container-1")
        self.assertEqual(baseline["exit_code"], 0)
        self.assertEqual(baseline["marker"], marker)
        self.assertEqual(baseline["artifact_names"], ["install.log", "junit.xml"])
        self.assertEqual(baseline["stdout"], "ok")
        candidate = records[1]
        self.assertIsNone(candidate["container_id"])
        self.assertIsNone(candidate["exit_code"])
        self.assertIsNone(candidate["marker"])
        self.assertEqual(candidate["artifact_names"], [])
        self.assertEqual(candidate["stderr"], "boom")
        self.assertEqual(self.store.attempts(second + 1), [])

    def test_events_have_increasing_ids_and_cursor_respects_after_and_limit(self):
        run_id, other = (self.create_run()[0] for _ in range(2))
        first = self.store.append_event(run_id, "state", {"state": "queued"})
        cross = self.store.append_event(other, "state", {"note": "other run"})
        self.assertGreater(cross, first)
        ids = [self.store.append_event(run_id, "progress", {"n": n}) for n in range(4)]
        self.assertEqual(ids, sorted(set(ids)))
        everything = self.store.events_after(run_id, 0)
        self.assertEqual([event["id"] for event in everything], [first] + ids)
        self.assertEqual([event["data"]["n"] for event in everything[1:]], [0, 1, 2, 3])
        self.assertEqual(everything[0]["data"], {"state": "queued"})
        self.assertEqual(everything[0]["kind"], "state")
        self.assertEqual(everything[0]["run_id"], run_id)
        window = self.store.events_after(run_id, ids[1], limit=2)
        self.assertEqual([event["id"] for event in window], ids[2:])
        self.assertEqual(self.store.events_after(run_id, ids[-1]), [])

    def test_artifact_round_trip_replace_and_name_validation(self):
        run_id, _ = self.create_run()
        record = self.store.save_artifact(run_id, "install.log", b"hello", kind="log")
        self.assertEqual(record["run_id"], run_id)
        self.assertEqual(record["name"], "install.log")
        self.assertEqual(record["bytes"], 5)
        self.assertEqual(record["sha256"], hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(record["kind"], "log")
        self.assertTrue(record["created_utc"])
        self.assertEqual(self.store.get_artifact(run_id, "install.log"), b"hello")
        self.assertTrue((self.artifact_dir / str(run_id) / "install.log").is_file())
        replaced = self.store.save_artifact(run_id, "install.log", b"longer content!", kind="log")
        self.assertEqual(replaced["bytes"], 15)
        self.assertEqual(replaced["sha256"], hashlib.sha256(b"longer content!").hexdigest())
        self.assertEqual(self.store.get_artifact(run_id, "install.log"), b"longer content!")
        self.store.save_artifact(run_id, "report.xml", b"<xml/>", kind="report")
        listed = self.store.list_artifacts(run_id)
        self.assertEqual([item["name"] for item in listed], ["install.log", "report.xml"])
        self.assertEqual(self.store.get_artifact(run_id + 500, "install.log"), None)
        self.assertIsNone(self.store.get_artifact(run_id, "missing.log"))
        for bad in ("../x", "a/b", "", "x" * 200):
            with self.assertRaises(ValueError):
                self.store.save_artifact(run_id, bad, b"x", kind="log")

    def test_advisory_cache_round_trip_and_overwrite(self):
        self.assertIsNone(self.store.get_advisory("requests", "2.31.0"))
        self.store.put_advisory("requests", "2.31.0", {"vulns": ["GHSA-9wx4-h78v-vm56"]})
        cached = self.store.get_advisory("requests", "2.31.0")
        self.assertEqual(cached["response"], {"vulns": ["GHSA-9wx4-h78v-vm56"]})
        self.assertTrue(cached["fetched_utc"].endswith("+00:00"))
        self.store.put_advisory("requests", "2.31.0", {"vulns": []})
        self.assertEqual(self.store.get_advisory("requests", "2.31.0")["response"], {"vulns": []})
        self.assertIsNone(self.store.get_advisory("requests", "2.32.2"))

    def test_delete_run_cascades_rows_files_and_tolerates_missing_files(self):
        run_id, _ = self.create_run()
        self.store.add_attempt(
            run_id, phase="baseline", status="passed", container_id="c",
            started_utc="2026-09-26T00:00:00+00:00", finished_utc="2026-09-26T00:00:01+00:00",
            elapsed_seconds=1.0, exit_code=0, marker={"ok": True}, artifact_names=["a.log"],
            stdout="", stderr="")
        self.store.append_event(run_id, "state", {"state": "queued"})
        self.store.save_artifact(run_id, "a.log", b"one", kind="log")
        self.store.save_artifact(run_id, "b.txt", b"two", kind="log")
        self.store.delete_run(run_id)
        self.assertIsNone(self.store.get_run(run_id))
        self.assertEqual(self.store.attempts(run_id), [])
        self.assertEqual(self.store.events_after(run_id, 0), [])
        self.assertEqual(self.store.list_artifacts(run_id), [])
        self.assertFalse((self.artifact_dir / str(run_id)).exists())
        self.assertEqual(self.store.storage_bytes(), 0)
        orphan = self.create_run()[0]
        self.store.save_artifact(orphan, "gone.log", b"x", kind="log")
        (self.artifact_dir / str(orphan) / "gone.log").unlink()
        self.store.delete_run(orphan)
        self.assertIsNone(self.store.get_run(orphan))
        self.store.delete_run(run_id + 5000)

    def test_storage_bytes_sums_artifacts(self):
        self.assertEqual(self.store.storage_bytes(), 0)
        run_id, _ = self.create_run()
        self.store.save_artifact(run_id, "a.log", b"abc", kind="log")
        self.store.save_artifact(run_id, "b.log", b"12345", kind="log")
        self.assertEqual(self.store.storage_bytes(), 8)
        self.store.save_artifact(run_id, "a.log", b"ab", kind="log")
        self.assertEqual(self.store.storage_bytes(), 7)

    def test_terminal_runs_older_than_count_state_and_ip_window(self):
        old = self.create_run()[0]
        self.store.update_run(old, terminal_utc="2026-01-01T00:00:00+00:00")
        self.store.set_run_state(old, "baseline_failed")
        future = self.create_run()[0]
        self.store.set_run_state(future, "cancelled")
        self.store.update_run(future, terminal_utc="2026-12-01T00:00:00+00:00")
        queued = self.create_run()[0]
        expired = self.store.terminal_runs_older_than("2026-06-01T00:00:00+00:00")
        self.assertEqual([run["id"] for run in expired], [old])
        self.assertEqual(self.store.count_state("queued"), 1)
        self.assertEqual(self.store.count_state("baseline_failed"), 1)
        self.assertEqual(self.store.count_state("cancelled"), 1)
        self.assertEqual(self.store.count_state("nope"), 0)
        self.assertEqual(self.store.recent_submissions_from_ip("127.0.0.1", 3600), 3)
        first, second = self.create_run(submit_ip="1.1.1.1")[0], self.create_run(submit_ip="1.1.1.1")[0]
        self.create_run(submit_ip="2.2.2.2")
        self.assertEqual(self.store.recent_submissions_from_ip("1.1.1.1", 3600), 2)
        self.assertEqual(self.store.recent_submissions_from_ip("2.2.2.2", 60), 1)
        self.assertEqual(self.store.recent_submissions_from_ip("9.9.9.9", 60), 0)

    def test_concurrent_lease_never_hands_out_the_same_run(self):
        queued = [self.create_run()[0] for _ in range(3)]
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(number: int) -> None:
            barrier.wait()
            lease = self.store.lease_next_run(f"worker-{number}", 300.0)
            with lock:
                results.append(lease)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 10)
        leased = [result["id"] for result in results if result is not None]
        self.assertEqual(sorted(leased), sorted(queued))
        self.assertEqual(len(set(leased)), 3)
        for run_id in leased:
            self.assertTrue(self.store.get_run(run_id)["leased_by"].startswith("worker-"))


if __name__ == "__main__":
    unittest.main()
