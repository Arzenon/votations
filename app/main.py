"""FastAPI application factory for Votations Françaises."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, check_production_config, get_settings
from app.crud import imports as imports_crud
from app.db import SessionLocal
from app.drafting import shutdown_drafting
from app.logging_config import configure_logging, get_logger
from app.routes import admin, auth, public
from app.routes.admin import InvalidFormValue
from app.templating import STATIC_DIR, templates

log = get_logger("app")

# Robots: everything is public and worth indexing except the administration and
# the bulk exports, which are large and pointless for a crawler.
ROBOTS_TXT = """\
User-agent: *
Disallow: /admin
Disallow: /loi/*/export.csv
Disallow: /loi/*/export.json
Allow: /
"""

# Appended at request time: the sitemap has to be an absolute URL, and only the
# request knows the site's own origin.
SITEMAP_LINE = "Sitemap: {base}/sitemap.xml\n"

# A tricolour ballot mark, inline so the tab icon costs no extra request and no
# third-party host.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#1d3a8a"/>'
    '<path d="M8 16.5l5.5 5.5L24 11.5" fill="none" stroke="#fff" '
    'stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

# Set by the app itself, not only by the reverse proxy. The Caddyfile sets the
# same headers and wins where it is present (these are applied only if absent),
# but the app must not depend on a particular proxy being in front of it for its
# protection: run it behind anything else and the headers used to vanish with no
# signal at all.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'"
    ),
    "Permissions-Policy": (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=(), interest-cohort=()"
    ),
}

# Only ever sent where the site is already HTTPS-only — `cookie_secure` is the
# app's own record of that, and production refuses to boot without it. Sending it
# over plain HTTP in development would pin the developer's browser to a scheme
# the local stack does not serve, and browsers honour that pin for a year.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

# Pages default to not being stored anywhere. The law page renders the visitor's
# own ballot, and the administration renders unpublished drafts; neither may sit
# in a shared cache, and "no cache directive at all" leaves that to the goodwill
# of whatever proxy is in the path. Anything genuinely shareable opts in by
# setting its own Cache-Control — the static files and the favicon do.
DEFAULT_CACHE_CONTROL = "private, no-store"
STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Development only: still cacheable, but revalidated every time. `immutable`
# there meant an edited stylesheet was invisible until a hard reload, because the
# files change under a running server and `--reload` only watches Python.
DEV_STATIC_CACHE_CONTROL = "no-cache"


class CachedStaticFiles(StaticFiles):
    """Static assets, cached hard.

    Safe only because the templates request them through ``asset_url``, which
    appends a fingerprint of the file's contents: a deployed edit is a new URL,
    so nothing can be served stale.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = (
            STATIC_CACHE_CONTROL if get_settings().is_production else DEV_STATIC_CACHE_CONTROL
        )
        return response


def default_headers(settings: Settings | None = None) -> list[tuple[str, str]]:
    """The security headers plus the conservative cache default, in one list.

    Applied in two places on purpose. The middleware below covers every ordinary
    response; :func:`_error_page` applies them again because an unhandled
    exception is served by Starlette's ``ServerErrorMiddleware``, which sits
    *above* our own middleware and never reaches it. That left the one page on
    the site most likely to be generated under stress as the only one with no
    CSP — and, worse, with no ``no-store``, which is the directive no reverse
    proxy puts back for us.
    """
    settings = settings or get_settings()
    headers = [*SECURITY_HEADERS.items(), ("Cache-Control", DEFAULT_CACHE_CONTROL)]
    if settings.cookie_secure:
        headers.append(HSTS_HEADER)
    return headers


