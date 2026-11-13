"""
IMAP fetcher for Gmail, mail.ru and Yandex.

What it does beyond "download the message":

* fetches only *metadata* first, then bodies for messages under a size cap
  (``BODY.PEEK[]``), so a 40 MB attachment never lands in RAM;
* keeps ``\\Seen``/``\\Flagged`` flags — the star is the strongest priority
  signal available and it used to be thrown away;
* detects bulk mail from ``List-Unsubscribe``/``Precedence`` headers instead of
  guessing from a sender substring;
* parses ``text/calendar`` invites into :class:`CalendarEvent`;
* records attachment names so the digest can say "PDF, счёт";
* groups replies into threads via ``References`` / ``In-Reply-To``;
* strips quoted replies and signatures before the text reaches the LLM.
"""
from __future__ import annotations

import contextlib
import email
import email.policy
import email.utils
import logging
import re
import ssl
from collections.abc import Iterable
from datetime import UTC, datetime

from imapclient import IMAPClient

from config import Settings
from fetcher.models import CalendarEvent, Email, make_thread_key

logger = logging.getLogger(__name__)

#: Messages above this size get a header-only fetch.
MAX_MESSAGE_BYTES = 5 * 1024 * 1024

_RE_QUOTE_MARKERS = (
    re.compile(r"^-{2,}\s*(original message|исходное сообщение|пересылаемое сообщение)\s*-{2,}", re.I | re.M),
    re.compile(r"^_{10,}\s*$", re.M),
    re.compile(r"^(on .{5,200}wrote:)\s*$", re.I | re.M),
    re.compile(r"^.{5,200}\s+(написал\(а\)|пишет|wrote):\s*$", re.I | re.M),
    re.compile(r"^from:\s.+$", re.I | re.M),
)
_RE_SIGNATURE = re.compile(r"^--\s?$", re.M)
_RE_EMPTY_LINES = re.compile(r"\n{3,}")
_RE_TRAILER = re.compile(
    r"^(отправлено с (моего )?iphone|sent from my (iphone|ipad|android)|"
    r"отправлено из (mail|яндекс почты))",
    re.I | re.M,
)
_RE_FOLDING = re.compile(r"\r?\n[ \t]")


# ── decoding helpers ─────────────────────────────────────────────────────────
def _decode_header_value(raw: str | bytes | None) -> str:
    """Safely decode RFC 2047 encoded header values."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parts = email.header.decode_header(raw)
    except Exception:  # noqa: BLE001 - malformed headers are common
        return str(raw).strip()
    out: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(str(part))
    return " ".join(out).strip()


def _decode_payload(payload: bytes, charset: str | None) -> str:
    """Decode a MIME part, falling back across the usual Cyrillic charsets."""
    for candidate in (charset, "utf-8", "cp1251", "koi8-r", "latin-1"):
        if not candidate:
            continue
        try:
            return payload.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    """Convert an HTML body to readable plain text."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard requirement now
        return re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", html))

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta", "noscript"]):
        tag.decompose()
    for br in soup.find_all(["br", "p", "div", "li", "tr"]):
        br.insert_after("\n")
    return soup.get_text(separator="")


def strip_quoted_reply(text: str, *, min_keep: int = 160) -> str:
    """Cut quoted history, signatures and mobile trailers.

    Only cuts when what remains is still meaningful — a short "see below"
    answer must not lose the context it refers to.
    """
    if not text:
        return ""
    original_len = len(text)
    cut = len(text)

    for pattern in _RE_QUOTE_MARKERS:
        match = pattern.search(text)
        if match and match.start() > 0:
            cut = min(cut, match.start())

    # A trailing block of "> " lines is a quote even without a header.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            cut = min(cut, len("\n".join(lines[:index])))
            break

    sig = _RE_SIGNATURE.search(text)
    if sig and sig.start() > 0:
        cut = min(cut, sig.start())

    candidate = text[:cut].rstrip()
    if len(candidate) < min(min_keep, original_len * 0.4):
        candidate = text

    candidate = _RE_TRAILER.sub("", candidate)
    candidate = _RE_EMPTY_LINES.sub("\n\n", candidate)
    return candidate.strip()


