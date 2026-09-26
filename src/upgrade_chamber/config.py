"""Controller settings sourced from the process environment."""

from pydantic import SecretStr, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Annotated


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    vultr_inference_api_key: SecretStr | None = None
    vultr_model_id: NonBlank | None = None

    # Worker execution paths, identity, and operational limits for the job queue.
    database_path: str | None = None
    artifact_dir: str | None = None
    worker_image: NonBlank | None = None
    worker_id: NonBlank = "upgrade-chamber-worker-1"
    max_queued_runs: int = 5
    ip_submissions_per_hour: int = 3
    retention_hours: int = 24
    storage_cap_bytes: int = 1024 * 1024 * 1024
    job_deadline_seconds: float = 900.0
    lease_seconds: float = 1000.0
    poll_seconds: float = 0.5
    operator_paused: bool = False

    def require_worker_settings(self) -> None:
        """Validate the worker's required paths and image before startup.

        Raises a ValueError listing each missing setting by name when
        database_path, artifact_dir, or worker_image is unset (None) or
        blank. Call this before entering the worker loop so a
        misconfigured deployment fails fast instead of surfacing as
        per-job failures.
        """
        missing = [
            name
            for name, value in (
                ("database_path", self.database_path),
                ("artifact_dir", self.artifact_dir),
                ("worker_image", self.worker_image),
            )
            if value is None or not str(value).strip()
        ]
        if missing:
            raise ValueError(f"Missing required worker settings: {', '.join(missing)}")

    def require_inference_key(self) -> SecretStr:
        key = self.vultr_inference_api_key
        if key is None or not key.get_secret_value().strip():
            raise ValueError("VULTR_INFERENCE_API_KEY is required for inference")
        return key

    def require_model_id(self) -> str:
        if self.vultr_model_id is None:
            raise ValueError("VULTR_MODEL_ID is required for an inference call")
        return self.vultr_model_id
