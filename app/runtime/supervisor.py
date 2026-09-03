"""The control loop that keeps workers running. One process, many accounts.

What this is not
----------------
Not a queue, not a broker, not a scheduler service. The phase forbids
adding Redis, Kafka, RabbitMQ, Elasticsearch or MongoDB, and nothing here
wants one: the work list is a database table the control plane already
maintains, and "who is collecting what" is answered by a select. A broker
would add a second place for that answer to live, which is the failure
mode this whole rebuild exists to remove.

Concurrency
-----------
One asyncio task per account, each with its own Telegram connection, its
own database sessions and its own failure handling. Accounts share
nothing, so the isolation is structural: an account whose session is
revoked cannot stop another account's task, because there is no shared
object between them to break.

``max_workers`` bounds how many run at once. The default of 10 is **the
number the concurrency test exercises**, not a measured capacity — the
real ceiling is whatever the database connection pool and the host allow,
and this project has not measured that. Naming it here as a parameter is
the honest form of not knowing.

Recovery
--------
Two kinds, and they are different:

- **Startup recovery** runs once, before any worker starts. Runs left
  ``RUNNING`` by a process that died are closed as ``WORKER_FAILURE``.
  Their watermarks are untouched — a watermark records what was persisted,
  so it is exactly the right place to resume from.
- **Worker restart** happens while running. A task that raises is
  restarted with bounded backoff; a task that stopped because its
  *account* failed is not restarted at all, because restarting it would
  reproduce the same failure against the same revoked credential.

Shutdown
--------
A signal sets the stop flag on every worker and waits. Each worker
finishes its in-flight message, drains its live queue within its grace
period, closes its run and disconnects. There is no kill path here on
purpose: a worker killed mid-run leaves a ``RUNNING`` row that the next
startup has to clean up, and doing that deliberately when a clean stop was
available is manufacturing work for the recovery sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collection import runs as run_log
from app.models import TelegramAccount
from app.rls import scope_session_to_workspace
from app.runtime.metrics import RuntimeMetrics
from app.runtime.protocol import SourceReader
from app.runtime.worker import AccountStopped, AccountWorker, WorkerConfig

logger = logging.getLogger(__name__)

#: Restart delays after a worker task raises, indexed by consecutive
#: failure count. The last entry repeats — bounded backoff, never a tight
#: loop and never a give-up that leaves the account silently uncollected.
RESTART_BACKOFF: tuple[timedelta, ...] = (
    timedelta(seconds=5),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
)


def restart_delay(consecutive_failures: int) -> timedelta:
    """How long to wait before restarting a worker that just died."""
    index = min(max(consecutive_failures - 1, 0), len(RESTART_BACKOFF) - 1)
    return RESTART_BACKOFF[index]


@dataclass
class WorkerSlot:
    """One account's place in the fleet, and what has happened to it."""

    account_id: int
    task: asyncio.Task[None] | None = None
    worker: AccountWorker | None = None
    consecutive_failures: int = 0
    #: Event-loop time before which this slot must not be restarted. Zero
    #: means "startable now"; the loop's clock rather than the wall clock
    #: because it is compared against ``loop.time()`` and nothing else.
    restart_at: float = 0.0
    #: Set when the account itself failed. Such a slot is not restarted;
    #: the account's state row says why, and a person or the control plane
    #: has to change something before it can work again.
    retired_reason: str | None = None


@dataclass
class SupervisorConfig:
    #: How many account workers run at once. See the module docstring:
    #: this is the tested number, not a measured capacity.
    max_workers: int = 10
    #: How long shutdown waits for every worker to finish.
    shutdown_timeout: float = 30.0
    #: Pause between supervision passes.
    supervise_interval: float = 5.0
    worker: WorkerConfig = field(default_factory=WorkerConfig)


