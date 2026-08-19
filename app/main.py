"""FastAPI application factory for Votations Françaises."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import check_production_config, get_settings
from app.db import SessionLocal
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

# A tricolour ballot mark, inline so the tab icon costs no extra request and no
# third-party host.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#1d3a8a"/>'
    '<path d="M8 16.5l5.5 5.5L24 11.5" fill="none" stroke="#fff" '
    'stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)


def _error_page(request: Request, *, status: int, titre: str, message: str) -> Response:
    """Render a failure as a page of this site, not as a bare JSON payload."""
    template = "not_found.html" if status == 404 else "erreur.html"
    context = {} if status == 404 else {"titre": titre, "message": message}
    return templates.TemplateResponse(request, template, context, status_code=status)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def create_app() -> FastAPI:
    # Refuse to serve real votes on placeholder secrets. Raising here stops the
    # container at boot instead of quietly running a forgeable configuration.
    settings = get_settings()
    check_production_config(settings)
    configure_logging()

    # The OpenAPI schema enumerates every administration route. Useful locally,
    # gratuitous reconnaissance in production.
    docs = (
        {}
        if not settings.is_production
        else {
            "docs_url": None,
            "redoc_url": None,
            "openapi_url": None,
        }
    )

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
        yield

    app = FastAPI(title="Votations Françaises", lifespan=lifespan, **docs)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
        titre = "Requête refusée" if exc.status_code == 403 else "Une erreur est survenue"
        return _error_page(request, status=exc.status_code, titre=titre, message=str(exc.detail))

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
    def robots() -> PlainTextResponse:
        return PlainTextResponse(ROBOTS_TXT)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(
            FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

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
