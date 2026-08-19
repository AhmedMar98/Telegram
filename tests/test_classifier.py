from app.classifier.rules import classify, extract_urls


def test_extension_match_is_high_confidence():
    result = classify("https://example.com/app-release.apk")
    assert result.category == "software_apps"
    assert result.confidence >= 0.9


def test_domain_match_movies():
    result = classify("https://www.youtube.com/watch?v=abc123")
    assert result.category == "movies_series"


def test_domain_match_strips_www():
    result = classify("https://youtube.com/watch?v=abc123")
    assert result.category == "movies_series"


def test_keyword_fallback_arabic():
    result = classify("https://random-host.example/x", raw_text="رابط تحميل كورس بايثون كامل")
    assert result.category == "books_courses"
    assert 0 < result.confidence < 0.9


def test_unmatched_falls_back_to_other():
    result = classify("https://totally-unknown-domain.example/page")
    assert result.category == "other"
    assert result.confidence == 0.0


def test_extract_urls_finds_multiple():
    text = "شاهد هنا https://a.example/1 وهنا كمان https://b.example/2.pdf"
    urls = extract_urls(text)
    assert urls == ["https://a.example/1", "https://b.example/2.pdf"]


def test_extract_urls_empty_text():
    assert extract_urls("") == []
    assert extract_urls(None) == []