class Supervisor:
    """Runs one worker per eligible account, and keeps them running."""

    def __init__(
        self,
        *,
        workspace_id: int,
        session_factory: Callable[[], Session],
        reader_factory: Callable[[TelegramAccount], SourceReader],
        config: SupervisorConfig | None = None,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.config = config or SupervisorConfig()
        self.metrics = metrics or RuntimeMetrics()
        self._session_factory = session_factory
        self._reader_factory = reader_factory
        self._slots: dict[int, WorkerSlot] = {}
        self._stopping = asyncio.Event()

    # -- session ---------------------------------------------------------

    @contextlib.contextmanager
    def _session(self):
        db = self._session_factory()
        try:
            scope_session_to_workspace(db, self.workspace_id)
            yield db
        finally:
            db.close()

    # -- startup ----------------------------------------------------------

    def recover(self) -> list[int]:
        """Close out runs a dead process left open. Returns their ids.

        Called before the first worker starts, so a source whose previous
        run is still ``RUNNING`` is not skipped by anything that treats an
        open run as "somebody is on it". The watermark is not touched:
        it records what was persisted, and the persisted rows survived the
        crash exactly as they were.
        """
        with self._session() as db:
            stale = run_log.recover_abandoned(db, self.workspace_id)
            db.commit()
            return [run.id for run in stale]

    def eligible_accounts(self) -> list[TelegramAccount]:
        """Accounts a worker may be started for.

        ``state == ACTIVE`` and nothing else: the account state machine is
        the control plane's answer to "may this account be used", and
        second-guessing it here would create the second authority the
        model was built to remove.

        Deliberately unbounded. ``max_workers`` is applied where workers
        are started, not here: limiting the query would let a retired
        account keep its place in the window forever, so an eleventh
        healthy account could never take over from a first one whose
        session was revoked.
        """
        with self._session() as db:
            rows = list(
                db.execute(
                    select(TelegramAccount)
                    .where(
                        TelegramAccount.workspace_id == self.workspace_id,
                        TelegramAccount.state == TelegramAccount.ACTIVE,
                    )
                    .order_by(TelegramAccount.id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                # Detach with the fields the reader factory needs already
                # loaded, so building a reader does not reach back into a
                # closed session.
                db.expunge(row)
            return rows

    # -- supervision ------------------------------------------------------

    def _start_worker(self, account: TelegramAccount) -> WorkerSlot:
        slot = self._slots.setdefault(account.id, WorkerSlot(account_id=account.id))
        worker = AccountWorker(
            workspace_id=self.workspace_id,
            account_id=account.id,
            reader=self._reader_factory(account),
            session_factory=self._session_factory,
            config=self.config.worker,
        )
        slot.worker = worker
        slot.restart_at = 0.0
        slot.task = asyncio.create_task(worker.run(), name=f"account-{account.id}")
        logger.info("supervisor: started worker for account %s", account.id)
        return slot

    def _reap(self, slot: WorkerSlot) -> timedelta | None:
        """Fold a finished task's outcome into its slot.

        Returns the restart delay, or ``None`` when the slot must not be
        restarted. Called only for a task that is already done, so nothing
        here awaits.
        """
        task = slot.task
        assert task is not None and task.done()
        slot.task = None
        if slot.worker is not None:
            self.metrics.merge(slot.worker.metrics)
            slot.worker = None

        if task.cancelled():
            return None
        error = task.exception()
        if error is None:
            # A worker that returned on its own was asked to stop.
            return None
        if isinstance(error, AccountStopped):
            slot.retired_reason = f"{error.kind.value}: {error.detail}"
            logger.error(
                "supervisor: account %s retired, not restarting (%s)",
                slot.account_id,
                slot.retired_reason,
            )
            return None

        slot.consecutive_failures += 1
        self.metrics.worker_restarts += 1
        delay = restart_delay(slot.consecutive_failures)
        logger.warning(
            "supervisor: worker for account %s died (%s: %s); restarting in %s",
            slot.account_id,
            type(error).__name__,
            error,
            delay,
        )
        return delay

    async def supervise_once(self) -> None:
        """One pass: reap what died, start what should be running.

        Written as a single callable pass so a test can drive the
        supervision logic deterministically instead of racing a loop.
        """
        for slot in list(self._slots.values()):
            if slot.task is not None and slot.task.done():
                delay = self._reap(slot)
                if delay is not None:
                    slot.restart_at = asyncio.get_running_loop().time() + delay.total_seconds()

        if self._stopping.is_set():
            return

        now = asyncio.get_running_loop().time()
        for account in self.eligible_accounts():
            existing = self._slots.get(account.id)
            if existing is not None and (
                existing.retired_reason is not None or existing.task is not None or existing.restart_at > now
            ):
                continue
            if len([s for s in self._slots.values() if s.task is not None]) >= self.config.max_workers:
                break
            self._start_worker(account)

    async def run(self) -> None:
        """Recover, then supervise until stopped, then shut everything down."""
        recovered = self.recover()
        if recovered:
            logger.warning("supervisor: recovered %d abandoned run(s): %s", len(recovered), recovered)

        try:
            while not self._stopping.is_set():
                await self.supervise_once()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.config.supervise_interval)
        finally:
            await self.shutdown()

    def stop(self) -> None:
        """Ask every worker to finish. Idempotent, and safe from a signal."""
        self._stopping.set()
        for slot in self._slots.values():
            if slot.worker is not None:
                slot.worker.stop()

    async def shutdown(self) -> None:
        """Wait for every worker to stop, then stop waiting.

        The timeout is the honest part: a worker that will not finish is
        cancelled, and the run it left open is closed by the *next*
        process's startup recovery rather than pretended away here.
        """
        self.stop()
        tasks = [slot.task for slot in self._slots.values() if slot.task is not None]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=self.config.shutdown_timeout)
        for task in pending:
            logger.warning("supervisor: worker %s did not stop in time; cancelling", task.get_name())
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                logger.warning("supervisor: worker %s ended with %s", task.get_name(), task.exception())
        for slot in self._slots.values():
            if slot.worker is not None:
                self.metrics.merge(slot.worker.metrics)
                slot.worker = None
            slot.task = None


__all__ = [
    "RESTART_BACKOFF",
    "Supervisor",
    "SupervisorConfig",
    "WorkerSlot",
    "restart_delay",
]
