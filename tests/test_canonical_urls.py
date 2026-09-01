"""URL canonicalisation (§43.3), and the one thing it must never touch.

Every test here is about the dedupe key. The invariant the whole module
rests on — ``Link.url`` is stored exactly as the message wrote it — is
pinned in ``test_the_stored_url_is_never_rewritten``: without it, a wrong
canonicalisation rule would corrupt the link a person clicks rather than
merely merging two rows.
"""

from app.classifier import hash_url
from app.classifier.canonical import canonical_url


def _same(left: str, right: str) -> bool:
    return hash_url(left) == hash_url(right)


def test_scheme_and_host_fold_but_the_path_does_not():
    assert canonical_url("HTTPS://Example.COM/Path") == "https://example.com/Path"
    # Case-sensitive servers serve different files at these two paths, and
    # the previous key (lower-casing the whole URL) merged them.
    assert not _same("https://example.com/File.pdf", "https://example.com/file.pdf")


def test_www_is_dropped():
    assert _same("https://www.example.com/a", "https://example.com/a")


def test_default_ports_are_dropped_and_others_are_kept():
    assert _same("http://example.com:80/a", "http://example.com/a")
    assert _same("https://example.com:443/a", "https://example.com/a")
    assert not _same("https://example.com:8443/a", "https://example.com/a")


def test_tracking_parameters_are_dropped_and_real_ones_are_kept():
    assert _same("https://example.com/a?utm_source=telegram", "https://example.com/a")
    assert _same("https://example.com/a?fbclid=xyz", "https://example.com/a")
    assert _same("https://example.com/a?id=7&utm_campaign=x", "https://example.com/a?id=7")
    # ``id`` decides which page this is; dropping it would merge two pages.
    assert not _same("https://example.com/a?id=7", "https://example.com/a?id=8")


def test_ambiguous_parameters_are_deliberately_kept():
    """``ref`` and ``si`` are usually tracking and sometimes not.

    A parameter wrongly dropped merges two different pages into one row and
    nothing afterwards can detect it; a parameter wrongly kept costs one
    extra row. The asymmetry decides the list.
    """
    assert not _same("https://example.com/a?ref=x", "https://example.com/a")
    assert not _same("https://example.com/a?si=x", "https://example.com/a")


def test_a_trailing_slash_on_a_non_root_path_is_dropped():
    assert _same("https://example.com/a/", "https://example.com/a")
    assert _same("https://t.me/channel/", "https://t.me/channel")


def test_the_fragment_is_dropped_unless_it_addresses_content():
    assert _same("https://example.com/a#section", "https://example.com/a")
    # Hashbang and hash-router URLs put the address *in* the fragment.
    assert not _same("https://example.com/a#!/video/7", "https://example.com/a")
    assert not _same("https://example.com/a#/page/2", "https://example.com/a")


def test_credentials_are_preserved():
    """Two URLs authenticating as different users are not the same URL."""
    assert not _same("https://alice@example.com/a", "https://bob@example.com/a")


def test_a_malformed_url_is_returned_rather_than_raising():
    """Ingestion runs over whatever a channel posted; this cannot throw."""
    for junk in ("", "   ", "not a url", "http://", "https://[", "ftp://x/y"):
        canonical_url(junk)
        hash_url(junk)


def test_the_stored_url_is_never_rewritten(client):
    """The containment that makes a wrong rule survivable.

    Canonicalisation feeds the hash and nothing else. Sabotage check: make
    ``store_link`` write ``canonical_url(url)`` into ``Link.url`` and this
    test fails, because the stored link stops being what the message said.
    """
    from tests.conftest import register_workspace

    register_workspace(client, email="canon@example.com", workspace_name="Canon")
    messy = "https://WWW.Example.com/Path/?utm_source=telegram#top"
    client.post("/links", json={"text": messy})

    stored = client.get("/links", params={"q": "Path"}).json()["items"]
    assert [item["url"] for item in stored] == [messy]


def test_two_spellings_of_one_link_become_one_row(client):
    from tests.conftest import register_workspace

    register_workspace(client, email="canon2@example.com", workspace_name="Canon2")
    client.post("/links", json={"text": "https://example.com/article"})
    client.post("/links", json={"text": "https://www.example.com/article/?utm_source=tg"})

    items = client.get("/links", params={"q": "article"}).json()["items"]
    assert len(items) == 1, "the same link written two ways is one link"
