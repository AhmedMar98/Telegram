"""The collection runtime: a process whose only job is to collect.

Independent of the web request lifecycle by construction — nothing here
imports FastAPI, reads a request, or is started by one. ``scripts/
run_runtime.py`` runs it; the web process can import these modules to
*read* status, but starting a worker inside a request handler would tie
collection to a process whose lifetime is decided by a load balancer.

The pieces, in the order they matter:

``protocol``    what the runtime needs from Telegram, and nothing else
``worker``      one account's collecting loop — the data plane
``supervisor``  keeps one worker per account alive — the control loop
``metrics``     what is counted, and the one number that is refused
"""

from app.runtime.metrics import RuntimeMetrics
from app.runtime.protocol import IncomingMessage, SourceReader
from app.runtime.supervisor import Supervisor, SupervisorConfig
from app.runtime.worker import AccountStopped, AccountWorker, CycleReport, WorkerConfig

__all__ = [
    "AccountStopped",
    "AccountWorker",
    "CycleReport",
    "IncomingMessage",
    "RuntimeMetrics",
    "SourceReader",
    "Supervisor",
    "SupervisorConfig",
    "WorkerConfig",
]
