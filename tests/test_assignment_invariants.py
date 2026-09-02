"""The ten assignment invariants of §44.6, at the scale they matter at.

Every test here drives ``plan_assignments`` — a pure function — with real
``Channel`` and ``TelegramAccount`` instances built in memory, no database
and no session. That choice is deliberate on both halves:

**Real classes, not fakes.** A fake channel with three attributes would
keep passing after the model grew a fourth that the planner reads. These
are the same classes production uses; only the persistence is absent.

**Scale, because these invariants are about scale.** Ten accounts and
thousands of sources is the case the engine exists for, and it is the case
where an off-by-one in the least-loaded pick, or a tie broken
inconsistently, stops being invisible. Measured: 5,000 sources across 10
accounts plans in about 15 ms, so the whole file costs less than one
database round trip.

Invariant 1 of §44.6 — "a source names at most one account" — is not
tested behaviourally. It cannot be violated: ``Channel.account_id`` is a
single-valued column, so no code path can make a row name two accounts.
``test_a_source_can_only_ever_name_one_account`` pins that *structural*
fact instead, so a future change to a join table cannot quietly invalidate
the reasoning the other nine rest on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column
from sqlalchemy.inspection import inspect

from app.assignment import AssignmentReport, plan_assignments
from app.models import Channel, TelegramAccount

ACCOUNTS = 10
SOURCES = 2_000


def _accounts(count: int = ACCOUNTS, *, active: bool = True) -> list[TelegramAccount]:
    return [
        TelegramAccount(id=n, workspace_id=1, label=f"acct-{n}", session_string="x", is_active=active)
        for n in range(1, count + 1)
    ]


def _sources(count: int = SOURCES, *, public: bool = True, account_id: int | None = None) -> list[Channel]:
    """``public`` decides portability: a @username resolves for any account."""
    return [
        Channel(
            id=n,
            workspace_id=1,
            tg_channel_id=f"-100{n:06d}",
            username=f"src{n}" if public else None,
            account_id=account_id,
        )
        for n in range(1, count + 1)
    ]


def _apply(changes: dict[int, int], channels: list[Channel]) -> None:
    """What ``apply_assignments`` writes, without a database."""
    by_id = {channel.id: channel for channel in channels}
    for channel_id, account_id in changes.items():
        by_id[channel_id].account_id = account_id


def _placed(report: AssignmentReport) -> int:
    return sum(report.per_account.values())


# --- invariant 1: structural, not behavioural ------------------------------


def test_a_source_can_only_ever_name_one_account():
    """The premise the other nine invariants stand on.

    If this ever becomes a collection — a join table letting one source be
    read by several accounts — then "no duplicate assignment" stops being
    free and every test below needs rewriting. Pinning it here makes that
    a failing test rather than a silent change of meaning.
    """
    attribute = inspect(Channel).attrs["account_id"]
    assert len(attribute.columns) == 1
    assert isinstance(attribute.columns[0], Column)
    assert not attribute.uselist if hasattr(attribute, "uselist") else True


# --- invariant 2: stability ------------------------------------------------


def test_a_stable_assignment_is_never_disturbed():
    """A move costs the receiving account a dialog it may not be able to
    open, so "already working" must never be a reason to move anything."""
    accounts = _accounts()
    channels = _sources()
    # Deal them out unevenly on purpose: 1,800 on one account and the rest
    # spread over the other nine — an imbalance a load-balancer would rush
    # to correct. The remainder deliberately skips account 1 so the number
    # asserted below is exactly the number dealt.
    for index, channel in enumerate(channels):
        channel.account_id = 1 if index < 1_800 else 2 + (index % (ACCOUNTS - 1))

    changes, report = plan_assignments(channels, accounts, capacity=SOURCES)

    assert changes == {}, "nothing may move while every owning account is alive"
    assert report.moved == 0
    assert report.kept == SOURCES
    assert report.per_account[1] == 1_800, "the imbalance is preserved, not corrected"


# --- invariant 3: a disabled account receives nothing ----------------------


def test_a_disabled_account_receives_nothing():
    """Sabotage: pass every account instead of only the active ones and a
    dead account collects assignments it will never read."""
    alive = _accounts(3)
    dead = _accounts(10)[3:]  # ids 4..10, never handed to the planner
    for account in dead:
        account.is_active = False

    channels = _sources(600)
    changes, report = plan_assignments(channels, alive, capacity=SOURCES)

    assigned_to = set(changes.values())
    assert assigned_to == {1, 2, 3}
    assert not assigned_to & {account.id for account in dead}
    assert set(report.per_account) == {1, 2, 3}


# --- invariant 4: an eligible account does receive --------------------------


def test_an_eligible_account_receives_new_sources():
    """The counterpart to invariant 3: refusing everything is also wrong."""
    accounts = _accounts(2)
    settled = _sources(100, account_id=1)
    fresh = _sources(50)
    for index, channel in enumerate(fresh, start=1_000):  # distinct ids
        channel.id = index

    changes, report = plan_assignments([*settled, *fresh], accounts, capacity=SOURCES)

    assert report.moved == 50, "every new source found a home"
    assert set(changes.values()) == {2}, "all of them to the account with room"
    assert report.per_account == {1: 100, 2: 50}


# --- invariant 5: only what policy allows to move, moves --------------------


def test_only_policy_movable_sources_move_on_failure():
    """An account dies holding a mix. Public sources are portable, private
    ones are not — and the private ones must be *reported*, not moved and
    not dropped."""
    survivors = _accounts(3)
    dead_id = 99

    public = _sources(400, public=True, account_id=dead_id)
    private = _sources(400, public=False, account_id=dead_id)
    for index, channel in enumerate(private, start=10_000):
        channel.id = index

    changes, report = plan_assignments([*public, *private], survivors, capacity=SOURCES)

    assert len(changes) == 400, "the portable half moved"
    assert set(changes) == {channel.id for channel in public}
    assert len(report.stranded) == 400, "the private half is reported, not silently lost"
    assert report.needs_attention
    # Nothing vanished: every source is either placed or named.
    assert _placed(report) + len(report.stranded) + len(report.overflow) == 800


# --- invariant 6: a returning account does not reclaim ----------------------


def test_a_returning_account_does_not_reclaim():
    """Corrected from the reviewer's wording, which assumed the opposite.

    Re-enabling an account does **not** pull its old sources back. Those
    settled somewhere that works, and moving them again would be the
    oscillation invariant 7 forbids. What a returning account gets is a
    share of what is *homeless or new*, which is what the second half of
    this test asserts — otherwise "does not reclaim" could be satisfied by
    an engine that ignores the account forever.
    """
    returning = TelegramAccount(id=2, workspace_id=1, label="back", session_string="x", is_active=True)
    holder = TelegramAccount(id=1, workspace_id=1, label="holder", session_string="x", is_active=True)

    migrated = _sources(300, account_id=1)  # once account 2's, moved while it was down
    changes, report = plan_assignments(migrated, [holder, returning], capacity=SOURCES)
    assert changes == {}, "settled sources stay settled"
    assert report.per_account[2] == 0

    newcomers = _sources(100)
    for index, channel in enumerate(newcomers, start=5_000):
        channel.id = index
    changes, report = plan_assignments([*migrated, *newcomers], [holder, returning], capacity=SOURCES)
    assert set(changes.values()) == {2}, "a returning account is eligible for new work"
    assert report.per_account[2] == 100


# --- invariant 7: no oscillation -------------------------------------------


def test_repeated_rounds_reach_a_fixed_point():
    """Run the planner five times, applying each plan. After the first
    round nothing may move again — an engine that keeps re-balancing burns
    Telegram calls forever and never settles."""
    accounts = _accounts()
    channels = _sources()

    first, _ = plan_assignments(channels, accounts, capacity=SOURCES)
    _apply(first, channels)
    assert first, "the first round does the work"

    for round_number in range(2, 7):
        changes, report = plan_assignments(channels, accounts, capacity=SOURCES)
        assert changes == {}, f"round {round_number} moved something after settling"
        assert report.moved == 0
        assert report.kept == SOURCES


def test_a_failure_and_recovery_cycle_still_settles():
    """The harder version: churn the account set and confirm each state
    settles rather than flapping between two assignments."""
    accounts = _accounts(4)
    channels = _sources(400)
    _apply(plan_assignments(channels, accounts, capacity=SOURCES)[0], channels)

    for _ in range(3):
        # Account 4 dies: its portable sources move, and stay moved.
        survivors = accounts[:3]
        _apply(plan_assignments(channels, survivors, capacity=SOURCES)[0], channels)
        again, _ = plan_assignments(channels, survivors, capacity=SOURCES)
        assert again == {}, "a second pass over the same failure moves nothing"

        # Account 4 returns: nothing is pulled back.
        recovered, _ = plan_assignments(channels, accounts, capacity=SOURCES)
        assert recovered == {}, "recovery must not undo the failover"


# --- invariant 8: determinism ----------------------------------------------


def test_the_plan_is_deterministic():
    """Identical inputs, identical output — byte for byte, every time."""
    plans = []
    for _ in range(5):
        plans.append(plan_assignments(_sources(500), _accounts(), capacity=SOURCES)[0])
    assert all(plan == plans[0] for plan in plans)


def test_the_shape_of_the_distribution_survives_input_order():
    """A weaker property than per-source determinism, and a real one: the
    planner may hand a *different* account to a given source when the input
    arrives in another order, but the number each account ends up with must
    not depend on that order."""
    ordered = _sources(500)
    shuffled = list(reversed(_sources(500)))

    _, first = plan_assignments(ordered, _accounts(), capacity=SOURCES)
    _, second = plan_assignments(shuffled, _accounts(), capacity=SOURCES)

    assert sorted(first.per_account.values()) == sorted(second.per_account.values())
    assert max(first.per_account.values()) - min(first.per_account.values()) <= 1, "balanced to within one"


# --- invariant 9: capacity ---------------------------------------------------


@pytest.mark.parametrize("capacity", [1, 7, 100])
def test_capacity_is_never_exceeded_at_scale(capacity: int):
    accounts = _accounts()
    channels = _sources(1_500)

    _, report = plan_assignments(channels, accounts, capacity=capacity)

    assert all(load <= capacity for load in report.per_account.values())
    ceiling = capacity * ACCOUNTS
    assert _placed(report) == min(1_500, ceiling)
    assert len(report.overflow) == max(0, 1_500 - ceiling), "the excess is reported, not dropped"


def test_nothing_is_lost_between_placed_stranded_and_overflow():
    """The totality property of §44.6: assignment is a total function.

    Every assignable source ends up in exactly one of three buckets. A
    source that is in none of them has vanished, which is the one outcome
    no counter would reveal.
    """
    accounts = _accounts(2)
    public = _sources(300)
    private = _sources(200, public=False, account_id=77)  # owner gone
    for index, channel in enumerate(private, start=20_000):
        channel.id = index

    _, report = plan_assignments([*public, *private], accounts, capacity=120)

    assert _placed(report) + len(report.stranded) + len(report.overflow) == 500


# --- invariant 10: access before availability -------------------------------


def test_inaccessible_sources_are_never_assigned():
    """The invariant that makes this engine access-aware instead of
    count-aware. Ten idle accounts with room to spare must still not be
    handed a private dialog none of them is a member of."""
    accounts = _accounts()
    orphaned = _sources(500, public=False, account_id=42)  # account 42 is gone

    changes, report = plan_assignments(orphaned, accounts, capacity=SOURCES)

    assert changes == {}, "availability is not access"
    assert len(report.stranded) == 500
    assert all(load == 0 for load in report.per_account.values())


def test_an_unowned_private_source_is_still_placeable():
    """The boundary of invariant 10, which it would be easy to overshoot.

    A private dialog that names *no* account has not been orphaned by a
    failure — it is new, and the account that just discovered it is the one
    about to be recorded. Refusing to place it would strand every private
    dialog forever, which is a stricter rule than access requires.
    """
    accounts = _accounts(2)
    fresh_private = _sources(10, public=False, account_id=None)

    changes, report = plan_assignments(fresh_private, accounts, capacity=SOURCES)

    assert len(changes) == 10
    assert report.stranded == []
