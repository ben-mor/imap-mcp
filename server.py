"""
IMAP MCP Server — exposes mailbox tools to Claude Desktop via stdio transport.

Supports multiple IMAP accounts.  Accounts are declared in mailboxes.toml
(git-ignored) with credentials stored in per-account
.config/<name>/connection.json files (also git-ignored).

Sending mail is intentionally not supported.
"""

import email
import email.header
import imaplib
import json
import logging
import quopri
import re
import sqlite3
import ssl
import traceback
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

mcp = FastMCP("imap-mcp")

# ── Config loading ─────────────────────────────────────────────────────────────

_ROOT    = Path(__file__).parent
_DB_PATH = _ROOT / ".config" / "imap-mcp.db"

# mailbox_name → connection-config dict
_mailbox_configs: dict[str, dict] = {}


def _load_configs() -> None:
    """Discover mailboxes by globbing .config/*/connection.json.

    Each subdirectory of .config/ that contains a connection.json becomes a
    mailbox; the directory name is the mailbox identifier.  Silently leaves
    _mailbox_configs empty when .config/ is absent so the module can be
    imported without real config (e.g. in tests that inject configs directly).
    """
    global _mailbox_configs

    config_base = _ROOT / ".config"
    if not config_base.exists():
        logger.warning(".config/ not found — no mailboxes configured")
        return

    configs: dict[str, dict] = {}
    for conn_file in sorted(config_base.glob("*/connection.json")):
        name = conn_file.parent.name
        with open(conn_file) as fh:
            cfg = json.load(fh)
        configs[name] = cfg
        logger.info("Loaded mailbox: %r (%s)", name, cfg.get("host", "?"))

    _mailbox_configs = configs


_load_configs()


# ── SQLite (rules + audit) ─────────────────────────────────────────────────────

_db_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    rule        TEXT    NOT NULL,
    rule_order  INTEGER NOT NULL DEFAULT 100,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    deleted_at  TEXT                -- NULL = active; soft-delete sets this
);

CREATE TABLE IF NOT EXISTS audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    mailbox     TEXT    NOT NULL,
    rule_id     INTEGER NOT NULL,
    action      TEXT    NOT NULL,   -- 'add' | 'change' | 'remove'
    caller      TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    old_value   TEXT,               -- JSON snapshot; NULL for 'add'
    new_value   TEXT                -- JSON snapshot; NULL for 'remove'
);
"""


def _db() -> sqlite3.Connection:
    """Return the shared SQLite connection, creating the schema on first call."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    _db_conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── IMAP connection pool ───────────────────────────────────────────────────────

_connections: dict[str, imaplib.IMAP4] = {}


def get_conn(mailbox: str) -> imaplib.IMAP4:
    """Return the persistent IMAP connection for *mailbox*, reconnecting if needed.

    Capabilities are fetched once at connect time and cached on the connection
    object by imaplib (conn.capabilities).  Each stdio process is single-
    threaded so no locking is needed within a process; concurrent processes
    each have their own connection pool.
    """
    cfg = _mailbox_configs.get(mailbox)
    if cfg is None:
        raise KeyError(f"Unknown mailbox: {mailbox!r}")

    conn = _connections.get(mailbox)
    if conn is not None:
        try:
            conn.noop()
            return conn
        except Exception:
            _connections.pop(mailbox, None)

    host    = cfg["host"]
    port    = int(cfg.get("port", 993))
    use_ssl = str(cfg.get("use_ssl", "true")).lower() not in ("false", "0", "no")
    timeout = float(cfg.get("timeout", 30))
    ssl_ctx = ssl.create_default_context()

    if use_ssl:
        new_conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl_ctx, timeout=timeout
        )
    else:
        new_conn = imaplib.IMAP4(host, port, timeout=timeout)
        new_conn.starttls(ssl_context=ssl_ctx)

    # imaplib encodes all string arguments (including the password) via
    # self._encoding, which defaults to 'ascii'.  Passwords that contain
    # non-ASCII characters (e.g. § \xa7, ä, ©) would raise UnicodeEncodeError.
    # UTF-8 is a strict superset of ASCII so this is safe for all accounts.
    new_conn._encoding = "utf-8"
    new_conn.login(cfg["user"], cfg["password"])
    _connections[mailbox] = new_conn
    logger.info("IMAP connected: %s → %s:%s", mailbox, host, port)
    return new_conn


# ── Small helpers ──────────────────────────────────────────────────────────────