# ── calendar invites ─────────────────────────────────────────────────────────
def _parse_ics_datetime(value: str, tzid: str | None = None) -> datetime | None:
    value = value.strip()
    utc = value.endswith("Z")
    value = value.rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if utc:
            return dt.replace(tzinfo=UTC)
        if tzid:
            try:
                from zoneinfo import ZoneInfo

                return dt.replace(tzinfo=ZoneInfo(tzid))
            except Exception:  # noqa: BLE001 - unknown TZID
                pass
        return dt
    return None


def parse_ics(text: str, max_events: int = 5) -> list[CalendarEvent]:
    """Minimal VEVENT parser — no dependency on ``icalendar``."""
    unfolded = _RE_FOLDING.sub("", text or "")
    events: list[CalendarEvent] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.S):
        props: dict[str, str] = {}
        tzids: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key_part, value = line.split(":", 1)
            params = key_part.split(";")
            key = params[0].upper()
            props.setdefault(key, value.strip())
            for param in params[1:]:
                if param.upper().startswith("TZID="):
                    tzids[key] = param.split("=", 1)[1]
        if not props:
            continue
        events.append(
            CalendarEvent(
                summary=props.get("SUMMARY", ""),
                start=_parse_ics_datetime(props.get("DTSTART", ""), tzids.get("DTSTART")),
                end=_parse_ics_datetime(props.get("DTEND", ""), tzids.get("DTEND")),
                location=props.get("LOCATION", ""),
                organizer=props.get("ORGANIZER", "").replace("mailto:", ""),
            )
        )
        if len(events) >= max_events:
            break
    return events


# ── bulk detection ───────────────────────────────────────────────────────────
def detect_bulk(msg: email.message.Message, sender_email: str, patterns: Iterable[str]) -> tuple[bool, str]:
    """Return ``(is_bulk, reason)`` using headers first, sender patterns second."""
    spam_flag = _decode_header_value(msg.get("X-Spam-Flag")).lower()
    if spam_flag in {"yes", "true"} or "[spam]" in _decode_header_value(msg.get("Subject")).lower():
        return True, "spam"
    if msg.get("List-Unsubscribe"):
        return True, "list-unsubscribe"
    if (msg.get("List-Id") or "").strip():
        return True, "list-id"
    precedence = _decode_header_value(msg.get("Precedence")).lower()
    if precedence in {"bulk", "list", "junk"}:
        return True, f"precedence:{precedence}"
    if (msg.get("X-Campaign-Id") or msg.get("X-Mailchimp-Campaign") or "").strip():
        return True, "campaign"
    lowered = (sender_email or "").lower()
    for pattern in patterns:
        if pattern and pattern.lower() in lowered:
            return True, f"sender:{pattern}"
    return False, ""


