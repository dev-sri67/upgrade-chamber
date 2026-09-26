"""Public API: admission-controlled run submission, status, events, cancellation, artifacts."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from upgrade_chamber.config import Settings
from upgrade_chamber.profiles import (
    UnsupportedProfileError,
    public_catalog,
    require_enabled_profile,
    require_profile_repository,
)
from upgrade_chamber.storage import Store


class ApiError(Exception):
    """Client-facing API failure carrying an HTTP status and a stable snake_case code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


class RunSubmission(BaseModel):
    """Strict admission request for POST /api/runs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    repository_url: str = Field(max_length=500)
    ref: str | None = Field(default=None, max_length=200)
    dependency: str = Field(max_length=200)
    profile_id: str = Field(max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)


_ARTIFACT_MEDIA_TYPES = {
    ".json": "application/json",
    ".txt": "text/plain",
    ".diff": "text/plain",
    ".md": "text/plain",
    ".log": "text/plain",
    ".xml": "application/xml",
}

_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def _default_port(scheme: str) -> int | None:
    return {"https": 443, "http": 80}.get(scheme.lower())


def _origin_host_port(origin: str) -> tuple[str | None, int | None]:
    """Host and effective port of an Origin header value; malformed values never match."""
    try:
        parts = urlsplit(origin.strip())
        port = parts.port
    except ValueError:
        return None, None
    hostname = parts.hostname.lower() if parts.hostname else None
    return hostname, port if port is not None else _default_port(parts.scheme)


def _request_host_port(url) -> tuple[str | None, int | None]:
    hostname = url.hostname.lower() if url.hostname else None
    return hostname, url.port if url.port is not None else _default_port(url.scheme)


def _register_routes(application: FastAPI, runtime: Callable[[], tuple[Settings, Store]]) -> None:
    """Attach every endpoint to ``application``; settings and store come from ``runtime``."""

    @application.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error_body(exc.code, exc.message))

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return JSONResponse(
            status_code=422,
            content=_error_body("invalid_request", f"Request validation failed: {details}"),
        )

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_ERROR_CODES.get(exc.status_code, "request_error")
        return JSONResponse(status_code=exc.status_code, content=_error_body(code, str(exc.detail)))

    @application.middleware("http")
    async def enforce_same_origin(request: Request, call_next) -> Response:
        origin = request.headers.get("origin")
        if origin and _origin_host_port(origin) != _request_host_port(request.url):
            return JSONResponse(
                status_code=403,
                content=_error_body("origin_rejected", "Request origin does not match the serving host."),
            )
        return await call_next(request)

    def run_authorization(run_id: int, authorization: Annotated[str | None, Header()] = None) -> dict:
        """Resolve the run row, then verify its bearer token; tokens never appear in errors."""
        _, store = runtime()
        row = store.get_run(run_id)
        if row is None:
            raise ApiError(404, "run_not_found", "No recorded run has that identifier.")
        header = authorization or ""
        if not header.startswith("Bearer "):
            raise ApiError(
                401, "unauthorized", "Send the run token as an 'Authorization: Bearer <token>' header.")
        token = header[len("Bearer "):].strip()
        if not token or not store.verify_token(run_id, token):
            raise ApiError(401, "unauthorized", "The access token does not match this run.")
        return row

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/profiles")
    def profiles() -> dict[str, list[dict[str, object]]]:
        return public_catalog()

    @application.get("/api/ready")
    def ready() -> dict:
        settings, store = runtime()
        try:
            queued = store.count_state("queued")
        except sqlite3.Error:
            return {"ready": False, "queue_slots": 0, "database": "error"}
        slots = max(0, settings.max_queued_runs - queued)
        return {"ready": slots > 0, "queue_slots": slots, "database": "ok"}

    @application.post("/api/runs", status_code=201)
    def submit_run(submission: RunSubmission, request: Request) -> dict:
        settings, store = runtime()
        if settings.operator_paused:
            raise ApiError(503, "operator_paused", "The operator has paused new run submissions.")
        try:
            profile = require_enabled_profile(submission.profile_id)
        except UnsupportedProfileError:
            raise ApiError(
                422,
                "unsupported_profile",
                f"No validated execution profile matches: {submission.profile_id}",
            )
        if submission.dependency != profile.dependency:
            raise ApiError(
                422,
                "dependency_mismatch",
                f"Profile {profile.id} upgrades {profile.dependency}; the submitted dependency must match.",
            )
        try:
            # An omitted ref means the profile's pinned commit; an explicit ref must equal it.
            require_profile_repository(
                submission.profile_id, submission.repository_url, submission.ref or profile.commit_sha)
        except UnsupportedProfileError:
            raise ApiError(
                422,
                "unsupported_repository",
                "This profile pins the exact repository and commit; submission must target "
                f"{profile.repository_url} at commit {profile.commit_sha}.",
            )
        if store.count_state("queued") >= settings.max_queued_runs:
            raise ApiError(429, "queue_full", "The run queue is full; try again later.")
        ip = request.client.host if request.client else "unknown"
        if store.recent_submissions_from_ip(ip, 3600) >= settings.ip_submissions_per_hour:
            raise ApiError(
                429, "rate_limited", "Too many submissions from this address in the last hour; try again later.")
        if store.storage_bytes() >= settings.storage_cap_bytes:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=settings.retention_hours)).isoformat()
            for expired in store.terminal_runs_older_than(cutoff):
                store.delete_run(expired["id"])
            if store.storage_bytes() >= settings.storage_cap_bytes:
                raise ApiError(
                    503,
                    "storage_full",
                    "Artifact storage is over its cap and pruning terminal runs could not free enough space.",
                )
        if submission.idempotency_key and (
            existing := store.find_run_by_idempotency(submission.idempotency_key)
        ):
            return JSONResponse(
                status_code=200,
                content={
                    "run_id": existing["id"],
                    "token": None,
                    "state": existing["state"],
                    "note": "token issued at first creation only",
                },
            )
        run_id, token = store.create_run(
            profile_id=submission.profile_id,
            repo_url=submission.repository_url,
            commit_sha=profile.commit_sha,
            dependency=submission.dependency,
            requested_ref=submission.ref,
            idempotency_key=submission.idempotency_key,
            submit_ip=ip,
            image_identity=settings.worker_image,
            source_sha256="",
            baseline_version=profile.baseline_version,
            target_version=profile.target_version,
            job_deadline_seconds=settings.job_deadline_seconds,
        )
        return {"run_id": run_id, "token": token, "state": "queued"}

    @application.get("/api/runs/{run_id}")
    def run_detail(run_id: int, run: Annotated[dict, Depends(run_authorization)]) -> dict:
        settings, store = runtime()
        result_text = run["result"]
        try:
            result = json.loads(result_text) if result_text is not None else None
        except (json.JSONDecodeError, TypeError):
            result = None
        attempts = [
            {
                "phase": attempt["phase"],
                "status": attempt["status"],
                "container_id": attempt["container_id"],
                "elapsed_seconds": attempt["elapsed_seconds"],
                "started_utc": attempt["started_utc"],
                "finished_utc": attempt["finished_utc"],
            }
            for attempt in store.attempts(run_id)
        ]
        artifacts = [
            {
                "name": artifact["name"],
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
                "kind": artifact["kind"],
            }
            for artifact in store.list_artifacts(run_id)
        ]
        queued = store.count_state("queued")
        return {
            "id": run["id"],
            "state": run["state"],
            "status_detail": run["status_detail"],
            "profile_id": run["profile_id"],
            "repository_url": run["repo_url"],
            "commit_sha": run["commit_sha"],
            "dependency": run["dependency"],
            "baseline_version": run["baseline_version"],
            "target_version": run["target_version"],
            "image_identity": run["image_identity"],
            "source_sha256": run["source_sha256"],
            "created_utc": run["created_utc"],
            "updated_utc": run["updated_utc"],
            "terminal_utc": run["terminal_utc"],
            "cleanup_state": run["cleanup_state"],
            "result": result,
            "attempts": attempts,
            "artifacts": artifacts,
            "limits": {"job_deadline_seconds": run["job_deadline_seconds"]},
            "queue_slots_remaining": max(0, settings.max_queued_runs - queued),
        }

    @application.get("/api/runs/{run_id}/events")
    def run_events(
        run_id: int,
        run: Annotated[dict, Depends(run_authorization)],
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        _, store = runtime()
        events = store.events_after(run_id, after, 200)
        return {
            "events": [
                {
                    "id": event["id"],
                    "kind": event["kind"],
                    "data": event["data"],
                    "created_utc": event["created_utc"],
                }
                for event in events
            ],
            "last": events[-1]["id"] if events else after,
        }

    @application.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int, run: Annotated[dict, Depends(run_authorization)]) -> dict:
        _, store = runtime()
        requested = store.request_cancel(run_id)
        return {"requested": requested, "state": run["state"]}

    @application.get("/api/runs/{run_id}/artifacts/{name}")
    def download_artifact(
        run_id: int, name: str, run: Annotated[dict, Depends(run_authorization)]
    ) -> Response:
        _, store = runtime()
        if name not in {record["name"] for record in store.list_artifacts(run_id)}:
            raise ApiError(404, "artifact_not_found", "No artifact with that name is recorded for this run.")
        content = store.get_artifact(run_id, name)
        if content is None:
            raise ApiError(404, "artifact_not_found", "No artifact with that name is recorded for this run.")
        media_type = _ARTIFACT_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )


_module_runtime_pair: tuple[Settings, Store] | None = None


def _module_runtime() -> tuple[Settings, Store]:
    """Resolve the environment-backed settings and store for the compatibility app.

    The store is created lazily and only when the environment provides
    database_path, artifact_dir, and worker_image; otherwise startup fails with
    an explicit message instead of serving a half-configured API.
    """
    global _module_runtime_pair
    if _module_runtime_pair is None:
        settings = Settings()
        missing = [
            name
            for name in ("database_path", "artifact_dir", "worker_image")
            if getattr(settings, name) is None or not str(getattr(settings, name)).strip()
        ]
        if missing:
            raise RuntimeError(
                "Upgrade Chamber API startup requires the database_path, artifact_dir, and"
                f" worker_image settings; missing: {', '.join(missing)} (configure them with the"
                " corresponding environment variables)"
            )
        _module_runtime_pair = (settings, Store(settings.database_path, settings.artifact_dir))
    return _module_runtime_pair


@asynccontextmanager
async def _module_lifespan(application: FastAPI):
    global _module_runtime_pair
    _module_runtime()
    try:
        yield
    finally:
        if _module_runtime_pair is not None:
            _module_runtime_pair[1].close()
        _module_runtime_pair = None


def create_app(settings: Settings, store: Store) -> FastAPI:
    """Build a fully configured application instance for the given settings and store."""
    application = FastAPI(title="Upgrade Chamber")
    _register_routes(application, lambda: (settings, store))
    return application


app = FastAPI(title="Upgrade Chamber", lifespan=_module_lifespan)
_register_routes(app, _module_runtime)
