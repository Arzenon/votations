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

# Set while the process is shutting down. Read in two places: a queued draft
# that has not started never starts, and `_Deadline.check` turns it into the
# ordinary "no time left" failure at the top of every step — so a draft caught
# between steps records *why* it stopped instead of vanishing.
_stopping = threading.Event()


class _DraftPool:
    """Drafts run here, on daemon threads, capped at ``draft_workers``.

    **Not a ThreadPoolExecutor**, and the reason is the whole point of this
    class. Its workers are ordinary threads, and `concurrent.futures.thread`
    registers an atexit hook that *joins every one of them* before the
    interpreter exits. ``shutdown(wait=False, cancel_futures=True)`` therefore
    returned instantly and changed nothing: `cancel_futures` drops what is still
    queued, a draft already running keeps its thread, and the process then sat in
    that join for as long as the draft had left — up to
    ``AI_DRAFT_DEADLINE_SECONDS``. Docker stops waiting after ten seconds, so a
    redeploy during a draft ended in SIGKILL and exit 137, on a shutdown path
    whose docstring promised the opposite.

    Cooperative cancellation alone cannot fix that: the budget is spent inside a
    single model call, whose timeout is already set and cannot be retracted from
    another thread. A daemon thread is not joined at exit, so the process leaves
    immediately and the abandoned draft is recovered at the next boot by
    `app.crud.imports.release_interrupted_drafts` — which exists for exactly this
    and already says so.

    Threads are spawned per job rather than kept alive, and the semaphore is what
    bounds how many *run* at once; the rest wait on it. That is a queue in a
    slightly different shape from the executor's, and it holds because of what
    feeds it: `mark_draft_pending` claims a staged row with a conditional UPDATE,
    so a row that is already drafting cannot be scheduled again, and the number of
    waiting jobs is bounded by the corpus rather than by how fast anybody clicks.
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
            # Checked after the wait, not before it: a job that queued while the
            # service was healthy and reached the front after SIGTERM has nothing
            # to write its result to.
            if _stopping.is_set():
                return
            fn(*args)


# Drafts run on their own small pool, not on the thread pool that serves
# requests. Starlette runs a sync background task on the request pool, which is
# capped at `db_max_threads`; a draft holds its thread for as long as
# `ai_draft_deadline_seconds` (ten minutes by default), so a couple of concurrent
# drafts used to take a visible slice of the site's capacity with them. Here the
# worst case is that drafts queue behind each other, which is the right queue to
# be in.
_pool: _DraftPool | None = None
# Built lazily from request threads, so two administrators clicking at the same
# instant could otherwise each see `_pool is None` and build one — leaving a pool
# nothing holds a reference to, with `draft_workers` threads of its own and no
# shutdown path. Cheap to hold: the guarded region only ever constructs the pool.
_pool_lock = threading.Lock()


def _executor() -> _DraftPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _DraftPool(get_settings().draft_workers)
        return _pool


def schedule_draft(item_id: int) -> None:
    """Queue a draft run. Returns immediately; the form polls for the outcome."""
    _executor().submit(build_draft, item_id)


def shutdown_drafting() -> None:
    """Stop accepting drafts and let the app exit without waiting ten minutes.

    Two mechanisms, because a draft can be stopped in two different places. One
    that has not started, or that is between steps, is refused cooperatively and
    records a readable failure. One that is inside a download or a model call
    cannot be interrupted at all — so it is simply abandoned, which is what the
    daemon threads in :class:`_DraftPool` are for, and which
    `release_interrupted_drafts` cleans up at the next boot.
    """
    _stopping.set()
    global _pool
    with _pool_lock:
        _pool = None


@dataclass
class _Deadline:
    """The job's wall-clock budget, and the thing every slow step is held to.

    It used to be checked only *between* the steps, which made it a budget in
    name and nothing in fact: each step was individually unbounded. Locating a
    law is up to seven downloads at a 180-second socket timeout apiece — and a
    socket timeout is per read, so a server dribbling bytes resets it for ever.
    Reading twenty sittings is forty more. The two model calls used the SDK's own
    ten-minute timeout, three attempts each. Ten minutes of budget could take
    hours, and the setting's promise — "gives up rather than holding a worker" —
    was not one the code could keep.

    So :meth:`remaining` is what the rest of the job now runs on: the getters
    take it per request (:func:`app.legifrance.bounded_getter`) and the model
    calls take it as their timeout (:func:`_within`). :meth:`check` stays, as the
    cheap gate at the top of each step that reports *which* step there was no
    time left for — and, since it is already called at every step boundary, as
    the place a shutdown gets to stop the job.
    """

    seconds: float
    started: float

    @classmethod
    def start(cls, seconds: float) -> _Deadline:
        return cls(seconds=seconds, started=time.monotonic())

    def remaining(self) -> float:
        """Seconds left in the budget; never negative."""
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def check(self, step: str) -> None:
        # A stopping service is a spent budget by another name: nothing after
        # this point can be written anywhere the administrator will see it, so
        # the job stops here and says which step it had reached.
        if _stopping.is_set():
            raise DeadlineExceeded(
                f"le service s'est arrêté avant l'étape « {step} » — relancez la "
                "génération, ou rédigez les blocs à la main"
            )
        if self.remaining() <= 0:
            raise DeadlineExceeded(
                f"délai dépassé ({self.seconds:.0f} s) avant l'étape « {step} » — "
                "à réessayer, ou à rédiger à la main"
            )


def _within(client, deadline: _Deadline):
    """``client``, with this call held to what is left of the budget.

    ``with_options`` shares the underlying connection pool, so the job still
    makes its four requests down one connection — this narrows the timeout and
    nothing else.

    Divided by :data:`~app.ai.AI_ATTEMPTS` because the SDK retries on its own,
    including on a timeout: a full-budget timeout retried once is two budgets. A
    call therefore gets half of what is left, and its retry fits in the other
    half.

    ``None`` passes straight through — that is the no-API-key path, where
    ``generate_draft`` builds a client itself purely in order to raise the
    "feature switched off" refusal the administrator needs to read.
    """
    if client is None:
        return None
    return client.with_options(timeout=deadline.remaining() / AI_ATTEMPTS)


SessionFactory = Callable[[], Session]


@contextmanager
def _session(new_session: SessionFactory) -> Iterator[Session]:
    """A session for one short piece of database work, closed straight after.

    The reason this job opens one to read and another to write, rather than
    holding a single session across the middle: a ``Session`` that has read
    anything keeps an open transaction, and with it a pooled connection *and* a
    database snapshot, until it commits or closes. This job then spends minutes
    downloading Journal Officiel archives and waiting on a model.

    Two drafts running at once therefore used to leave two backends idle in
    transaction for as long as ``AI_DRAFT_DEADLINE_SECONDS``. An open snapshot
    stops autovacuum reclaiming dead rows *anywhere* in the database — including
    ``votes``, which every ballot writes to — and an open transaction on
    ``imported_laws`` makes the ``ALTER TABLE`` of a redeploy's migration queue
    behind a model call.
    """
    db = new_session()
    try:
        yield db
    finally:
        db.close()


@dataclass(frozen=True)
class _Job:
    """What the long part of the work needs from the staged row, as plain values.

    Read once and carried out of the transaction that read it, so the network
    work below holds no database resources at all.
    """

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
            # Copied, not referenced: the list belongs to a row whose session is
            # about to close, and reading it afterwards would be a lazy load on a
            # detached instance.
            seances=list(item.seances or []),
        )


def _fail(new_session: SessionFactory, item_id: int, *, message: str) -> None:
    """Record why the job gave up. Re-reads the row: minutes have passed."""
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
    """Produce and store a draft for one staged law. Never raises.

    Opens its own sessions: it runs after the request that scheduled it has been
    answered, so the request's session is long gone. ``session_factory`` exists so
    the tests can drive the job against their own transaction.

    Sessions, plural, and short — see :func:`_session` for why the long part of
    this job must not be holding one.
    """
    settings = get_settings()
    deadline = _Deadline.start(settings.ai_draft_deadline_seconds)
    new_session = session_factory or SessionLocal
    # One client for the whole job, closed in the `finally` below. The two calls
    # this job makes are four requests to the same host, and building a client
    # inside each of them opened two connections to make them — see
    # `app.ai.build_client`. Left as None when there is no key: `generate_draft`
    # then raises the "feature switched off" refusal exactly as it always did,
    # which is the message the administrator needs to see.
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
            # Bounded getters, so the archive downloads are held to the same
            # budget as everything else rather than to a socket timeout each.
            law_text = fetch_law_text(job.nor, job.jo_date, legifrance_getter(deadline.remaining))
            deadline.check("rédaction des blocs factuels")
            draft = generate_draft(law_text, client=_within(client, deadline))
        except (LegifranceFetchError, AiDraftError, DeadlineExceeded) as exc:
            log.warning("brouillon échoué pour l'import %s : %s", item_id, exc)
            _fail(new_session, item_id, message=f"LOI n° {job.law_code} : {exc}")
            return

        # Arguments are best-effort: the factual blocks are worth having on their
        # own, so a sitting record that cannot be read must not lose them.
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
            _fail(new_session, item_id, message=f"erreur inattendue : {exc}")
        except Exception:
            log.exception("impossible d'enregistrer l'échec du brouillon %s", item_id)
    finally:
        if client is not None:
            client.close()
