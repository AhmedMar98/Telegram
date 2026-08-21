import httpx

from app.classifier import llm
from app.classifier.rules import ClassificationResult


def test_no_api_key_returns_fallback_untouched(monkeypatch):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", None)
    fallback = ClassificationResult("other", 0.0, "unmatched")
    result = llm.try_improve("https://x.example/y", "some text", fallback)
    assert result is fallback


def test_network_error_never_raises(monkeypatch):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")

    def _boom(*args, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "post", _boom)
    fallback = ClassificationResult("other", 0.0, "unmatched")
    result = llm.try_improve("https://x.example/y", "text", fallback)
    assert result is fallback


def test_malformed_json_response_falls_back(monkeypatch):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")

    class FakeResponse:
        # Real httpx responses always carry headers, and the classifier now
        # reads rate-limit information off them (idea 160). A double without
        # them is a double that does not model the thing it stands in for.
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())
    fallback = ClassificationResult("other", 0.0, "unmatched")
    result = llm.try_improve("https://x.example/y", "text", fallback)
    assert result is fallback


def test_valid_response_improves_result(monkeypatch):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")

    class FakeResponse:
        # Real httpx responses always carry headers, and the classifier now
        # reads rate-limit information off them (idea 160). A double without
        # them is a double that does not model the thing it stands in for.
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"category": "movies_series", "confidence": 0.8}'}}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())
    fallback = ClassificationResult("other", 0.0, "unmatched")
    result = llm.try_improve("https://x.example/y", "text", fallback)
    assert result.category == "movies_series"
    assert result.matched_rule == "llm:groq"


# --- the optional tier must never be able to block a request ---------------
#
# The web process calls this synchronously while a user waits. An unbounded
# HTTP call to a third party would hold a worker until the OS gives up,
# which on a single free-tier instance is an outage, not a slow response.


MAX_ACCEPTABLE_TIMEOUT_SECONDS = 15.0


def test_the_groq_request_is_given_a_bounded_timeout(monkeypatch):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")
    captured: dict = {}

    class FakeResponse:
        # Real httpx responses always carry headers, and the classifier now
        # reads rate-limit information off them (idea 160). A double without
        # them is a double that does not model the thing it stands in for.
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"category": "other", "confidence": 0.1}'}}]}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", _capture)
    llm.try_improve("https://x.example/y", "text", ClassificationResult("other", 0.0, "unmatched"))

    # httpx defaults to a 5s timeout, but only if the argument is omitted
    # *and* no client-level default overrides it; relying on that is exactly
    # the kind of implicit behaviour that changes under you.
    assert "timeout" in captured, "no explicit timeout passed to httpx.post"
    assert isinstance(captured["timeout"], int | float)
    assert 0 < captured["timeout"] <= MAX_ACCEPTABLE_TIMEOUT_SECONDS


def test_a_timeout_falls_back_to_the_rules_result(monkeypatch):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")

    def _timeout(*args, **kwargs):
        raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(httpx, "post", _timeout)
    fallback = ClassificationResult("books_courses", 0.4, "extension:pdf")
    assert llm.try_improve("https://x.example/y", "text", fallback) is fallback


def test_a_confident_rules_result_never_reaches_the_network(monkeypatch):
    """The LLM tier is a bonus for uncertain cases, not a step in the path.
    If a confident result still called out, every ingest would depend on a
    third party being up."""
    from app import classifier

    settings = llm.get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "fake-key")

    def _fail(*args, **kwargs):
        raise AssertionError("the LLM tier was called for a confident result")

    monkeypatch.setattr(httpx, "post", _fail)
    result = classifier.classify_link("https://example.com/book.pdf", "كتاب")
    assert result.confidence >= 0.6
    assert result.matched_rule.startswith("extension")
