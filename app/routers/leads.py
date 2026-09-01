"""Requests noticed in monitored dialogs, the rules that noticed them, and
the people behind them.

Every route here is behind a permission, and the interesting one is that
``operator`` — the technical role that manages collection accounts — has
none of them. Whoever keeps the userbots running does not need the names
and messages of the people those userbots observed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import roles
from app.audit import record as audit_record
from app.database import get_db
from app.leads import LEAD_STATUSES, forget, leads_enabled, purge_expired, score_text
from app.models import Beneficiary, KeywordRule, Lead, User
from app.schemas import (
    BeneficiaryOut,
    KeywordRuleCreate,
    KeywordRuleOut,
    KeywordRuleUpdate,
    LeadOut,
    LeadsStatusOut,
    LeadStatusUpdate,
    LeadTestIn,
    LeadTestOut,
    TeamMemberOut,
    TeamRoleUpdate,
)

router = APIRouter(tags=["leads"])


@router.get("/leads/status", response_model=LeadsStatusOut)
def leads_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_READ)),
) -> LeadsStatusOut:
    """Whether the feature is on, and how much it has found.

    Reports ``enabled: false`` rather than 404 when the flag is off, so the
    interface can say "this is switched off" instead of showing a broken
    panel — which is what an empty list would look like.
    """
    ws = current_user.workspace_id
    return LeadsStatusOut(
        enabled=leads_enabled(),
        total=db.query(Lead).filter(Lead.workspace_id == ws).count(),
        new=db.query(Lead).filter(Lead.workspace_id == ws, Lead.status == "new").count(),
        beneficiaries=db.query(Beneficiary).filter(Beneficiary.workspace_id == ws).count(),
        rules=db.query(KeywordRule).filter(KeywordRule.workspace_id == ws).count(),
    )


@router.get("/leads", response_model=list[LeadOut])
def list_leads(
    lead_status: str | None = Query(default=None, alias="status"),
    min_score: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_READ)),
) -> list[Lead]:
    query = db.query(Lead).filter(Lead.workspace_id == current_user.workspace_id)
    if lead_status:
        if lead_status not in LEAD_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of: {', '.join(LEAD_STATUSES)}",
            )
        query = query.filter(Lead.status == lead_status)
    if min_score:
        query = query.filter(Lead.score >= min_score)

    # Most serious first, then newest. Ordering by date alone would bury a
    # strong request under a page of weak ones, which is the failure this
    # whole scoring exercise exists to prevent.
    return query.order_by(Lead.score.desc(), Lead.created_at.desc(), Lead.id.desc()).limit(limit).all()


@router.patch("/leads/{lead_id}", response_model=LeadOut)
def update_lead_status(
    lead_id: int,
    payload: LeadStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_WRITE)),
) -> Lead:
    if payload.status not in LEAD_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of: {', '.join(LEAD_STATUSES)}",
        )
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.workspace_id == current_user.workspace_id).first()
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lead not found")

    lead.status = payload.status
    db.commit()
    db.refresh(lead)
    return lead


# --- keyword rules ---------------------------------------------------------


@router.get("/leads/keywords", response_model=list[KeywordRuleOut])
def list_keywords(
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_READ)),
) -> list[KeywordRule]:
    return (
        db.query(KeywordRule)
        .filter(KeywordRule.workspace_id == current_user.workspace_id)
        .order_by(KeywordRule.weight.desc(), KeywordRule.id)
        .all()
    )


@router.post("/leads/keywords", response_model=KeywordRuleOut, status_code=status.HTTP_201_CREATED)
def add_keyword(
    payload: KeywordRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_WRITE)),
) -> KeywordRule:
    phrase = payload.phrase.strip()
    existing = (
        db.query(KeywordRule)
        .filter(KeywordRule.workspace_id == current_user.workspace_id, KeywordRule.phrase == phrase)
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="هذه العبارة مضافة بالفعل")

    rule = KeywordRule(workspace_id=current_user.workspace_id, phrase=phrase, weight=payload.weight)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/leads/keywords/{rule_id}", response_model=KeywordRuleOut)
def update_keyword(
    rule_id: int,
    payload: KeywordRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_WRITE)),
) -> KeywordRule:
    rule = (
        db.query(KeywordRule)
        .filter(KeywordRule.id == rule_id, KeywordRule.workspace_id == current_user.workspace_id)
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")

    if payload.weight is not None:
        rule.weight = payload.weight
    if payload.is_active is not None:
        rule.is_active = payload.is_active
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/leads/keywords/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_keyword(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_WRITE)),
) -> None:
    # Looked up before deleting rather than issuing a blind DELETE with a
    # workspace filter. Both are equally safe, but a blind delete answers
    # 204 whether or not the row existed — so an owner whose rule id was
    # stale gets "done" for an operation that did nothing.
    rule = (
        db.query(KeywordRule)
        .filter(KeywordRule.id == rule_id, KeywordRule.workspace_id == current_user.workspace_id)
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")

    db.delete(rule)
    db.commit()


@router.post("/leads/test", response_model=LeadTestOut)
def test_keywords(
    payload: LeadTestIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_READ)),
) -> LeadTestOut:
    """Run the live rule set against a sample message.

    A rule set nobody can try is a rule set nobody tunes: without this the
    only way to find out whether "مساعدة" is too broad is to wait an hour
    and read what it caught.
    """
    rules = (
        db.query(KeywordRule)
        .filter(KeywordRule.workspace_id == current_user.workspace_id, KeywordRule.is_active.is_(True))
        .all()
    )
    match = score_text(payload.text, rules)
    return LeadTestOut(score=match.score, matched=match.phrases, would_record=match.matched)


# --- beneficiaries ---------------------------------------------------------


@router.get("/leads/beneficiaries", response_model=list[BeneficiaryOut])
def list_beneficiaries(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_READ)),
) -> list[Beneficiary]:
    return (
        db.query(Beneficiary)
        .filter(Beneficiary.workspace_id == current_user.workspace_id)
        .order_by(Beneficiary.last_seen_at.desc())
        .limit(limit)
        .all()
    )


@router.delete("/leads/beneficiaries/{beneficiary_id}", status_code=status.HTTP_204_NO_CONTENT)
def forget_beneficiary(
    beneficiary_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_WRITE)),
) -> None:
    """Erase one person and every request recorded from them.

    Their leads go too. A lead's text is that person's own words, so a
    "forget this person" that left the messages behind would be a deletion
    in name only — and the name is the part that matters here.
    """
    if not forget(db, current_user.workspace_id, beneficiary_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="beneficiary not found")

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="beneficiary.forget",
        target_type="beneficiary",
        target_id=str(beneficiary_id),
        detail="erased with all their leads",
    )
    db.commit()


@router.post("/leads/purge", response_model=dict)
def purge_old_leads(
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.LEADS_WRITE)),
) -> dict:
    """Apply the retention window now instead of waiting for the schedule."""
    removed = purge_expired(db, current_user.workspace_id)
    return {"removed": removed}


# --- team and roles --------------------------------------------------------
#
# Separate from /auth on purpose: signing in is about proving who you are,
# and this is about deciding what that person may do. They were the same
# thing while roles were unenforced, which is roughly why they were.

team_router = APIRouter(prefix="/team", tags=["team"])


@team_router.get("", response_model=list[TeamMemberOut])
def list_team(
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.WORKSPACE_MANAGE)),
) -> list[TeamMemberOut]:
    members = (
        db.query(User)
        .filter(User.workspace_id == current_user.workspace_id)
        .order_by(User.created_at, User.id)
        .all()
    )
    return [
        TeamMemberOut(
            id=u.id,
            email=u.email,
            role=u.role,
            role_label=roles.ROLE_LABELS.get(u.role, u.role),
            is_active=u.is_active,
            created_at=u.created_at,
        )
        for u in members
    ]


@team_router.get("/roles", response_model=dict)
def available_roles(
    current_user: User = Depends(roles.require(roles.WORKSPACE_MANAGE)),
) -> dict:
    return {
        "roles": [
            {"value": r, "label": roles.ROLE_LABELS[r], "permissions": sorted(roles.ROLES[r])}
            for r in roles.ASSIGNABLE_ROLES
        ]
    }


@team_router.patch("/{user_id}", response_model=TeamMemberOut)
def set_role(
    user_id: int,
    payload: TeamRoleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(roles.require(roles.WORKSPACE_MANAGE)),
) -> TeamMemberOut:
    if payload.role not in roles.ASSIGNABLE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"role must be one of: {', '.join(roles.ASSIGNABLE_ROLES)}",
        )

    member = db.query(User).filter(User.id == user_id, User.workspace_id == current_user.workspace_id).first()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")

    # Demoting yourself out of workspace.manage would leave a workspace
    # nobody can administer, and the only fix would be database access.
    # Refused rather than warned about: an irreversible mistake that a
    # confirmation dialog invites you to click through is still
    # irreversible.
    if member.id == current_user.id and roles.WORKSPACE_MANAGE not in roles.permissions_for(payload.role):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="لا يمكنك إنزال دورك بنفسك — اطلب من مالك آخر فعل ذلك، وإلّا بقيت مساحة العمل بلا مدير",
        )

    # The same rule applied to the workspace rather than to one person: the
    # last account holding workspace.manage cannot be demoted by anyone,
    # including another owner.
    if roles.WORKSPACE_MANAGE in roles.permissions_for(member.role) and roles.WORKSPACE_MANAGE not in (
        roles.permissions_for(payload.role)
    ):
        remaining = [
            u
            for u in db.query(User).filter(
                User.workspace_id == current_user.workspace_id, User.is_active.is_(True)
            )
            if u.id != member.id and roles.WORKSPACE_MANAGE in roles.permissions_for(u.role)
        ]
        if not remaining:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="هذا آخر حساب يملك إدارة مساحة العمل — عيّن مديراً آخر أولاً",
            )

    before = member.role
    member.role = payload.role
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="team.role_change",
        target_type="user",
        target_id=str(member.id),
        detail=f"{before} -> {payload.role}",
    )
    db.commit()
    db.refresh(member)
    return TeamMemberOut(
        id=member.id,
        email=member.email,
        role=member.role,
        role_label=roles.ROLE_LABELS.get(member.role, member.role),
        is_active=member.is_active,
        created_at=member.created_at,
    )