class SecurityHeaders:
    """Apply the site's headers, whatever is (or is not) proxying for us.

    A plain ASGI middleware rather than ``@app.middleware("http")``: the
    ``BaseHTTPMiddleware`` behind that decorator pipes every response body
    through an anyio memory stream, task group included. The open-data exports
    are the one place that streams a whole votation row by row, and they paid
    that cost on every chunk for a handful of headers that never vary.

    Headers already on the response win, so a route that sets its own
    ``Cache-Control`` — the static files, the favicon, robots.txt — keeps it.
    """

    def __init__(self, app: ASGIApp, *, headers: list[tuple[str, str]]) -> None:
        self.app = app
        # Encoded once at construction: they are the same bytes on every
        # response, and latin-1 is what the HTTP layer wants.
        self.headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = list(message.get("headers") or [])
                present = {name.lower() for name, _ in existing}
                existing.extend((n, v) for n, v in self.headers if n not in present)
                message["headers"] = existing
            await send(message)

        await self.app(scope, receive, send_with_headers)


# What the heading says, for the refusals a visitor can actually meet. Anything
# not named here falls back to the generic wording — « Une erreur est survenue »
# is the honest heading for a status nobody has thought about yet, and a wrong
# specific one would be worse.
_ERROR_TITLES = {
    403: "Requête refusée",
    429: "Trop de tentatives",
}


def _error_page(request: Request, *, status: int, titre: str, message: str) -> Response:
    """Render a failure as a page of this site, not as a bare JSON payload."""
    template = "not_found.html" if status == 404 else "erreur.html"
    context = {} if status == 404 else {"titre": titre, "message": message}
    response = templates.TemplateResponse(request, template, context, status_code=status)
    for name, value in default_headers():
        response.headers.setdefault(name, value)
    return response


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _release_interrupted_drafts() -> None:
    """Hand back the rows a previous process was still drafting when it stopped.

    Deliberately swallows every failure. The database may not be reachable at the
    instant we boot — the entrypoint migrates first, but nothing guarantees the
    connection survives to here — and refusing to start the site over a piece of
    administrative housekeeping would trade a stuck banner for an outage.

    **Assumes one app container**, which is what the shipped compose file runs
    and is stated here because nothing in the code would say so otherwise. « en
    cours » means "a thread in *this* process is working on it", and this reads
    it as "a thread in *any* process was" — true of a single container starting,
    false the moment there are two: a rolling restart would mark the other
    replica's live draft as interrupted while it is still running, and the draft
    would then finish and overwrite the failure.

    Scaling the app therefore needs a shared claim rather than this: a
    ``claimed_at`` on the staged row, released once it is older than
    ``AI_DRAFT_DEADLINE_SECONDS``. That column does not exist, and inventing it
    for a deployment nobody runs would be a migration in exchange for nothing.
    The same caveat applies to the migrations themselves — see
    ``docker/entrypoint.sh``.
    """
    db = SessionLocal()
    try:
        released = imports_crud.release_interrupted_drafts(db)
        if released:
            log.warning(
                "%s brouillon(s) interrompu(s) par un arrêt précédent — repassés en échec",
                released,
            )
    except Exception:
        log.exception("impossible de libérer les brouillons interrompus")
    finally:
        db.close()


