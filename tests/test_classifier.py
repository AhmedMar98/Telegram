"""The evidence classifier (§43.4), and what it deliberately no longer does.

Two tests here previously asserted the opposite of what this file now
asserts: ``youtube.com`` was expected to classify as ``movies_series``.
That expectation was the bug — the domain answers which *platform* a link
is on, which the ``platform`` column has answered since §39 — so the tests
were rewritten with the rule they were pinning, not kept and worked around.
"""

from app.classifier import CLASSIFIER_VERSION, classify_link, extract_urls
from app.classifier.evidence import W_DOMAIN, W_EXTENSION, collect_evidence, decide


def test_extension_is_the_strongest_single_signal():
    # A filename with no category word in it, so this measures the
    # extension alone: 4/(4+1) under the smoothing formula. High, but not
    # certainty — one signal is one signal however good it is.
    result = classify_link("https://example.com/v3-release.apk")
    assert result.category == "software_apps"
    assert result.confidence == 0.8


def test_words_in_the_url_itself_count_as_evidence():
    """A slug is text. ``/app-release.apk`` says "app" as much as a caption."""
    named = classify_link("https://example.com/app-release.apk")
    bare = classify_link("https://example.com/v3-release.apk")
    assert named.category == bare.category == "software_apps"
    assert named.confidence > bare.confidence


def test_a_known_domain_classifies_without_any_text():
    assert classify_link("https://udemy.com/course/x").category == "books_courses"
    assert classify_link("https://store.steampowered.com/app/123").category == "games"


def test_www_is_stripped_before_the_domain_is_matched():
    assert classify_link("https://www.udemy.com/x").category == "books_courses"


def test_youtube_is_a_platform_not_a_category():
    """The rule this replaced: youtube.com -> movies_series, always, at 0.9.

    A course posted on YouTube was a film, a music video was a film, and a
    gaming stream was a film — with high confidence, from a rule that had
    already stopped anything else from being considered.
    """
    evidence = collect_evidence("https://youtube.com/watch?v=abc")
    assert not [item for item in evidence if item.kind == "domain"]

    course = classify_link("https://youtube.com/watch?v=abc", "شرح كورس بايثون كامل")
    assert course.category == "books_courses"

    film = classify_link("https://youtube.com/watch?v=abc", "فيلم مترجم كامل")
    assert film.category == "movies_series"


def test_agreeing_evidence_raises_confidence_above_either_signal_alone():
    alone = classify_link("https://example.com/x.pdf")
    supported = classify_link("https://example.com/x.pdf", "كتاب رواية جديدة")
    assert supported.category == alone.category == "books_courses"
    assert supported.confidence > alone.confidence


def test_disagreeing_evidence_lowers_confidence():
    """A contested link must not report the same certainty as a clear one."""
    clear = classify_link("https://example.com/x.apk", "برنامج تفعيل")
    contested = classify_link("https://example.com/x.apk", "فيلم مسلسل حلقة")
    assert contested.confidence < clear.confidence


def test_a_latin_keyword_does_not_match_inside_another_word():
    """The old matcher was ``"app" in text``: *happens* matched."""
    result = classify_link("https://example.com/page", "this happens to be a nice page")
    assert result.category == "other"
    assert result.confidence == 0.0


def test_an_arabic_keyword_still_matches_with_a_prefix_attached():
    """Arabic glues و/ب/ال to the front, so substring matching is correct."""
    assert classify_link("https://x.example/a", "شاهد الفيلم هنا").category == "movies_series"
    assert classify_link("https://x.example/a", "وفيلم آخر").category == "movies_series"


def test_harakat_and_alef_spellings_do_not_break_a_match():
    assert classify_link("https://x.example/a", "فِيلْم جديد").category == "movies_series"
    assert classify_link("https://x.example/a", "الافلام").category == "movies_series"


def test_the_channel_title_never_overrides_an_explicit_message_word():
    """Message content outranks source metadata. Found by review, not
    written in first: a channel titled "قناة الأفلام" was overriding a
    message that explicitly said "كتاب" (book), because the title's
    weight (1.5) exceeded a single keyword's (0.75 now, was equal at 1.5).
    Sabotage: set W_CHANNEL >= W_KEYWORD and this flips to movies_series.
    """
    result = classify_link("https://unknown.example/x", "كتاب رائع", channel_title="قناة الأفلام")
    assert result.category == "books_courses"
    assert result.matched_rule == "keyword:كتاب"


def test_the_channel_title_is_evidence_for_a_bare_url():
    bare = classify_link("https://unknown.example/xyz")
    assert bare.category == "other"

    with_channel = classify_link("https://unknown.example/xyz", channel_title="قناة الأفلام العربية")
    assert with_channel.category == "movies_series"
    assert with_channel.matched_rule.startswith("channel:")


def test_a_sibling_category_can_decide_an_otherwise_blank_link():
    blank = classify_link("https://unknown.example/xyz")
    assert blank.category == "other"

    lent = classify_link("https://unknown.example/xyz", siblings=("movies_series",))
    assert lent.category == "movies_series"


def test_a_repeated_keyword_cannot_outweigh_the_file_itself():
    """The cap: six mentions of one word are one voice, not six."""
    result = classify_link("https://example.com/x.apk", "فيلم فيلم فيلم فيلم فيلم فيلم مسلسل حلقة")
    assert result.category == "software_apps"


def test_the_winner_is_the_heaviest_pile_not_the_first_rule():
    """Two path+keyword signals outvote one domain that disagrees."""
    evidence = collect_evidence("https://spotify.com/course/lesson/x", "كورس دورة محاضرة")
    kinds = {item.kind for item in evidence}
    assert {"domain", "path", "keyword"} <= kinds
    assert decide(evidence).category == "books_courses"
    assert W_EXTENSION > W_DOMAIN


def test_unmatched_falls_back_to_other_with_zero_confidence():
    result = classify_link("https://totally-unknown-domain.example/page")
    assert result.category == "other"
    assert result.confidence == 0.0
    assert result.matched_rule == "unmatched"


def test_every_result_carries_the_classifier_version():
    assert classify_link("https://example.com/x.apk").classifier_version == CLASSIFIER_VERSION
    assert CLASSIFIER_VERSION == "rules-v2"


def test_confidence_never_reaches_certainty():
    piled = classify_link(
        "https://udemy.com/course/x.pdf",
        "كتاب كورس دورة",
        channel_title="كتب ودورات",
        siblings=("books_courses",),
    )
    assert piled.category == "books_courses"
    assert piled.confidence <= 0.99


def test_extract_urls_finds_multiple():
    text = "شاهد هنا https://a.example/1 وهنا كمان https://b.example/2.pdf"
    assert extract_urls(text) == ["https://a.example/1", "https://b.example/2.pdf"]


def test_extract_urls_empty_text():
    assert extract_urls("") == []
    assert extract_urls(None) == []