def _q(folder: str) -> str:
    """Wrap a folder name in IMAP double-quotes."""
    if folder.startswith('"') and folder.endswith('"'):
        return folder
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _default_folder(mailbox: str) -> str:
    return _mailbox_configs.get(mailbox, {}).get("folder", "INBOX")


def _prohibited_patterns(mailbox: str) -> list[str]:
    return _mailbox_configs.get(mailbox, {}).get(
        "prohibited_folders", ["Sent", "Outbox", "Queue"]
    )


def _max_body_bytes(mailbox: str) -> int:
    return int(
        _mailbox_configs.get(mailbox, {}).get("max_body_bytes", 20 * 1024 * 1024)
    )


def _is_prohibited(folder: str, prohibited: list[str]) -> bool:
    """Return True if *folder* matches any pattern (case-insensitive substring)."""
    fl = folder.lower()
    return any(p.lower() in fl for p in prohibited)


def _valid_uid(uid: str) -> bool:
    return bool(uid) and uid.isdigit()


def _check_mailbox(mailbox: str) -> Optional[str]:
    """Return an error string if *mailbox* is not configured, else None."""
    if mailbox not in _mailbox_configs:
        return f"Unknown mailbox {mailbox!r}. Known: {list(_mailbox_configs.keys())}"
    return None


def _bulk_summary(results: list[dict]) -> dict:
    """Count statuses in a per-UID result list and return a summary dict."""
    counts: dict[str, int] = {}
    for r in results:
        s = r["status"]
        counts[s] = counts.get(s, 0) + 1
    return {"total": len(results), **counts}


# ── Parsing helpers ────────────────────────────────────────────────────────────

# Matches:  (\Flags ...) "sep" "Folder Name"
#       or: (\Flags ...) "sep" FolderName
#       or: (\Flags ...) NIL  "Folder Name"
_LIST_LINE = re.compile(
    r"\(([^)]*)\)\s+(?:\"([^\"]*)\"|NIL)\s+(?:\"([^\"]*)\"|(\S+))"
)

# Matches a single RFC2047 encoded word: =?charset?Q|B?encoded_text?=
_RFC2047_WORD = re.compile(r"=\?([^?]+)\?([BbQq])\?([^?]*)\?=")


def _decode_rfc2047_fallback(value: str) -> str:
    """Fallback for malformed QP encoded words that email.header.decode_header()
    returns undecoded (e.g. raw 8-bit chars inside a QP token instead of =XX).

    Only handles Q (quoted-printable) encoding — if a B (base64) encoded word
    failed the standard decoder it is genuinely broken and returned unchanged.
    No charset guessing: uses the declared charset via _decode_bytes.
    """
    def _decode_word(m: re.Match) -> str:
        charset, enc, text = m.group(1), m.group(2).upper(), m.group(3)
        if enc != "Q":
            return m.group(0)
        try:
            raw = text.encode("latin-1", errors="replace")
            decoded = quopri.decodestring(raw.replace(b"_", b" "))
            return _decode_bytes(decoded, charset)
        except Exception:
            return m.group(0)

    return _RFC2047_WORD.sub(_decode_word, value)


def _decode_bytes(data: bytes, charset: Optional[str]) -> str:
    """Decode bytes with two fallback layers.

    errors='replace' swallows bad byte sequences within a known codec.
    LookupError catches completely unknown charset names (e.g. 'unknown-8bit').
    latin-1 maps all 256 byte values 1-to-1 and never raises.
    """
    enc = charset or "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("latin-1", errors="replace")


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    chunks: list[str] = []
    for chunk, charset in parts:
        text = _decode_bytes(chunk, charset) if isinstance(chunk, bytes) else str(chunk)
        # Fire fallback whether decode_header returned bytes or a plain str —
        # both paths can leave an encoded word intact.
        if "=?" in text and "?=" in text:
            text = _decode_rfc2047_fallback(text)
        chunks.append(text)
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
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _strip_html(raw: str) -> str:
    s = _Stripper()
    s.feed(raw)
    return s.result()


