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
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"category": "movies_series", "confidence": 0.8}'}}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())
    fallback = ClassificationResult("other", 0.0, "unmatched")
    result = llm.try_improve("https://x.example/y", "text", fallback)
    assert result.category == "movies_series"
    assert result.matched_rule == "llm:groq"
