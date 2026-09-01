"""Who may do what.

``User.role`` has existed since the first migration and has never been
checked. It is set to ``owner`` at registration, read once to display it,
and consulted nowhere else — so every member of a workspace has every
power in it, including deleting the workspace and exporting everyone's
data. The column looked like access control and was decoration.

Two things this module does not do, both deliberate:

* **It does not lock existing deployments out.** ``owner`` keeps
  everything it had. ``member`` — the only other role that exists in the
  wild — keeps everything except the three workspace-level powers that
  should never have been shared: deleting the workspace, exporting all of
  its data, and managing the team. That is a real behaviour change and it
  is the point; it is also the narrowest one that closes the gap.

* **It does not invent a permission per endpoint.** Six permissions, named
  after what somebody is trying to *do*. A permission set with forty
  entries is one nobody can reason about, and the failure mode of access
  control is not too few checks — it is checks nobody understands well
  enough to notice are wrong.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.deps import get_current_user
from app.models import User

# --- permissions -----------------------------------------------------------

LINKS_READ = "links.read"
LINKS_WRITE = "links.write"
COLLECTION_MANAGE = "collection.manage"
LEADS_READ = "leads.read"
LEADS_WRITE = "leads.write"
WORKSPACE_MANAGE = "workspace.manage"

ALL_PERMISSIONS = frozenset(
    {LINKS_READ, LINKS_WRITE, COLLECTION_MANAGE, LEADS_READ, LEADS_WRITE, WORKSPACE_MANAGE}
)

OWNER = "owner"
MEMBER = "member"
AGENT = "agent"
OPERATOR = "operator"

ROLES: dict[str, frozenset[str]] = {
    # Everything. Unchanged from today, so an upgrade cannot lock out the
    # person who created the workspace.
    OWNER: ALL_PERMISSIONS,
    # Everything except the workspace itself. This is the one role whose
    # powers shrink, and the three it loses — delete the workspace, export
    # all of its data, manage the team — are the ones that were never
    # meant to be shared with an ordinary member.
    MEMBER: frozenset({LINKS_READ, LINKS_WRITE, COLLECTION_MANAGE, LEADS_READ, LEADS_WRITE}),
    # "مقدّم خدمة": works the leads and reads the archive. Cannot touch
    # collection accounts, because a person answering requests has no
    # reason to hold the credentials that produce them.
    AGENT: frozenset({LINKS_READ, LEADS_READ, LEADS_WRITE}),
    # "مشرف تقني": keeps collection running, and is deliberately the one
    # role with no access to leads at all.
    #
    # That omission is the interesting part. Whoever maintains the
    # userbots does not need the names, handles and messages of the people
    # those userbots observed — and separating the two means a technical
    # contractor can be given exactly the access their job needs without
    # also being handed a database of identifiable third parties.
    OPERATOR: frozenset({LINKS_READ, COLLECTION_MANAGE}),
}

DEFAULT_ROLE = MEMBER
ASSIGNABLE_ROLES = (OWNER, MEMBER, AGENT, OPERATOR)

ROLE_LABELS: dict[str, str] = {
    OWNER: "مالك — كل الصلاحيات",
    MEMBER: "عضو — كل شيء عدا إدارة مساحة العمل",
    AGENT: "مقدّم خدمة — الطلبات والأرشيف، بلا حسابات جمع",
    OPERATOR: "مشرف تقني — الجمع والحالة، بلا أي وصول لبيانات المستفيدين",
}


def permissions_for(role: str | None) -> frozenset[str]:
    """What this role may do.

    An unrecognised role gets the narrowest set rather than the widest.
    A typo in a role name must not be a privilege escalation, and a role
    written by a future version of this code that this one does not know
    is exactly that case.
    """
    return ROLES.get(role or "", ROLES[OPERATOR])


def has_permission(user: User, permission: str) -> bool:
    return permission in permissions_for(user.role)


def require(permission: str):
    """A FastAPI dependency that refuses a caller without ``permission``.

    403 rather than 404: the caller is authenticated and the resource
    exists, so hiding it would be a lie that makes the interface harder to
    debug without making anything safer — they already know the endpoint
    is there, they just used it.
    """

    def guard(current_user: User = Depends(get_current_user)) -> User:
        if not has_permission(current_user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"دورك «{current_user.role}» لا يملك صلاحية {permission}",
            )
        return current_user

    return guard