def _extract_body(msg: email.message.Message) -> dict:
    """Walk MIME parts; return best text body + attachment metadata.

    Preference order: text/plain > text/html (stripped).
    """
    plain:     Optional[str] = None
    html_body: Optional[str] = None
    attachments: list[dict]  = []

    for part in msg.walk():
        ct          = part.get_content_type()
        disposition = part.get("Content-Disposition", "")
        filename    = part.get_filename()

        if filename or "attachment" in disposition.lower():
            attachments.append({
                "filename":     _decode_header(filename) if filename else None,
                "content_type": ct,
                "size_bytes":   len(part.get_payload(decode=True) or b""),
            })
            continue

        raw_payload = part.get_payload(decode=True)
        if raw_payload is None:
            continue
        charset = part.get_content_charset()

        if ct == "text/plain" and plain is None:
            plain = _decode_bytes(raw_payload, charset)
        elif ct == "text/html" and html_body is None:
            html_body = _decode_bytes(raw_payload, charset)

    if plain is not None:
        return {"body": plain,               "html_stripped": False, "attachments": attachments}
    if html_body is not None:
        return {"body": _strip_html(html_body), "html_stripped": True,  "attachments": attachments}
    return     {"body": "",                  "html_stripped": False, "attachments": attachments}


def _parse_fetch_info(info_line: str) -> tuple[Optional[str], set[str]]:
    """Extract UID and FLAGS from an IMAP FETCH response info line."""
    uid_m   = re.search(r"UID (\d+)",        info_line)
    flags_m = re.search(r"FLAGS \(([^)]*)\)", info_line)
    uid       = uid_m.group(1)         if uid_m   else None
    raw_flags = set(flags_m.group(1).split()) if flags_m else set()
    return uid, raw_flags


_COPYUID_RE   = re.compile(rb"\[COPYUID \d+ [\d,:]+ ([\d,:]+)\]")
_APPENDUID_RE = re.compile(rb"\[APPENDUID \d+ (\d+)\]")


def _parse_copyuid(data: list) -> Optional[str]:
    """Return the destination UID set string from a COPYUID response, or None."""
    if not data or not data[0]:
        return None
    raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
    m = _COPYUID_RE.search(raw)
    return m.group(1).decode() if m else None


def _parse_appenduid(data: list) -> Optional[str]:
    """Return the assigned UID from an APPENDUID response code, or None."""
    if not data or not data[0]:
        return None
    raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
    m = _APPENDUID_RE.search(raw)
    return m.group(1).decode() if m else None


# ── MCP Tools — Mailbox discovery ─────────────────────────────────────────────


@mcp.tool()
def list_mailboxes() -> list[dict]:
    """
    List all configured mailbox accounts.

    Returns a list of dicts with keys:
      name           – account identifier used in all other tool calls
      host           – IMAP server hostname
      user           – login username
      default_folder – default folder for this account
    """
    return [
        {
            "name":           name,
            "host":           cfg.get("host", ""),
            "user":           cfg.get("user", ""),
            "default_folder": cfg.get("folder", "INBOX"),
        }
        for name, cfg in _mailbox_configs.items()
    ]


# ── MCP Tools — Mail ───────────────────────────────────────────────────────────


@mcp.tool()
def list_folders(mailbox: str) -> list[dict]:
    """
    List all IMAP folders available in a mailbox account.

    Args:
        mailbox: Account name (see list_mailboxes).

    Returns a list of dicts with keys:
      name       – full folder name as used in other tool calls
      separator  – hierarchy separator (typically "/" or ".")
      flags      – IMAP attribute flags (e.g. \\HasNoChildren, \\Noselect)
    """
    err = _check_mailbox(mailbox)
    if err:
        return [{"error": err}]

    conn = get_conn(mailbox)
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
        folders.append({
            "name":      name,
            "separator": sep or "/",
            "flags":     [f for f in flags_str.split() if f],
        })
    return folders


@mcp.tool()
def list_emails(
    mailbox: str,
    limit:  int = 20,
    folder: Optional[str] = None,
    flags:  Optional[list[str]] = None,
) -> list[dict]:
    """
    List recent emails from a folder with their metadata.

    Args:
        mailbox: Account name (see list_mailboxes).
        limit:   Maximum emails to return (most recent first). Hard-capped at 200.
        folder:  IMAP folder to read. Defaults to the account default folder.
        flags:   IMAP flags to report per message, e.g. ["\\\\Seen", "\\\\Flagged"].
                 Defaults to ["\\\\Seen", "\\\\Flagged"] when omitted.

    Returns a list of dicts with keys:
      uid, from, subject, date, message_id, flags (dict flag→bool).
    """
    err = _check_mailbox(mailbox)
    if err:
        return [{"error": err}]

    try:
        return _list_emails_inner(mailbox, limit, folder, flags)
    except Exception:
        tb = traceback.format_exc()
        logger.error("list_emails crashed:\n%s", tb)
        return [{"error": "list_emails raised an exception", "traceback": tb}]


