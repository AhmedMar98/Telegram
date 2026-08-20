"""Password policy, per the NIST SP 800-63B review in app/passwords.py.

Both halves matter: the blocklist must reject what an attacker guesses
first, and — just as importantly — it must *not* reintroduce the
composition rules the guideline advises against.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.passwords import MIN_PASSWORD_LENGTH, is_common_password, rejection_reason

# --- the policy itself -----------------------------------------------------


@pytest.mark.parametrize("password", ["password", "PASSWORD", "  Password  ", "123456789", "qwerty123"])
def test_common_passwords_are_recognised_regardless_of_case_or_padding(password: str):
    assert is_common_password(password) is True


@pytest.mark.parametrize("password", ["correct horse battery staple", "الحصان الصحيح", "j8Kd0-slwQ2x"])
def test_a_reasonable_password_is_accepted(password: str):
    assert is_common_password(password) is False
    assert rejection_reason(password) is None


def test_the_project_own_vocabulary_is_blocked():
    """The service's own name is the first thing tried against it."""
    assert is_common_password("telegram") is True
    assert is_common_password("linkintel") is True


def test_too_short_is_rejected_with_a_length_message():
    reason = rejection_reason("a" * (MIN_PASSWORD_LENGTH - 1))
    assert reason is not None
    assert "at least" in reason


def test_length_is_checked_before_commonness():
    """'qwerty' is both short and common; the actionable message is length."""
    assert "at least" in (rejection_reason("qwerty") or "")


def test_no_composition_rules_are_imposed():
    """NIST advises against forcing mixed case, digits or symbols."""
    assert rejection_reason("aaaaaaaaaaaaaaaa") is None  # lowercase only
    assert rejection_reason("################") is None  # symbols only
    assert rejection_reason("كلمة سر طويلة بالعربية") is None  # non-ASCII


def test_long_passwords_are_accepted():
    """The guideline asks verifiers to accept at least 64 characters."""
    assert rejection_reason("x" * 64) is None
    assert rejection_reason("x" * 199) is None


# --- enforcement at the API boundary --------------------------------------


def test_registration_refuses_a_breached_password(client: TestClient):
    response = client.post(
        "/auth/register",
        json={"email": "weak@example.com", "password": "password123", "workspace_name": "Weak Co"},
    )

    assert response.status_code == 422
    assert "breach" in response.json()["detail"]


def test_registration_accepts_a_strong_password(client: TestClient):
    response = client.post(
        "/auth/register",
        json={"email": "strong@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "Strong Co"},
    )

    assert response.status_code == 201


def test_changing_to_a_breached_password_is_refused(client: TestClient):
    client.post(
        "/auth/register",
        json={"email": "chg@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "Chg Co"},
    )

    response = client.post(
        "/auth/change-password",
        json={"current_password": "j8Kd0-slwQ2x", "new_password": "letmein123"},
    )

    assert response.status_code == 422


def test_a_wrong_current_password_is_still_checked_first(client: TestClient):
    """Password strength must not become an oracle for the current password."""
    client.post(
        "/auth/register",
        json={"email": "order@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "Order Co"},
    )

    response = client.post(
        "/auth/change-password",
        json={"current_password": "not-the-password", "new_password": "letmein123"},
    )

    assert response.status_code == 401  # not 422 — authentication comes first
