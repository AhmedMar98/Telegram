"""Can this path read this source, and how do we know.

Access is a relationship, not a property of a source. The same channel is
readable by account 3, invisible to account 7, and available over the
public path all at once — three facts about three pairs, not three
sources and not one source with a status.

Three distinctions this module exists to keep:

    UNKNOWN      != INACCESSIBLE      absent measurement vs failed one
    REQUEST_SENT != ACCESS_GRANTED    asking is not being answered
    access failure != invalid source  a real channel we cannot read is
                                      still a real channel

Every state written here carries the time it was observed and, where one
exists, the evidence that supports it. A state with neither is a claim,
and this module does not make claims.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, Evidence, SourceAccess
from app.timeutil import utcnow

#: The paths a source can be read over. ``userbot`` needs an account with
#: real access; ``public`` needs none. Same two words ``Channel.source``
#: uses — one vocabulary, not two.
PATH_USERBOT = "userbot"
PATH_PUBLIC = "public"

#: The states that mean "this pair can collect right now". Everything else
#: is a reason it cannot, and they are different reasons.
USABLE = (SourceAccess.ACCESSIBLE,)


def record(
    db: Session,
    channel: Channel,
    state: str,
    *,
    path_kind: str = PATH_USERBOT,
    account_id: int | None = None,
    observed_at: datetime | None = None,
    evidence_kind: str | None = None,
    evidence_summary: str | None = None,
    evidence_detail: dict | None = None,
) -> SourceAccess:
    """Write what was observed about one (source, path, account). No commit.

    Upserts on the pair rather than appending, because this table holds
    the *current* answer — the history of how it changed lives in
    ``source_events``, and duplicating it here would give two places to
    read a state from and two chances to read a stale one.
    """
    if state not in SourceAccess.STATES:
        raise ValueError(f"unknown access state: {state!r}")
    if path_kind == PATH_PUBLIC and account_id is not None:
        raise ValueError("the public path is not read by an account; leave account_id unset")
    if path_kind == PATH_USERBOT and account_id is None:
        raise ValueError("a userbot path needs the account that observed it")

    evidence_id = None
    if evidence_summary is not None:
        evidence = Evidence(
            workspace_id=channel.workspace_id,
            kind=evidence_kind or "access_probe",
            observed_at=observed_at or utcnow(),
            summary=evidence_summary[:300],
            detail=json.dumps(evidence_detail, ensure_ascii=False) if evidence_detail else None,
        )
        db.add(evidence)
        db.flush()
        evidence_id = evidence.id

    row = db.execute(
        select(SourceAccess).where(
            SourceAccess.source_id == channel.id,
            SourceAccess.path_kind == path_kind,
            SourceAccess.account_id.is_(None) if account_id is None else SourceAccess.account_id == account_id,
        )
    ).scalar_one_or_none()

    now = utcnow()
    if row is None:
        row = SourceAccess(
            workspace_id=channel.workspace_id,
            source_id=channel.id,
            account_id=account_id,
            path_kind=path_kind,
            state=state,
            observed_at=observed_at,
            evidence_id=evidence_id,
        )
        db.add(row)
    else:
        row.state = state
        row.observed_at = observed_at
        row.updated_at = now
        if evidence_id is not None:
            row.evidence_id = evidence_id
    db.flush()
    return row


def state_for(db: Session, source_id: int, account_id: int) -> str:
    """What is known about this account's access to this source.

    Returns ``UNKNOWN`` when there is no row, which is the honest answer:
    nobody has looked. A caller that needs "can collect" must test for
    ACCESSIBLE, never for "not INACCESSIBLE".
    """
    row = db.execute(
        select(SourceAccess).where(
            SourceAccess.source_id == source_id,
            SourceAccess.path_kind == PATH_USERBOT,
            SourceAccess.account_id == account_id,
        )
    ).scalar_one_or_none()
    return row.state if row is not None else SourceAccess.UNKNOWN


def accounts_with_access(db: Session, source_id: int) -> set[int]:
    """Accounts observed to be able to read this source."""
    rows = db.execute(
        select(SourceAccess.account_id).where(
            SourceAccess.source_id == source_id,
            SourceAccess.path_kind == PATH_USERBOT,
            SourceAccess.state.in_(USABLE),
            SourceAccess.account_id.is_not(None),
        )
    ).all()
    return {row[0] for row in rows}


def public_path_state(db: Session, source_id: int) -> str:
    row = db.execute(
        select(SourceAccess).where(
            SourceAccess.source_id == source_id,
            SourceAccess.path_kind == PATH_PUBLIC,
        )
    ).scalar_one_or_none()
    return row.state if row is not None else SourceAccess.UNKNOWN


def needs_access(db: Session, workspace_id: int) -> list[SourceAccess]:
    """Pairs waiting on somebody: a request to send, or a person to act.

    ``REQUEST_SENT`` is included deliberately. It is not access, and a
    queue that hides pending requests is a queue that cannot tell you why
    a source is not being collected.
    """
    return list(
        db.execute(
            select(SourceAccess).where(
                SourceAccess.workspace_id == workspace_id,
                SourceAccess.state.in_(
                    (SourceAccess.NEEDS_ACCESS, SourceAccess.REQUEST_SENT, SourceAccess.ACCESS_DENIED)
                ),
            )
        )
        .scalars()
        .all()
    )