def _list_emails_inner(
    mailbox: str,
    limit:   int,
    folder:  Optional[str],
    flags:   Optional[list[str]],
) -> list[dict]:
    """Inner implementation — called by list_emails which wraps it in try/except."""
    limit  = min(limit, 200)
    conn   = get_conn(mailbox)
    target = folder or _default_folder(mailbox)

    typ, _ = conn.select(_q(target), readonly=True)
    if typ != "OK":
        return [{"error": f"Cannot open folder: {target}"}]

    capabilities_repr = repr(conn.capabilities)

    if "SORT" in conn.capabilities or b"SORT" in conn.capabilities:
        typ, data = conn.uid("sort", "(REVERSE DATE)", "UTF-8", "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        raw_tokens = data[0].split() if isinstance(data[0], bytes) else [
            t.encode("latin-1", errors="replace") for t in data[0].split()
        ]
        uid_list = [u for u in raw_tokens if u.isdigit()][:limit]
    else:
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        raw_tokens = data[0].split() if isinstance(data[0], bytes) else [
            t.encode("latin-1", errors="replace") for t in data[0].split()
        ]
        uid_list = [u for u in raw_tokens if u.isdigit()][-limit:][::-1]

    if not uid_list:
        return []

    uid_str    = ",".join(u.decode("ascii") for u in uid_list)
    fetch_spec = "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
    typ, fetch_data = conn.uid("fetch", uid_str, fetch_spec)
    if typ != "OK":
        return [{"error": "FETCH command failed"}]

    wanted: set[str] = set(flags) if flags else {"\\Seen", "\\Flagged"}
    results: list[dict] = []

    for part in fetch_data:
        if not isinstance(part, tuple):
            continue
        info         = part[0].decode("utf-8", errors="replace")
        header_bytes = part[1] if len(part) > 1 else b""
        uid, raw_flags = _parse_fetch_info(info)
        msg = email.message_from_bytes(header_bytes)
        results.append({
            "uid":        uid,
            "from":       _decode_header(msg.get("From")),
            "subject":    _decode_header(msg.get("Subject")),
            "date":       msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
            "flags":      {f: (f in raw_flags) for f in wanted},
        })

    return results


