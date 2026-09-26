"""Public API: admission-controlled run submission, status, events, cancellation, artifacts.

Internal endpoints POST /internal/inference/select and /internal/inference/repair wrap the
structured inference calls for the worker; they carry no run-token auth and persist nothing
about runs.
"""

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from upgrade_chamber.config import Settings
from upgrade_chamber.inference import InferenceError, StructuredResult, VultrInferenceClient
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


class FileContext(BaseModel):
    """One bounded source file snapshot supplied to a repair request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    content: str = Field(max_length=12000)


class SelectRequest(BaseModel):
    """Strict body for POST /internal/inference/select."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: int
    package: str
    eligible_versions: list[str] = Field(min_length=1, max_length=8)
    context: str = Field(max_length=8000)


class RepairRequest(BaseModel):
    """Strict body for POST /internal/inference/repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: int
    package: str
    target_version: str
    failure_context: str = Field(max_length=12000)
    file_contexts: list[FileContext] = Field(min_length=1, max_length=4)
    allowed_paths: list[str] = Field(min_length=1, max_length=8)


_SELECT_SYSTEM_PROMPT = (
    "You are a dependency upgrade selector for a Python repository. Choose exactly one "
    "eligible target version from the provided list. Respond ONLY with a single JSON object: "
    '{"package": "...", "target_version": "...", "rationale": "..."}. '
    "The rationale must be at most 2000 characters."
)

_REPAIR_SYSTEM_PROMPT = (
    "You are a compatibility repairer for one Python package upgrade. Propose the smallest "
    "safe source change that makes the failing tests pass under the target version. Edit only "
    "files from the provided allowed paths; never edit tests, test configuration, or CI. "
    "Respond ONLY with a single JSON object: "
    '{"summary": "...", "edits": [{"path": "...", "original_sha256": "...", "replacement_text": "..."}]}. '
    "The replacement_text must be the complete new content of that file."
)

_STRUCTURED_CORRECTION_PROMPT = (
    "Your previous reply was not a single JSON object of the required shape. "
    "Respond ONLY with the JSON object."
)


_ARTIFACT_MEDIA_TYPES = {
    ".json": "application/json",
    ".txt": "text/plain",
    ".diff": "text/plain",
    ".md": "text/plain",
    ".log": "text/plain",
    ".xml": "application/xml",
}

_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def _selection_violation(content: dict, package: str, eligible_versions: list[str]) -> str | None:
    """Name the first violated selection schema or eligibility check, if any."""
    if set(content) != {"package", "target_version", "rationale"}:
        return "Selection JSON must have exactly the keys package, target_version, and rationale."
    if content["package"] != package:
        return "Selection package must equal the requested package."
    if content["target_version"] not in eligible_versions:
        return "Selection target_version must be one of the eligible versions."
    rationale = content["rationale"]
    if not isinstance(rationale, str) or not 1 <= len(rationale) <= 2000:
        return "Selection rationale must be a string of 1..2000 characters."
    return None


def _repair_violation(content: dict) -> str | None:
    """Name the first violated repair JSON schema check, if any.

    Semantic validation (allow-list membership, hash match, line caps) belongs to the
    worker's edits.validate_edits; only the schema-level constraints are checked here.
    """
    summary = content.get("summary")
    if not isinstance(summary, str) or not 1 <= len(summary) <= 2000:
        return "Repair summary must be a string of 1..2000 characters."
    edits = content.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 5:
        return "Repair edits must be a list of 1..5 edit objects."
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict) or set(edit) != {"path", "original_sha256", "replacement_text"}:
            return (
                f"Repair edit {index} must have exactly the keys path, original_sha256, "
                "and replacement_text."
            )
        if not isinstance(edit["path"], str) or not 1 <= len(edit["path"]) <= 200:
            return f"Repair edit {index} path must be a string of 1..200 characters."
        digest = edit["original_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return f"Repair edit {index} original_sha256 must be a 64-character hexadecimal string."
        replacement = edit["replacement_text"]
        if not isinstance(replacement, str) or not 1 <= len(replacement) <= 262144:
            return f"Repair edit {index} replacement_text must be a string of 1..262144 characters."
    return None


def _require_model_id(settings: Settings) -> str:
    """Resolve the model id; the internal endpoints treat a missing one as unavailable."""
    try:
        return settings.require_model_id()
    except ValueError:
        raise ApiError(502, "inference_unavailable", "model id not configured") from None


def _structured_content(result: StructuredResult) -> dict:
    """Unwrap a StructuredResult or map its failure to 502 inference_unavailable."""
    if not result.ok or not isinstance(result.content, dict):
        detail = str(result.error or "structured response invalid after one correction retry")[:500]
        raise ApiError(502, "inference_unavailable", detail)
    return result.content


def _default_inference_factory(settings: Settings) -> VultrInferenceClient:
    """Build a fresh Vultr client per request; the endpoint closes it after the call."""
    return VultrInferenceClient(settings)


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

    def internal_authorization(x_internal_token: Annotated[str | None, Header()] = None) -> None:
        """Require the configured shared internal token; open when none is configured."""
        settings, _store = runtime()
        expected = settings.internal_token
        if expected is None:
            return
        supplied = x_internal_token or ""
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise ApiError(
                401,
                "unauthorized",
                "Send the configured shared token in the 'X-Internal-Token' header.",
            )

    def structured_call(settings: Settings, messages: list[dict[str, str]], *, max_tokens: int) -> StructuredResult:
        """Run one structured inference call and close the client when it owns one."""
        factory = getattr(application.state, "inference_factory", None) or _default_inference_factory
        client = factory(settings)
        try:
            return client.chat_completion_structured(
                messages, max_tokens=max_tokens, correction_prompt=_STRUCTURED_CORRECTION_PROMPT)
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()

    @application.post("/internal/inference/select")
    def select_inference(
        request: SelectRequest,
        _token_ok: Annotated[None, Depends(internal_authorization)],
    ) -> dict:
        """Pick one eligible target version with a structured inference call."""
        settings, _store = runtime()
        model_id = _require_model_id(settings)
        messages = [
            {"role": "system", "content": _SELECT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Package: {request.package}\n"
                    f"Eligible target versions: {json.dumps(request.eligible_versions)}\n"
                    f"Repository context (bounded):\n{request.context}"
                ),
            },
        ]
        try:
            result = structured_call(settings, messages, max_tokens=512)
        except (InferenceError, ValueError) as exc:
            raise ApiError(502, "inference_unavailable", str(exc)[:500]) from None
        content = _structured_content(result)
        violation = _selection_violation(content, request.package, request.eligible_versions)
        if violation is not None:
            raise ApiError(422, "invalid_selection", violation)
        return {
            "selection": {
                "package": content["package"],
                "target_version": content["target_version"],
                "rationale": content["rationale"],
            },
            "model_id": model_id,
            "attempts": result.attempts,
            "usage": result.usage,
        }

    @application.post("/internal/inference/repair")
    def repair_inference(
        request: RepairRequest,
        _token_ok: Annotated[None, Depends(internal_authorization)],
    ) -> dict:
        """Propose bounded source edits for one failing upgrade via structured inference."""
        settings, _store = runtime()
        model_id = _require_model_id(settings)
        sections = [
            f"Package: {request.package}",
            f"Target version: {request.target_version}",
            f"Failure context:\n{request.failure_context}",
        ]
        for item in request.file_contexts:
            digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
            sections.append(f"File: {item.path}\nCurrent sha256: {digest}\nContent:\n{item.content}")
        sections.append(f"Allowed paths: {json.dumps(request.allowed_paths)}")
        messages = [
            {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(sections)},
        ]
        try:
            result = structured_call(settings, messages, max_tokens=4096)
        except (InferenceError, ValueError) as exc:
            raise ApiError(502, "inference_unavailable", str(exc)[:500]) from None
        content = _structured_content(result)
        violation = _repair_violation(content)
        if violation is not None:
            raise ApiError(422, "invalid_repair", violation)
        return {
            "repair": {"summary": content["summary"], "edits": content["edits"]},
            "model_id": model_id,
            "attempts": result.attempts,
            "usage": result.usage,
        }


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


def create_app(
    settings: Settings,
    store: Store,
    *,
    inference_factory: Callable[[Settings], Any] | None = None,
) -> FastAPI:
    """Build a fully configured application instance for the given settings and store.

    ``inference_factory`` builds the per-request structured inference client from the
    settings; it defaults to constructing a VultrInferenceClient lazily per request.
    Tests inject a stub here. The factory is stored on the app state and used by the
    /internal inference endpoints.
    """
    application = FastAPI(title="Upgrade Chamber")
    application.state.inference_factory = inference_factory
    _register_routes(application, lambda: (settings, store))
    return application


app = FastAPI(title="Upgrade Chamber", lifespan=_module_lifespan)
_register_routes(app, _module_runtime)
