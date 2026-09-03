"""Row-level security: tenant isolation enforced by Postgres, not by us.

The isolation this project already has is real and tested — every query
filters on ``workspace_id`` and ``tests/test_isolation_sweep.py`` probes
*every* endpoint that takes a resource id. But it protects one road. Code
that reaches the database another way — a script running under cron, a
future maintenance job, a query written in a hurry — is outside it, and
nothing would notice.

RLS moves the check to the database, where it applies whichever road the
query arrived on.

**Everything below was measured against PostgreSQL 16, not assumed.** The
behaviour has three traps, and getting any of them wrong produces a
feature that looks present and protects nothing:

1. **ENABLE alone does nothing for the application.** Postgres exempts a
   table's *owner* from its policies, and the application's role owns the
   tables it created through Alembic. Measured: owner + ENABLE, context
   set to workspace 100, still returned **both** rows. ``FORCE`` is what
   closes it — same query, same role, with FORCE: **one** row.

2. **A superuser bypasses RLS even with FORCE.** Measured: connecting as
   ``postgres`` returned both rows regardless. So a deployment whose
   database user is a superuser gets **no protection at all** from any of
   this — silently. That is why ``rls_effective()`` exists below: the
   project does not get to claim a protection it has not confirmed.

3. **It fails closed, in both directions.** With no context set, SELECT
   returns zero rows and INSERT *errors*. And an INSERT carrying another
   workspace's id while the context says 100 is refused outright. That
   last one is the real prize — a cross-tenant *write*, the kind an
   application-level filter cannot catch because the filter is on reads.

4. **A reverted transaction-local setting is the empty string, not NULL.**
   This one was found by a failing test, not by reading the manual, and it
   is the trap that would have shipped. ``current_setting(name, true)``
   returns NULL only on a connection that has *never* carried a tenant;
   once any transaction has set one and ended, the value reads back as
   ``''``. So the obvious policy expression —
   ``workspace_id = current_setting('app.workspace_id', true)::int`` —
   works on the first request of a fresh connection and raises *invalid
   input syntax for type integer* on every request after it, because
   ``''::int`` is an error rather than a falsy value. Behind a connection
   pool that is nearly every request. ``NULLIF(..., '')`` in the migration
   is what turns that into the intended fail-closed NULL.

The cost of trap 3 is that every writer must set the context. Request
paths get it from ``app/deps.py``; scripts call
``scope_session_to_workspace`` below.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from sqlalchemy import select, text
from sqlalchemy.orm import Session

# The Postgres run-time parameter carrying the current tenant. A custom
# GUC (it must contain a dot) rather than a temp table or a session
# variable, because SET LOCAL is transaction-scoped and rolls back with
# the transaction — a connection returned to the pool cannot leak the
# previous request's tenant.
TENANT_SETTING = "app.workspace_id"

# Tables carrying workspace_id whose every writer runs with a tenant set.
PROTECTED_TABLES: tuple[str, ...] = (
    "channels",
    "messages",
    "audit_log",
    "notification_preferences",
    "notifications",
    "workflow_runs",
    "saved_searches",
    "classification_feedback",
    # The target source model (migration 0026). Protected from the moment
    # they exist rather than added later: ``links`` is unprotected today
    # because a cross-workspace script was written against it first and
    # retrofitting the policy now means rewriting that script. These tables
    # have no readers yet, so the cheap moment to get it right is now.
    "resources",
    "occurrences",
    "source_access",
    "source_assignments",
    "source_events",
    "evidence",
    # Phase 2. Which sources an account is trying to get into is exactly
    # as tenant-private as which sources it already collects.
    "join_requests",
)

# Two tables carry workspace_id, would benefit from RLS, and are **not**
# protected. This is the limitation of this feature as shipped, stated
# plainly rather than discovered later:
#
#   links               scripts/check_link_vitality.py selects one global
#                       batch across all workspaces, probes it
#                       concurrently, and commits once. Under RLS the
#                       writes for every workspace but the last would be
#                       refused.
#   telegram_accounts   scripts/rotate_encryption_key.py walks every row
#                       in the table to re-encrypt it.
#
# ``links`` is the most valuable table here, so excluding it is a real
# cost, not a technicality. What it would take to include it: restructure
# the vitality checker to select, probe and commit **per workspace** using
# ``for_each_workspace`` below. That is not a large change, but it lands
# in logic that phase 3A spent a batch getting right — the run that used
# to mark every link on a rate-limiting host dead — and the protection it
# buys is worth approximately nothing on a single-workspace deployment,
# which is this one. Deferred on that trade, not forgotten.
UNPROTECTED_BY_DESIGN: tuple[str, ...] = (
    "links",
    "telegram_accounts",
)

# Four tables carry workspace_id and are deliberately NOT protected,
# because RLS cannot protect them without making the system unusable.
#
# Each is read to *establish* which tenant the caller is — before any
# context exists to check against:
#
#   users            login: SELECT ... WHERE email = ? (app/routers/auth.py)
#   api_keys         key auth: SELECT ... WHERE token_hash = ? (app/apikeys.py)
#   bot_links        bot: db.get(BotLink, chat_id) (app/bot/shared.py)
#   bot_link_codes   redeeming a link code, before a link exists
#
# Putting a fail-closed policy on any of them means login returns nothing,
# API keys resolve to nothing, and the bot answers nobody. This is not a
# gap to close later — it is structural. An identity layer cannot be
# guarded by a mechanism that requires the identity to already be known.
# They keep the protection they have: application-level scoping plus the
# endpoint sweep.
BOOTSTRAP_TABLES: tuple[str, ...] = (
    "users",
    "api_keys",
    "bot_links",
    "bot_link_codes",
)

POLICY_NAME = "tenant_isolation"


class RlsStatus(TypedDict):
    """What ``rls_effective`` found, typed so callers can act on it.

    A ``dict[str, object]`` would have been shorter and was what this
    returned first; it cost two mypy errors at the one call site that
    tries to *use* the answer, which is the point of returning a
    description instead of a bool.
    """

    supported: bool
    reason: Literal[
        "not_postgresql",
        "superuser_bypasses_rls",
        "tables_not_forced",
        "tables_without_rls",
        "effective",
    ]
    superuser: bool | None
    unforced_tables: list[str]
    tables_without_rls: list[str]


def set_tenant(db: Session, workspace_id: int) -> None:
    """Scope this transaction to one workspace.

    ``set_config(..., true)`` rather than ``SET LOCAL``, and the reason is
    measured, not stylistic: **SET does not accept bind parameters.**
    ``SET LOCAL app.workspace_id = :value`` fails with *syntax error at or
    near "$1"*. ``set_config`` is the function form of the same statement,
    its third argument ``true`` meaning transaction-local, and it takes
    parameters normally — so the value never has to be interpolated into
    SQL text.

    Transaction-local is the property that makes this safe on a pooled
    connection: the setting dies with the transaction, so one request's
    tenant cannot survive into the next. The cost is that it must be
    re-issued after every commit, which is why the request path hooks
    ``after_begin`` instead of calling this once per session.
    """
    db.execute(text(f"SELECT set_config('{TENANT_SETTING}', :value, true)"), {"value": str(workspace_id)})


def set_tenant_for_current_transaction(db: Session, workspace_id: int) -> None:
    """Apply the tenant to the transaction that is already open.

    The ``after_begin`` hook in ``app/database.py`` covers every
    transaction *started from now on*, but by the time a request knows who
    the caller is, a transaction is already in flight — it is the one that
    just read the session row. Without this, everything between
    authentication and the next commit would run with no tenant set: reads
    returning nothing, writes erroring.

    A no-op on SQLite, which has no row-level security. That keeps the
    development engine and the test suite working unchanged rather than
    forcing a Postgres dependency on every contributor.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    set_tenant(db, workspace_id)