@mcp.tool()
def get_email(mailbox: str, uid: str, folder: Optional[str] = None) -> dict:
    """
    Fetch the full content of a single email by its IMAP UID.

    Uses BODY.PEEK so the message is NOT marked as read by this call.
    HTML-only messages are stripped to plain text.

    Args:
        mailbox: Account name (see list_mailboxes).
        uid:     IMAP UID of the message (from list_emails / search_emails).
        folder:  Folder containing the message. Defaults to account default.

    Returns a dict with keys:
      uid, from, to, cc, subject, date, message_id,
      body (str), html_stripped (bool), attachments (list).
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}
    if not _valid_uid(uid):
        return {"error": f"Invalid UID: {uid!r}"}

    conn   = get_conn(mailbox)
    target = folder or _default_folder(mailbox)

    typ, _ = conn.select(_q(target), readonly=True)
    if typ != "OK":
        return {"error": f"Cannot open folder: {target}"}

    # Pre-check size to avoid loading huge messages into memory.
    typ, size_data = conn.uid("fetch", uid, "(RFC822.SIZE)")
    if typ == "OK" and size_data and isinstance(size_data[0], tuple):
        size_m = re.search(
            r"RFC822\.SIZE (\d+)",
            size_data[0][0].decode("utf-8", errors="replace"),
        )
        if size_m:
            msg_size = int(size_m.group(1))
            limit    = _max_body_bytes(mailbox)
            if msg_size > limit:
                return {
                    "error": (
                        f"Message too large to fetch: {msg_size:,} bytes "
                        f"(limit: {limit:,} bytes)."
                    )
                }

    typ, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or data[0] is None:
        return {"error": f"UID {uid} not found in {target}"}

    first = data[0]
    if not isinstance(first, tuple) or len(first) < 2:
        return {"error": f"Unexpected server response for UID {uid}"}

    msg       = email.message_from_bytes(first[1])
    body_info = _extract_body(msg)

    return {
        "uid":        uid,
        "from":       _decode_header(msg.get("From")),
        "to":         _decode_header(msg.get("To")),
        "cc":         _decode_header(msg.get("Cc")),
        "subject":    _decode_header(msg.get("Subject")),
        "date":       msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        **body_info,
    }


@mcp.tool()
def search_emails(
    mailbox:  str,
    criteria: list[str],
    folder:   Optional[str] = None,
) -> dict:
    """
    Search a folder using raw IMAP SEARCH criteria tokens.

    Args:
        mailbox:  Account name (see list_mailboxes).
        criteria: IMAP SEARCH tokens as a list, e.g.:
                    ["UNSEEN"]
                    ["FROM", "sender@example.com"]
                    ["HEADER", "X-Spam-Flag", "YES"]
                    ["SINCE", "01-May-2026"]
                    ["UNSEEN", "FROM", "boss@example.com"]
        folder:   Folder to search. Defaults to account default.

    Returns {"uids": [...], "count": N}.
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err, "uids": [], "count": 0}
    if not criteria:
        return {"error": "criteria must not be empty", "uids": [], "count": 0}

    conn   = get_conn(mailbox)
    target = folder or _default_folder(mailbox)

    typ, _ = conn.select(_q(target), readonly=True)
    if typ != "OK":
        return {"error": f"Cannot open folder: {target}", "uids": [], "count": 0}

    typ, data = conn.uid("search", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return {"uids": [], "count": 0}

    uid_list = [u.decode("ascii") for u in data[0].split() if u.isdigit()]
    return {"uids": uid_list, "count": len(uid_list)}


@mcp.tool()
def mark_as_read(mailbox: str, uid: str, folder: Optional[str] = None) -> dict:
    """
    Mark an email as read by adding the \\Seen flag.

    Args:
        mailbox: Account name (see list_mailboxes).
        uid:     IMAP UID of the message.
        folder:  Folder containing the message. Defaults to account default.
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}
    if not _valid_uid(uid):
        return {"error": f"Invalid UID: {uid!r}"}

    conn   = get_conn(mailbox)
    target = folder or _default_folder(mailbox)

    typ, _ = conn.select(_q(target))          # read-write (no readonly=True)
    if typ != "OK":
        return {"error": f"Cannot open folder: {target}"}

    typ, _ = conn.uid("store", uid, "+FLAGS", "\\Seen")
    if typ != "OK":
        return {"error": f"STORE failed for UID {uid}"}

    return {"success": True, "uid": uid, "flags_added": ["\\Seen"]}


@mcp.tool()
def move_mail(
    mailbox:       str,
    uids:          list[str],
    target_folder: str,
    source_folder: Optional[str] = None,
) -> dict:
    """
    Move one or more emails to a folder within the same mailbox account.

    Accepts 1–N UIDs in a single call.  Existing UIDs are moved with a single
    IMAP command (UID MOVE uid-set, or COPY+STORE+EXPUNGE on servers without
    MOVE); each UID is reported individually so partial failures are visible.

    Duplicate UIDs in the input list are collapsed to a single result entry.

    Args:
        mailbox:       Account name (see list_mailboxes).
        uids:          List of IMAP UIDs to move (positive integer strings).
        target_folder: Destination folder name.
        source_folder: Source folder.  Defaults to account default.

    Returns a dict with keys:
      results  – list of per-UID dicts (in input order), each with:
                   uid     – the UID as supplied
                   status  – one of:
                     "moved"          successfully moved to target_folder
                     "not_found"      UID was not in source_folder before the move
                     "failed"         move attempted but message confirmed still
                                      in source (nothing changed — safe to retry)
                     "lost"           move attempted, message gone from source and
                                      cannot be confirmed in destination (check
                                      target_folder manually)
                     "copy_stuck"     COPY succeeded but EXPUNGE failed; message
                                      exists in both folders (marked \\\\Deleted in
                                      source — run EXPUNGE manually to clean up)
                     "invalid"        UID is not a positive integer string
                   detail  – human-readable explanation (omitted when status is
                              "moved")
      summary  – {"total": N, "<status>": count, ...}
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}
    if not uids:
        return {"error": "uids must not be empty"}
    if _is_prohibited(target_folder, _prohibited_patterns(mailbox)):
        return {
            "error": (
                f"'{target_folder}' matches a prohibited pattern. "
                f"Blocked: {_prohibited_patterns(mailbox)}"
            )
        }

    # ── Phase 1: validate UID format ──────────────────────────────────────────
    # per_uid: first-occurrence result entry; None = valid but not yet resolved.
    per_uid:    dict[str, Optional[dict]] = {}
    valid_uids: list[str]                 = []  # deduped, format-valid, input order

    for uid in uids:
        if uid in per_uid:
            continue                    # collapse duplicates
        if not _valid_uid(uid):
            per_uid[uid] = {
                "uid":    uid,
                "status": "invalid",
                "detail": "UID must be a positive integer string",
            }
        else:
            valid_uids.append(uid)
            per_uid[uid] = None         # placeholder

    if not valid_uids:
        results = [per_uid[u] for u in dict.fromkeys(uids)]
        return {"results": results, "summary": _bulk_summary(results)}

    # ── Phase 2: open folder, pre-check which UIDs exist ─────────────────────
    conn = get_conn(mailbox)
    src  = source_folder or _default_folder(mailbox)

    typ, _ = conn.select(_q(src))
    if typ != "OK":
        return {"error": f"Cannot open source folder: {src}"}

    uid_set_str = ",".join(valid_uids)
    typ, data   = conn.uid("search", None, f"UID {uid_set_str}")

    found_set: set[str] = set()
    if typ == "OK" and data and data[0]:
        found_set = set(data[0].decode("ascii", errors="replace").split())

    for uid in valid_uids:
        if uid not in found_set:
            per_uid[uid] = {
                "uid":    uid,
                "status": "not_found",
                "detail": f"UID not present in {src}",
            }

    found_list = [uid for uid in valid_uids if uid in found_set]

    if not found_list:
        results = [per_uid[u] for u in dict.fromkeys(uids)]
        return {"results": results, "summary": _bulk_summary(results)}

    # ── Phase 3: move all found UIDs in one IMAP command ─────────────────────
    move_uid_set = ",".join(found_list)
    copy_failed  = False
    copy_stuck   = False

    if "MOVE" in conn.capabilities:
        typ, _ = conn.uid("MOVE", move_uid_set, _q(target_folder))
        move_ok = (typ == "OK")
    else:
        typ, _ = conn.uid("copy", move_uid_set, _q(target_folder))
        if typ != "OK":
            move_ok     = False
            copy_failed = True
        else:
            conn.uid("store", move_uid_set, "+FLAGS", "\\Deleted")
            typ, _    = conn.expunge()
            move_ok   = (typ == "OK")
            copy_stuck = not move_ok

    # ── Phase 4: classify results ─────────────────────────────────────────────
    if move_ok:
        for uid in found_list:
            per_uid[uid] = {"uid": uid, "status": "moved"}

    elif copy_failed:
        for uid in found_list:
            per_uid[uid] = {
                "uid":    uid,
                "status": "failed",
                "detail": (
                    f"COPY to '{target_folder}' failed; "
                    "message remains in source unchanged"
                ),
            }

    elif copy_stuck:
        for uid in found_list:
            per_uid[uid] = {
                "uid":    uid,
                "status": "copy_stuck",
                "detail": (
                    f"Copied to '{target_folder}' but EXPUNGE failed; message "
                    f"exists in both folders (marked \\Deleted in {src})"
                ),
            }

    else:
        # MOVE command failed — post-check to distinguish failed vs lost
        typ2, data2 = conn.uid("search", None, f"UID {move_uid_set}")
        still_in_src: set[str] = set()
        if typ2 == "OK" and data2 and data2[0]:
            still_in_src = set(data2[0].decode("ascii", errors="replace").split())

        for uid in found_list:
            if uid in still_in_src:
                per_uid[uid] = {
                    "uid":    uid,
                    "status": "failed",
                    "detail": "Move command failed; message confirmed in source",
                }
            else:
                per_uid[uid] = {
                    "uid":    uid,
                    "status": "lost",
                    "detail": (
                        "Move command failed and message is no longer in source — "
                        f"check '{target_folder}' manually; it may have arrived "
                        "there under a new UID, or been removed by another client"
                    ),
                }

    results = [per_uid[u] for u in dict.fromkeys(uids)]
    return {"results": results, "summary": _bulk_summary(results)}


