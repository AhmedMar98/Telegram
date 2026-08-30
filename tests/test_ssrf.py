"""The vitality checker must not be usable as a probe into private space.

Links come from Telegram channels this deployment does not control, and
the checker fetches every one of them from inside the runner. Before
app/ssrf.py existed, ``client.head(url, follow_redirects=True)`` would
happily fetch ``http://169.254.169.254/`` (cloud metadata) or
``http://127.0.0.1:5432`` — and the resulting status code was stored,
which is enough to enumerate what is listening.

Two halves are tested here because either alone is worthless:

  * the address check itself (a hostname is not an address — a public
    name can resolve to 127.0.0.1), and
  * the redirect handling (a URL that resolves publicly can answer 302
    pointing anywhere, so checking only the first URL protects nothing).
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from app import ssrf
from scripts import check_link_vitality as vitality


def _check(url: str) -> str | None:
    return asyncio.run(ssrf.check_url(url))


# --- literal addresses: no DNS involved -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:5432/",
        "https://[::1]/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/",
        "https://[fd00::1]/",  # unique-local IPv6
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    ],
)
def test_private_and_loopback_literals_are_refused(url):
    assert _check(url) is not None


def test_the_metadata_endpoint_is_named_as_link_local():
    """The reason string ends up in a log an operator reads, so it has to
    say which rule fired, not just that one did."""
    reason = _check("http://169.254.169.254/latest/meta-data/")

    assert reason is not None
    assert "link-local" in reason


@pytest.mark.parametrize("url", ["https://example.com/", "http://93.184.216.34/"])
def test_public_addresses_pass(url, monkeypatch):
    monkeypatch.setattr(
        ssrf.asyncio,
        "get_running_loop",
        lambda: _FakeLoop([("93.184.216.34", 0)]),
    )
    assert _check(url) is None


# --- non-HTTP schemes -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "gopher://127.0.0.1:70/", "dict://localhost:11211/stats", "ftp://example.com/"],
)
def test_non_http_schemes_are_refused(url):
    reason = _check(url)

    assert reason is not None
    assert "http(s)" in reason


# --- DNS: the hostname is not the address -----------------------------------


class _FakeLoop:
    """Stands in for the event loop so getaddrinfo is deterministic."""

    def __init__(self, addresses):
        self._addresses = addresses

    async def getaddrinfo(self, host, port, **kwargs):
        if self._addresses is None:
            raise socket.gaierror("name does not resolve")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", addr) for addr in self._addresses]


def test_a_public_name_resolving_to_loopback_is_refused(monkeypatch):
    """The attack blocking on the literal string would miss entirely."""
    monkeypatch.setattr(ssrf.asyncio, "get_running_loop", lambda: _FakeLoop([("127.0.0.1", 0)]))

    reason = _check("https://totally-innocent.example.com/")

    assert reason is not None
    assert "loopback" in reason


def test_every_resolved_address_is_checked_not_just_the_first(monkeypatch):
    """A host answering with one public and one private address would
    otherwise pass, then be connected to on whichever the OS picked."""
    monkeypatch.setattr(
        ssrf.asyncio,
        "get_running_loop",
        lambda: _FakeLoop([("93.184.216.34", 0), ("10.1.2.3", 0)]),
    )

    reason = _check("https://mixed.example.com/")

    assert reason is not None
    assert "private" in reason


def test_a_name_that_does_not_resolve_is_not_an_ssrf_problem(monkeypatch):
    """An unresolvable name is an ordinary dead link; the caller's existing
    error handling reports it better than this check could."""
    monkeypatch.setattr(ssrf.asyncio, "get_running_loop", lambda: _FakeLoop(None))

    assert _check("https://gone.example.com/") is None


def test_a_url_with_no_host_is_refused():
    assert _check("http:///no-host") is not None


# --- redirects: the half that follow_redirects=True made impossible ---------


def _probe_with(handler, url="https://start.example.com/") -> vitality.ProbeResult:
    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await vitality.check_one(client, url)

    return asyncio.run(run())


def test_a_redirect_into_private_space_is_refused(monkeypatch):
    """The whole reason follow_redirects=True had to go: the first URL is
    a perfectly ordinary public address, and the 302 is the attack.

    The redirect target is a literal address, so it never reaches DNS —
    the fake loop only has to answer for the public starting host.
    """
    monkeypatch.setattr(ssrf.asyncio, "get_running_loop", lambda: _FakeLoop([("93.184.216.34", 0)]))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    result = _probe_with(handler)

    assert result.outcome == "dead"
    assert result.http_status is None
    # The metadata endpoint must never have been requested at all.
    assert not any("169.254.169.254" in url for url in requested)


def test_a_normal_redirect_chain_still_resolves(monkeypatch):
    """The guard must not break http->https or apex->www, which is most
    of the real web."""
    monkeypatch.setattr(ssrf.asyncio, "get_running_loop", lambda: _FakeLoop([("93.184.216.34", 0)]))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(301, headers={"location": "https://www.example.com/final"})
        return httpx.Response(200)

    result = _probe_with(handler, "http://example.com/")

    assert result.outcome == "alive"
    assert result.http_status == 200
    assert len(seen) == 2


def test_an_endless_redirect_loop_is_bounded(monkeypatch):
    monkeypatch.setattr(ssrf.asyncio, "get_running_loop", lambda: _FakeLoop([("93.184.216.34", 0)]))
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": f"https://example.com/{len(hops)}"})

    result = _probe_with(handler, "https://example.com/0")

    assert result.outcome == "unreachable"
    assert len(hops) <= vitality.MAX_REDIRECT_HOPS


def test_head_rejected_falls_back_to_get(monkeypatch):
    """Pre-existing behaviour that the rewrite must not lose."""
    monkeypatch.setattr(ssrf.asyncio, "get_running_loop", lambda: _FakeLoop([("93.184.216.34", 0)]))
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200)

    result = _probe_with(handler, "https://example.com/")

    assert methods == ["HEAD", "GET"]
    assert result.outcome == "alive"
