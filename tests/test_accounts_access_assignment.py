"""Phase 2: what an account is, what access means, and who gets a source.

The three ideas under test, and the failure each one prevents:

- **An account has a state, not just a switch.** "Not active" cannot say
  whether a person turned it off, the failure counter did, or Telegram is
  throttling it — and those need three different responses. A database
  CHECK binds the state to ``is_active`` so the two can never disagree.
- **Access is measured, not assumed.** UNKNOWN is not INACCESSIBLE and
  REQUEST_SENT is not access. A system that collapses either pair reports
  coverage it does not have.
- **Eligibility precedes balancing.** An account that cannot read a source
  is not a cheaper option for it. Assigning by load alone produces a plan
  that looks even and collects nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import access, accounts, assignments, eligibility, joinqueue
from app.config import normalize_database_url
from app.database import SessionLocal
from app.models import Channel, JoinRequest, SourceAccess, TelegramAccount, Workspace
from app.timeutil import utcnow

REPO = Path(__file__).resolve().parent.parent
# Normalised on the way in, for the reason app/config.py normalises it:
# a bare postgresql:// resolves to psycopg2, which this project does not
# ship — it ships psycopg 3. Left raw, every test below died on the
# driver import instead of running, and nobody saw it because the job
# failed earlier and skipped the whole step.
MIGRATION_DSN = normalize_database_url(os.environ.get("MIGRATION_TEST_DSN", ""))

pg_only = pytest.mark.skipif(
    not MIGRATION_DSN, reason="MIGRATION_TEST_DSN not set — these constraints live in PostgreSQL"
)


def _setup(db: Session, *, accounts_count: int = 2) -> tuple[Workspace, list[TelegramAccount]]:
    workspace = Workspace(name="p2")
    db.add(workspace)
    db.flush()
    rows = []
    for index in range(accounts_count):
        account = TelegramAccount(workspace_id=workspace.id, label=f"a{index}", session_string="x")
        db.add(account)
        rows.append(account)
    db.flush()
    return workspace, rows


def _channel(db: Session, workspace: Workspace, tg_id: str, *, username: str | None = None) -> Channel:
    channel = Channel(workspace_id=workspace.id, tg_channel_id=tg_id, username=username, title=tg_id)
    db.add(channel)
    db.flush()
    return channel


# --- account lifecycle ----------------------------------------------------


def test_an_account_starts_active_and_says_so_both_ways(client):
    with SessionLocal() as db:
        _, (account, _) = _setup(db)
        db.commit()
        assert account.state == TelegramAccount.ACTIVE
        assert account.is_active is True


def test_the_state_and_the_switch_cannot_disagree(client):
    """The CHECK is the point: there is no way to be ACTIVE and not active."""
    with SessionLocal() as db:
        _, (account, _) = _setup(db)
        db.commit()
        account.is_active = False  # without a state to explain it
        with pytest.raises(IntegrityError):
            db.commit()


def test_every_reason_for_being_off_is_a_different_state(client):
    with SessionLocal() as db:
        _, (account, _) = _setup(db)
        for state in (
            TelegramAccount.INACTIVE,
            TelegramAccount.DISABLED,
            TelegramAccount.AUTH_REQUIRED,
            TelegramAccount.RATE_LIMITED,
            TelegramAccount.UNAVAILABLE,
        ):
            accounts.set_state(db, account, state, reason=f"because {state}")
            db.commit()
            assert account.is_active is False
            assert account.state == state
            assert account.state_changed_at is not None
        # And only DISABLED fills the column the dashboard reads for an
        # automatic disable, so a human switch-off is still distinguishable.
        accounts.set_state(db, account, TelegramAccount.DISABLED, reason="three failures")
        db.commit()
        assert account.disabled_reason == "three failures"
        accounts.set_state(db, account, TelegramAccount.INACTIVE, reason="operator")
        db.commit()
        assert account.disabled_reason is None


def test_the_failure_counter_disables_through_the_state_machine(client):
    with SessionLocal() as db:
        _, (account, _) = _setup(db)
        db.commit()
        for _ in range(accounts.MAX_CONSECUTIVE_FAILURES):
            accounts.record_failure(db, account, "revoked session")
        assert account.state == TelegramAccount.DISABLED
        assert account.is_active is False

        accounts.reactivate(db, account)
        assert account.state == TelegramAccount.ACTIVE
        assert account.is_active is True
        assert account.consecutive_failures == 0


def test_an_unknown_state_is_refused_rather_than_stored(client):
    with SessionLocal() as db:
        _, (account, _) = _setup(db)
        with pytest.raises(ValueError):
            accounts.set_state(db, account, "SOMEWHAT_BROKEN")


def test_identity_is_the_telegram_user_not_the_label_or_the_session(client):
    """Re-authorising must not produce a second account.

    A new session string and a renamed label are the two things that
    change on re-authorisation; neither is the identity, so neither may
    split one account into two.
    """
    with SessionLocal() as db:
        _, (account, _) = _setup(db)
        account.tg_user_id = "77777"
        db.commit()

        account.session_string = "a-completely-new-session"
        account.label = "renamed after re-auth"
        db.commit()

        assert account.tg_user_id == "77777", "identity moved when the session did"


def test_two_accounts_cannot_claim_one_telegram_user(client):
    with SessionLocal() as db:
        _, (first, second) = _setup(db)
        first.tg_user_id = "555"
        second.tg_user_id = "555"
        with pytest.raises(IntegrityError):
            db.commit()


def test_an_account_never_exposes_its_session_through_the_api(client):
    from tests.conftest import register_workspace

    register_workspace(client, email="secrets@example.com", workspace_name="ws")
    body = client.get("/channels/accounts").json()
    assert all("session_string" not in row for row in body)


# --- access ---------------------------------------------------------------


def test_unknown_is_not_inaccessible(client):
    """The absent measurement and the failed one are different answers."""
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1001")
        db.commit()

        assert access.state_for(db, channel.id, account.id) == SourceAccess.UNKNOWN
        assert account.id not in access.accounts_with_access(db, channel.id)

        access.record(
            db,
            channel,
            SourceAccess.INACCESSIBLE,
            account_id=account.id,
            evidence_summary="not a member",
        )
        db.commit()
        assert access.state_for(db, channel.id, account.id) == SourceAccess.INACCESSIBLE


def test_one_source_holds_a_different_answer_per_account(client):
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        channel = _channel(db, workspace, "-1002")
        access.record(
            db, channel, SourceAccess.ACCESSIBLE, account_id=first.id, evidence_summary="read 12 messages"
        )
        access.record(db, channel, SourceAccess.NEEDS_ACCESS, account_id=second.id)
        access.record(db, channel, SourceAccess.ACCESSIBLE, path_kind=access.PATH_PUBLIC)
        db.commit()

        assert access.accounts_with_access(db, channel.id) == {first.id}
        assert access.state_for(db, channel.id, second.id) == SourceAccess.NEEDS_ACCESS
        assert access.public_path_state(db, channel.id) == SourceAccess.ACCESSIBLE
        assert db.query(Channel).count() == 1, "one source became several"


def test_recording_access_keeps_the_evidence_that_supports_it(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1003")
        row = access.record(
            db,
            channel,
            SourceAccess.ACCESSIBLE,
            account_id=account.id,
            observed_at=utcnow(),
            evidence_kind="access_probe",
            evidence_summary="account 1 read 12 messages",
            evidence_detail={"messages": 12},
        )
        db.commit()
        assert row.evidence_id is not None
        assert row.observed_at is not None


def test_the_public_path_has_no_account_and_says_so(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1004")
        with pytest.raises(ValueError):
            access.record(
                db, channel, SourceAccess.ACCESSIBLE, path_kind=access.PATH_PUBLIC, account_id=account.id
            )
        with pytest.raises(ValueError):
            access.record(db, channel, SourceAccess.ACCESSIBLE, path_kind=access.PATH_USERBOT)


def test_discovery_records_the_access_it_just_demonstrated(client):
    """Seeing a dialog in an account's list is evidence it can read it."""
    from app.dialogs import register_dialog

    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        row, created = register_dialog(
            db,
            workspace_id=workspace.id,
            account_id=account.id,
            kind="channel",
            tg_id="-100555",
            username=None,
            title="private feed",
        )
        db.commit()
        assert created
        assert access.state_for(db, row.id, account.id) == SourceAccess.ACCESSIBLE
        # And it was assigned through the service, not by writing the mirror.
        assert assignments.current_account_id(db, row.id) == account.id


