"""Application settings, loaded from environment / .env."""

from functools import lru_cache

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
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_max_threads: int = 24

    # "development" keeps the local stack and the tests permissive. Setting
    # "production" turns on the startup checks below, secure cookies and rate limits.
    environment: str = "development"

    # Placeholders on purpose: `check_production_config` refuses to boot on them.
    secret_key: str = "dev-insecure-secret-change-me"  # noqa: S105

    admin_username: str = "admin"
    admin_password: str = "admin"  # noqa: S105

    # Send the voter cookie over HTTPS only. Off locally, forced on in production.
    cookie_secure: bool = False

    # Wall-clock zone the administration form is written and read in. Opening and
    # closing times are entered as local civil time and stored as UTC.
    display_timezone: str = "Europe/Paris"

    # Vote rate limit: at most this many votes from one address per window.
    vote_rate_limit: int = 20
    vote_rate_window_seconds: int = 300

    # Failed administrator logins tolerated from one address per window, so the
    # provisional HTTP Basic guard cannot be brute-forced at network speed.
    admin_rate_limit: int = 10
    admin_rate_window_seconds: int = 300

    # How many reverse proxies sit in front of the app. The client address is read
    # that many entries from the *end* of X-Forwarded-For: everything further left
    # was written by the caller and is forgeable. 0 disables the header entirely.
    trusted_proxy_hops: int = 1

    # Anti re-identification: hide time-bucketed breakdowns below this many votes.
    min_bucket_volume: int = 50

    # Cell-suppression threshold (k-anonymity): a demographic breakdown is hidden
    # unless every one of its cells holds at least this many votes.
    min_cell_count: int = 10

    # Resolution of the `created_at` column in the open-data export. The evolution
    # curve is withheld below `min_bucket_volume` to prevent re-identification;
    # publishing exact per-vote timestamps would hand back everything that gate
    # withholds, so exported times are truncated and the rows are shuffled.
    # "day" | "hour" | "exact" — only set "exact" if you accept that anyone who
    # knows when a person voted learns how they voted.
    export_time_resolution: str = "day"

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
    an_import_since_days: int = 365
    an_import_since: str = ""
    # An import older than this is stale enough to warn about on the dashboard.
    an_import_stale_after_hours: int = 48

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
    # Whole-job deadline for a draft (archive downloads + model calls). Past this
    # the job gives up with a message rather than holding a worker indefinitely.
    ai_draft_deadline_seconds: int = 600

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"


class InsecureConfiguration(RuntimeError):
    """Raised at startup when production is about to run on development defaults."""


def check_production_config(settings: Settings) -> None:
    """Refuse to serve real votes on placeholder secrets.

    The voter cookie is signed with SECRET_KEY: on the shipped default anyone can
    forge a voter token and vote without limit, which makes every published
    percentage meaningless. Failing loudly at boot beats discovering that later.
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
