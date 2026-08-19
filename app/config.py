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

    # INSEE reference data (annual refresh). The estimation-population dataset is
    # open (no key required); a key is only supported for rate-limit purposes.
    insee_melodi_base: str = "https://api.insee.fr/melodi"
    insee_dataset: str = "DS_ESTIMATION_POPULATION"
    insee_api_key: str = ""

    # Assemblée nationale open data (daily import of promulgated laws). The `17`
    # in the path is the legislature and changes once per legislature, which is
    # why the whole URL is configurable. Licence Ouverte, no authentication.
    an_dossiers_url: str = (
        "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/"
        "dossiers_legislatifs/Dossiers_Legislatifs.json.zip"
    )
    # Ignore promulgations older than this (ISO date). Empty imports the full
    # backlog — 141 laws back to the 15th legislature at the time of writing.
    an_import_since: str = "2026-01-01"

    # Journal Officiel open data (DILA). Daily incremental archives of the JO,
    # Licence Ouverte and no authentication; used to fetch the official text of a
    # promulgated law on demand, keyed on its NOR. Only ~13 months are retained.
    jorf_dumps_url: str = "https://echanges.dila.gouv.fr/OPENDATA/JORFSIMPLE/"

    # AI-assisted drafting of the two factual presentation blocks. Optional: with
    # no key the feature is simply absent from the admin. Drafts are never
    # generated automatically and never stored — a human edits and validates.
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5"
    # Refuse to send anything larger, rather than silently spending on an outlier
    # (a loi de finances can be far bigger than an ordinary law).
    ai_max_input_tokens: int = 150000
    # Thinking tokens bill as output. Summarising and quoting do not need deep
    # reasoning, so "medium" roughly halves the cost of a draft; raise it if the
    # drafts read as shallow.
    ai_effort: str = "medium"
    # Interventions sent when selecting arguments. The biggest debates run to 86
    # speeches; the longest are the substantive ones, so a cap trims cost far more
    # than it trims quality.
    ai_max_speeches: int = 40
    # Candidate arguments proposed per side. These are a pool to choose from, not
    # what gets published: extra quotes cost output tokens only (cents), while a
    # public page with five arguments a side is a lot to read. Curate before saving.
    ai_arguments_per_side: int = 5
    # Réserves are supplementary information, so they stay tighter.
    ai_reserves_max: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