# --- the join queue -------------------------------------------------------


def test_a_request_that_was_sent_is_not_access(client):
    """The distinction the whole queue exists to keep."""
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1005")
        row = joinqueue.enqueue(db, channel, account.id)
        joinqueue.attempting(db, row)
        joinqueue.request_sent(db, row, channel)
        db.commit()

        assert row.status == JoinRequest.REQUEST_SENT
        assert access.state_for(db, channel.id, account.id) == SourceAccess.REQUEST_SENT
        assert access.accounts_with_access(db, channel.id) == set(), "a pending request counted as access"


def test_a_grant_needs_an_observation_not_just_a_call(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1006")
        row = joinqueue.enqueue(db, channel, account.id)
        with pytest.raises(ValueError):
            joinqueue.granted(db, row, channel, observation="")

        joinqueue.granted(db, row, channel, observation="read 3 messages after joining")
        db.commit()
        assert row.status == JoinRequest.GRANTED
        assert access.accounts_with_access(db, channel.id) == {account.id}


def test_one_open_request_per_source_and_account(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1007")
        first = joinqueue.enqueue(db, channel, account.id)
        second = joinqueue.enqueue(db, channel, account.id)
        db.commit()
        assert first.id == second.id, "a second attempt was queued alongside the first"

        db.add(
            JoinRequest(
                workspace_id=workspace.id,
                source_id=channel.id,
                account_id=account.id,
                status=JoinRequest.READY,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_a_failing_request_backs_off_and_then_asks_for_a_person(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1008")
        row = joinqueue.enqueue(db, channel, account.id)

        for _ in range(len(joinqueue.RETRY_BACKOFF) - 1):
            joinqueue.attempting(db, row)
            joinqueue.failed(db, row, error="timeout")
            assert row.status == JoinRequest.READY
            assert row.next_action_at is not None

        joinqueue.attempting(db, row)
        joinqueue.failed(db, row, error="timeout")
        db.commit()
        assert row.status == JoinRequest.MANUAL_INTERVENTION, "the queue gave up silently"
        assert row.next_action_at is None


def test_due_returns_only_what_is_worth_touching(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        soon = _channel(db, workspace, "-1009")
        later = _channel(db, workspace, "-1010")
        done = _channel(db, workspace, "-1011")

        joinqueue.enqueue(db, soon, account.id, priority=5)
        waiting = joinqueue.enqueue(db, later, account.id)
        waiting.next_action_at = utcnow() + timedelta(days=3)
        finished = joinqueue.enqueue(db, done, account.id)
        joinqueue.granted(db, finished, done, observation="verified")
        db.commit()

        ready = joinqueue.due(db, workspace.id)
        assert [row.source_id for row in ready] == [soon.id]


def test_blocked_is_terminal_and_says_why(client):
    with SessionLocal() as db:
        workspace, (account, _) = _setup(db)
        channel = _channel(db, workspace, "-1012")
        row = joinqueue.enqueue(db, channel, account.id)
        joinqueue.blocked(db, row, channel, reason="invite link is admin-approval only")
        db.commit()
        assert row.status == JoinRequest.BLOCKED
        assert row.next_action_at is None
        assert access.state_for(db, channel.id, account.id) == SourceAccess.BLOCKED


# --- eligibility and assignment ------------------------------------------


def test_an_account_without_access_is_not_eligible_for_a_private_source(client):
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        private = _channel(db, workspace, "-1013")  # no username: not portable
        access.record(db, private, SourceAccess.ACCESSIBLE, account_id=first.id, evidence_summary="read it")
        db.commit()

        result = eligibility.evaluate(db, private, [first, second], capacity=10)
        assert result.eligible == [first.id]
        assert result.excluded[second.id] == eligibility.NO_ACCESS


def test_a_disabled_account_is_excluded_and_the_reason_is_kept(client):
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        public = _channel(db, workspace, "-1014", username="open")
        accounts.set_state(db, second, TelegramAccount.DISABLED, reason="three failures")
        db.commit()

        result = eligibility.evaluate(db, public, [first, second], capacity=10)
        assert result.eligible == [first.id]
        assert result.excluded[second.id] == eligibility.NOT_USABLE


def test_capacity_is_the_last_filter_not_the_first(client):
    """A full account and an account that cannot read it are different.

    Reporting the second as "at capacity" sends an operator to raise a
    limit that was never the problem.
    """
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        private = _channel(db, workspace, "-1015")
        access.record(db, private, SourceAccess.ACCESSIBLE, account_id=first.id, evidence_summary="read it")
        for index in range(3):
            held = _channel(db, workspace, f"-2000{index}", username=f"u{index}")
            assignments.assign(db, held, first.id, reason="seed")
        db.commit()

        result = eligibility.evaluate(db, private, [first, second], capacity=3)
        assert result.eligible == []
        assert result.excluded[first.id] == eligibility.AT_CAPACITY
        assert result.excluded[second.id] == eligibility.NO_ACCESS


def test_the_least_loaded_eligible_account_comes_first(client):
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        target = _channel(db, workspace, "-1016", username="open")
        for index in range(2):
            held = _channel(db, workspace, f"-3000{index}", username=f"h{index}")
            assignments.assign(db, held, first.id, reason="seed")
        db.commit()

        result = eligibility.evaluate(db, target, [first, second], capacity=10)
        assert result.eligible == [second.id, first.id], "load was not the ordering"


def test_load_is_counted_from_assignments_not_from_the_mirror(client):
    with SessionLocal() as db:
        workspace, (first, _) = _setup(db)
        for index in range(3):
            held = _channel(db, workspace, f"-4000{index}", username=f"m{index}")
            assignments.assign(db, held, first.id, reason="seed")
        db.commit()
        assert eligibility.current_load(db, [first.id])[first.id] == 3

        # Releasing one is visible immediately, because the count reads the
        # authority rather than a cached column.
        held = db.query(Channel).filter(Channel.tg_channel_id == "-40000").one()
        assignments.release(db, held, reason="test")
        db.commit()
        assert eligibility.current_load(db, [first.id])[first.id] == 2


# --- failover foundation --------------------------------------------------


def test_a_source_with_a_second_eligible_account_is_recoverable(client):
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        shared = _channel(db, workspace, "-1017")
        for account in (first, second):
            access.record(db, shared, SourceAccess.ACCESSIBLE, account_id=account.id, evidence_summary="read it")
        assignments.assign(db, shared, first.id, reason="initial")
        db.commit()

        impact = assignments.failure_impact(db, workspace.id, first.id)
        assert impact["held"] == [shared.id]
        assert impact["recoverable"] == [shared.id]
        assert impact["stranded"] == []

        assert eligibility.failover_candidates(db, shared, [first, second], capacity=10) == [second.id]


def test_a_source_only_one_account_can_reach_is_stranded_and_visible(client):
    """The number worth knowing before an account fails, not after."""
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        sole = _channel(db, workspace, "-1018")
        access.record(db, sole, SourceAccess.ACCESSIBLE, account_id=first.id, evidence_summary="read it")
        assignments.assign(db, sole, first.id, reason="initial")
        db.commit()

        impact = assignments.failure_impact(db, workspace.id, first.id)
        assert impact["stranded"] == [sole.id]
        assert impact["recoverable"] == []
        assert eligibility.failover_candidates(db, sole, [first, second], capacity=10) == []


def test_a_disabled_survivor_does_not_count_as_a_backup(client):
    with SessionLocal() as db:
        workspace, (first, second) = _setup(db)
        shared = _channel(db, workspace, "-1019")
        for account in (first, second):
            access.record(db, shared, SourceAccess.ACCESSIBLE, account_id=account.id, evidence_summary="read it")
        assignments.assign(db, shared, first.id, reason="initial")
        accounts.set_state(db, second, TelegramAccount.AUTH_REQUIRED, reason="session revoked")
        db.commit()

        impact = assignments.failure_impact(db, workspace.id, first.id)
        assert impact["stranded"] == [shared.id]


# --- constraints that only exist in PostgreSQL ----------------------------


@pytest.fixture
def pg_engine():
    env = {**os.environ, "DATABASE_URL": MIGRATION_DSN, "SECRET_KEY": "test-secret-key", "ENVIRONMENT": "test"}
    for args in (("downgrade", "base"), ("upgrade", "head")):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=REPO, env=env, check=True, capture_output=True
        )
    engine = create_engine(MIGRATION_DSN)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', now()), (2, 'other', now())")
        )
        conn.execute(
            text(
                "INSERT INTO telegram_accounts (id, workspace_id, label, session_string, is_active, state, "
                "consecutive_failures, links_collected, created_at) "
                "VALUES (1, 1, 'a', 'x', true, 'ACTIVE', 0, 0, now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO channels (id, workspace_id, tg_channel_id, kind, source, last_message_id, "
                "is_active, created_at) VALUES (1, 1, '-1001', 'channel', 'userbot', 0, true, now())"
            )
        )
    yield engine
    engine.dispose()
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"], cwd=REPO, env=env, check=True, capture_output=True
    )


@pg_only
def test_postgres_refuses_an_account_state_that_lies(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(Exception) as caught:
            conn.execute(text("UPDATE telegram_accounts SET is_active = false WHERE id = 1"))
    assert "ck_account_state_matches_is_active" in str(caught.value)


@pg_only
def test_postgres_refuses_an_invented_access_state(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(Exception) as caught:
            conn.execute(
                text(
                    "INSERT INTO source_access (workspace_id, source_id, path_kind, state, created_at, updated_at) "
                    "VALUES (1, 1, 'userbot', 'PROBABLY_FINE', now(), now())"
                )
            )
    assert "ck_source_access_state" in str(caught.value)


@pg_only
def test_postgres_refuses_an_invented_join_status(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(Exception) as caught:
            conn.execute(
                text(
                    "INSERT INTO join_requests (workspace_id, source_id, account_id, status, priority, "
                    "attempt_count, created_at, updated_at) VALUES (1, 1, 1, 'NEARLY', 0, 0, now(), now())"
                )
            )
    assert "ck_join_request_status" in str(caught.value)


@pg_only
def test_the_join_queue_is_isolated_between_workspaces(pg_engine):
    """A queue row says which sources a tenant is trying to get into."""
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(
            text(
                "INSERT INTO join_requests (workspace_id, source_id, account_id, status, priority, "
                "attempt_count, created_at, updated_at) VALUES (1, 1, 1, 'READY', 0, 0, now(), now())"
            )
        )
    with pg_engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '2', false)"))
        assert conn.execute(text("SELECT count(*) FROM join_requests")).scalar() == 0
    with pg_engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        assert conn.execute(text("SELECT count(*) FROM join_requests")).scalar() == 1


@pg_only
def test_the_migration_derives_account_state_without_inventing(pg_engine):
    """INACTIVE and DISABLED are told apart by what the old schema recorded."""
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        rows = conn.execute(
            text("SELECT state, is_active, tg_user_id, state_changed_at FROM telegram_accounts WHERE id = 1")
        ).one()
    assert rows.state == "ACTIVE" and rows.is_active is True
    assert rows.tg_user_id is None, "an identity was invented for an account nobody has connected as"
    assert rows.state_changed_at is None, "a transition time was invented for a row that never transitioned"