def scope_session_to_workspace(db: Session, workspace_id: int) -> None:
    """Bind a whole session to one workspace, for the rest of its life.

    Two things, because one is not enough:

    - the tenant is applied to the transaction that is **already open**,
      since callers reach this point having read something already;
    - it is recorded in ``session.info`` so the ``after_begin`` hook in
      ``app/database.py`` re-applies it to every *later* transaction.

    The second half is what makes this survive a mid-run commit. Without
    it, a script that commits half way through — the collector commits per
    channel — would run everything after that commit with no tenant: reads
    returning nothing, writes erroring. Silently, for the reads.

    Every scheduled script that touches a protected table calls this once,
    immediately after it learns which workspace it is working on. The
    request path does the same thing through ``app/deps.py``.
    """
    db.info["workspace_id"] = workspace_id
    set_tenant_for_current_transaction(db, workspace_id)


def current_tenant(db: Session) -> int | None:
    """What Postgres thinks the current tenant is. For diagnostics."""
    raw = db.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)")).scalar()
    return int(raw) if raw else None


def rls_effective(db: Session) -> RlsStatus:
    """Whether RLS is actually protecting anything on this connection.

    Written because three separate conditions each silently reduce RLS to
    decoration, and a deployment cannot tell which applies by looking at
    the migration:

    - the engine is SQLite, so none of this exists
    - the connecting role is a superuser, which bypasses RLS entirely
    - a table has RLS enabled but not FORCED, so the owner bypasses it

    Returns a description rather than a bare bool so the answer can be
    shown, logged, and acted on: "RLS is not protecting anything here"
    and "RLS is off" need different responses.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return RlsStatus(
            supported=False,
            reason="not_postgresql",
            superuser=None,
            unforced_tables=[],
            tables_without_rls=[],
        )

    superuser = bool(db.execute(text("SELECT usesuper FROM pg_user WHERE usename = current_user")).scalar())

    unforced = [
        row[0]
        for row in db.execute(
            text(
                "SELECT relname FROM pg_class "
                "WHERE relname = ANY(:names) AND relrowsecurity AND NOT relforcerowsecurity"
            ),
            {"names": list(PROTECTED_TABLES)},
        ).all()
    ]

    missing = [
        name
        for name in PROTECTED_TABLES
        if not db.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = :name"), {"name": name}
        ).scalar()
    ]

    reason: Literal["superuser_bypasses_rls", "tables_not_forced", "tables_without_rls", "effective"]
    if superuser:
        reason = "superuser_bypasses_rls"
    elif unforced:
        reason = "tables_not_forced"
    elif missing:
        reason = "tables_without_rls"
    else:
        reason = "effective"

    return RlsStatus(
        supported=True,
        reason=reason,
        superuser=superuser,
        unforced_tables=sorted(unforced),
        tables_without_rls=sorted(missing),
    )


def for_each_workspace(db: Session):
    """Iterate every workspace with the tenant set, for maintenance jobs.

    Four scheduled scripts are cross-workspace **by design** — pruning
    expired rows, checking link vitality, rotating the encryption key,
    reporting database size. Under RLS with no tenant set they would see
    zero rows: pruning nothing, checking nothing, rotating nothing, and
    reporting a database that looks empty. Silently, for the reads.

    The textbook fix is a second Postgres role carrying ``BYPASSRLS``.
    That was rejected on a checkable ground rather than taste: granting
    ``BYPASSRLS`` requires superuser, and a managed provider's database
    user generally is not one — so the fix may simply be unavailable on
    the platform this project actually deploys to. A design whose safety
    valve might not exist in production is not a design.

    Iterating instead needs no new role and no new secret. It changes one
    thing honestly: a job that used to take a single global batch now
    takes one batch per workspace. With a single workspace — this
    deployment — the two are identical; with several, the limit applies
    per workspace rather than in total, and any caller that cares must
    divide it.

    Yields ``workspace_id`` with the tenant already applied, so the body
    of the loop needs no awareness of any of this.
    """
    from app.models import Workspace

    # Read the ids up front: Workspace itself carries no RLS policy, and
    # holding the result open while the tenant changes underneath would be
    # asking for a surprise.
    ids = [row[0] for row in db.execute(select(Workspace.id).order_by(Workspace.id)).all()]
    for workspace_id in ids:
        set_tenant_for_current_transaction(db, workspace_id)
        yield workspace_id
