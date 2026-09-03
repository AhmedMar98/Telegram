"""Which accounts may be given a source, and why the rest may not.

Assignment without eligibility produces a plan that looks balanced and
collects nothing: an account is handed a private channel it is not a
member of, the run fails every hour, and the numbers on the dashboard say
the work is evenly distributed. So eligibility runs first, and the reason
each account was excluded is returned rather than thrown away — "no
eligible account" and "no eligible account *because every one of them is
disabled*" need different responses from a person.

The order is fixed and each step answers a different question:

    access    can this account actually read it?
    state     is the account usable at all?
    capacity  is there room, under the configured operating limit?

Capacity is last on purpose. An account that cannot read the source is not
"full", and reporting it that way sends the operator to raise a limit that
was never the problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import access
from app.models import Channel, SourceAssignment, TelegramAccount

#: Why an account was not eligible. Strings rather than an enum because
#: they are shown to a person and logged, and a code they have to look up
#: helps nobody.
NO_ACCESS = "no observed access"
NOT_USABLE = "account state is not ACTIVE"
AT_CAPACITY = "at the configured capacity"


@dataclass
class Eligibility:
    """The answer, with its reasoning kept."""

    eligible: list[int] = field(default_factory=list)
    #: account id -> why it was excluded.
    excluded: dict[int, str] = field(default_factory=dict)

    @property
    def has_candidate(self) -> bool:
        return bool(self.eligible)


def current_load(db: Session, account_ids: list[int]) -> dict[int, int]:
    """How many sources each account currently holds.

    Counted from open assignments — the authority — rather than from the
    ``channels.account_id`` mirror, so a count can never disagree with the
    assignment it is counting.
    """
    if not account_ids:
        return {}
    rows = db.execute(
        select(SourceAssignment.account_id, func.count(SourceAssignment.id))
        .where(
            SourceAssignment.account_id.in_(account_ids),
            SourceAssignment.released_at.is_(None),
        )
        .group_by(SourceAssignment.account_id)
    ).all()
    load = dict.fromkeys(account_ids, 0)
    for account_id, count in rows:
        load[account_id] = count
    return load


def evaluate(
    db: Session,
    channel: Channel,
    accounts: list[TelegramAccount],
    *,
    capacity: int,
    require_observed_access: bool = True,
) -> Eligibility:
    """Which of ``accounts`` may take ``channel``, and why the others may not.

    ``require_observed_access`` exists because access has only been
    *observed* for pairs that have actually collected: everything else is
    UNKNOWN, and refusing every UNKNOWN pair today would strand every
    source that has not run yet. So a public source — one Telegram will
    resolve for any account — stays assignable while UNKNOWN, and a
    private one does not. That is the same rule the balancer already used
    (``is_portable``), stated where it can be seen rather than buried in a
    sort key.
    """
    result = Eligibility()
    with_access = access.accounts_with_access(db, channel.id)
    load = current_load(db, [account.id for account in accounts])
    portable = bool(channel.username)

    for account in accounts:
        if account.state != TelegramAccount.ACTIVE:
            result.excluded[account.id] = NOT_USABLE
            continue
        # A private dialog nobody has been observed to read is not a
        # candidate: handing it over produces an assignment that cannot
        # collect, which is worse than an unassigned source because it
        # looks like coverage.
        if account.id not in with_access and require_observed_access and not portable:
            result.excluded[account.id] = NO_ACCESS
            continue
        if load.get(account.id, 0) >= capacity:
            result.excluded[account.id] = AT_CAPACITY
            continue
        result.eligible.append(account.id)

    # Lowest load first, then account id: a deterministic order, so the
    # same fleet in the same state always produces the same plan.
    result.eligible.sort(key=lambda account_id: (load.get(account_id, 0), account_id))
    return result


def failover_candidates(
    db: Session, channel: Channel, accounts: list[TelegramAccount], *, capacity: int
) -> list[int]:
    """Accounts that could take this source if the current holder stopped.

    The foundation for failover, not failover itself: this answers "is
    there anyone else", which is what makes a source's risk visible before
    the account fails rather than after.
    """
    holder = channel.account_id
    result = evaluate(db, channel, accounts, capacity=capacity)
    return [account_id for account_id in result.eligible if account_id != holder]