def create_app() -> FastAPI:
    # Refuse to serve real votes on placeholder secrets. Raising here stops the
    # container at boot instead of quietly running a forgeable configuration.
    settings = get_settings()
    check_production_config(settings)
    configure_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The routes are sync `def`, so Starlette runs each one in this thread
        # pool. Keeping it in step with the connection pool means a burst queues
        # on the database at most once, rather than here *and* on checkout. Only
        # settable from inside the loop, hence the lifespan hook.
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.db_max_threads
        log.info(
            "démarrage — environnement=%s, threads=%s, pool=%s",
            settings.environment,
            settings.db_max_threads,
            settings.db_pool_size,
        )
        _release_interrupted_drafts()
        yield
        # Drafts can run for minutes; do not make a redeploy wait for one.
        shutdown_drafting()

    # The OpenAPI schema enumerates every administration route. Useful locally,
    # gratuitous reconnaissance in production, so all three are switched off
    # there. Spelled out rather than unpacked from a dict of overrides: FastAPI's
    # own defaults are exactly these paths, and naming them keeps the intent — and
    # the types — visible.
    hide_docs = settings.is_production
    app = FastAPI(
        title="Votations Françaises",
        lifespan=lifespan,
        docs_url=None if hide_docs else "/docs",
        redoc_url=None if hide_docs else "/redoc",
        openapi_url=None if hide_docs else "/openapi.json",
    )

    app.add_middleware(SecurityHeaders, headers=default_headers(settings))

    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(admin.router)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Serve HTTP errors as pages of this site when a browser asked for one."""
        # A 401 must keep its WWW-Authenticate header or the browser never prompts.
        if exc.status_code == 401 or not _wants_html(request):
            return PlainTextResponse(
                str(exc.detail), status_code=exc.status_code, headers=exc.headers
            )
        titre = _ERROR_TITLES.get(exc.status_code, "Une erreur est survenue")
        response = _error_page(
            request, status=exc.status_code, titre=titre, message=str(exc.detail)
        )
        # The headers the refusal itself carries — `Retry-After` on the
        # administration's 429, in practice. They were reaching a `curl` through
        # the branch above and being dropped for a browser, which is the caller
        # that most needs telling how long to wait.
        for name, value in (exc.headers or {}).items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        """A field FastAPI could not read at all, rendered as a page.

        Its own handler answers with a JSON list of pydantic error dicts, which
        is right for an API and wrong for a site whose forms are posted by
        browsers: a mistyped field showed the visitor raw JSON. The status stays
        FastAPI's 422 — the request really was unprocessable, and nothing is
        gained by disagreeing with the framework about which number says so.

        The detail is deliberately not echoed: it names internal field paths,
        which help nobody reading the page and describe the shape of the form
        handler to anyone probing it.
        """
        if not _wants_html(request):
            return PlainTextResponse("Requête invalide", status_code=422)
        return _error_page(
            request,
            status=422,
            titre="Saisie invalide",
            message=(
                "Le formulaire n'a pas pu être lu : un champ manque ou n'a pas le "
                "format attendu. Revenez en arrière pour le corriger."
            ),
        )

    @app.exception_handler(InvalidFormValue)
    def invalid_form_value(request: Request, exc: InvalidFormValue) -> Response:
        return _error_page(
            request,
            status=400,
            titre="Saisie invalide",
            message=f"{exc} — revenez en arrière pour corriger le formulaire.",
        )

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> Response:
        """Last resort: log the failure, show a page, leak nothing about it."""
        log.error(
            "erreur non gérée sur %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return _error_page(
            request,
            status=500,
            titre="Une erreur est survenue",
            message=(
                "Le problème a été enregistré. Réessayez dans un instant ; si cela "
                "persiste, le site est peut-être en cours de maintenance."
            ),
        )

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots(request: Request) -> PlainTextResponse:
        base = str(request.base_url).rstrip("/")
        return PlainTextResponse(
            ROBOTS_TXT + SITEMAP_LINE.format(base=base),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _favicon() -> Response:
        return Response(
            FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Two URLs, one image, and the pair is the point. `<link rel="icon">` in the
    # page points at the `.svg`, which is what the mark actually is and what
    # every browser that reads the link will fetch. `/favicon.ico` stays because
    # a browser that ignores the link asks for that path by itself, and answering
    # it with a 404 costs a log line and an empty tab icon — but a file named
    # `.ico` holding SVG is a mismatch some clients (Safari, historically) refuse
    # to render, which is exactly why it is no longer the one the page names.
    app.get("/favicon.svg", include_in_schema=False)(_favicon)
    app.get("/favicon.ico", include_in_schema=False)(_favicon)

    @app.get("/sante", response_class=PlainTextResponse, include_in_schema=False)
    def sante() -> PlainTextResponse:
        """Liveness probe: the app answers *and* the database does.

        Deliberately cheap and free of personal data, so it can be polled often.
        """
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            log.warning("sonde de santé : base de données injoignable")
            return PlainTextResponse("base de données injoignable", status_code=503)
        finally:
            db.close()
        return PlainTextResponse("ok")

    return app


app = create_app()
