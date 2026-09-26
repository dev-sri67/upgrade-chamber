"""Controller settings sourced from the process environment."""

from pydantic import SecretStr, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Annotated


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    vultr_inference_api_key: SecretStr | None = None
    vultr_model_id: NonBlank | None = None

    def require_inference_key(self) -> SecretStr:
        key = self.vultr_inference_api_key
        if key is None or not key.get_secret_value().strip():
            raise ValueError("VULTR_INFERENCE_API_KEY is required for inference")
        return key

    def require_model_id(self) -> str:
        if self.vultr_model_id is None:
            raise ValueError("VULTR_MODEL_ID is required for an inference call")
        return self.vultr_model_id
