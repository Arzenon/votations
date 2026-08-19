"""FastAPI application factory for Votations Françaises."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import check_production_config, get_settings
from app.db import SessionLocal
from app.routes import admin, auth, public

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    # Refuse to serve real votes on placeholder secrets. Raising here stops the
    # container at boot instead of quietly running a forgeable configuration.
    check_production_config(get_settings())

    app = FastAPI(title="Votations Françaises")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(admin.router)

    @app.get("/sante", response_class=PlainTextResponse, include_in_schema=False)
    def sante() -> PlainTextResponse:
        """Liveness probe: the app answers *and* the database does.

        Deliberately cheap and free of personal data, so it can be polled often.
        """
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any failure means "not ready"
            return PlainTextResponse("base de données injoignable", status_code=503)
        finally:
            db.close()
        return PlainTextResponse("ok")

    return app


app = create_app()
