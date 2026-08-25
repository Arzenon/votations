"""The AI drafting job, run in the background.

Producing a draft means downloading Journal Officiel archives (up to a few
megabytes each, scanned for one text), calling a model, fetching the sitting
records, and calling the model again. That is minutes of work, far longer than a
browser or a reverse proxy will hold a request open, and it would pin a worker
thread throughout.

So the administration only *schedules* it: this module runs the job, writes the
result onto the staged row (:class:`~app.models.imported_law.ImportedLaw`), and
the form polls for the outcome. The row already carried the draft as a cache, so
the state machine costs one small column.

Nothing here is ever published: the result pre-fills a form a person edits and
validates.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai import AiDraftError, build_client, generate_draft, select_arguments
from app.config import get_settings
from app.crud import imports as imports_crud
from app.db import SessionLocal
from app.debates import DebatesFetchError, fetch_speeches
from app.legifrance import LegifranceFetchError, fetch_law_text
from app.logging_config import get_logger

log = get_logger("drafting")

# Drafts run on their own small pool, not on the thread pool that serves
# requests. Starlette runs a sync background task on the request pool, which is
# capped at `db_max_threads`; a draft holds its thread for as long as
# `ai_draft_deadline_seconds` (ten minutes by default), so a couple of concurrent
# drafts used to take a visible slice of the site's capacity with them. Here the
# worst case is that drafts queue behind each other, which is the right queue to
# be in.
_pool: ThreadPoolExecutor | None = None
# Built lazily from request threads, so two administrators clicking at the same
# instant could otherwise each see `_pool is None` and build one — leaving a pool
# nothing holds a reference to, with `draft_workers` threads of its own and no
# shutdown path. Cheap to hold: the guarded region only ever constructs the pool.
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=get_settings().draft_workers, thread_name_prefix="draft"
            )
        return _pool


def schedule_draft(item_id: int) -> Future:
    """Queue a draft run. Returns immediately; the form polls for the outcome."""
    return _executor().submit(build_draft, item_id)


def shutdown_drafting() -> None:
    """Stop accepting drafts and let the app exit without waiting ten minutes."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        # Outside the lock: `shutdown` does not wait here, but keeping it out
        # means a later `_executor()` never blocks behind a teardown.
        pool.shutdown(wait=False, cancel_futures=True)


class DeadlineExceeded(RuntimeError):
    """Raised when the job has spent its whole budget and stops rather than hang."""


@dataclass
class _Deadline:
    """A wall-clock budget checked between the job's long steps."""

    seconds: float
    started: float

    @classmethod
    def start(cls, seconds: float) -> _Deadline:
        return cls(seconds=seconds, started=time.monotonic())

    def check(self, step: str) -> None:
        if time.monotonic() - self.started > self.seconds:
            raise DeadlineExceeded(
                f"délai dépassé ({self.seconds:.0f} s) avant l'étape « {step} » — "
                "à réessayer, ou à rédiger à la main"
            )


def build_draft(item_id: int, *, session_factory: Callable[[], Session] | None = None) -> None:
    """Produce and store a draft for one staged law. Never raises.

    Opens its own session: it runs after the request that scheduled it has been
    answered, so the request's session is long gone. ``session_factory`` exists so
    the tests can drive the job against their own transaction.
    """
    settings = get_settings()
    deadline = _Deadline.start(settings.ai_draft_deadline_seconds)
    db = (session_factory or SessionLocal)()
    # One client for the whole job, closed in the `finally` below. The two calls
    # this job makes are four requests to the same host, and building a client
    # inside each of them opened two connections to make them — see
    # `app.ai.build_client`. Left as None when there is no key: `generate_draft`
    # then raises the "feature switched off" refusal exactly as it always did,
    # which is the message the administrator needs to see.
    client = build_client() if settings.anthropic_api_key else None
    try:
        item = imports_crud.get_by_id(db, item_id)
        if item is None:
            log.warning("brouillon abandonné : import %s introuvable", item_id)
            return

        if not (item.nor and item.jo_date):
            imports_crud.fail_draft(
                db, item, message=f"LOI n° {item.law_code} : référence NOR ou date JO manquante."
            )
            return

        try:
            deadline.check("texte officiel")
            law_text = fetch_law_text(item.nor, item.jo_date)
            deadline.check("rédaction des blocs factuels")
            draft = generate_draft(law_text, client=client)
        except (LegifranceFetchError, AiDraftError, DeadlineExceeded) as exc:
            log.warning("brouillon échoué pour l'import %s : %s", item_id, exc)
            imports_crud.fail_draft(db, item, message=f"LOI n° {item.law_code} : {exc}")
            return

        # Arguments are best-effort: the factual blocks are worth having on their
        # own, so a sitting record that cannot be read must not lose them.
        arguments, notes = None, []
        if not item.seances:
            notes.append(
                "Aucune séance publique enregistrée pour ce texte : les arguments sont "
                "à rédiger et à sourcer à la main."
            )
        else:
            try:
                deadline.check("comptes rendus de séance")
                speeches = fetch_speeches(item.seances)
                deadline.check("sélection des arguments")
                arguments = select_arguments(law_text, speeches, client=client)
                notes = list(arguments.notes)
            except (DebatesFetchError, AiDraftError, DeadlineExceeded) as exc:
                notes.append(f"Arguments non proposés : {exc}")

        imports_crud.save_draft(
            db,
            item,
            draft={
                "summary_text": draft.summary_text,
                "practical_changes": draft.practical_changes,
                "arguments_for": arguments.pour if arguments else [],
                "arguments_against": arguments.contre if arguments else [],
                "reserves": arguments.reserves if arguments else [],
                "notes": notes,
                "eli_url": law_text.eli_url or "",
            },
        )
        # The archive name is the provenance of the text the draft was written
        # from: a law can land in the morning or the evening dump, and "which
        # file did this come from" is the first question when a draft looks wrong.
        log.info(
            "brouillon prêt pour l'import %s (archive JO %s)",
            item_id,
            law_text.source_dump or "inconnue",
        )
    except Exception as exc:
        log.exception("brouillon interrompu pour l'import %s", item_id)
        try:
            item = imports_crud.get_by_id(db, item_id)
            if item is not None:
                imports_crud.fail_draft(db, item, message=f"erreur inattendue : {exc}")
        except Exception:
            log.exception("impossible d'enregistrer l'échec du brouillon %s", item_id)
    finally:
        db.close()
        if client is not None:
            client.close()
