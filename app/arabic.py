"""Folding the spelling differences in Arabic that are not meaning differences.

Two consumers need exactly the same folding and must never drift apart:
the keyword engine that decides whether a message is a request worth
recording (``app.leads``), and the classifier that weighs the words of a
message as evidence for a category (``app.classifier.evidence``). A rule
list matched with one folding and a category matched with another would be
two behaviours nobody could explain from one message.

It lives here rather than in either consumer because it belongs to neither:
this is text mechanics, and putting it inside ``leads`` made the classifier
import the lead engine to read a message — a dependency with no meaning.
"""

from __future__ import annotations

import re
import unicodedata

# Matching "مشروع" must find "مشروعي" and "المشروع", and must not miss a
# word because it was written with a different alef or carries harakat.
# Without this a rule set has to enumerate spellings, which is how a
# keyword list becomes unmaintainable and quietly stops matching.
#
# **Written as escapes, never as literal characters, and this is not
# stylistic.** A character class of Arabic marks is a sequence of ranges,
# and every editor and terminal that applies the bidirectional algorithm
# displays that sequence *reordered* — so copying the visually correct
# class produces a class whose endpoints have swapped. Measured, exactly
# that happened while this module was being written: U+0610-U+061A and
# U+064B-U+065F came out as U+0610-U+064B and U+061A-U+0670, and the
# second of those swallows every Arabic letter. The failure was silent,
# not loud: normalise() returned the empty string for all Arabic input,
# every keyword pattern therefore compiled to the empty pattern, and the
# empty pattern matches everything — so the classifier briefly assigned
# every category to every link. One invisible character swap.
_HARAKAT = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_ALEF = re.compile("[\u0622\u0623\u0625\u0671]")  # aa, a-hamza, i-hamza, wasla
_ALEF_MAQSURA, _YEH = "\u0649", "\u064a"
_TEH_MARBUTA, _HEH = "\u0629", "\u0647"
_DEFINITE_ARTICLE = "\u0627\u0644"  # alef + lam


def normalise(text: str) -> str:
    """Fold the spelling differences that are not meaning differences."""
    folded = unicodedata.normalize("NFKC", text or "").lower()
    folded = _HARAKAT.sub("", folded)
    folded = _ALEF.sub("ا", folded)  # أ إ آ ٱ  ->  ا
    folded = folded.replace(_ALEF_MAQSURA, _YEH)
    folded = folded.replace(_TEH_MARBUTA, _HEH)
    folded = re.sub(r"\s+", " ", folded).strip()

    # Strip the definite article from each word. Without this the rule
    # "مشروع تخرج" does not match "مشروع التخرج" — an article *between*
    # the two words of a phrase, which is how people actually write it, and
    # no amount of substring matching gets past it.
    #
    # Applied to both the rule and the message, so the two are folded the
    # same way and the comparison stays symmetric. Skipped when the
    # remainder would be shorter than three characters, which is what keeps
    # "الآن" from becoming "ان".
    return " ".join(
        word[2:] if word.startswith(_DEFINITE_ARTICLE) and len(word) - 2 >= 3 else word
        for word in folded.split(" ")
    )