@mcp.tool()
def move_mail_cross(
    src_mailbox: str,
    uid:         str,
    src_folder:  str,
    dst_mailbox: str,
    dst_folder:  str,
) -> dict:
    """
    Move an email from one mailbox account to another.

    IMAP has no native cross-server move.  This tool fetches the raw message
    (RFC822), APPENDs it to the destination folder, then deletes the original.

    Failure handling:
      • APPEND fails  → original is untouched; error returned.
      • DELETE fails  → message exists in both places; error returned with
                        new_uid so the duplicate can be cleaned up.

    Args:
        src_mailbox: Source account name (see list_mailboxes).
        uid:         IMAP UID of the message in the source account.
        src_folder:  Source folder name.
        dst_mailbox: Destination account name.
        dst_folder:  Destination folder name.

    Returns a dict with keys on success:
      success, src_mailbox, src_folder, src_uid, dst_mailbox, dst_folder,
      new_uid (present when the server returns an APPENDUID response code).
    """
    err = _check_mailbox(src_mailbox) or _check_mailbox(dst_mailbox)
    if err:
        return {"error": err}
    if not _valid_uid(uid):
        return {"error": f"Invalid UID: {uid!r}"}
    if _is_prohibited(dst_folder, _prohibited_patterns(dst_mailbox)):
        return {
            "error": (
                f"'{dst_folder}' matches a prohibited pattern in {dst_mailbox}. "
                f"Blocked: {_prohibited_patterns(dst_mailbox)}"
            )
        }

    src_conn = get_conn(src_mailbox)
    dst_conn = get_conn(dst_mailbox)

    typ, _ = src_conn.select(_q(src_folder))
    if typ != "OK":
        return {"error": f"Cannot open source folder: {src_folder}"}

    typ, exists = src_conn.uid("search", None, f"UID {uid}")
    if typ != "OK" or not exists[0].strip():
        return {"error": f"UID {uid} not found in {src_mailbox}/{src_folder}"}

    # Fetch raw message bytes.
    typ, fetch_data = src_conn.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not fetch_data or not isinstance(fetch_data[0], tuple):
        return {"error": f"Failed to fetch UID {uid} from {src_mailbox}"}
    raw_message: bytes = fetch_data[0][1]

    # Append to destination — if this fails the original is untouched.
    typ, append_data = dst_conn.append(_q(dst_folder), None, None, raw_message)
    if typ != "OK":
        return {
            "error": (
                f"APPEND to {dst_mailbox}/{dst_folder} failed — "
                "original message is untouched."
            )
        }

    new_uid = _parse_appenduid(append_data)

    # Delete from source.
    src_conn.uid("store", uid, "+FLAGS", "\\Deleted")
    typ, _ = src_conn.expunge()
    if typ != "OK":
        result: dict = {
            "error": (
                f"Message was copied to {dst_mailbox}/{dst_folder} but "
                f"could not be deleted from {src_mailbox}/{src_folder}. "
                "Delete the original manually."
            ),
            "src_mailbox": src_mailbox, "src_folder": src_folder, "src_uid": uid,
            "dst_mailbox": dst_mailbox, "dst_folder": dst_folder,
        }
        if new_uid:
            result["new_uid"] = new_uid
        return result

    result = {
        "success":    True,
        "src_mailbox": src_mailbox, "src_folder": src_folder, "src_uid": uid,
        "dst_mailbox": dst_mailbox, "dst_folder": dst_folder,
    }
    if new_uid:
        result["new_uid"] = new_uid
    return result


