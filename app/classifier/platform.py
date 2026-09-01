"""Which platform a link points at — a second, independent axis.

``rules.py`` answers "what *kind of thing* is behind this URL?" (a film, a
course, an app). This module answers a different question that the first
one cannot: "which *service* is this link for?" — a Telegram invite, a
WhatsApp group, a YouTube video, an ordinary web page.

The two are orthogonal and neither substitutes for the other. A ``t.me``
link can be a film channel or a books channel; a ``books_courses`` link
can live on Telegram, on Google Drive, or on a university's own site.
Collapsing them into one column would force a choice between "how many
Telegram links do I have" and "how many course links do I have", and both
questions are real.

Deliberately a lookup over the host, not a keyword search over the whole
URL. ``example.com/?ref=whatsapp`` is not a WhatsApp link, and a substring
match would say it was. The host is the only part of a URL that names the
service with authority.
"""

from __future__ import annotations

from urllib.parse import urlparse

# The platform names this module can return.
#
# "web" rather than "other" for the catch-all: an unmatched link is not a
# failure to classify, it is an ordinary web address, and calling it
# "other" invites the reader to think something went wrong.
PLATFORMS = (
    "telegram",
    "whatsapp",
    "youtube",
    "instagram",
    "twitter",
    "facebook",
    "tiktok",
    "snapchat",
    "discord",
    "drive",
    "web",
)

DEFAULT_PLATFORM = "web"

# Host suffixes, longest-match-wins by construction: the lookup walks the
# host's own dot-separated suffixes from the most specific, so an entry for
# "drive.google.com" is found before one for "google.com" without the
# table needing to be ordered.
#
# Values are the platform each host belongs to. Short-link hosts are
# included with their parent service because a "youtu.be" link is a
# YouTube link to everyone except a parser.
_HOSTS: dict[str, str] = {
    # Telegram, including the two invite-link hosts and the legacy one.
    "t.me": "telegram",
    "telegram.me": "telegram",
    "telegram.dog": "telegram",
    "telesco.pe": "telegram",
    "tlgrm.ru": "telegram",
    # WhatsApp.
    "wa.me": "whatsapp",
    "whatsapp.com": "whatsapp",
    "chat.whatsapp.com": "whatsapp",
    "api.whatsapp.com": "whatsapp",
    # Video.
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "m.youtube.com": "youtube",
    "music.youtube.com": "youtube",
    # Social.
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "t.co": "twitter",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "fb.watch": "facebook",
    "m.facebook.com": "facebook",
    "tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "snapchat.com": "snapchat",
    "discord.gg": "discord",
    "discord.com": "discord",
    "discordapp.com": "discord",
    # File hosts that carry most of what a links archive actually stores.
    "drive.google.com": "drive",
    "docs.google.com": "drive",
    "mega.nz": "drive",
    "mediafire.com": "drive",
    "dropbox.com": "drive",
    "1drv.ms": "drive",
}


def _host_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    # Strip credentials and port, both of which are legal in a netloc and
    # neither of which is part of the host name.
    host = host.rpartition("@")[2]
    if host.startswith("["):  # IPv6 literal: never one of ours
        return host
    host = host.partition(":")[0]
    return host[4:] if host.startswith("www.") else host


def link_platform(url: str) -> str:
    """Name the service this URL belongs to, or ``"web"``.

    Never raises and never returns an empty string: this runs on every
    collected link, and a classifier that can fail is a collector that can
    fail.
    """
    host = _host_of(url)
    if not host:
        return DEFAULT_PLATFORM

    # Walk suffixes from most specific to least: "vm.tiktok.com" matches
    # its own entry, "www.beta.tiktok.com" falls through to "tiktok.com",
    # and "nottiktok.com" matches neither — which is the point of testing
    # suffixes at label boundaries rather than doing a substring search.
    labels = host.split(".")
    for index in range(len(labels) - 1):
        candidate = ".".join(labels[index:])
        platform = _HOSTS.get(candidate)
        if platform is not None:
            return platform

    return DEFAULT_PLATFORM