# ── body extraction ──────────────────────────────────────────────────────────
def extract_body(msg: email.message.Message, max_chars: int, *, strip_quotes: bool = True) -> str:
    """Prefer ``text/plain``; fall back to HTML converted to text."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    parts: list[email.message.Message] = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        if part.get_content_maintype() != "text":
            continue
        # Skip text/plain attachments (e.g. a .txt invoice) in favour of the body.
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition and content_type == "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        decoded = _decode_payload(payload, part.get_content_charset())
        if content_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)

    text = "\n".join(plain_parts).strip()
    if not text and html_parts:
        text = _html_to_text("\n".join(html_parts))

    if strip_quotes:
        text = strip_quoted_reply(text)
    text = _RE_EMPTY_LINES.sub("\n\n", text).strip()
    return text[:max_chars]


def _extract_attachments(msg: email.message.Message, limit: int = 5) -> list[str]:
    names: list[str] = []
    if not msg.is_multipart():
        return names
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        decoded = _decode_header_value(filename)
        if decoded and decoded not in names:
            names.append(decoded)
        if len(names) >= limit:
            break
    return names


def _extract_calendar(msg: email.message.Message) -> list[CalendarEvent]:
    if not msg.is_multipart():
        if msg.get_content_type() == "text/calendar":
            payload = msg.get_payload(decode=True) or b""
            return parse_ics(_decode_payload(payload, msg.get_content_charset()))
        return []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() == "text/calendar":
            payload = part.get_payload(decode=True) or b""
            return parse_ics(_decode_payload(payload, part.get_content_charset()))
        filename = (part.get_filename() or "").lower()
        if filename.endswith(".ics"):
            payload = part.get_payload(decode=True) or b""
            return parse_ics(_decode_payload(payload, part.get_content_charset()))
    return []


def _references(msg: email.message.Message) -> list[str]:
    raw = msg.get("References") or ""
    refs = [item for item in re.split(r"\s+", _decode_header_value(raw)) if item]
    if not refs:
        in_reply_to = _decode_header_value(msg.get("In-Reply-To"))
        refs = [item for item in re.split(r"\s+", in_reply_to) if item]
    return refs[:10]


def _internal_date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


class IMAPFetcher:
    """Connects to a single mailbox and yields :class:`Email` objects."""

    def __init__(
        self,
        *,
        mailbox_name: str,
        host: str,
        port: int,
        email_addr: str,
        password: str,
        max_body_chars: int = 3000,
        imap_folder: str = "INBOX",
        timeout: int = 60,
        skip_patterns: Iterable[str] = (),
        strip_quotes: bool = True,
        max_attachment_names: int = 5,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self.mailbox_name = mailbox_name
        self.host = host
        self.port = port
        self.email_addr = email_addr
        self.password = password
        self.max_body_chars = max_body_chars
        self.imap_folder = imap_folder
        self.timeout = timeout
        self.skip_patterns = list(skip_patterns)
        self.strip_quotes = strip_quotes
        self.max_attachment_names = max_attachment_names
        self.max_message_bytes = max_message_bytes

    # ── connection ───────────────────────────────────────────────────────────
    def _connect(self) -> IMAPClient:
        context = ssl.create_default_context()
        client = IMAPClient(
            self.host,
            port=self.port,
            ssl=True,
            ssl_context=context,
            timeout=self.timeout,
        )
        client.login(self.email_addr, self.password)
        return client

    def _select(self, client: IMAPClient) -> None:
        try:
            client.select_folder(self.imap_folder, readonly=True)
            return
        except Exception as exc:  # noqa: BLE001
            try:
                folders = [name for _flags, _delim, name in client.list_folders()]
            except Exception:  # noqa: BLE001
                folders = []
            hint = ", ".join(str(f) for f in folders[:15]) or "не удалось получить список"
            raise RuntimeError(
                f"[{self.mailbox_name}] Папка {self.imap_folder!r} недоступна. "
                f"Доступные папки: {hint}"
            ) from exc

    def test_connection(self) -> str:
        """Used by ``main.py --check``: verify login and folder without fetching."""
        client = self._connect()
        try:
            self._select(client)
            return f"OK {self.mailbox_name} ({self.email_addr}) → {self.imap_folder}"
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    # ── fetch ────────────────────────────────────────────────────────────────
    def fetch_since(self, since: datetime, *, limit: int = 500) -> list[Email]:
        """Fetch messages received after *since* (UTC-aware)."""
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        since_date = since.date()
        emails: list[Email] = []

        logger.info("[%s] Connecting to %s:%d …", self.mailbox_name, self.host, self.port)
        client = self._connect()
        try:
            self._select(client)
            uids = client.search(["SINCE", since_date, "NOT", "DELETED"])
            logger.info("[%s] %d candidate messages", self.mailbox_name, len(uids))
            if not uids:
                return []

            if len(uids) > limit:
                logger.warning(
                    "[%s] %d candidates, fetching the newest %d (raise the limit if this is wrong)",
                    self.mailbox_name,
                    len(uids),
                    limit,
                )
                uids = uids[-limit:]

            # Pass 1 — cheap metadata only.
            meta = client.fetch(uids, ["FLAGS", "INTERNALDATE", "RFC822.SIZE"])
            candidates: list[tuple[int, datetime, bool, bool, int]] = []
            for uid, data in meta.items():
                internal_date = _internal_date(data.get(b"INTERNALDATE"))
                if internal_date is None or internal_date < since:
                    continue
                flags = data.get(b"FLAGS") or data.get(b"FLAGS", ())
                flag_set = {f.decode() if isinstance(f, bytes) else str(f) for f in (flags or ())}
                candidates.append(
                    (
                        uid,
                        internal_date,
                        "\\Seen" in flag_set,
                        "\\Flagged" in flag_set,
                        int(data.get(b"RFC822.SIZE") or 0),
                    )
                )

            if not candidates:
                logger.info("[%s] nothing newer than %s", self.mailbox_name, since.isoformat())
                return []

            candidates.sort(key=lambda item: item[1])

            # Pass 2 — bodies for the ones we can afford to parse.
            small = [c for c in candidates if c[4] <= self.max_message_bytes]
            large = [c for c in candidates if c[4] > self.max_message_bytes]
            bodies: dict[int, bytes] = {}
            for chunk in _chunks([c[0] for c in small], 50):
                fetched = client.fetch(chunk, ["BODY.PEEK[]"])
                for uid, data in fetched.items():
                    raw = (
                        data.get(b"BODY[]")
                        or data.get(b"BODY.PEEK[]")
                        or data.get(b"RFC822")
                    )
                    if raw:
                        bodies[uid] = raw

            headers: dict[int, bytes] = {}
            for uid in [c[0] for c in large]:
                fetched = client.fetch([uid], ["BODY.PEEK[HEADER]"])
                for key, data in fetched.items():
                    raw = data.get(b"BODY[HEADER]") or data.get(b"BODY.PEEK[HEADER]")
                    if raw:
                        headers[key] = raw

            for uid, internal_date, is_read, is_flagged, size in candidates:
                try:
                    raw = bodies.get(uid)
                    if raw is None:
                        header_bytes = headers.get(uid, b"")
                        msg = email.message_from_bytes(
                            header_bytes, policy=email.policy.compat32
                        )
                        body_text = f"(письмо слишком большое: {size // 1024} КБ, тело не загружено)"
                        attachments: list[str] = []
                        calendar: list[CalendarEvent] = []
                    else:
                        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
                        body_text = extract_body(
                            msg, self.max_body_chars, strip_quotes=self.strip_quotes
                        )
                        attachments = _extract_attachments(msg, self.max_attachment_names)
                        calendar = _extract_calendar(msg)

                    message_id = _decode_header_value(msg.get("Message-ID")) or f"{self.mailbox_name}_{uid}"
                    subject = _decode_header_value(msg.get("Subject")) or "(без темы)"
                    from_raw = _decode_header_value(msg.get("From"))
                    sender_name, sender_email = email.utils.parseaddr(from_raw)
                    sender_email = (sender_email or "").lower()
                    sender_name = sender_name or sender_email or "неизвестный отправитель"

                    is_bulk, bulk_reason = detect_bulk(msg, sender_email, self.skip_patterns)
                    references = _references(msg)
                    thread_key = make_thread_key(
                        subject,
                        references,
                        _decode_header_value(msg.get("In-Reply-To")),
                        message_id,
                    )

                    emails.append(
                        Email(
                            message_id=message_id.strip(),
                            mailbox=self.mailbox_name,
                            subject=subject.strip(),
                            sender=sender_name.strip(),
                            sender_email=sender_email,
                            date=internal_date,
                            body_text=body_text,
                            is_read=is_read,
                            is_flagged=is_flagged,
                            is_bulk=is_bulk,
                            bulk_reason=bulk_reason,
                            attachments=attachments,
                            calendar_events=calendar,
                            references=references,
                            in_reply_to=_decode_header_value(msg.get("In-Reply-To")),
                            thread_key=thread_key,
                            size_bytes=size,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] failed to parse uid=%s: %s", self.mailbox_name, uid, exc)
        finally:
            with contextlib.suppress(Exception):
                client.logout()

        logger.info("[%s] %d emails since %s", self.mailbox_name, len(emails), since.isoformat())
        return emails


def _chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_fetchers(settings: Settings) -> list[IMAPFetcher]:
    """Instantiate one fetcher per enabled mailbox."""
    fetchers: list[IMAPFetcher] = []
    for box in settings.enabled_mailboxes():
        fetchers.append(
            IMAPFetcher(
                mailbox_name=str(box["name"]),
                host=str(box["host"]),
                port=int(box["port"]),  # type: ignore[arg-type]
                email_addr=str(box["email"]),
                password=str(box["password"]),
                max_body_chars=settings.max_body_chars,
                imap_folder=str(box["folder"]),
                timeout=settings.imap_timeout,
                skip_patterns=settings.skip_senders_containing,
                strip_quotes=settings.strip_quoted_replies,
                max_attachment_names=settings.max_attachment_names,
            )
        )
    return fetchers
