"""Application settings, loaded from the environment or .env.

Settings with a few allowed values are ``Literal`` and numbers have a floor, so
a typo fails at boot instead of silently weakening a privacy setting.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PostgreSQL only. Overridden in Docker to point at the `db` service.
    database_url: str = "postgresql+psycopg://votations:votations@localhost:5432/votations"

    # Pool size should cover `db_max_threads`, the threads serving sync routes.
    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_max_threads: int = Field(default=24, ge=1)

    # "production" enables the startup checks, secure cookies and rate limits.
    environment: Literal["development", "production"] = "development"

    # Placeholder: `check_production_config` refuses to boot with it.
    secret_key: str = "dev-insecure-secret-change-me"  # noqa: S105

    # Old keys still accepted to verify cookies (comma-separated, oldest first).
    # Use when changing SECRET_KEY, e.g. on a host migration: otherwise every
    # voter gets a new token and can vote again. Remove after a year.
    # Never put a leaked key here; rotate it without a fallback.
    secret_key_fallbacks: list[str] = []

    admin_username: str = "admin"
    admin_password: str = "admin"  # noqa: S105

    # HTTPS-only voter cookie. Required in production.
    cookie_secure: bool = False

    # Zone for displaying dates and entering opening/closing times (stored as UTC).
    display_timezone: str = "Europe/Paris"

    # Max votes per IP address per window. Kept loose on purpose: mobile
    # carriers (CGNAT), offices and campuses share one address between many
    # people. One vote per person is enforced by the cookie; this only slows
    # down scripts.
    vote_rate_limit: int = Field(default=60, ge=0)
    vote_rate_window_seconds: int = Field(default=600, ge=1)

    # Failed admin logins allowed per address per window (anti brute force).
    admin_rate_limit: int = Field(default=10, ge=0)
    admin_rate_window_seconds: int = Field(default=300, ge=1)

    # Exports per address per window. Each export scans all of a law's votes.
    export_rate_limit: int = Field(default=20, ge=0)
    export_rate_window_seconds: int = Field(default=600, ge=1)

    # Concurrent exports, all clients combined. Each holds a DB connection while
    # streaming, so keep this well below `db_pool_size`.
    export_max_concurrent: int = Field(default=4, ge=1)

    # Number of reverse proxies in front of the app. The client IP is read that
    # many entries from the end of X-Forwarded-For. 0 ignores the header
    # (safe default); the compose file sets 1.
    trusted_proxy_hops: int = Field(default=0, ge=0)

    # Below this many votes, the evolution curve and export timestamps are hidden.
    min_bucket_volume: int = Field(default=50, ge=1)

    # A demographic breakdown is hidden unless every cell has at least this many votes.
    min_cell_count: int = Field(default=10, ge=1)

    # Precision of exported timestamps. With "exact", anyone who knows when
    # someone voted can find out how they voted.
    export_time_resolution: Literal["day", "hour", "exact"] = "day"

    # Mentions légales (LCEN art. 6-III). The host is mandatory and shown as
    # missing if empty. The publisher's identity isn't asked for: a
    # non-professional publisher may give it to the host only (art. 6-III-2).
    # The contact email is optional.
    legal_contact_email: str = ""
    legal_host: str = ""

    # Linked from the footer. A modified public instance must link to its own
    # source code (AGPL-3.0).
    source_code_url: str = "https://github.com/Arzenon/votations"

    # INSEE reference data (yearly refresh). Open dataset; the key is optional.
    insee_melodi_base: str = "https://api.insee.fr/melodi"
    insee_dataset: str = "DS_ESTIMATION_POPULATION"
    insee_api_key: str = ""

    # Assemblée nationale open data (daily import). "17" in the path is the
    # legislature number and must be updated with each new legislature.
    an_dossiers_url: str = (
        "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/"
        "dossiers_legislatifs/Dossiers_Legislatifs.json.zip"
    )
    # Recorded votes. Keep the same legislature as `an_dossiers_url`.
    an_scrutins_url: str = (
        "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/"
        "scrutins/Scrutins.json.zip"
    )
    # Skip laws promulgated more than this many days ago. `an_import_since`
    # (ISO date) overrides it.
    an_import_since_days: int = Field(default=365, ge=0)
    an_import_since: str = ""
    # The dashboard warns when the last import is older than this.
    an_import_stale_after_hours: int = Field(default=48, ge=1)

    # Journal Officiel daily archives (DILA), used to fetch a law's text by NOR.
    # Only about 13 months are kept online.
    jorf_dumps_url: str = "https://echanges.dila.gouv.fr/OPENDATA/JORFSIMPLE/"

    # AI drafting in the admin. Disabled without a key. A human always reviews.
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5"
    # Refuse larger inputs (a loi de finances can be huge).
    ai_max_input_tokens: int = Field(default=150000, ge=1)
    # Main cost setting (thinking is billed as output). "medium" roughly halves
    # the cost of a draft.
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Max speeches sent when selecting arguments (the longest are kept).
    ai_max_speeches: int = Field(default=40, ge=1)
    # Candidate arguments per side, for the admin to pick from.
    ai_arguments_per_side: int = Field(default=5, ge=1)
    # Overall deadline for a draft, downloads and model calls included. Each step
    # gets the remaining time as its timeout. A long law can take several minutes
    # to generate, so don't lower this too much.
    ai_draft_deadline_seconds: int = Field(default=600, ge=1)
    # Dedicated drafting threads, separate from the request pool since a draft
    # can take minutes.
    draft_workers: int = Field(default=2, ge=1)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def signing_keys(self) -> list[str]:
        """Keys for itsdangerous, oldest first. The last one signs."""
        return [*self.secret_key_fallbacks, self.secret_key]

    @field_validator("secret_key_fallbacks", mode="before")
    @classmethod
    def _split_fallback_keys(cls, value):
        """Accept ``a,b`` instead of JSON. token_urlsafe keys never contain commas."""
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value

    @field_validator("admin_username", "admin_password")
    @classmethod
    def _basic_auth_is_ascii(cls, value: str, info) -> str:
        """Reject non-ASCII credentials.

        Starlette decodes the Basic auth header as ASCII, so an accented
        password would always get a 401.
        """
        if not value.isascii():
            raise ValueError(
                f"{info.field_name.upper()} contient des caractères non ASCII, que "
                "l'authentification HTTP Basic ne peut pas transmettre ici : la "
                "connexion serait toujours refusée. Utilisez des caractères ASCII "
                "(une phrase de passe longue vaut mieux qu'un mot court accentué)."
            )
        return value

    @field_validator("display_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"DISPLAY_TIMEZONE inconnu : « {value} ». Utilisez un identifiant "
                "IANA, par exemple Europe/Paris."
            ) from exc
        return value


def _key_problems(name: str, key: str) -> list[str]:
    """Problems with a signing key, if any."""
    if "change-me" in key or "insecure" in key:
        return [f"{name} is still a placeholder: voter cookies would be forgeable"]
    if len(key) < 32:
        return [f"{name} is shorter than 32 characters"]
    return []


class InsecureConfiguration(RuntimeError):
    """Raised at startup when production runs with development defaults."""


def check_production_config(settings: Settings) -> None:
    """Refuse to start in production with placeholder secrets or weak thresholds.

    With the default SECRET_KEY anyone could forge voter tokens.
    """
    if not settings.is_production:
        return

    problems = []
    problems += _key_problems("SECRET_KEY", settings.secret_key)
    # Fallback keys are accepted like the main one, so check them too.
    for index, key in enumerate(settings.secret_key_fallbacks, start=1):
        problems += _key_problems(f"SECRET_KEY_FALLBACKS[{index}]", key)
        if key == settings.secret_key:
            problems.append(
                f"SECRET_KEY_FALLBACKS[{index}] repeats SECRET_KEY: a rotation that "
                "kept the same key has not rotated anything"
            )
    if settings.admin_password in ("admin", "change-me", ""):
        problems.append("ADMIN_PASSWORD is still a placeholder")
    if not settings.cookie_secure:
        problems.append("COOKIE_SECURE must be true behind HTTPS")
    if settings.min_cell_count < 5:
        problems.append(
            f"MIN_CELL_COUNT={settings.min_cell_count} is too low to suppress small "
            "demographic cells"
        )
    if settings.min_bucket_volume < 20:
        problems.append(
            f"MIN_BUCKET_VOLUME={settings.min_bucket_volume} is too low to protect the "
            "evolution curve and the exported timestamps"
        )
    # EXPORT_TIME_RESOLUTION="exact" is allowed: it's a documented choice.

    if problems:
        raise InsecureConfiguration(
            "Configuration de production incomplète :\n  - "
            + "\n  - ".join(problems)
            + "\n\nVoir DEPLOY.md. Pour générer une clé :\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def display_zone() -> ZoneInfo:
    """The site's display time zone (validated at boot)."""
    return ZoneInfo(get_settings().display_timezone)