# ── MCP Tools — Rules ──────────────────────────────────────────────────────────


@mcp.tool()
def get_rules(mailbox: str) -> dict:
    """
    Return all active rules for a mailbox, ordered by rule_order then id.

    Args:
        mailbox: Account name (see list_mailboxes).

    Returns {"mailbox": ..., "rules": [...], "count": N}.
    Each rule dict has keys: id, name, rule, rule_order, created_at, updated_at.
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}

    rows = _db().execute(
        """
        SELECT id, name, rule, rule_order, created_at, updated_at
        FROM   rules
        WHERE  mailbox = ? AND deleted_at IS NULL
        ORDER  BY rule_order ASC, id ASC
        """,
        (mailbox,),
    ).fetchall()

    return {
        "mailbox": mailbox,
        "rules":   [dict(r) for r in rows],
        "count":   len(rows),
    }


@mcp.tool()
def add_rule(
    mailbox:    str,
    name:       str,
    rule:       str,
    caller:     str,
    reason:     str,
    rule_order: int = 100,
) -> dict:
    """
    Add a new rule to a mailbox knowledge base.

    Every write is logged to the append-only audit table with the caller
    identity and reason.

    Args:
        mailbox:    Account name (see list_mailboxes).
        name:       Short human-readable label for the rule.
        rule:       Rule body in markdown (may be arbitrarily long).
        caller:     Identity of the session/agent creating this rule,
                    e.g. "claude-desktop · Sonnet 4.6".
        reason:     Why this rule is being added.
        rule_order: Sort key among rules (non-unique, lower = first, default 100).

    Returns {"success": True, "id": <auto-generated integer>}.
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}

    now = _now()
    db  = _db()

    cur = db.execute(
        "INSERT INTO rules (mailbox, name, rule, rule_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mailbox, name, rule, rule_order, now, now),
    )
    rule_id = cur.lastrowid

    db.execute(
        "INSERT INTO audit "
        "(ts, mailbox, rule_id, action, caller, reason, old_value, new_value) "
        "VALUES (?, ?, ?, 'add', ?, ?, NULL, ?)",
        (
            now, mailbox, rule_id, caller, reason,
            json.dumps({"id": rule_id, "name": name, "rule": rule,
                        "rule_order": rule_order}),
        ),
    )
    db.commit()
    return {"success": True, "id": rule_id}


