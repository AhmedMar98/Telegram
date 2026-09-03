#!/usr/bin/env python3
"""Start the collection runtime. A process, not a request.

    python scripts/run_runtime.py

Runs until SIGINT or SIGTERM, then stops every worker cleanly: each
finishes its in-flight message, drains its live queue within its grace
period, closes its run and disconnects. Nothing is killed while a run is
open unless a worker refuses to stop within the shutdown timeout, and
even then the run it leaves behind is closed by the next process's
startup recovery rather than pretended away.

Environment:
  DATABASE_URL              the same database the web service uses
  COLLECTOR_WORKSPACE_ID    the workspace this runtime collects for
  TG_API_ID / TG_API_HASH   Telegram application credentials
  FIELD_ENCRYPTION_KEY      decrypts each account's stored session string

Optional:
  RUNTIME_MAX_WORKERS       accounts collected concurrently (default 10)
  RUNTIME_BATCH_LIMIT       messages read per source per run (default 200)
  RUNTIME_CYCLE_PAUSE       seconds between sweeps (default 30)

**Deployment status: this entrypoint is not deployed anywhere yet.** The
repository's cron collector (``.github/workflows/collector.yml``) is still
what runs in production. Running this process needs a host that keeps a
long-lived worker, which is a deployment decision this phase does not
make. Written, importable and tested; not yet scheduled.

Nothing here logs a credential. The session string is decrypted into a
local variable, handed to the reader, and never printed — not at INFO, not
in a failure path, not in the error attached to a run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings, require_real_secrets  # noqa: E402
from app.crypto import decrypt_field  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import TelegramAccount  # noqa: E402
from app.runtime.protocol import SourceReader  # noqa: E402
from app.runtime.supervisor import Supervisor, SupervisorConfig  # noqa: E402
from app.runtime.telethon_reader import TelethonReader  # noqa: E402
from app.runtime.worker import WorkerConfig  # noqa: E402

logger = logging.getLogger("runtime")


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ[name]))
    except (KeyError, ValueError):
        return default


def _client_credentials() -> tuple[int, str] | None:
    """TG_API_ID / TG_API_HASH, read raw from the environment.

    Read here rather than imported from ``app.account_login``: this
    process is meant to run without the web application, and reaching
    into a request-side module for two environment variables would make
    the runtime depend on the thing it was separated from. Same two
    variables, same absence of any safe default, four lines.
    """
    raw_api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not raw_api_id or not api_hash:
        return None
    try:
        return int(raw_api_id), api_hash
    except ValueError:
        return None


def build_reader(account: TelegramAccount) -> SourceReader:
    """A real Telegram reader for one account.

    The only place a session string is decrypted in this process. It goes
    straight into the reader and is not returned, logged or stored.
    """
    credentials = _client_credentials()
    if credentials is None:
        raise RuntimeError("TG_API_ID and TG_API_HASH are required to run the collection runtime")
    api_id, api_hash = credentials
    return TelethonReader(decrypt_field(account.session_string), api_id, api_hash)


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Before anything connects. This process decrypts a Telegram session
    # string for every account it runs, and FIELD_ENCRYPTION_KEY carries a
    # working default that is published in this repository — so under that
    # default the rows it reads are plaintext to anyone holding the
    # database, and the encryption is decoration. scripts/collect.py and
    # scripts/add_account.py have refused to run on the published default
    # since they were written; this entrypoint was added in phase 3 and
    # was missed, which is what tests/test_production_secrets.py now
    # checks for every script that touches the credential rather than for
    # a list somebody has to remember to extend.
    #
    # SECRET_KEY is not checked: this process serves no HTTP and signs no
    # cookie, so failing on it would stop the runtime for an irrelevant
    # reason.
    require_real_secrets(get_settings(), names=("FIELD_ENCRYPTION_KEY",), job="runtime")

    raw_workspace = os.environ.get("COLLECTOR_WORKSPACE_ID")
    if not raw_workspace:
        logger.error("COLLECTOR_WORKSPACE_ID is not set; refusing to guess which workspace to collect")
        return 2
    workspace_id = int(raw_workspace)

    supervisor = Supervisor(
        workspace_id=workspace_id,
        session_factory=SessionLocal,
        reader_factory=build_reader,
        config=SupervisorConfig(
            max_workers=_int_env("RUNTIME_MAX_WORKERS", 10),
            worker=WorkerConfig(
                batch_limit=_int_env("RUNTIME_BATCH_LIMIT", 200),
                cycle_pause=_float_env("RUNTIME_CYCLE_PAUSE", 30.0),
            ),
        ),
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler rather than signal.signal: the handler has to
        # run on the loop, because stopping is an asyncio.Event the
        # workers are awaiting. A signal.signal handler fires on whatever
        # thread the OS picks and cannot touch that event safely.
        try:
            loop.add_signal_handler(sig, supervisor.stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            logger.warning("no signal handler for %s on this platform", sig)

    await supervisor.run()
    logger.info("runtime stopped; counters: %s", supervisor.metrics.snapshot())
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
