"""Read bookmark files other tools export: browsers, Pocket, Instapaper.

These are the credential-free import paths (ideas 168 and 169). Like the
Telegram archive importer, they need no API key, no session and no login —
a file you already have is enough.

**Every file here is untrusted input.** It was produced by another program,
possibly years ago, possibly half-written when a disk filled up. So the
parsers share three rules:

- **Never raise on malformed input.** A broken row is skipped and counted,
  not fatal. Losing one bookmark out of nine hundred beats importing none.
- **Only ``http`` and ``https`` survive.** Browser bookmark bars are full
  of ``javascript:`` bookmarklets and ``place:`` / ``chrome://`` internal
  entries; those are not links to anything and must never reach the
  database. ``/links/{id}/open`` re-checks the scheme at redirect time
  precisely because a non-http row means something wrote it by another
  path — this is that other path, and it stops here.
- **No network, no file references.** Nothing in a bookmark file is
  fetched, and ``ICON``/``ICON_URI`` attributes are ignored rather than
  followed.

Parsing uses only the standard library (``html.parser``, ``csv``): the web
service runs on a 512 MB tier, and a bookmark parser is not worth a
dependency.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http://", "https://")


@dataclass
class Bookmark:
    url: str
    title: str | None = None
    added_at: datetime | None = None
    tags: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """The URL plus its context, for the classifier to read.

        Title and tags are the only description these formats carry, and
        the classifier is markedly better with them than with a bare URL —
        "https://example.com/x" says nothing that "Django ORM performance"
        alongside it does not.
        """
        parts = [self.url]
        if self.title:
            parts.append(self.title)
        if self.tags:
            parts.append(" ".join(self.tags))
        return " — ".join(parts)


@dataclass
class ParseResult:
    bookmarks: list[Bookmark] = field(default_factory=list)
    skipped_unsupported_scheme: int = 0
    skipped_malformed: int = 0

    @property
    def total_skipped(self) -> int:
        return self.skipped_unsupported_scheme + self.skipped_malformed


def _timestamp(value: str | None) -> datetime | None:
    """A unix timestamp as used by Netscape ADD_DATE and Pocket time_added.

    Returns None for anything unparseable — including the empty string,
    plain dates, and values so large they are milliseconds rather than
    seconds. An unknown date is recorded as unknown; guessing one would
    put a fabricated timestamp on a real row.
    """
    if not value:
        return None
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    # Reject implausible values rather than letting them raise: 0 is the
    # epoch (meaning "unset" in practice), and anything past year 9999
    # overflows datetime.
    if seconds <= 0 or seconds > 253_402_300_799:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag.strip() for tag in value.replace("|", ",").split(",") if tag.strip()]


class _NetscapeParser(HTMLParser):
    """The <A> elements of a Netscape bookmark file.

    Every browser exports this format, and all of them produce technically
    invalid HTML for it — unclosed <DT>, unquoted attributes, stray text.
    ``html.parser`` in non-strict mode tolerates that, which is exactly why
    it is used instead of anything that expects well-formed markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = ParseResult()
        self._pending: Bookmark | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {name.lower(): (value or "") for name, value in attrs}
        url = values.get("href", "").strip()
        if not url:
            self.result.skipped_malformed += 1
            return
        if not url.lower().startswith(ALLOWED_SCHEMES):
            # Bookmarklets and browser-internal entries. Counted, not
            # silently dropped: a person whose import is short by forty
            # deserves to know why.
            self.result.skipped_unsupported_scheme += 1
            return
        self._pending = Bookmark(
            url=url,
            added_at=_timestamp(values.get("add_date")),
            tags=_split_tags(values.get("tags")),
        )

    def handle_data(self, data: str) -> None:
        if self._pending is not None and data.strip() and self._pending.title is None:
            self._pending.title = data.strip()[:500]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._pending is not None:
            self.result.bookmarks.append(self._pending)
            self._pending = None

    def close(self) -> None:
        super().close()
        # Flush an entry whose closing tag never arrived, so a file cut
        # short mid-element does not lose the bookmark it was midway
        # through. Best-effort by nature: whether the interpreter reports
        # a start tag for unterminated markup at EOF differs even between
        # CPython patch releases (3.11.15 does, 3.11.16 does not). What is
        # guaranteed either way is that every *completed* entry before the
        # break survives, and that nothing here raises.
        if self._pending is not None:
            self.result.bookmarks.append(self._pending)
            self._pending = None


def parse_netscape_html(content: str) -> ParseResult:
    """Bookmarks exported by Chrome, Firefox, Safari or Edge (idea 168)."""
    parser = _NetscapeParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - a parser crash must not lose the rows already read
        logger.warning("bookmark HTML parsing stopped early: %s", exc)
    return parser.result


# Column names differ between exporters, so the file is inspected rather
# than assumed. Pocket writes lowercase headers; Instapaper capitalises.
_URL_COLUMNS = ("url", "URL", "Url")
_TITLE_COLUMNS = ("title", "Title", "name")
_TIME_COLUMNS = ("time_added", "Timestamp", "timestamp", "time")
_TAG_COLUMNS = ("tags", "Tags", "folder", "Folder")


def _first_present(row: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = row.get(name)
        if value:
            return value
    return None


def parse_bookmark_csv(content: str) -> ParseResult:
    """Pocket or Instapaper CSV (idea 169).

    One function for both because the only real difference is the header
    spelling, and detecting that is cheaper — and less likely to rot — than
    two near-identical parsers plus a format argument the caller has to get
    right.
    """
    result = ParseResult()
    try:
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    except (csv.Error, UnicodeDecodeError) as exc:
        logger.warning("bookmark CSV could not be read: %s", exc)
        return result

    for row in rows:
        if not isinstance(row, dict):
            result.skipped_malformed += 1
            continue
        url = (_first_present(row, _URL_COLUMNS) or "").strip()
        if not url:
            result.skipped_malformed += 1
            continue
        if not url.lower().startswith(ALLOWED_SCHEMES):
            result.skipped_unsupported_scheme += 1
            continue
        title = _first_present(row, _TITLE_COLUMNS)
        result.bookmarks.append(
            Bookmark(
                url=url,
                title=title.strip()[:500] if title else None,
                added_at=_timestamp(_first_present(row, _TIME_COLUMNS)),
                tags=_split_tags(_first_present(row, _TAG_COLUMNS)),
            )
        )
    return result


def parse(content: str, *, filename: str = "") -> ParseResult:
    """Parse by shape, falling back to the filename only to break a tie.

    Content beats extension deliberately: a Pocket export saved as .txt is
    still a CSV, and an HTML file named .csv is still HTML.
    """
    head = content.lstrip()[:400].lower()
    if "<!doctype netscape-bookmark-file" in head or "<a href" in content[:4000].lower():
        return parse_netscape_html(content)
    if filename.lower().endswith((".html", ".htm")):
        return parse_netscape_html(content)
    return parse_bookmark_csv(content)