@mcp.tool()
def change_rule(
    mailbox:    str,
    rule_id:    int,
    caller:     str,
    reason:     str,
    name:       Optional[str] = None,
    rule:       Optional[str] = None,
    rule_order: Optional[int] = None,
) -> dict:
    """
    Update one or more fields of an existing rule (patch semantics).

    Pass only the fields you want to change; omitted fields are left as-is.
    The previous state is stored in the audit log before the update.

    Args:
        mailbox:    Account name (see list_mailboxes).
        rule_id:    ID of the rule to update (from add_rule / get_rules).
        caller:     Identity of the session/agent making the change.
        reason:     Why this change is being made.
        name:       New label (omit to leave unchanged).
        rule:       New rule body (omit to leave unchanged).
        rule_order: New sort key (omit to leave unchanged).
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}

    db  = _db()
    row = db.execute(
        "SELECT * FROM rules WHERE id = ? AND mailbox = ? AND deleted_at IS NULL",
        (rule_id, mailbox),
    ).fetchone()
    if not row:
        return {"error": f"Rule {rule_id} not found in mailbox {mailbox!r}"}

    updates: dict = {}
    if name       is not None: updates["name"]       = name
    if rule       is not None: updates["rule"]        = rule
    if rule_order is not None: updates["rule_order"]  = rule_order
    if not updates:
        return {"error": "Nothing to update — pass at least one of: name, rule, rule_order"}

    now             = _now()
    old_snapshot    = json.dumps(dict(row))
    updates["updated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE rules SET {set_clause} WHERE id = ? AND mailbox = ?",
        [*updates.values(), rule_id, mailbox],
    )

    new_row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    db.execute(
        "INSERT INTO audit "
        "(ts, mailbox, rule_id, action, caller, reason, old_value, new_value) "
        "VALUES (?, ?, ?, 'change', ?, ?, ?, ?)",
        (now, mailbox, rule_id, caller, reason,
         old_snapshot, json.dumps(dict(new_row))),
    )
    db.commit()
    return {"success": True, "id": rule_id}


@mcp.tool()
def remove_rule(
    mailbox: str,
    rule_id: int,
    caller:  str,
    reason:  str,
) -> dict:
    """
    Soft-delete a rule.

    The rule record and its full change history are preserved in the audit
    log; get_rules will no longer return it.  The deletion itself is also
    logged with caller and reason.

    Args:
        mailbox: Account name (see list_mailboxes).
        rule_id: ID of the rule to remove.
        caller:  Identity of the session/agent removing the rule.
        reason:  Why this rule is being removed.
    """
    err = _check_mailbox(mailbox)
    if err:
        return {"error": err}

    db  = _db()
    row = db.execute(
        "SELECT * FROM rules WHERE id = ? AND mailbox = ? AND deleted_at IS NULL",
        (rule_id, mailbox),
    ).fetchone()
    if not row:
        return {"error": f"Rule {rule_id} not found in mailbox {mailbox!r}"}

    now          = _now()
    old_snapshot = json.dumps(dict(row))

    db.execute("UPDATE rules SET deleted_at = ? WHERE id = ?", (now, rule_id))
    db.execute(
        "INSERT INTO audit "
        "(ts, mailbox, rule_id, action, caller, reason, old_value, new_value) "
        "VALUES (?, ?, ?, 'remove', ?, ?, ?, NULL)",
        (now, mailbox, rule_id, caller, reason, old_snapshot),
    )
    db.commit()
    return {"success": True, "id": rule_id}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
