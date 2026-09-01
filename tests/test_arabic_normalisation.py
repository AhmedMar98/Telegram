"""The folding both the lead engine and the classifier depend on.

This file exists because of a specific failure. The harakat character class
was written with literal Arabic characters, and the bidirectional algorithm
displays a sequence of ranges *reordered* — so the visually correct class
was, in memory, ``U+0610-U+064B``: a range that contains every Arabic
letter. ``normalise()`` therefore returned the empty string for all Arabic
input, every keyword compiled to the empty pattern, the empty pattern
matched everything, and the classifier assigned every category to every
link.

Not one existing test noticed, because every test that used Arabic asserted
a *category*, and "matches everything" satisfies most of those. What was
missing was a test of the folding itself — one that asserts a word survives
it. That is the first test below.
"""

from app.arabic import normalise
from app.classifier import classify_link


def test_a_plain_word_survives_normalisation():
    """The regression that started this file: folding must not delete."""
    assert normalise("مشروع") == "مشروع"
    assert normalise("فيلم") == "فيلم"
    assert normalise("كتاب جديد") == "كتاب جديد"
    assert normalise("hello world") == "hello world"


def test_harakat_are_stripped_and_the_letters_are_not():
    assert normalise("مُشْروع") == "مشروع"
    assert normalise("كِتَاب") == "كتاب"


def test_alef_spellings_fold_together():
    assert normalise("أحمد") == normalise("احمد") == normalise("إحمد")


def test_ta_marbuta_and_alef_maqsura_fold():
    assert normalise("دورة") == "دوره"
    assert normalise("مصطفى") == "مصطفي"


def test_the_definite_article_is_stripped_per_word():
    """Needed for phrases: "مشروع التخرج" must match the rule "مشروع تخرج"."""
    assert normalise("مشروع التخرج") == "مشروع تخرج"
    # But not when it would leave a stub: "الآن" must not become "ان".
    assert normalise("الآن") == "الان"


def test_normalisation_is_idempotent():
    for text in ("مشروع التخرج", "مُشْروع", "أحمد", "hello", ""):
        assert normalise(normalise(text)) == normalise(text)


def test_the_classifier_does_not_match_every_category_at_once():
    """The visible symptom of the folding collapse, pinned directly.

    With an over-wide harakat range this returned evidence for *every*
    category on any input, so no assertion about one category could catch
    it — only counting the categories can.
    """
    evidence = classify_link("https://example.com/x.apk", "برنامج تفعيل").evidence
    categories = {item.category for item in evidence}
    assert categories == {"software_apps"}, f"one message, one topic — got {categories}"

    empty = classify_link("https://example.com/nothing-here", "").evidence
    assert empty == (), "a message with no signal must produce no evidence"
