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

    # Keys that may still *verify* a cookie, while SECRET_KEY signs the new ones.
    # Comma-separated, oldest first.
    #
    # This exists for one operation: moving a running instance to another host.
    # SECRET_KEY signs the voter token, so a new host with a new key cannot read
    # the cookies its voters already carry — every one of them is issued a fresh
    # token, which matches no existing row, and every open votation quietly
    # accepts a second ballot from everybody who had already voted. Carrying the
    # old key here keeps voters identified across the move; drop it after a year,
    # when the cookies it signed have expired anyway.
    #
    # **Never put a key here that you think has leaked.** A fallback is an
    # instruction to keep honouring cookies signed with it, which is the opposite
    # of what a compromise calls for. Rotate a leaked key with no fallback and
    # accept that every voter is logged out — that is the point. Note that even
    # then, ballots already cast with forged tokens stay in the table and cannot
    # be told apart from genuine ones: rotating stops the forgery, it does not
    # undo it.
    secret_key_fallbacks: list[str] = []

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
    #
    # **An address is not a person.** French mobile carriers put thousands of
    # subscribers behind one CGNAT address, and so does every workplace, campus
    # and shared connection. The limit is therefore spent by *everyone* on that
    # address together: a votation that goes round on social media can refuse
    # people who have not voted once, on a platform whose entire premise is that
    # you get to have your say. That is a far likelier harm than the one this
    # setting defends against.
    #
    # So it is deliberately loose. What actually enforces one vote per person is
    # the signed cookie and the uniqueness constraint behind it; this only makes
    # a script that mints fresh cookies in a loop slow, and a determined attacker
    # rotates addresses anyway. Sixty votes per ten minutes still costs such a
    # script a day and a half per ten thousand ballots, and leaves room for a
    # busy carrier pool. Raise it further if you serve a lot of mobile traffic —
    # the honest reading of a 429 here is "this address is shared", not "this
    # person is voting too much".
    vote_rate_limit: int = Field(default=60, ge=0)
    vote_rate_window_seconds: int = Field(default=600, ge=1)

    # Failed administrator logins tolerated from one address per window, so the
    # provisional HTTP Basic guard cannot be brute-forced at network speed.
    admin_rate_limit: int = Field(default=10, ge=0)
    admin_rate_window_seconds: int = Field(default=300, ge=1)

    # Open-data downloads tolerated from one address per window. Far lower than
    # the vote limit: an export is a full scan of a law's votes, and legitimate
    # use is a handful of downloads, not a loop. The same shared-address caveat
    # applies (see `vote_rate_limit`), which is why the window is wide rather than
    # the rate high — a newsroom or a class downloading both formats for a few
    # laws in one sitting must not be refused, a loop still is.
    export_rate_limit: int = Field(default=20, ge=0)
    export_rate_window_seconds: int = Field(default=600, ge=1)

    # Exports streaming at the same time, across all callers. An export holds a
    # pooled database connection for as long as the client takes to read it, so
    # this — not the rate limit — is what stops slow readers from occupying the
    # pool and starving the ordinary pages. Keep it well under `db_pool_size`.
    export_max_concurrent: int = Field(default=4, ge=1)

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

    # Mentions légales (LCEN art. 6-III). Every French public-facing site must
    # name its publisher and its host. Kept in the environment rather than in the
    # template so deploying does not mean editing HTML — and so that a fork or a
    # second instance fills in its own identity instead of inheriting someone
    # else's. Left empty the page says so plainly, which is the right failure:
    # visibly incomplete beats confidently wrong about who is responsible.
    legal_editor: str = ""
    legal_editor_address: str = ""
    legal_publication_director: str = ""
    legal_contact_email: str = ""
    legal_host: str = ""

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
    # Thinking tokens bill as output, so this is the main cost dial. "high" is
    # the default because the drafts are read and corrected by a person before
    # anything is published: an hour of editing costs more than the extra tokens,
    # and a shallow summary of a law is worse than a slow one. Drop to "medium"
    # to roughly halve the cost of a draft if the quality holds for your corpus;
    # "xhigh" and "max" exist above it and are rarely worth it here.
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Interventions sent when selecting arguments. The biggest debates run to 86
    # speeches; the longest are the substantive ones, so a cap trims cost far more
    # than it trims quality.
    ai_max_speeches: int = Field(default=40, ge=1)
    # Candidate arguments proposed per side. These are a pool to choose from, not
    # what gets published: extra quotes cost output tokens only (cents), while a
    # public page with five arguments a side is a lot to read. Curate before saving.
    ai_arguments_per_side: int = Field(default=5, ge=1)
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

    @property
    def signing_keys(self) -> list[str]:
        """Every key that may verify a cookie, oldest first — the last one signs.

        ``itsdangerous`` takes the list directly and tries each in turn, so a
        cookie signed before a rotation still opens while new ones are written
        with the current key.
        """
        return [*self.secret_key_fallbacks, self.secret_key]

    @field_validator("secret_key_fallbacks", mode="before")
    @classmethod
    def _split_fallback_keys(cls, value):
        """Read ``a,b`` as a list, because that is what fits in an env var.

        Pydantic reads a bare ``list[str]`` from the environment as JSON, which
        means an operator carrying one old key over has to remember to type
        ``["…"]``. A key generated by ``secrets.token_urlsafe`` is base64url, so
        it can never contain a comma and splitting on one is unambiguous.
        """
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value

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


def _key_problems(name: str, key: str) -> list[str]:
    """Whether one signing key is fit to sign real ballots."""
    if "change-me" in key or "insecure" in key:
        return [f"{name} is still a placeholder — voter cookies would be forgeable"]
    if len(key) < 32:
        return [f"{name} is shorter than 32 characters"]
    return []


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
    problems += _key_problems("SECRET_KEY", settings.secret_key)
    # A fallback is honoured exactly as the current key is, so a weak one there is
    # a weak key in production — just one nobody is looking at.
    for index, key in enumerate(settings.secret_key_fallbacks, start=1):
        problems += _key_problems(f"SECRET_KEY_FALLBACKS[{index}]", key)
        if key == settings.secret_key:
            problems.append(
                f"SECRET_KEY_FALLBACKS[{index}] repeats SECRET_KEY — a rotation that "
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


def display_zone() -> ZoneInfo:
    """The wall-clock zone this site writes and reads civil time in.

    Validated at boot by :class:`Settings`, so it can no longer fall back to UTC
    in silence. Lives here, next to the setting, because three modules need it —
    the administration form parses opening and closing times into it, the results
    round their buckets in it, and the labels format instants in it — and each had
    grown its own one-line copy of the lookup.
    """
    return ZoneInfo(get_settings().display_timezone)
