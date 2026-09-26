"""API contract checks: admission, run records, events, cancellation, artifacts, and readiness."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from upgrade_chamber.api import create_app
from upgrade_chamber.config import Settings
from upgrade_chamber.inference import InferenceError, StructuredResult
from upgrade_chamber.profiles import ENABLED_PROFILES
from upgrade_chamber.storage import Store


PROFILE = ENABLED_PROFILES[0]


def utc_offset(seconds: float) -> str:
    """ISO-8601 UTC timestamp shifted from now, matching the store's format."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def submission_body(**overrides) -> dict:
    body = {
        "repository_url": PROFILE.repository_url,
        "ref": None,
        "dependency": PROFILE.dependency,
        "profile_id": PROFILE.id,
        "idempotency_key": None,
    }
    body.update(overrides)
    return body


def run_params(**overrides) -> dict:
    """Parameters for store.create_run that mirror a POST-created queued run."""
    params = {
        "profile_id": PROFILE.id,
        "repo_url": PROFILE.repository_url,
        "commit_sha": PROFILE.commit_sha,
        "dependency": PROFILE.dependency,
        "requested_ref": None,
        "idempotency_key": None,
        "submit_ip": "testclient",
        "image_identity": "python-runner@sha256:" + "b" * 64,
        "source_sha256": "",
        "baseline_version": PROFILE.baseline_version,
        "target_version": PROFILE.target_version,
        "job_deadline_seconds": 900.0,
    }
    params.update(overrides)
    return params


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class StubInference:
    """Test double exposing chat_completion_structured with a scripted result."""

    def __init__(self) -> None:
        self.result = None
        self.calls = []

    def chat_completion_structured(self, messages, *, max_tokens, correction_prompt, timeout=None):
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "correction_prompt": correction_prompt,
                "timeout": timeout,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def select_body(**overrides) -> dict:
    body = {
        "run_id": 1,
        "package": "requests",
        "eligible_versions": ["2.31.0", "2.32.0"],
        "context": "The repository uses requests for all HTTP calls.",
    }
    body.update(overrides)
    return body


def agent_turn_body(**overrides) -> dict:
    body = {
        "run_id": 1,
        "messages": [
            {"role": "system", "content": "You are the upgrade worker agent."},
            {"role": "user", "content": "Choose the next tool call and answer with one JSON object."},
        ],
        "max_tokens": 2048,
    }
    body.update(overrides)
    return body


def selection_result(**overrides) -> StructuredResult:
    content = {"package": "requests", "target_version": "2.32.0", "rationale": "minor bump"}
    content.update(overrides)
    return StructuredResult(ok=True, content=content, usage={"total_tokens": 7}, attempts=1, error=None)


