"""Application settings, loaded from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PostgreSQL is the only supported engine. In Docker this is overridden to
    # point at the `db` service; the default targets a local Postgres on :5432.
    database_url: str = "postgresql+psycopg://votations:votations@localhost:5432/votations"

    secret_key: str = "dev-insecure-secret-change-me"

    admin_username: str = "admin"
    admin_password: str = "admin"

    # Anti re-identification: hide time-bucketed breakdowns below this many votes.
    min_bucket_volume: int = 50

    # Cell-suppression threshold (k-anonymity): a demographic breakdown is hidden
    # unless every one of its cells holds at least this many votes.
    min_cell_count: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
