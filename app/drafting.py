"""Background AI drafting job.

A draft means downloading Journal Officiel archives, fetching sitting records
and two model calls: minutes of work, too long for a request. The admin
schedules it, this module stores the result on the `ImportedLaw` row, and the
form polls. Drafts are only used to pre-fill a form that a person reviews.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.ai import AI_ATTEMPTS, AiDraftError, build_client, generate_draft, select_arguments
from app.config import get_settings
from app.crud import imports as imports_crud
from app.db import SessionLocal
from app.debates import DebatesFetchError, fetch_speeches
from app.debates import bounded_getter as debates_getter
from app.legifrance import LegifranceFetchError, fetch_law_text
from app.legifrance import bounded_getter as legifrance_getter
from app.logging_config import get_logger
from app.opendata import DeadlineExceeded

log = get_logger("drafting")

# Set on shutdown. Queued drafts don't start; running ones stop at the next step.
_stopping = threading.Event()


class _DraftPool:
    """Runs drafts on daemon threads, at most ``draft_workers`` at a time.

    Not a ThreadPoolExecutor: its threads are joined at exit, so a redeploy
    would wait for the running draft until Docker kills the container. Daemon
    threads are abandoned instead, and `release_interrupted_drafts` cleans up
    at the next start.
    """

    def __init__(self, workers: int) -> None:
        self._slots = threading.Semaphore(workers)

    def submit(self, fn: Callable[..., None], *args: object) -> None:
        threading.Thread(
            target=self._run,
            args=(fn, args),
            daemon=True,
            name="draft",
        ).start()

    def _run(self, fn: Callable[..., None], args: tuple) -> None:
        with self._slots:
            # Checked after waiting for a slot.
            if _stopping.is_set():
                return
            fn(*args)


# Separate from the request thread pool, since a draft can take ten minutes.
_pool: _DraftPool | None = None
# Created lazily from request threads.
_pool_lock = threading.Lock()


def _executor() -> _DraftPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _DraftPool(get_settings().draft_workers)
        return _pool


def schedule_draft(item_id: int) -> None:
    """Queue a draft. Returns immediately; the form polls for the result."""
    _executor().submit(build_draft, item_id)


def shutdown_drafting() -> None:
    """Stop drafting so the app can exit right away.

    Drafts between steps stop with an error message. Those in the middle of a
    download or a model call are abandoned (daemon threads).
    """
    _stopping.set()
    global _pool
    with _pool_lock:
        _pool = None


@dataclass
class _Deadline:
    """The job's overall time budget.

    Downloads and model calls get `remaining()` as their timeout. `check()`
    runs before each step and reports which step ran out of time (or was
    interrupted by a shutdown).
    """

    seconds: float
    started: float

    @classmethod
    def start(cls, seconds: float) -> _Deadline:
        return cls(seconds=seconds, started=time.monotonic())

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def check(self, step: str) -> None:
        if _stopping.is_set():
            raise DeadlineExceeded(
                f"le service s'est arrêté avant l'étape « {step} ». Relancez la "
                "génération, ou rédigez les blocs à la main"
            )
        if self.remaining() <= 0:
            raise DeadlineExceeded(
                f"délai dépassé ({self.seconds:.0f} s) avant l'étape « {step} », "
                "à réessayer ou à rédiger à la main"
            )


def _within(client, deadline: _Deadline):
    """``client`` with its timeout set to the remaining budget.

    Divided by AI_ATTEMPTS so the SDK's retry fits in the budget too. None
    (no API key) is passed through.
    """
    if client is None:
        return None
    return client.with_options(timeout=deadline.remaining() / AI_ATTEMPTS)


SessionFactory = Callable[[], Session]


@contextmanager
def _session(new_session: SessionFactory) -> Iterator[Session]:
    """A short-lived session.

    The job doesn't keep a session open while downloading or calling the
    model: an idle transaction would hold a connection, block autovacuum and
    make migrations wait.
    """
    db = new_session()
    try:
        yield db
    finally:
        db.close()


@dataclass(frozen=True)
class _Job:
    """The fields the job needs, copied out of the session."""

    law_code: str
    nor: str | None
    jo_date: date | None
    seances: list


def _read_job(new_session: SessionFactory, item_id: int) -> _Job | None:
    with _session(new_session) as db:
        item = imports_crud.get_by_id(db, item_id)
        if item is None:
            return None
        return _Job(
            law_code=item.law_code,
            nor=item.nor,
            jo_date=item.jo_date,
            # Copied: the session closes right after.
            seances=list(item.seances or []),
        )


def _fail(new_session: SessionFactory, item_id: int, *, message: str) -> None:
    """Record why the job failed. Re-reads the row, since minutes have passed."""
    with _session(new_session) as db:
        item = imports_crud.get_by_id(db, item_id)
        if item is None:
            log.warning("échec non enregistré : import %s a disparu entre-temps", item_id)
            return
        imports_crud.fail_draft(db, item, message=message)


def _store(new_session: SessionFactory, item_id: int, *, draft: dict) -> None:
    with _session(new_session) as db:
        item = imports_crud.get_by_id(db, item_id)
        if item is None:
            log.warning("brouillon perdu : import %s a disparu entre-temps", item_id)
            return
        imports_crud.save_draft(db, item, draft=draft)


def build_draft(item_id: int, *, session_factory: SessionFactory | None = None) -> None:
    """Generate and store a draft for a staged law. Never raises.

    Opens its own short sessions (see `_session`). ``session_factory`` is for
    tests.
    """
    settings = get_settings()
    deadline = _Deadline.start(settings.ai_draft_deadline_seconds)
    new_session = session_factory or SessionLocal
    # One client for the whole job. None without a key: `generate_draft` then
    # raises the "feature disabled" error.
    client = build_client() if settings.anthropic_api_key else None
    try:
        job = _read_job(new_session, item_id)
        if job is None:
            log.warning("brouillon abandonné : import %s introuvable", item_id)
            return

        if not (job.nor and job.jo_date):
            _fail(
                new_session,
                item_id,
                message=f"LOI n° {job.law_code} : référence NOR ou date JO manquante.",
            )
            return

        try:
            deadline.check("texte officiel")
            law_text = fetch_law_text(job.nor, job.jo_date, legifrance_getter(deadline.remaining))
            deadline.check("rédaction des blocs factuels")
            draft = generate_draft(law_text, client=_within(client, deadline))
        except (LegifranceFetchError, AiDraftError, DeadlineExceeded) as exc:
            log.warning("brouillon échoué pour l'import %s : %s", item_id, exc)
            _fail(new_session, item_id, message=f"LOI n° {job.law_code} : {exc}")
            return

        # Arguments are best effort: keep the factual blocks if they fail.
        arguments, notes = None, []
        if not job.seances:
            notes.append(
                "Aucune séance publique enregistrée pour ce texte : les arguments sont "
                "à rédiger et à sourcer à la main."
            )
        else:
            try:
                deadline.check("comptes rendus de séance")
                speeches = fetch_speeches(job.seances, debates_getter(deadline.remaining))
                deadline.check("sélection des arguments")
                arguments = select_arguments(law_text, speeches, client=_within(client, deadline))
                notes = list(arguments.notes)
            except (DebatesFetchError, AiDraftError, DeadlineExceeded) as exc:
                notes.append(f"Arguments non proposés : {exc}")

        _store(
            new_session,
            item_id,
            draft={
                "summary_text": draft.summary_text,
                "practical_changes": draft.practical_changes,
                "arguments_for": arguments.pour if arguments else [],
                "arguments_against": arguments.contre if arguments else [],
                "debate_summary": arguments.sujet_debat if arguments else "",
                "notes": notes,
                "eli_url": law_text.eli_url or "",
            },
        )
        # Log which JO archive the text came from, for debugging.
        log.info(
            "brouillon prêt pour l'import %s (archive JO %s)",
            item_id,
            law_text.source_dump or "inconnue",
        )
    except Exception as exc:
        log.exception("brouillon interrompu pour l'import %s", item_id)
        try:
            _fail(new_session, item_id, message=f"erreur inattendue : {exc}")
        except Exception:
            log.exception("impossible d'enregistrer l'échec du brouillon %s", item_id)
    finally:
        if client is not None:
            client.close()
