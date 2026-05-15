"""
IMAP MCP Server — exposes mailbox tools to Claude Desktop via stdio transport.
Supports implicit TLS (port 993) and STARTTLS (port 143).
"""

import email
import email.header
import imaplib
import logging
import os
import re
import ssl
from html.parser import HTMLParser
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

IMAP_HOST: str = os.environ["IMAP_HOST"]
IMAP_PORT: int = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER: str = os.environ["IMAP_USER"]
IMAP_PASSWORD: str = os.environ["IMAP_PASSWORD"]
IMAP_FOLDER: str = os.getenv("IMAP_FOLDER", "INBOX")
IMAP_USE_SSL: bool = os.getenv("IMAP_USE_SSL", "true").lower() == "true"
# Refuse to fetch a message body larger than this (bytes).  Protects against
# attachment-heavy emails exhausting memory.  Default: 20 MB.
IMAP_MAX_BODY_BYTES: int = int(os.getenv("IMAP_MAX_BODY_BYTES", str(20 * 1024 * 1024)))

# Folders whose names match any of these patterns (case-insensitive) are
# blocked as move_mail destinations to prevent accidental mail sending.
IMAP_PROHIBITED_FOLDERS: list[str] = [
    p.strip()
    for p in os.getenv("IMAP_PROHIBITED_FOLDERS", "Sent,Outbox,Queue").split(",")
    if p.strip()
]

mcp = FastMCP("imap-mcp")

# ── Connection management ──────────────────────────────────────────────────────

_conn: Optional[imaplib.IMAP4] = None


def get_conn() -> imaplib.IMAP4:
    """Return the shared IMAP connection, reconnecting transparently on failure."""
    global _conn
    if _conn is not None:
        try:
            _conn.noop()
            return _conn
        except Exception:
            _conn = None

    ssl_ctx = ssl.create_default_context()

    if IMAP_USE_SSL:
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl_ctx)
    else:
        conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        conn.starttls(ssl_context=ssl_ctx)

    conn.login(IMAP_USER, IMAP_PASSWORD)
    _conn = conn
    logger.info("IMAP connection established to %s:%s", IMAP_HOST, IMAP_PORT)
    return conn


def _q(folder: str) -> str:
    """Wrap a folder name in double-quotes for IMAP commands."""
    if folder.startswith('"') and folder.endswith('"'):
        return folder
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ── Parsing helpers ────────────────────────────────────────────────────────────

# Matches:  (\Flags ...) "sep" "Folder Name"
#       or: (\Flags ...) "sep" FolderName
#       or: (\Flags ...) NIL  "Folder Name"
_LIST_LINE = re.compile(
    r"\(([^)]*)\)\s+(?:\"([^\"]*)\"|NIL)\s+(?:\"([^\"]*)\"|(\S+))"
)


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    chunks: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            chunks.append(str(chunk))
    return "".join(chunks)


