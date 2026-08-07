"""Tests for the rules-based classifier tier."""

import pytest

from app.classifier.rules_layer import classify_with_rules, host_hint
from app.models import LinkCategory


@pytest.mark.parametrize(
    "url,expected_cat,expected_conf_min",
    [
        ("https://chat.whatsapp.com/ABC123xyz", LinkCategory.WHATSAPP, 0.90),
        ("https://wa.me/1234567890", LinkCategory.WHATSAPP, 0.90),
        ("https://t.me/joinchat/abcdef", LinkCategory.TELEGRAM, 0.90),
        ("https://t.me/+private_invite", LinkCategory.TELEGRAM, 0.90),
        ("https://t.me/publicchannel", LinkCategory.TELEGRAM, 0.80),
        ("https://example.com/file.pdf", LinkCategory.FILE, 0.90),
        ("https://example.com/file.zip", LinkCategory.FILE, 0.90),
        ("https://example.com/config.json", LinkCategory.SETTING, 0.90),
        ("https://github.com/user/repo", LinkCategory.TOOL, 0.90),
        ("https://twitter.com/user/status/123", LinkCategory.SOCIAL, 0.85),
        ("https://bbc.com/news/world-123", LinkCategory.NEWS, 0.85),
    ],
)
def test_rules_match_expected_categories(url, expected_cat, expected_conf_min):
    result = classify_with_rules(url)
    assert result is not None, f"Expected rule match for {url}"
    assert result.category == expected_cat
    assert result.confidence >= expected_conf_min


def test_rules_return_none_for_unknown_url():
    """Generic URLs with no matching pattern should return None."""
    result = classify_with_rules("https://random-site.example.org/page")
    assert result is None


def test_rules_use_context():
    """Rules should also scan context text for hints."""
    url = "https://example.org/xyz"
    # No rule matches the URL alone
    assert classify_with_rules(url) is None
    # But with whatsapp context...
    result = classify_with_rules(url, context="Join our group: https://chat.whatsapp.com/ABC")
    assert result is not None
    assert result.category == LinkCategory.WHATSAPP


def test_host_hint_gov():
    assert host_hint("https://data.gov/report") == LinkCategory.NEWS


def test_host_hint_returns_none_for_unknown():
    assert host_hint("https://example.org/") is None
    assert host_hint("not-a-url") is None
