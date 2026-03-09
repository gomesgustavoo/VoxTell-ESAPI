import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_dir: str = Field(
        ..., description="Path to VoxTell model dir (plans.json + fold_0/)"
    )
    device: str = "cuda"  # "cuda" or "cpu"
    gpu_id: int = 0
    text_model: str = "Qwen/Qwen3-Embedding-4B"
    session_dir: str = "/tmp/voxtell_sessions"
    session_ttl_seconds: int = 7200  # 2 hours
    cleanup_interval_seconds: int = 300  # 5 minutes
    log_level: str = "INFO"

    @property
    def device_str(self) -> str:
        return f"cuda:{self.gpu_id}" if self.device == "cuda" else "cpu"

    model_config = SettingsConfigDict(env_prefix="VOXTELL_")


settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