class _Stripper(HTMLParser):
    """Minimal HTML → plain-text converter using only the stdlib."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def result(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of blank lines and horizontal whitespace
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _strip_html(raw: str) -> str:
    s = _Stripper()
    s.feed(raw)
    return s.result()


def _extract_body(msg: email.message.Message) -> dict:
    """
    Walk MIME parts and return the best available text body plus attachment metadata.

    Preference order: text/plain > text/html (stripped).
    Further development: a richer variant could return both parts and let the
    caller decide, or render HTML with a proper library (e.g. html2text).
    """
    plain: Optional[str] = None
    html_body: Optional[str] = None
    attachments: list[dict] = []

    for part in msg.walk():
        ct = part.get_content_type()
        disposition = part.get("Content-Disposition", "")
        filename = part.get_filename()

        # Treat parts with a filename or explicit attachment disposition as attachments
        if filename or "attachment" in disposition.lower():
            attachments.append(
                {
                    "filename": _decode_header(filename) if filename else None,
                    "content_type": ct,
                    "size_bytes": len(part.get_payload(decode=True) or b""),
                }
            )
            continue

        raw_payload = part.get_payload(decode=True)
        if raw_payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"

        if ct == "text/plain" and plain is None:
            plain = raw_payload.decode(charset, errors="replace")
        elif ct == "text/html" and html_body is None:
            html_body = raw_payload.decode(charset, errors="replace")

    if plain is not None:
        return {"body": plain, "html_stripped": False, "attachments": attachments}
    if html_body is not None:
        return {
            "body": _strip_html(html_body),
            "html_stripped": True,
            "attachments": attachments,
        }
    return {"body": "", "html_stripped": False, "attachments": attachments}


def _is_prohibited(folder: str) -> bool:
    fl = folder.lower()
    return any(pattern.lower() in fl for pattern in IMAP_PROHIBITED_FOLDERS)


def _parse_fetch_info(info_line: str) -> tuple[Optional[str], set[str]]:
    """Extract UID and FLAGS from an IMAP FETCH response info line."""
    uid_m = re.search(r"UID (\d+)", info_line)
    flags_m = re.search(r"FLAGS \(([^)]*)\)", info_line)
    uid = uid_m.group(1) if uid_m else None
    raw_flags = set(flags_m.group(1).split()) if flags_m else set()
    return uid, raw_flags


def _valid_uid(uid: str) -> bool:
    """Return True iff uid is a non-empty string of decimal digits (IMAP UID)."""
    return bool(uid) and uid.isdigit()


# ── MCP Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
def list_folders() -> list[dict]:
    """
    List all IMAP folders/mailboxes available on the server.

    Returns a list of dicts with keys:
      name       – full folder name as used in other tool calls
      separator  – hierarchy separator character (typically "/" or ".")
      flags      – IMAP attribute flags (e.g. \\HasNoChildren, \\Noselect)
    """
    conn = get_conn()
    typ, data = conn.list()
    if typ != "OK":
        return [{"error": "Server rejected LIST command"}]

    folders: list[dict] = []
    for item in data:
        if not isinstance(item, bytes):
            continue
        raw = item.decode("utf-8", errors="replace").strip()
        m = _LIST_LINE.match(raw)
        if not m:
            logger.warning("Could not parse LIST line: %r", raw)
            continue
        flags_str, sep, name_quoted, name_bare = m.groups()
        name = (name_quoted if name_quoted is not None else name_bare or "").strip()
        folders.append(
            {
                "name": name,
                "separator": sep or "/",
                "flags": [f for f in flags_str.split() if f],
            }
        )
    return folders


@mcp.tool()
def list_emails(
    limit: int = 20,
    folder: Optional[str] = None,
    flags: Optional[list[str]] = None,
) -> list[dict]:
    """
    List recent emails from a folder with their metadata.

    Args:
        limit:  Maximum number of emails to return (most recent first, by UID).
                Hard-capped at 200 to avoid oversized responses.
        folder: IMAP folder to read.  Defaults to IMAP_FOLDER from .env.
        flags:  IMAP flags to report for each message.
                Pass e.g. ["\\\\Seen", "\\\\Flagged", "\\\\Answered"].
                Defaults to ["\\\\Seen", "\\\\Flagged"] when omitted.
                All system flags on each message are fetched in one round-trip;
                only the requested subset is included in the response.

    Returns a list of dicts with keys:
      uid, from, subject, date, message_id, flags (dict flag→bool).
    """
    limit = min(limit, 200)
    conn = get_conn()
    target = folder or IMAP_FOLDER

    typ, _ = conn.select(_q(target), readonly=True)
    if typ != "OK":
        return [{"error": f"Cannot open folder: {target}"}]

    typ, data = conn.uid("search", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return []

    uid_list = data[0].split()
    uid_list = uid_list[-limit:][::-1]  # newest-UID-first

    if not uid_list:
        return []

    uid_str = ",".join(u.decode() for u in uid_list)
    fetch_spec = "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
    typ, fetch_data = conn.uid("fetch", uid_str, fetch_spec)
    if typ != "OK":
        return [{"error": "FETCH command failed"}]

    wanted: set[str] = set(flags) if flags else {"\\Seen", "\\Flagged"}
    results: list[dict] = []

    for part in fetch_data:
        if not isinstance(part, tuple):
            continue
        info = part[0].decode("utf-8", errors="replace")
        header_bytes = part[1] if len(part) > 1 else b""

        uid, raw_flags = _parse_fetch_info(info)
        msg = email.message_from_bytes(header_bytes)

        results.append(
            {
                "uid": uid,
                "from": _decode_header(msg.get("From")),
                "subject": _decode_header(msg.get("Subject")),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
                "flags": {f: (f in raw_flags) for f in wanted},
            }
        )

    return results


@mcp.tool()
def get_email(uid: str, folder: Optional[str] = None) -> dict:
    """
    Fetch the full content of a single email by its IMAP UID.

    Uses BODY.PEEK so the message is NOT marked as read by this call.
    HTML-only messages are stripped to plain text (tags removed, entities decoded).

    Args:
        uid:    IMAP UID of the message (obtain from list_emails).
        folder: Folder containing the message.  Defaults to IMAP_FOLDER.

    Returns a dict with keys:
      uid, from, to, cc, subject, date, message_id,
      body (str), html_stripped (bool), attachments (list of metadata dicts).
    """
    if not _valid_uid(uid):
        return {"error": f"Invalid UID: {uid!r}"}

    conn = get_conn()
    target = folder or IMAP_FOLDER

    typ, _ = conn.select(_q(target), readonly=True)
    if typ != "OK":
        return {"error": f"Cannot open folder: {target}"}

    # Check message size before fetching to avoid loading huge attachments into memory.
    typ, size_data = conn.uid("fetch", uid, "(RFC822.SIZE)")
    if typ == "OK" and size_data and isinstance(size_data[0], tuple):
        size_m = re.search(r"RFC822\.SIZE (\d+)", size_data[0][0].decode("utf-8", errors="replace"))
        if size_m:
            msg_size = int(size_m.group(1))
            if msg_size > IMAP_MAX_BODY_BYTES:
                return {
                    "error": (
                        f"Message too large to fetch: {msg_size:,} bytes "
                        f"(limit: {IMAP_MAX_BODY_BYTES:,} bytes). "
                        "Use get_email on a smaller message or increase IMAP_MAX_BODY_BYTES."
                    )
                }

    typ, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or data[0] is None:
        return {"error": f"UID {uid} not found in {target}"}

    first = data[0]
    if not isinstance(first, tuple) or len(first) < 2:
        return {"error": f"Unexpected server response for UID {uid}"}

    msg = email.message_from_bytes(first[1])
    body_info = _extract_body(msg)

    return {
        "uid": uid,
        "from": _decode_header(msg.get("From")),
        "to": _decode_header(msg.get("To")),
        "cc": _decode_header(msg.get("Cc")),
        "subject": _decode_header(msg.get("Subject")),
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        **body_info,
    }


@mcp.tool()
def move_mail(
    uid: str,
    target_folder: str,
    source_folder: Optional[str] = None,
) -> dict:
    """
    Move an email to a different folder.

    Folders whose names contain a prohibited pattern are rejected as destinations
    to prevent accidentally triggering mail delivery.  The default blocked patterns
    are "Sent", "Outbox", and "Queue"; override via IMAP_PROHIBITED_FOLDERS in .env.

    Tries the atomic RFC 6851 MOVE command first; falls back to COPY + \\Deleted
    + EXPUNGE on servers that do not advertise the MOVE capability.

    Args:
        uid:           IMAP UID of the message to move.
        target_folder: Destination folder name (must exist on the server).
        source_folder: Source folder.  Defaults to IMAP_FOLDER.
    """
    if not _valid_uid(uid):
        return {"error": f"Invalid UID: {uid!r}"}

    if _is_prohibited(target_folder):
        return {
            "error": (
                f"'{target_folder}' matches a prohibited pattern. "
                f"Blocked patterns: {IMAP_PROHIBITED_FOLDERS}"
            )
        }

    conn = get_conn()
    src = source_folder or IMAP_FOLDER

    typ, _ = conn.select(_q(src))
    if typ != "OK":
        return {"error": f"Cannot open source folder: {src}"}

    # Prefer RFC 6851 MOVE (atomic, no intermediate \Deleted state)
    try:
        typ, _ = conn.uid("MOVE", uid, _q(target_folder))
        if typ == "OK":
            return {"success": True, "uid": uid, "moved_to": target_folder}
    except imaplib.IMAP4.error:
        pass  # server does not support MOVE extension

    # Fallback: COPY → flag \Deleted → EXPUNGE
    typ, _ = conn.uid("copy", uid, _q(target_folder))
    if typ != "OK":
        return {"error": f"COPY to '{target_folder}' failed — folder may not exist"}

    conn.uid("store", uid, "+FLAGS", "\\Deleted")
    conn.expunge()
    return {"success": True, "uid": uid, "moved_to": target_folder}


@mcp.tool()
def mark_as_read(uid: str, folder: Optional[str] = None) -> dict:
    """
    Mark an email as read by adding the \\Seen flag.

    Args:
        uid:    IMAP UID of the message.
        folder: Folder containing the message.  Defaults to IMAP_FOLDER.
    """
    if not _valid_uid(uid):
        return {"error": f"Invalid UID: {uid!r}"}

    conn = get_conn()
    target = folder or IMAP_FOLDER

    # Must open read-write (no readonly=True) so STORE is allowed
    typ, _ = conn.select(_q(target))
    if typ != "OK":
        return {"error": f"Cannot open folder: {target}"}

    typ, _ = conn.uid("store", uid, "+FLAGS", "\\Seen")
    if typ != "OK":
        return {"error": f"STORE failed for UID {uid}"}

    return {"success": True, "uid": uid, "flags_added": ["\\Seen"]}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