def agent_turn_result(**overrides) -> StructuredResult:
    fields = {
        "ok": True,
        "content": {"tool": "read_file", "args": {"path": "x.py"}},
        "usage": {"total_tokens": 11},
        "attempts": 1,
        "error": None,
    }
    fields.update(overrides)
    return StructuredResult(**fields)


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self._context = tempfile.TemporaryDirectory()
        self.addCleanup(self._context.cleanup)
        self.base = Path(self._context.name)

    def make_client(self, inference_factory=None, **overrides):
        settings = Settings(
            database_path=str(self.base / "runs.db"),
            artifact_dir=str(self.base / "artifacts"),
            worker_image="python-runner@sha256:" + "b" * 64,
            **overrides,
        )
        store = Store(settings.database_path, settings.artifact_dir)
        self.addCleanup(store.close)
        client = TestClient(create_app(settings, store, inference_factory=inference_factory))
        return client, store, settings

    def test_healthz(self):
        client, _, _ = self.make_client()
        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})

    def test_profiles_catalog_contains_enabled_profile_fields(self):
        client, _, _ = self.make_client()
        response = client.get("/api/profiles")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        profile = catalog["profiles"][0]
        self.assertEqual(profile["id"], PROFILE.id)
        self.assertEqual(profile["repository_url"], PROFILE.repository_url)
        self.assertEqual(profile["commit_sha"], PROFILE.commit_sha)
        self.assertEqual(profile["dependency"], PROFILE.dependency)
        self.assertEqual(profile["baseline_version"], PROFILE.baseline_version)
        self.assertEqual(profile["target_version"], PROFILE.target_version)
        self.assertEqual(catalog["research_candidates"][0]["enabled"], False)

    def test_submit_run_returns_token_and_queues_row(self):
        client, store, _ = self.make_client()
        response = client.post("/api/runs", json=submission_body())
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body["run_id"], int)
        self.assertIsInstance(body["token"], str)
        self.assertEqual(body["state"], "queued")
        row = store.get_run(body["run_id"])
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["repo_url"], PROFILE.repository_url)
        self.assertEqual(row["commit_sha"], PROFILE.commit_sha)
        self.assertIsNone(row["requested_ref"])
        self.assertTrue(store.verify_token(body["run_id"], body["token"]))

    def test_idempotent_replay_returns_200_without_token(self):
        client, store, _ = self.make_client()
        first = client.post("/api/runs", json=submission_body(idempotency_key="job-1"))
        self.assertEqual(first.status_code, 201)
        replay = client.post("/api/runs", json=submission_body(idempotency_key="job-1"))
        self.assertEqual(replay.status_code, 200)
        body = replay.json()
        self.assertEqual(body["run_id"], first.json()["run_id"])
        self.assertIsNone(body["token"])
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["note"], "token issued at first creation only")
        self.assertEqual(store.count_state("queued"), 1)

    def test_unknown_profile_rejected(self):
        client, _, _ = self.make_client()
        response = client.post("/api/runs", json=submission_body(profile_id="requests-unixsocket-unknown"))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unsupported_profile")

    def test_wrong_dependency_rejected(self):
        client, _, _ = self.make_client()
        response = client.post("/api/runs", json=submission_body(dependency="urllib3"))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "dependency_mismatch")

    def test_wrong_repository_url_rejected(self):
        client, _, _ = self.make_client()
        response = client.post(
            "/api/runs", json=submission_body(repository_url="https://github.com/evil/requests-unixsocket"))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unsupported_repository")

    def test_wrong_ref_rejected(self):
        client, _, _ = self.make_client()
        response = client.post("/api/runs", json=submission_body(ref="f" * 40))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unsupported_repository")

    def test_queue_full_rejected(self):
        client, store, _ = self.make_client(max_queued_runs=1)
        store.create_run(**run_params())
        response = client.post("/api/runs", json=submission_body())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "queue_full")

    def test_rate_limit_per_ip(self):
        client, _, _ = self.make_client(ip_submissions_per_hour=1)
        first = client.post("/api/runs", json=submission_body())
        self.assertEqual(first.status_code, 201)
        second = client.post("/api/runs", json=submission_body())
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "rate_limited")

    def test_operator_pause_blocks_submissions(self):
        client, _, _ = self.make_client(operator_paused=True)
        response = client.post("/api/runs", json=submission_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "operator_paused")

    def test_body_validation_errors_use_error_shape(self):
        client, _, _ = self.make_client()
        response = client.post("/api/runs", json={"dependency": "requests"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    def test_missing_and_wrong_token_rejected(self):
        client, store, _ = self.make_client()
        run_id, _ = store.create_run(**run_params())
        missing = client.get(f"/api/runs/{run_id}")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "unauthorized")
        wrong = client.get(f"/api/runs/{run_id}", headers=auth_headers("not-the-token"))
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["error"]["code"], "unauthorized")

    def test_run_detail_summary(self):
        client, store, _ = self.make_client()
        run_id, token = store.create_run(**run_params())
        started = utc_offset(-120)
        finished = utc_offset(-118)
        store.add_attempt(
            run_id, phase="preparing", status="completed", container_id="container-1",
            started_utc=started, finished_utc=finished, elapsed_seconds=2.0,
            exit_code=0, marker=None, artifact_names=[], stdout="", stderr="",
        )
        artifact = store.save_artifact(run_id, "summary.json", b'{"checks": 3}', kind="evidence")
        store.update_run(run_id, result=json.dumps({"verdict": "upgrade_failed"}))
        response = client.get(f"/api/runs/{run_id}", headers=auth_headers(token))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], run_id)
        self.assertEqual(body["state"], "queued")
        self.assertIsNone(body["status_detail"])
        self.assertEqual(body["profile_id"], PROFILE.id)
        self.assertEqual(body["repository_url"], PROFILE.repository_url)
        self.assertEqual(body["commit_sha"], PROFILE.commit_sha)
        self.assertEqual(body["dependency"], PROFILE.dependency)
        self.assertEqual(body["baseline_version"], PROFILE.baseline_version)
        self.assertEqual(body["target_version"], PROFILE.target_version)
        self.assertEqual(body["image_identity"], run_params()["image_identity"])
        self.assertEqual(body["source_sha256"], "")
        self.assertTrue(body["created_utc"].endswith("+00:00"))
        self.assertTrue(body["updated_utc"].endswith("+00:00"))
        self.assertIsNone(body["terminal_utc"])
        self.assertIsNone(body["cleanup_state"])
        self.assertEqual(body["result"], {"verdict": "upgrade_failed"})
        self.assertEqual(body["limits"], {"job_deadline_seconds": 900.0})
        self.assertEqual(body["queue_slots_remaining"], 4)
        self.assertEqual(body["attempts"], [{
            "phase": "preparing",
            "status": "completed",
            "container_id": "container-1",
            "elapsed_seconds": 2.0,
            "started_utc": started,
            "finished_utc": finished,
        }])
        self.assertEqual(body["artifacts"], [{
            "name": "summary.json",
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
            "kind": "evidence",
        }])

    def test_detail_missing_run_not_found(self):
        client, _, _ = self.make_client()
        response = client.get("/api/runs/999999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "run_not_found")

    def test_events_after_cursor(self):
        client, store, _ = self.make_client()
        run_id, token = store.create_run(**run_params())
        first = store.append_event(run_id, "queued", {"state": "queued"})
        second = store.append_event(run_id, "preparing", {"state": "preparing"})
        third = store.append_event(run_id, "baseline_started", {"phase": "baseline"})
        response = client.get(f"/api/runs/{run_id}/events", headers=auth_headers(token))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([event["id"] for event in body["events"]], [first, second, third])
        self.assertEqual(body["events"][0]["data"], {"state": "queued"})
        self.assertEqual(body["events"][0]["kind"], "queued")
        self.assertTrue(body["events"][0]["created_utc"].endswith("+00:00"))
        self.assertEqual(body["last"], third)

        later = client.get(f"/api/runs/{run_id}/events?after={second}", headers=auth_headers(token))
        body = later.json()
        self.assertEqual([event["id"] for event in body["events"]], [third])
        self.assertEqual(body["last"], third)

        negative = client.get(f"/api/runs/{run_id}/events?after=-1", headers=auth_headers(token))
        self.assertEqual(negative.status_code, 422)
        self.assertEqual(negative.json()["error"]["code"], "invalid_request")

    def test_cancel_requests_active_and_ignores_terminal(self):
        client, store, _ = self.make_client()
        active_id, active_token = store.create_run(**run_params())
        response = client.post(f"/api/runs/{active_id}/cancel", headers=auth_headers(active_token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"requested": True, "state": "queued"})
        self.assertTrue(store.cancel_requested(active_id))

        terminal_id, terminal_token = store.create_run(**run_params())
        store.set_run_state(terminal_id, "cancelled")
        response = client.post(f"/api/runs/{terminal_id}/cancel", headers=auth_headers(terminal_token))
        self.assertEqual(response.json(), {"requested": False, "state": "cancelled"})

        missing = client.post("/api/runs/999999/cancel", headers=auth_headers(terminal_token))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "run_not_found")

    def test_artifact_download(self):
        client, store, _ = self.make_client()
        run_id, token = store.create_run(**run_params())
        content = b'{"checks": 3}'
        store.save_artifact(run_id, "summary.json", content, kind="evidence")
        response = client.get(f"/api/runs/{run_id}/artifacts/summary.json", headers=auth_headers(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="summary.json"')

        store.save_artifact(run_id, "patch.diff", b"--- a/x.py\n+++ b/x.py\n", kind="patch")
        diff = client.get(f"/api/runs/{run_id}/artifacts/patch.diff", headers=auth_headers(token))
        self.assertTrue(diff.headers["content-type"].startswith("text/plain"))

        unrecorded = client.get(
            f"/api/runs/{run_id}/artifacts/not-recorded.json", headers=auth_headers(token))
        self.assertEqual(unrecorded.status_code, 404)
        self.assertEqual(unrecorded.json()["error"]["code"], "artifact_not_found")

        unauthenticated = client.get(f"/api/runs/{run_id}/artifacts/summary.json")
        self.assertEqual(unauthenticated.status_code, 401)

    def test_storage_prune_admits_new_run(self):
        client, store, settings = self.make_client(retention_hours=1, storage_cap_bytes=13)
        old_id, _ = store.create_run(**run_params())
        store.set_run_state(old_id, "cancelled")
        store.update_run(old_id, terminal_utc=utc_offset(-2 * 3600))
        artifact = store.save_artifact(old_id, "summary.json", b'{"checks": 3}', kind="evidence")
        self.assertEqual(settings.storage_cap_bytes, artifact["bytes"])
        self.assertEqual(store.storage_bytes(), artifact["bytes"])

        response = client.post("/api/runs", json=submission_body())
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(store.get_run(old_id))

    def test_storage_full_when_pruning_cannot_free_enough(self):
        client, store, _ = self.make_client(retention_hours=1, storage_cap_bytes=13)
        recent_id, _ = store.create_run(**run_params())
        store.set_run_state(recent_id, "cancelled")
        store.update_run(recent_id, terminal_utc=utc_offset(-60))
        store.save_artifact(recent_id, "summary.json", b'{"checks": 3}', kind="evidence")

        response = client.post("/api/runs", json=submission_body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "storage_full")
        self.assertIsNotNone(store.get_run(recent_id))

    def test_ready_reflects_queue_capacity(self):
        client, _, _ = self.make_client()
        self.assertEqual(client.get("/api/ready").json(), {"ready": True, "queue_slots": 5, "database": "ok"})

    def test_ready_false_when_queue_full(self):
        client, store, _ = self.make_client(max_queued_runs=1)
        store.create_run(**run_params())
        self.assertEqual(
            client.get("/api/ready").json(), {"ready": False, "queue_slots": 0, "database": "ok"})

    def test_ready_reports_database_error(self):
        client, store, _ = self.make_client()
        store.close()
        self.assertEqual(
            client.get("/api/ready").json(), {"ready": False, "queue_slots": 0, "database": "error"})

    def test_same_origin_middleware(self):
        client, _, _ = self.make_client()
        foreign = client.get("/healthz", headers={"Origin": "https://evil.example"})
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(foreign.json()["error"]["code"], "origin_rejected")
        same = client.get("/healthz", headers={"Origin": "http://testserver"})
        self.assertEqual(same.status_code, 200)
        absent = client.get("/healthz")
        self.assertEqual(absent.status_code, 200)


class InternalInferenceTests(unittest.TestCase):
    """Contract checks for the shared internal inference endpoints."""

    def setUp(self):
        self._context = tempfile.TemporaryDirectory()
        self.addCleanup(self._context.cleanup)
        self.base = Path(self._context.name)

    def make_client(self, stub, **overrides):
        params = {
            "database_path": str(self.base / "runs.db"),
            "artifact_dir": str(self.base / "artifacts"),
            "worker_image": "python-runner@sha256:" + "b" * 64,
            "vultr_model_id": "vultr-model-1",
        }
        params.update(overrides)
        settings = Settings(**params)
        store = Store(settings.database_path, settings.artifact_dir)
        self.addCleanup(store.close)
        client = TestClient(create_app(settings, store, inference_factory=lambda _settings: stub))
        return client, settings

    def test_select_happy_path(self):
        stub = StubInference()
        client, settings = self.make_client(stub)
        stub.result = selection_result()
        response = client.post("/internal/inference/select", json=select_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "selection": {
                "package": "requests",
                "target_version": "2.32.0",
                "rationale": "minor bump",
            },
            "model_id": settings.vultr_model_id,
            "attempts": 1,
            "usage": {"total_tokens": 7},
        })
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["max_tokens"], 4096)
        self.assertIsNone(stub.calls[0]["timeout"])
        self.assertEqual(stub.calls[0]["messages"][0]["role"], "system")
        self.assertEqual(
            stub.calls[0]["messages"][0]["content"],
            "You are a dependency upgrade selector for a Python repository. Choose exactly one "
            "eligible target version from the provided list. Respond ONLY with a single JSON object: "
            "{\"package\": \"...\", \"target_version\": \"...\", \"rationale\": \"...\"}. "
            "The rationale must be at most 2000 characters.",
        )
        self.assertEqual(
            stub.calls[0]["messages"][1]["content"],
            "Package: requests\n"
            "Eligible target versions: [\"2.31.0\", \"2.32.0\"]\n"
            "Repository context (bounded):\nThe repository uses requests for all HTTP calls.",
        )

    def test_select_out_of_list_version_rejected(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = selection_result(target_version="9.9.9")
        response = client.post("/internal/inference/select", json=select_body())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_selection")
        self.assertIn("eligible", response.json()["error"]["message"])

    def test_select_failed_structured_result_maps_to_502(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = StructuredResult(
            ok=False,
            content=None,
            usage=None,
            attempts=2,
            error="structured response invalid after one correction retry",
        )
        response = client.post("/internal/inference/select", json=select_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "inference_unavailable")
        self.assertIn("structured response invalid", response.json()["error"]["message"])

    def test_select_transport_error_maps_to_502(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = InferenceError("Vultr inference transport failed")
        response = client.post("/internal/inference/select", json=select_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "inference_unavailable")

    def test_select_requires_model_settings(self):
        stub = StubInference()
        client, _ = self.make_client(stub, vultr_model_id=None)
        stub.result = selection_result()
        response = client.post("/internal/inference/select", json=select_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["message"], "model id not configured")

    def test_agent_turn_happy_path_passthrough(self):
        stub = StubInference()
        client, settings = self.make_client(stub)
        stub.result = agent_turn_result()
        body = agent_turn_body()
        response = client.post("/internal/inference/agent-turn", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "message": {"tool": "read_file", "args": {"path": "x.py"}},
            "usage": {"total_tokens": 11},
            "attempts": 1,
            "model_id": settings.vultr_model_id,
        })
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["max_tokens"], 2048)
        self.assertEqual(stub.calls[0]["timeout"], 300.0)
        self.assertEqual(stub.calls[0]["messages"], body["messages"])
        self.assertEqual(
            stub.calls[0]["correction_prompt"],
            "Your previous reply was not a single JSON object. "
            "Respond ONLY with the required JSON object.",
        )

    def test_agent_turn_failed_structured_result_maps_to_502(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = StructuredResult(
            ok=False,
            content=None,
            usage=None,
            attempts=2,
            error="structured response invalid after one correction retry",
        )
        response = client.post("/internal/inference/agent-turn", json=agent_turn_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "inference_unavailable")
        self.assertIn("structured response invalid", response.json()["error"]["message"])

    def test_agent_turn_transport_error_maps_to_502(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = InferenceError("Vultr inference transport failed")
        response = client.post("/internal/inference/agent-turn", json=agent_turn_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "inference_unavailable")

    def test_agent_turn_non_dict_content_maps_to_502(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = StructuredResult(
            ok=True, content=["not", "a", "dict"], usage=None, attempts=2, error=None)
        response = client.post("/internal/inference/agent-turn", json=agent_turn_body())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "inference_unavailable")
        self.assertIn("structured response invalid", response.json()["error"]["message"])

    def test_agent_turn_message_count_over_cap_rejected(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = agent_turn_result()
        body = agent_turn_body(messages=[
            {"role": "user", "content": "turn"} for _ in range(25)
        ])
        response = client.post("/internal/inference/agent-turn", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertIn("messages", response.json()["error"]["message"])

    def test_agent_turn_content_over_cap_rejected(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = agent_turn_result()
        body = agent_turn_body(messages=[{"role": "user", "content": "x" * 65537}])
        response = client.post("/internal/inference/agent-turn", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertIn("content", response.json()["error"]["message"])

    def test_agent_turn_accepts_large_reasoning_budget(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = agent_turn_result()
        response = client.post(
            "/internal/inference/agent-turn", json=agent_turn_body(max_tokens=32768))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(stub.calls[0]["max_tokens"], 32768)

    def test_agent_turn_body_shape_and_caps_rejected(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        for body in (
            agent_turn_body(messages=[]),
            agent_turn_body(max_tokens=0),
            agent_turn_body(max_tokens=32769),
            agent_turn_body(messages=[{"role": "tool", "content": "not allowed"}]),
            {**agent_turn_body(), "unexpected": True},
        ):
            response = client.post("/internal/inference/agent-turn", json=body)
            self.assertEqual(response.status_code, 422, body)
            self.assertEqual(response.json()["error"]["code"], "invalid_request")

    def test_internal_token_required_when_configured(self):
        stub = StubInference()
        client, _ = self.make_client(stub, internal_token="secret-token")
        stub.result = selection_result()
        missing = client.post("/internal/inference/select", json=select_body())
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "unauthorized")
        wrong = client.post(
            "/internal/inference/select", json=select_body(), headers={"X-Internal-Token": "nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["error"]["code"], "unauthorized")
        good = client.post(
            "/internal/inference/select",
            json=select_body(),
            headers={"X-Internal-Token": "secret-token"},
        )
        self.assertEqual(good.status_code, 200)

        stub.result = agent_turn_result()
        turn_missing = client.post("/internal/inference/agent-turn", json=agent_turn_body())
        self.assertEqual(turn_missing.status_code, 401)
        self.assertEqual(turn_missing.json()["error"]["code"], "unauthorized")
        turn_good = client.post(
            "/internal/inference/agent-turn",
            json=agent_turn_body(),
            headers={"X-Internal-Token": "secret-token"},
        )
        self.assertEqual(turn_good.status_code, 200)

    def test_internal_endpoints_open_without_token(self):
        stub = StubInference()
        client, _ = self.make_client(stub)
        stub.result = selection_result()
        self.assertEqual(
            client.post("/internal/inference/select", json=select_body()).status_code, 200)
        stub.result = agent_turn_result()
        self.assertEqual(
            client.post("/internal/inference/agent-turn", json=agent_turn_body()).status_code, 200)


if __name__ == "__main__":
    unittest.main()
