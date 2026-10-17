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
from app.security import HEAD_SCOPE_KEY
from app.templating import STATIC_DIR, templates

log = get_logger("app")

# Everything is indexable except the admin and the exports (large, useless to
# crawlers). See also `app.seo.votation_dataset`.
ROBOTS_TXT = """\
User-agent: *
Disallow: /admin
Disallow: /loi/*/export.csv
Disallow: /loi/*/export.json
Allow: /
"""

# Added per request: the sitemap URL must be absolute.
SITEMAP_LINE = "Sitemap: {base}/sitemap.xml\n"

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#1d3a8a"/>'
    '<path d="M8 16.5l5.5 5.5L24 11.5" fill="none" stroke="#fff" '
    'stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

# Also set by Caddy, but the app shouldn't depend on a particular proxy.
# Only added when the response doesn't already have them.
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

# Only sent when cookie_secure is set, so a dev browser isn't pinned to HTTPS.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

# No caching by default: the law page shows the visitor's ballot and the admin
# shows drafts. Cacheable responses set their own Cache-Control.
DEFAULT_CACHE_CONTROL = "private, no-store"
STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
# In development, static files are revalidated on each request.
DEV_STATIC_CACHE_CONTROL = "no-cache"


class CachedStaticFiles(StaticFiles):
    """Static files with a long cache. Safe because `asset_url` adds a content hash."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = (
            STATIC_CACHE_CONTROL if get_settings().is_production else DEV_STATIC_CACHE_CONTROL
        )
        return response


def default_headers(settings: Settings | None = None) -> list[tuple[str, str]]:
    """Security headers plus the default Cache-Control.

    Also applied in `_error_page`, because 500 responses come from
    ServerErrorMiddleware, which runs outside our middleware.
    """
    settings = settings or get_settings()
    headers = [*SECURITY_HEADERS.items(), ("Cache-Control", DEFAULT_CACHE_CONTROL)]
    if settings.cookie_secure:
        headers.append(HSTS_HEADER)
    return headers


class SecurityHeaders:
    """Adds the default headers unless the response already sets them.

    Plain ASGI rather than BaseHTTPMiddleware, which adds overhead to every
    chunk of a streamed response (the exports).
    """

    def __init__(self, app: ASGIApp, *, headers: list[tuple[str, str]]) -> None:
        self.app = app
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


# Error page headings. Other statuses get a generic one.
_ERROR_TITLES = {
    403: "Requête refusée",
    429: "Trop de tentatives",
}


def _error_page(request: Request, *, status: int, titre: str, message: str) -> Response:
    """Render an error as an HTML page."""
    template = "not_found.html" if status == 404 else "erreur.html"
    context = {} if status == 404 else {"titre": titre, "message": message}
    response = templates.TemplateResponse(request, template, context, status_code=status)
    for name, value in default_headers():
        response.headers.setdefault(name, value)
    return response


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _release_interrupted_drafts() -> None:
    """Release drafts left "running" by a previous process.

    Errors are logged and ignored: not worth refusing to start over.

    Assumes a single app container. With several, this would mark another
    replica's running draft as failed. Scaling would need a ``claimed_at``
    column instead.
    """
    db = SessionLocal()
    try:
        released = imports_crud.release_interrupted_drafts(db)
        if released:
            log.warning(
                "%s brouillon(s) interrompu(s) par un arrêt précédent - repassés en échec",
                released,
            )
    except Exception:
        log.exception("impossible de libérer les brouillons interrompus")
    finally:
        db.close()


class HeadAsGet:
    """Handle HEAD as GET (FastAPI doesn't add HEAD routes by itself).

    A middleware rather than adding HEAD to every route, which would produce
    duplicate operationIds. The scope is copied, not mutated: the server reads
    the original method to know it must not send a body.

    Sets HEAD_SCOPE_KEY so expensive routes (exports) can skip the work, see
    `app.security.is_head`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "HEAD":
            scope = {**scope, "method": "GET", HEAD_SCOPE_KEY: True}
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    check_production_config(settings)
    configure_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Sync routes run in this thread pool; match it to the DB pool. Can only
        # be set inside the event loop.
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.db_max_threads
        log.info(
            "démarrage - environnement=%s, threads=%s, pool=%s",
            settings.environment,
            settings.db_max_threads,
            settings.db_pool_size,
        )
        _release_interrupted_drafts()
        yield
        # Don't wait for drafts, which can take minutes.
        shutdown_drafting()

    # No API docs in production: they list every admin route.
    hide_docs = settings.is_production
    app = FastAPI(
        title="Votations Françaises",
        lifespan=lifespan,
        docs_url=None if hide_docs else "/docs",
        redoc_url=None if hide_docs else "/redoc",
        openapi_url=None if hide_docs else "/openapi.json",
    )

    app.add_middleware(SecurityHeaders, headers=default_headers(settings))
    # Outermost, so it runs before routing.
    app.add_middleware(HeadAsGet)

    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(admin.router)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """HTML error page for browsers, plain text otherwise."""
        # A 401 needs its WWW-Authenticate header for the browser to prompt.
        if exc.status_code == 401 or not _wants_html(request):
            return PlainTextResponse(
                str(exc.detail), status_code=exc.status_code, headers=exc.headers
            )
        titre = _ERROR_TITLES.get(exc.status_code, "Une erreur est survenue")
        response = _error_page(
            request, status=exc.status_code, titre=titre, message=str(exc.detail)
        )
        # Keep the exception's headers (e.g. Retry-After on a 429).
        for name, value in (exc.headers or {}).items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        """Show a 422 as a page instead of FastAPI's JSON. Details aren't shown."""
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
            message=f"{exc}. Revenez en arrière pour corriger le formulaire.",
        )

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> Response:
        """Log the error and show a generic page."""
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

    # Pages link to the .svg. /favicon.ico is for browsers that request it anyway.
    app.get("/favicon.svg", include_in_schema=False)(_favicon)
    app.get("/favicon.ico", include_in_schema=False)(_favicon)

    @app.get("/sante", response_class=PlainTextResponse, include_in_schema=False)
    def sante() -> PlainTextResponse:
        """Health check: the app and the database respond."""
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
