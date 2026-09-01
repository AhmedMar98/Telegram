"""Which account collects which source, decided rather than inherited.

Until now the answer was an accident of insertion order. ``Channel``
carried an ``account_id`` written once by whichever account happened to
discover the dialog, rows with no account fell to the lowest-numbered
account, and nothing ever revisited either. Add a second account and the
first one keeps everything; disable the account holding four hundred
channels and all four hundred stop being collected, silently, because the
rows still name an account that no longer runs.

**The constraint that shapes this module is access, not load.** A userbot
can only read a dialog it is actually in. Balancing purely by count —
"account A has 400, account B has 100, move 150 across" — produces an
assignment that looks even and collects nothing, because account B is not
a member of the channels it was handed. Telegram will resolve a public
``@username`` for any account, so those are portable; a private channel
known only by numeric id is readable by its own account or by nobody.

So the plan here moves what it can prove is movable, pins what it cannot,
and **reports what it had to strand** instead of quietly producing an
assignment that cannot work. A stranded source is an operator decision —
add the account back, or add another account to that channel — and the
one thing this module must never do is hide that the decision is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import get_settings
from app.dialogs import SOURCE_USERBOT, is_synthetic
from app.models import Channel, TelegramAccount

logger = logging.getLogger(__name__)


def is_portable(channel: Channel) -> bool:
    """Whether an account that never discovered this channel could read it.

    A public ``@username`` resolves for any account; a private dialog known
    only by numeric id does not. This is the whole access constraint, and
    it is deliberately conservative: a wrong "portable" produces a channel
    nobody collects, while a wrong "pinned" only leaves it where it is.
    """
    return bool(channel.username)


@dataclass
class AssignmentReport:
    """What one planning pass decided, in the operator's terms."""

    moved: int = 0
    kept: int = 0
    #: Channels whose account is gone and which no other account can reach.
    stranded: list[str] = field(default_factory=list)
    #: Channels that found no account under the capacity limit.
    overflow: list[str] = field(default_factory=list)
    #: account id -> channels assigned to it after this pass.
    per_account: dict[int, int] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return bool(self.stranded or self.overflow)


def capacity_per_account() -> int:
    """How many sources one account is asked to carry.

    A configured operating limit, **not a claim about what an account can
    survive**. The safe number depends on the dialogs, their message rate,
    the account's age and Telegram's own limits on the day — none of which
    this system can measure — so it is settable, and nothing here promises
    that any particular value is safe.
    """
    return max(1, get_settings().assignment_capacity_per_account)


def plan_assignments(
    channels: list[Channel], accounts: list[TelegramAccount], *, capacity: int
) -> tuple[dict[int, int], AssignmentReport]:
    """Decide the channel → account mapping. Pure: touches no database.

    Returns ``({channel_id: account_id}, report)`` containing only the
    channels whose assignment *changes*, so a caller can tell "nothing to
    do" from "everything reassigned" without diffing.
    """
    report = AssignmentReport()
    if not accounts:
        # Every channel is stranded, and saying so is the point: this is
        # what "all accounts disabled" looks like from the source's side.
        report.stranded = [_label(channel) for channel in channels]
        return {}, report

    active_ids = {account.id for account in accounts}
    load = {account.id: 0 for account in accounts}
    changes: dict[int, int] = {}
    homeless: list[Channel] = []

    # Pass one: everything already assigned to a live account stays there.
    # Stability is not cosmetic — a move costs the new account a dialog it
    # may not be able to open, so it must be justified, and "already
    # working" never justifies it.
    for channel in channels:
        if channel.account_id in active_ids:
            load[channel.account_id] += 1
            report.kept += 1
        else:
            homeless.append(channel)

    # Pass two: place what is left, least-loaded first.
    for channel in homeless:
        if channel.account_id is not None and not is_portable(channel):
            # Pinned to an account that is gone. Nobody else can open it.
            report.stranded.append(_label(channel))
            continue
        candidates = [account_id for account_id in load if load[account_id] < capacity]
        if not candidates:
            report.overflow.append(_label(channel))
            continue
        target = min(sorted(candidates), key=lambda account_id: load[account_id])
        changes[channel.id] = target
        load[target] += 1
        report.moved += 1

    report.per_account = load
    return changes, report


def collectable_channels(db: Session, workspace_id: int) -> list[Channel]:
    """Every channel a userbot is supposed to be reading for this workspace."""
    rows = (
        db.query(Channel)
        .filter(
            Channel.workspace_id == workspace_id,
            Channel.is_active.is_(True),
            Channel.source == SOURCE_USERBOT,
        )
        .order_by(Channel.id)
        .all()
    )
    return [row for row in rows if not is_synthetic(row.tg_channel_id)]


def apply_assignments(db: Session, workspace_id: int) -> AssignmentReport:
    """Plan, write the changes, and report. Commits."""
    accounts = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.is_active.is_(True))
        .order_by(TelegramAccount.id)
        .all()
    )
    channels = collectable_channels(db, workspace_id)
    changes, report = plan_assignments(channels, accounts, capacity=capacity_per_account())

    if changes:
        by_id = {channel.id: channel for channel in channels}
        for channel_id, account_id in changes.items():
            by_id[channel_id].account_id = account_id
        db.commit()

    if report.stranded:
        logger.warning(
            "assignment: %d source(s) have no account that can read them: %s",
            len(report.stranded),
            ", ".join(report.stranded[:10]),
        )
    if report.overflow:
        logger.warning(
            "assignment: %d source(s) exceed the per-account capacity of %d",
            len(report.overflow),
            capacity_per_account(),
        )
    return report


def _label(channel: Channel) -> str:
    return channel.username or channel.title or channel.tg_channel_id
