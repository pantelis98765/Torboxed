from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    data_dir: str = "/data"
    db_path: str = "/data/torboxed.db"
    download_dir: str = "/data/downloads"

    # Torbox API behavior
    torbox_base_url: str = "https://api.torbox.app"
    torbox_api_key: str | None = None
    torbox_rate_limit_per_minute: int = 10

    # Local download behavior
    max_concurrent_local_downloads: int = 2

    class Config:
        env_prefix = "TORBOXED_"


settings = Settings()

