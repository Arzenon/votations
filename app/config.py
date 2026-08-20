"""Application settings, loaded from environment / .env.

Every setting that can only hold a few values is typed as a ``Literal``, and
every numeric bound carries its floor. That is deliberate: several of these
settings *are* the privacy controls the charter promises, and a typo in one of
them used to fail open — ``EXPORT_TIME_RESOLUTION=days`` published the exact
instant of every vote, silently, because an unrecognised value took the same code
path as "exact". A configuration this load-bearing has to refuse to boot rather
than quietly serve something else.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PostgreSQL is the only supported engine. In Docker this is overridden to
    # point at the `db` service; the default targets a local Postgres on :5432.
    database_url: str = "postgresql+psycopg://votations:votations@localhost:5432/votations"

    # Connection pool. Sized against the thread pool that serves the sync routes:
    # every in-flight request holds a connection for the length of its query, so a
    # pool smaller than the worker count makes threads queue on checkout instead
    # of on the database. See `db_max_threads`.
    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_max_threads: int = Field(default=24, ge=1)

    # "development" keeps the local stack and the tests permissive. Setting
    # "production" turns on the startup checks below, secure cookies and rate limits.
    environment: Literal["development", "production"] = "development"

    # Placeholders on purpose: `check_production_config` refuses to boot on them.
    secret_key: str = "dev-insecure-secret-change-me"  # noqa: S105

    admin_username: str = "admin"
    admin_password: str = "admin"  # noqa: S105

    # Send the voter cookie over HTTPS only. Off locally, forced on in production.
    cookie_secure: bool = False

    # Wall-clock zone the administration form is written and read in. Opening and
    # closing times are entered as local civil time and stored as UTC. Validated
    # at boot: an unknown zone used to fall back to UTC in silence, which shifted
    # every scheduled opening and closing by the offset the admin assumed.
    display_timezone: str = "Europe/Paris"

    # Vote rate limit: at most this many votes from one address per window.
    vote_rate_limit: int = Field(default=20, ge=0)
    vote_rate_window_seconds: int = Field(default=300, ge=1)

    # Failed administrator logins tolerated from one address per window, so the
    # provisional HTTP Basic guard cannot be brute-forced at network speed.
    admin_rate_limit: int = Field(default=10, ge=0)
    admin_rate_window_seconds: int = Field(default=300, ge=1)

    # Open-data downloads tolerated from one address per window. Far lower than
    # the vote limit: an export is a full scan of a law's votes, and legitimate
    # use is a handful of downloads, not a loop.
    export_rate_limit: int = Field(default=10, ge=0)
    export_rate_window_seconds: int = Field(default=300, ge=1)

    # How many reverse proxies sit in front of the app. The client address is read
    # that many entries from the *end* of X-Forwarded-For: everything further left
    # was written by the caller and is forgeable. 0 disables the header entirely,
    # and is the default because it is the safe answer when nothing is known about
    # the deployment — running with no proxy while trusting the header would let a
    # caller pick their own rate-limit key. The shipped compose file sets 1.
    trusted_proxy_hops: int = Field(default=0, ge=0)

    # Anti re-identification: hide time-bucketed breakdowns below this many votes.
    min_bucket_volume: int = Field(default=50, ge=1)

    # Cell-suppression threshold (k-anonymity): a demographic breakdown is hidden
    # unless every one of its cells holds at least this many votes.
    min_cell_count: int = Field(default=10, ge=1)

    # Resolution of the `created_at` column in the open-data export. The evolution
    # curve is withheld below `min_bucket_volume` to prevent re-identification;
    # publishing exact per-vote timestamps would hand back everything that gate
    # withholds, so exported times are truncated. Only set "exact" if you accept
    # that anyone who knows when a person voted learns how they voted.
    export_time_resolution: Literal["day", "hour", "exact"] = "day"

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
    # Ignore promulgations older than this many days. A rolling window, so the
    # import does not quietly grow a backlog the way a fixed date would. Set
    # `an_import_since` to pin an absolute ISO date instead; empty imports
    # everything the export carries.
    an_import_since_days: int = Field(default=365, ge=0)
    an_import_since: str = ""
    # An import older than this is stale enough to warn about on the dashboard.
    an_import_stale_after_hours: int = Field(default=48, ge=1)

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
    ai_max_input_tokens: int = Field(default=150000, ge=1)
    # Thinking tokens bill as output. Summarising and quoting do not need deep
    # reasoning, so "medium" roughly halves the cost of a draft; raise it if the
    # drafts read as shallow.
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Interventions sent when selecting arguments. The biggest debates run to 86
    # speeches; the longest are the substantive ones, so a cap trims cost far more
    # than it trims quality.
    ai_max_speeches: int = Field(default=40, ge=1)
    # Candidate arguments proposed per side. These are a pool to choose from, not
    # what gets published: extra quotes cost output tokens only (cents), while a
    # public page with five arguments a side is a lot to read. Curate before saving.
    ai_arguments_per_side: int = Field(default=5, ge=1)
    # Réserves are supplementary information, so they stay tighter.
    ai_reserves_max: int = Field(default=3, ge=0)
    # Whole-job deadline for a draft (archive downloads + model calls). Past this
    # the job gives up with a message rather than holding a worker indefinitely.
    ai_draft_deadline_seconds: int = Field(default=600, ge=1)
    # Threads reserved for drafting. Drafts run on their own small pool rather
    # than on the pool that serves requests: a job holds its thread for minutes,
    # and borrowing request capacity for that long makes the public site queue.
    draft_workers: int = Field(default=2, ge=1)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @field_validator("admin_username", "admin_password")
    @classmethod
    def _basic_auth_is_ascii(cls, value: str, info) -> str:
        """Refuse credentials HTTP Basic cannot carry here.

        Starlette decodes the Authorization header with ``.decode("ascii")`` and
        answers 401 when that fails, so an accented password can never match — the
        browser sends UTF-8 and the header is rejected before any comparison.
        Left unchecked, an operator sets a French password and is simply locked
        out, with a 401 that says nothing about why.

        Refusing at boot is the honest answer while the guard is HTTP Basic. The
        constraint disappears with FranceConnect in V3.
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


class InsecureConfiguration(RuntimeError):
    """Raised at startup when production is about to run on development defaults."""


def check_production_config(settings: Settings) -> None:
    """Refuse to serve real votes on placeholder secrets or defeated anonymity.

    The voter cookie is signed with SECRET_KEY: on the shipped default anyone can
    forge a voter token and vote without limit, which makes every published
    percentage meaningless. Failing loudly at boot beats discovering that later.

    The anonymity thresholds are checked here too. They are valid integers as far
    as the type system is concerned, but a production deployment that publishes
    breakdowns down to single votes is not one this charter allows.
    """
    if not settings.is_production:
        return

    problems = []
    if "change-me" in settings.secret_key or "insecure" in settings.secret_key:
        problems.append("SECRET_KEY is still a placeholder — voter cookies would be forgeable")
    elif len(settings.secret_key) < 32:
        problems.append("SECRET_KEY is shorter than 32 characters")
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
    # EXPORT_TIME_RESOLUTION="exact" is deliberately *not* refused here. It is a
    # documented choice with its consequence spelled out at the setting and in
    # DEPLOY.md; this function exists to catch placeholders and values nobody
    # meant to set, not to overrule an operator who read the warning.

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
