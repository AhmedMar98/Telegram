"""Lead detection, the beneficiaries it creates, and who may see them.

The privacy tests here are not decoration. This feature turns a link
archive into a database about identifiable people, and the assertions that
matter most are the ones about what the system refuses to do: run while
switched off, keep records forever, and show them to the technical role
that maintains the collectors.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import roles
from app.config import get_settings
from app.database import SessionLocal
from app.leads import detect, normalise, purge_expired, score_text
from app.models import Beneficiary, KeywordRule, Lead, User
from tests.conftest import register_workspace


@pytest.fixture
def leads_on():
    os.environ["LEADS_ENABLED"] = "true"
    get_settings.cache_clear()
    yield
    os.environ.pop("LEADS_ENABLED", None)
    get_settings.cache_clear()


class _Rule:
    def __init__(self, phrase, weight=1, is_active=True):
        self.phrase, self.weight, self.is_active = phrase, weight, is_active


# --- Arabic matching -------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "rule"),
    [
        ("أبغى مُسَاعَدَة", "مساعدة"),  # harakat
        ("احتاج مساعده", "مساعدة"),  # ة / ه
        ("عندي إستفسار", "استفسار"),  # أ إ آ -> ا
        ("خلصت المشروع", "مشروع"),  # definite article
        ("مشروع التخرج جاهز", "مشروع تخرج"),  # article *between* the words
    ],
)
def test_spelling_differences_that_are_not_meaning_differences_still_match(written: str, rule: str):
    """Without normalisation the rule set has to enumerate spellings, which
    is how a keyword list becomes unmaintainable and quietly stops
    matching."""
    assert score_text(written, [_Rule(rule)]).matched


def test_the_definite_article_is_not_stripped_from_short_words():
    """Stripping it everywhere turns "الآن" into "ان" and starts matching
    things nobody asked for."""
    assert normalise("الآن") == "الان"
    assert normalise("الي") == "الي"


def test_weights_add_up_and_inactive_rules_are_ignored():
    rules = [_Rule("مشروع تخرج", 5), _Rule("مساعدة", 1), _Rule("تسويق", 3, is_active=False)]

    match = score_text("أبغى مساعدة في مشروع التخرج وأيضاً تسويق", rules)

    assert match.score == 6
    assert "تسويق" not in match.phrases


def test_an_unrelated_message_scores_zero():
    assert score_text("صباح الخير يا شباب", [_Rule("مشروع تخرج", 5)]).score == 0


# --- the off switch --------------------------------------------------------


def _seed_rule(workspace_id: int, phrase: str = "مساعدة", weight: int = 1) -> None:
    db = SessionLocal()
    try:
        db.add(KeywordRule(workspace_id=workspace_id, phrase=phrase, weight=weight))
        db.commit()
    finally:
        db.close()


def _workspace_and_channel(client: TestClient, email: str) -> tuple[int, int]:
    register_workspace(client, email=email, workspace_name="Leads")
    channel = client.post("/channels", json={"tg_channel_id": "lead-ch", "username": "leadch"}).json()
    db = SessionLocal()
    try:
        workspace_id = db.query(User).filter(User.email == email).one().workspace_id
    finally:
        db.close()
    return workspace_id, channel["id"]


def test_nothing_is_recorded_while_the_feature_is_off(client: TestClient):
    """The flag is the consent boundary, not a convenience. With it unset
    no row about any person may be written, however well the text matches."""
    workspace_id, channel_id = _workspace_and_channel(client, "off@example.com")
    _seed_rule(workspace_id)

    db = SessionLocal()
    try:
        lead = detect(
            db,
            workspace_id=workspace_id,
            channel_id=channel_id,
            text="أبغى مساعدة",
            message_id=1,
            sender_id="900",
        )
        db.commit()

        assert lead is None
        assert db.query(Lead).count() == 0
        assert db.query(Beneficiary).count() == 0
    finally:
        db.close()


def test_a_matching_message_creates_a_lead_and_its_person(client: TestClient, leads_on):
    workspace_id, channel_id = _workspace_and_channel(client, "on@example.com")
    _seed_rule(workspace_id, "مشروع تخرج", 5)

    db = SessionLocal()
    try:
        lead = detect(
            db,
            workspace_id=workspace_id,
            channel_id=channel_id,
            text="محتاج مساعدة في مشروع التخرج",
            message_id=11,
            sender_id="900",
            sender_username="student_x",
            sender_name="طالب",
        )
        db.commit()

        assert lead is not None and lead.score == 5
        assert "مشروع تخرج" in lead.matched, "a score with no explanation cannot be argued with"

        person = db.query(Beneficiary).filter(Beneficiary.workspace_id == workspace_id).one()
        assert person.tg_user_id == "900"
        assert person.request_count == 1
    finally:
        db.close()


def test_no_phone_number_is_stored_about_anybody():
    """Telegram exposes a phone on some peers. Storing it would turn a lead
    list into a contact database nobody consented to, so the column does
    not exist to be filled in by accident."""
    columns = {c.name for c in Beneficiary.__table__.columns}

    assert not {"phone", "phone_number", "email"} & columns


def test_the_same_message_seen_twice_produces_one_lead(client: TestClient, leads_on):
    """Normal operation, not an edge case: the live listener catches the
    message as it arrives and the hourly collector reads it again from
    history."""
    workspace_id, channel_id = _workspace_and_channel(client, "twice@example.com")
    _seed_rule(workspace_id)

    db = SessionLocal()
    try:
        for _ in range(2):
            detect(
                db,
                workspace_id=workspace_id,
                channel_id=channel_id,
                text="أبغى مساعدة",
                message_id=77,
                sender_id="901",
            )
            db.commit()

        assert db.query(Lead).filter(Lead.workspace_id == workspace_id).count() == 1
    finally:
        db.close()


def test_a_channel_post_with_no_author_still_records_a_lead(client: TestClient, leads_on):
    """A request with nobody to contact is still a request. Dropping it
    would lose exactly the broadcast announcements worth answering."""
    workspace_id, channel_id = _workspace_and_channel(client, "anon@example.com")
    _seed_rule(workspace_id)

    db = SessionLocal()
    try:
        lead = detect(
            db, workspace_id=workspace_id, channel_id=channel_id, text="أبغى مساعدة", message_id=5, sender_id=None
        )
        db.commit()

        assert lead is not None
        assert lead.beneficiary_id is None
    finally:
        db.close()


def test_detection_failing_never_costs_the_message_its_links(client: TestClient, leads_on, monkeypatch):
    """This runs inside ingestion. A lead missed is a lead; a raised
    exception here would be every link in the message."""
    workspace_id, channel_id = _workspace_and_channel(client, "boom@example.com")
    _seed_rule(workspace_id)

    import app.leads as leads_module

    monkeypatch.setattr(leads_module, "score_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    db = SessionLocal()
    try:
        assert detect(db, workspace_id=workspace_id, channel_id=channel_id, text="مساعدة", message_id=3) is None
    finally:
        db.close()


# --- retention and erasure -------------------------------------------------


def test_leads_past_the_retention_window_are_purged(client: TestClient, leads_on):
    """This table holds other people's words. Keeping them forever by
    default is what turns a lead pipeline into an archive nobody agreed
    to."""
    from datetime import timedelta

    from app.timeutil import utcnow

    workspace_id, channel_id = _workspace_and_channel(client, "old@example.com")
    _seed_rule(workspace_id)

    db = SessionLocal()
    try:
        detect(db, workspace_id=workspace_id, channel_id=channel_id, text="مساعدة", message_id=1, sender_id="9")
        db.commit()
        lead = db.query(Lead).filter(Lead.workspace_id == workspace_id).one()
        lead.created_at = utcnow() - timedelta(days=get_settings().leads_retention_days + 1)
        db.commit()

        assert purge_expired(db, workspace_id) == 1
        assert db.query(Lead).filter(Lead.workspace_id == workspace_id).count() == 0
        # The person survives with their counter: "has asked four times"
        # outlives the texts themselves.
        assert db.query(Beneficiary).filter(Beneficiary.workspace_id == workspace_id).count() == 1
    finally:
        db.close()


def test_forgetting_a_person_takes_their_messages_with_them(client: TestClient, leads_on):
    """A lead's text is that person's own words. A "forget" that left them
    behind would be a deletion in name only."""
    workspace_id, channel_id = _workspace_and_channel(client, "forget@example.com")
    _seed_rule(workspace_id)

    db = SessionLocal()
    try:
        detect(db, workspace_id=workspace_id, channel_id=channel_id, text="مساعدة", message_id=1, sender_id="55")
        db.commit()
        person_id = db.query(Beneficiary).filter(Beneficiary.workspace_id == workspace_id).one().id
    finally:
        db.close()

    assert client.delete(f"/leads/beneficiaries/{person_id}").status_code == 204

    db = SessionLocal()
    try:
        assert db.query(Beneficiary).filter(Beneficiary.workspace_id == workspace_id).count() == 0
        assert db.query(Lead).filter(Lead.workspace_id == workspace_id).count() == 0
    finally:
        db.close()


# --- the API ---------------------------------------------------------------


def test_the_status_endpoint_says_the_feature_is_off_rather_than_404ing(client: TestClient):
    """An empty list looks identical to "on and found nothing", which is
    how a switched-off feature gets reported as a broken one."""
    register_workspace(client, email="status@example.com", workspace_name="S")

    body = client.get("/leads/status").json()

    assert body["enabled"] is False


def test_the_keyword_lab_runs_the_live_rule_set(client: TestClient):
    """A rule set nobody can try is a rule set nobody tunes."""
    register_workspace(client, email="lab@example.com", workspace_name="Lab")
    client.post("/leads/keywords", json={"phrase": "مشروع تخرج", "weight": 5})

    body = client.post("/leads/test", json={"text": "خلصت مشروع التخرج"}).json()

    assert body["score"] == 5
    assert body["would_record"] is True


def test_leads_are_ordered_by_seriousness_not_only_by_date(client: TestClient, leads_on):
    """Ordering by date alone buries a strong request under a page of weak
    ones, which is what the scoring exists to prevent."""
    workspace_id, channel_id = _workspace_and_channel(client, "order@example.com")
    _seed_rule(workspace_id, "مساعدة", 1)
    _seed_rule(workspace_id, "مشروع تخرج", 9)

    db = SessionLocal()
    try:
        detect(
            db, workspace_id=workspace_id, channel_id=channel_id, text="مساعدة بسيطة", message_id=1, sender_id="1"
        )
        db.commit()
        detect(
            db, workspace_id=workspace_id, channel_id=channel_id, text="مشروع التخرج", message_id=2, sender_id="2"
        )
        db.commit()
    finally:
        db.close()

    items = client.get("/leads").json()

    assert items[0]["score"] == 9, "the weakest lead was listed first"


# --- roles -----------------------------------------------------------------


def test_the_technical_role_cannot_reach_beneficiary_data():
    """The separation that makes the role worth having: whoever maintains
    the userbots does not need the names, handles and messages of the
    people those userbots observed."""
    assert roles.LEADS_READ not in roles.permissions_for(roles.OPERATOR)
    assert roles.LEADS_WRITE not in roles.permissions_for(roles.OPERATOR)
    assert roles.COLLECTION_MANAGE in roles.permissions_for(roles.OPERATOR)


def test_the_service_role_cannot_touch_collection_accounts():
    """A person answering requests has no reason to hold the credentials
    that produce them."""
    assert roles.COLLECTION_MANAGE not in roles.permissions_for(roles.AGENT)
    assert roles.LEADS_WRITE in roles.permissions_for(roles.AGENT)


def test_an_unknown_role_gets_the_narrowest_set_not_the_widest():
    """A typo in a role name must not be a privilege escalation."""
    assert roles.permissions_for("typo") == roles.permissions_for(roles.OPERATOR)
    assert roles.WORKSPACE_MANAGE not in roles.permissions_for(None)


def test_an_ordinary_member_can_no_longer_delete_the_workspace():
    """The gap this closes: `role` existed since the first migration and
    was checked nowhere, so every member held every power including this
    one."""
    assert roles.WORKSPACE_MANAGE not in roles.permissions_for(roles.MEMBER)
    assert roles.WORKSPACE_MANAGE in roles.permissions_for(roles.OWNER)


def test_an_owner_keeps_everything_it_had():
    """An upgrade that locks the workspace creator out is not an
    improvement."""
    assert roles.permissions_for(roles.OWNER) == roles.ALL_PERMISSIONS


def test_a_role_without_the_permission_is_refused_by_the_endpoint(client: TestClient):
    register_workspace(client, email="demote@example.com", workspace_name="D")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "demote@example.com").one()
        user.role = roles.OPERATOR
        db.commit()
    finally:
        db.close()

    response = client.get("/leads")

    assert response.status_code == 403
    assert "leads.read" in response.json()["detail"]


def test_the_last_administrator_cannot_be_demoted(client: TestClient):
    """Otherwise the only way back is database access."""
    register_workspace(client, email="solo@example.com", workspace_name="Solo")
    db = SessionLocal()
    try:
        user_id = db.query(User).filter(User.email == "solo@example.com").one().id
    finally:
        db.close()

    response = client.patch(f"/team/{user_id}", json={"role": "agent"})

    assert response.status_code == 409
