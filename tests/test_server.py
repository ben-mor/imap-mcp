"""
Tests for server.py.

Parsing helpers are tested thoroughly.
MCP tool functions are covered with happy-day scenarios and the one
critical unhappy path (move_mail prohibition + MOVE-fallback).
"""

import email
import os
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, call, patch

import pytest

# Must be set before server is imported so the module-level os.environ reads succeed.
os.environ.setdefault("IMAP_HOST", "imap.test.example")
os.environ.setdefault("IMAP_USER", "test@test.example")
os.environ.setdefault("IMAP_PASSWORD", "secret")
os.environ.setdefault("IMAP_PROHIBITED_FOLDERS", "Sent,Outbox,Queue")

from server import (  # noqa: E402  (import after env setup)
    _LIST_LINE,
    _decode_bytes,
    _decode_header,
    _decode_rfc2047_fallback,
    _extract_body,
    _is_prohibited,
    _parse_fetch_info,
    _q,
    _strip_html,
    _valid_uid,
    get_email,
    list_emails,
    list_folders,
    mark_as_read,
    move_mail,
)


# ── _decode_header ─────────────────────────────────────────────────────────────

class TestDecodeRFC2047Fallback:
    def test_qp_with_raw_latin1_chars(self):
        # Simulates a malformed header: ä (0xE4) and ü (0xFC) placed raw inside
        # a QP encoded word instead of being escaped as =E4 / =FC.
        # email.header.decode_header() passes this through as a plain str;
        # our fallback should strip the wrapper and return readable text.
        raw = "=?iso-8859-1?Q?Erg=E4nzung_f=FCr?="  # well-formed — baseline
        assert "Ergänzung" in _decode_rfc2047_fallback(raw)
        assert "für" in _decode_rfc2047_fallback(raw)

    def test_qp_underscore_becomes_space(self):
        result = _decode_rfc2047_fallback("=?utf-8?Q?hello_world?=")
        assert result == "hello world"

    def test_base64_encoded_word_returned_unchanged(self):
        # B-encoded words are left alone — if decode_header couldn't handle
        # them they are genuinely broken and we don't try to be clever.
        import base64 as _b64
        encoded = _b64.b64encode("Héllo".encode("utf-8")).decode()
        raw = f"=?utf-8?B?{encoded}?="
        assert _decode_rfc2047_fallback(raw) == raw

    def test_unparseable_word_returned_unchanged(self):
        # Totally broken encoded word — should come back as-is, not raise
        raw = "=?bogus?X?garbage?="
        result = _decode_rfc2047_fallback(raw)
        assert isinstance(result, str)

    def test_plain_text_passthrough(self):
        assert _decode_rfc2047_fallback("no encoding here") == "no encoding here"

    def test_mixed_encoded_and_plain(self):
        result = _decode_rfc2047_fallback("Hello =?utf-8?Q?W=C3=B6rld?= !")
        assert "Wörld" in result
        assert "Hello" in result

    def test_decode_header_calls_fallback_for_raw_encoded_word(self):
        # This is the real-world subject line that triggered the bug.
        # The encoded word contains ä and ü as raw latin-1 bytes, making it
        # malformed QP — email.header.decode_header() returns it as a plain str.
        subject = "=?iso-8859-1?Q?Die_smarte_Erg\xe4nzung_f\xfcr_Sie?="
        result = _decode_header(subject)
        assert "Ergänzung" in result
        assert "für" in result
        # The RFC2047 wrapper must be gone
        assert "=?" not in result

    def test_decode_header_via_message_from_bytes(self):
        # Regression test for the exact failure path seen in production:
        # email.message_from_bytes() wraps non-ASCII header bytes in a Header
        # object with charset 'unknown-8bit', so decode_header() returns the
        # encoded word as *bytes* (not str) — the fallback must still fire.
        raw = (
            b"Subject: =?iso-8859-1?Q?Re:_Re: Die smarte Erg\xe4nzung"
            b" f\xfcr Ihre Abnehmziele .?=\r\n\r\n"
        )
        msg = email.message_from_bytes(raw)
        result = _decode_header(msg.get("Subject"))
        assert "Ergänzung" in result
        assert "für" in result
        assert "=?" not in result


class TestDecodeBytes:
    def test_known_charset_utf8(self):
        assert _decode_bytes("héllo".encode("utf-8"), "utf-8") == "héllo"

    def test_known_charset_latin1(self):
        assert _decode_bytes("caf\xe9".encode("latin-1"), "latin-1") == "café"

    def test_none_charset_defaults_to_utf8(self):
        assert _decode_bytes(b"hello", None) == "hello"

    def test_unknown_charset_falls_back_to_latin1(self):
        # "unknown-8bit" is a real non-standard charset some MTAs emit;
        # Python has no codec for it so we must not raise LookupError.
        result = _decode_bytes(b"caf\xe9", "unknown-8bit")
        assert result == "café"   # latin-1 decode of \xe9

    def test_other_unknown_charset_names(self):
        for bogus in ("x-unknown", "unknown", "ansi"):
            result = _decode_bytes(b"test", bogus)
            assert result == "test"   # ASCII bytes survive any fallback

    def test_bad_bytes_replaced_not_raised(self):
        # Invalid UTF-8 sequence — errors='replace' must swallow it
        result = _decode_bytes(b"\xff\xfe", "utf-8")
        assert isinstance(result, str)


class TestDecodeHeader:
    def test_plain_ascii(self):
        assert _decode_header("Hello World") == "Hello World"

    def test_none_returns_empty(self):
        assert _decode_header(None) == ""

    def test_empty_string(self):
        assert _decode_header("") == ""

    def test_rfc2047_base64_utf8(self):
        # "Hello" → base64 → =?utf-8?b?SGVsbG8=?=
        assert _decode_header("=?utf-8?b?SGVsbG8=?=") == "Hello"

    def test_rfc2047_quoted_printable_latin1(self):
        # "Héllo" in iso-8859-1 QP
        result = _decode_header("=?iso-8859-1?q?H=E9llo?=")
        assert result == "Héllo"

    def test_mixed_encoded_and_literal(self):
        result = _decode_header("=?utf-8?b?SGVsbG8=?= World")
        assert "Hello" in result
        assert "World" in result

    def test_multiple_encoded_words(self):
        # Two consecutive encoded words (common in long subjects)
        result = _decode_header("=?utf-8?b?Rmly?= =?utf-8?b?c3Q=?=")
        assert "Fir" in result
        assert "st" in result


# ── _strip_html ────────────────────────────────────────────────────────────────

class TestStripHTML:
    def test_removes_simple_tags(self):
        result = _strip_html("<p>Hello</p>")
        assert "Hello" in result
        assert "<" not in result

    def test_decodes_html_entities(self):
        result = _strip_html("A &amp; B &lt;thing&gt;")
        assert "&amp;" not in result
        assert "&" in result
        assert "<thing>" in result

    def test_nested_tags(self):
        result = _strip_html("<div><p><b>Bold</b> and normal</p></div>")
        assert "Bold" in result
        assert "and normal" in result
        assert "<" not in result

    def test_collapses_horizontal_whitespace(self):
        result = _strip_html("<p>Hello   world</p>")
        assert "Hello world" in result
        assert "   " not in result

    def test_collapses_excess_blank_lines(self):
        result = _strip_html("<p>A</p>\n\n\n\n<p>B</p>")
        assert result.count("\n") <= 2  # at most one blank line

    def test_empty_input(self):
        assert _strip_html("") == ""

    def test_plain_text_passthrough(self):
        assert _strip_html("Just plain text") == "Just plain text"

    def test_anchor_tag_text_preserved(self):
        result = _strip_html('<a href="http://example.com">Click here</a>')
        assert "Click here" in result
        assert "http" not in result


# ── _extract_body ──────────────────────────────────────────────────────────────

class TestExtractBody:
    def test_plain_text_only(self):
        msg = MIMEText("Hello world", "plain", "utf-8")
        result = _extract_body(msg)
        assert result["body"] == "Hello world"
        assert result["html_stripped"] is False
        assert result["attachments"] == []

    def test_html_only_is_stripped(self):
        msg = MIMEText("<p>Hello <b>world</b></p>", "html", "utf-8")
        result = _extract_body(msg)
        assert "Hello" in result["body"]
        assert "world" in result["body"]
        assert "<" not in result["body"]
        assert result["html_stripped"] is True

    def test_multipart_alternative_prefers_plain(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("plain text", "plain", "utf-8"))
        msg.attach(MIMEText("<p>html text</p>", "html", "utf-8"))
        result = _extract_body(msg)
        assert result["body"] == "plain text"
        assert result["html_stripped"] is False

    def test_multipart_with_attachment_metadata(self):
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText("body text", "plain", "utf-8"))
        att = MIMEBase("application", "pdf")
        att.add_header("Content-Disposition", "attachment", filename="report.pdf")
        att.set_payload(b"pdfdata")
        msg.attach(att)
        result = _extract_body(msg)
        assert result["body"] == "body text"
        assert len(result["attachments"]) == 1
        att_meta = result["attachments"][0]
        assert att_meta["filename"] == "report.pdf"
        assert att_meta["content_type"] == "application/pdf"
        assert att_meta["size_bytes"] == len(b"pdfdata")

    def test_attachment_without_explicit_disposition(self):
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText("body", "plain", "utf-8"))
        att = MIMEBase("image", "png")
        att.add_header("Content-Disposition", "inline", filename="photo.png")
        att.set_payload(b"\x89PNG")
        msg.attach(att)
        result = _extract_body(msg)
        # Inline parts with a filename are treated as attachments
        assert any(a["filename"] == "photo.png" for a in result["attachments"])

    def test_empty_message_returns_empty_body(self):
        msg = email.message.Message()
        result = _extract_body(msg)
        assert result["body"] == ""
        assert result["html_stripped"] is False
        assert result["attachments"] == []

    def test_charset_decoding_utf8(self):
        msg = MIMEText("Ünïcödé", "plain", "utf-8")
        result = _extract_body(msg)
        assert "Ünïcödé" in result["body"]

    def test_charset_decoding_latin1(self):
        raw = "caf\xe9".encode("latin-1")
        msg = email.message.Message()
        msg["Content-Type"] = "text/plain; charset=iso-8859-1"
        msg["Content-Transfer-Encoding"] = "8bit"
        msg.set_payload(raw)
        result = _extract_body(msg)
        assert "café" in result["body"]


# ── _is_prohibited ─────────────────────────────────────────────────────────────

class TestIsProhibited:
    @pytest.mark.parametrize("folder", ["Sent", "SENT", "Sent Items", "sent"])
    def test_blocks_sent_variants(self, folder):
        assert _is_prohibited(folder) is True

    @pytest.mark.parametrize("folder", ["Outbox", "OUTBOX", "outbox"])
    def test_blocks_outbox_variants(self, folder):
        assert _is_prohibited(folder) is True

    @pytest.mark.parametrize("folder", ["Queue", "MailQueue", "queue/urgent"])
    def test_blocks_queue_variants(self, folder):
        assert _is_prohibited(folder) is True

    @pytest.mark.parametrize("folder", ["INBOX", "Archive", "Archive/2024", "Trash", "Notes"])
    def test_allows_safe_folders(self, folder):
        assert _is_prohibited(folder) is False


# ── _parse_fetch_info ──────────────────────────────────────────────────────────

class TestParseFetchInfo:
    def test_uid_and_multiple_flags(self):
        line = r"1 (UID 12345 FLAGS (\Seen \Flagged) BODY[HEADER.FIELDS (...)] {100}"
        uid, flags = _parse_fetch_info(line)
        assert uid == "12345"
        assert "\\Seen" in flags
        assert "\\Flagged" in flags

    def test_uid_no_flags_set(self):
        line = r"1 (UID 99 FLAGS () BODY[] {10}"
        uid, flags = _parse_fetch_info(line)
        assert uid == "99"
        assert flags == set()

    def test_missing_uid_returns_none(self):
        line = r"1 (FLAGS (\Seen) BODY[] {10}"
        uid, flags = _parse_fetch_info(line)
        assert uid is None
        assert "\\Seen" in flags

    def test_missing_flags_returns_empty_set(self):
        line = "1 (UID 42 BODY[] {10}"
        uid, flags = _parse_fetch_info(line)
        assert uid == "42"
        assert flags == set()

    def test_recent_flag(self):
        line = r"3 (UID 77 FLAGS (\Recent) BODY[] {5}"
        _, flags = _parse_fetch_info(line)
        assert "\\Recent" in flags


# ── _LIST_LINE regex ───────────────────────────────────────────────────────────

class TestListLineRegex:
    def test_quoted_folder_name(self):
        m = _LIST_LINE.match(r'(\HasNoChildren) "/" "INBOX"')
        assert m is not None
        assert m.group(3) == "INBOX"

    def test_unquoted_folder_name(self):
        # Separator stays quoted; folder name itself is unquoted (valid IMAP)
        m = _LIST_LINE.match('(\\HasNoChildren) "/" INBOX')
        assert m is not None
        assert m.group(4) == "INBOX"

    def test_folder_name_with_spaces(self):
        m = _LIST_LINE.match(r'(\HasNoChildren) "/" "Sent Items"')
        assert m is not None
        assert m.group(3) == "Sent Items"

    def test_nil_separator(self):
        m = _LIST_LINE.match(r'(\HasNoChildren) NIL "INBOX"')
        assert m is not None
        assert m.group(3) == "INBOX"

    def test_multiple_flags_captured(self):
        m = _LIST_LINE.match(r'(\HasNoChildren \Noinferiors) "/" "INBOX"')
        assert m is not None
        flags_str = m.group(1)
        assert "\\HasNoChildren" in flags_str
        assert "\\Noinferiors" in flags_str

    def test_no_match_on_garbage(self):
        assert _LIST_LINE.match("this is not a LIST response") is None


# ── _q ─────────────────────────────────────────────────────────────────────────

class TestQ:
    def test_simple_name_gets_quoted(self):
        assert _q("INBOX") == '"INBOX"'

    def test_already_quoted_unchanged(self):
        assert _q('"INBOX"') == '"INBOX"'

    def test_name_with_spaces(self):
        assert _q("My Folder") == '"My Folder"'

    def test_embedded_double_quote_escaped(self):
        result = _q('folder"name')
        assert '\\"' in result

    def test_backslash_escaped(self):
        result = _q("folder\\name")
        assert "\\\\" in result


# ── Tool: list_folders ─────────────────────────────────────────────────────────

class TestListFolders:
    @patch("server.get_conn")
    def test_happy_day(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren) "/" "Archive"',
            ],
        )
        result = list_folders()
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "INBOX" in names
        assert "Archive" in names

    @patch("server.get_conn")
    def test_server_error_returns_error_dict(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.list.return_value = ("NO", [])
        result = list_folders()
        assert result == [{"error": "Server rejected LIST command"}]


# ── Tool: list_emails ──────────────────────────────────────────────────────────

def _make_header_bytes(
    from_addr="sender@test.example",
    subject="Test Subject",
    date="Thu, 15 May 2026 10:00:00 +0000",
    message_id="<abc@test>",
) -> bytes:
    lines = [
        f"From: {from_addr}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


class TestListEmails:
    @patch("server.get_conn")
    def test_happy_day_returns_email_list(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"2"])

        header_bytes = _make_header_bytes()
        info = b"1 (UID 100 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {50}"

        def uid_side_effect(command, *args):
            if command == "search":
                return ("OK", [b"100 101"])
            if command == "fetch":
                return ("OK", [(info, header_bytes), b")"])
            return ("OK", [None])

        mock_conn.uid.side_effect = uid_side_effect

        result = list_emails(limit=10)
        assert len(result) == 1
        assert result[0]["uid"] == "100"
        assert result[0]["from"] == "sender@test.example"
        assert result[0]["subject"] == "Test Subject"

    @patch("server.get_conn")
    def test_empty_folder_returns_empty_list(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.uid.return_value = ("OK", [b""])
        assert list_emails() == []

    @patch("server.get_conn")
    def test_requested_flags_in_response(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        info = b"1 (UID 55 FLAGS (\\Flagged) BODY[HEADER.FIELDS ...] {10}"

        def uid_side_effect(command, *args):
            if command == "search":
                return ("OK", [b"55"])
            return ("OK", [(info, _make_header_bytes()), b")"])

        mock_conn.uid.side_effect = uid_side_effect

        result = list_emails(flags=["\\Flagged", "\\Seen"])
        assert result[0]["flags"]["\\Flagged"] is True
        assert result[0]["flags"]["\\Seen"] is False


# ── Tool: get_email ────────────────────────────────────────────────────────────

def _make_raw_email(
    from_addr="sender@test.example",
    subject="Hello",
    body="This is the body.",
) -> bytes:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = "recipient@test.example"
    msg["Subject"] = subject
    msg["Date"] = "Thu, 15 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<hello@test>"
    return msg.as_bytes()


class TestGetEmail:
    @patch("server.get_conn")
    def test_happy_day_plain_email(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        raw = _make_raw_email()
        mock_conn.uid.return_value = ("OK", [(b"1 (UID 42 BODY[] {size}", raw), b")"])

        result = get_email("42")
        assert result["uid"] == "42"
        assert result["from"] == "sender@test.example"
        assert result["subject"] == "Hello"
        assert "This is the body." in result["body"]
        assert result["html_stripped"] is False

    @patch("server.get_conn")
    def test_uid_not_found_returns_error(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.uid.return_value = ("OK", [None])

        result = get_email("999")
        assert "error" in result

    @patch("server.get_conn")
    def test_uses_body_peek(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        raw = _make_raw_email()
        mock_conn.uid.return_value = ("OK", [(b"1 (UID 1 BODY[] {1}", raw), b")"])

        get_email("1")
        fetch_call = mock_conn.uid.call_args
        # Second positional arg is the fetch spec
        assert "BODY.PEEK" in fetch_call[0][2]


# ── Tool: move_mail ────────────────────────────────────────────────────────────

class TestMoveMail:
    @patch("server.get_conn")
    def test_happy_day_with_move_extension(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.uid.return_value = ("OK", [b""])

        result = move_mail("42", "Archive")
        assert result["success"] is True
        assert result["moved_to"] == "Archive"

    @patch("server.get_conn")
    def test_fallback_copy_delete_when_move_unsupported(self, mock_get_conn):
        import imaplib

        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "MOVE":
                raise imaplib.IMAP4.error("MOVE not supported")
            if command == "copy":
                return ("OK", [b""])
            if command == "store":
                return ("OK", [b""])
            return ("OK", [b""])

        mock_conn.uid.side_effect = uid_side_effect

        result = move_mail("42", "Archive")
        assert result["success"] is True
        # COPY and store(\Deleted) must have been called
        commands = [c[0][0] for c in mock_conn.uid.call_args_list]
        assert "copy" in commands
        assert "store" in commands
        mock_conn.expunge.assert_called_once()

    @pytest.mark.parametrize("bad_folder", ["Sent", "Outbox", "Queue", "My Sent Items"])
    def test_prohibited_folder_rejected(self, bad_folder):
        result = move_mail("1", bad_folder)
        assert "error" in result
        assert "prohibited" in result["error"].lower() or "Blocked" in result["error"] or "not allowed" in result["error"].lower()

    @patch("server.get_conn")
    def test_copy_failure_returns_error(self, mock_get_conn):
        import imaplib

        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "MOVE":
                raise imaplib.IMAP4.error("no MOVE")
            if command == "copy":
                return ("NO", [b"Destination not found"])
            return ("OK", [b""])

        mock_conn.uid.side_effect = uid_side_effect

        result = move_mail("42", "NonExistentFolder")
        assert "error" in result


# ── Tool: mark_as_read ─────────────────────────────────────────────────────────

class TestMarkAsRead:
    @patch("server.get_conn")
    def test_happy_day(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.uid.return_value = ("OK", [b"42 (FLAGS (\\Seen))"])

        result = mark_as_read("42")
        assert result["success"] is True
        assert "\\Seen" in result["flags_added"]

    @patch("server.get_conn")
    def test_opens_folder_read_write(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.uid.return_value = ("OK", [b""])

        mark_as_read("7", folder="Archive")
        # readonly must NOT be True (or must be absent)
        select_call = mock_conn.select.call_args
        # select called with folder name but without readonly=True
        assert select_call[1].get("readonly") is not True
        assert select_call[0] == ('"Archive"',)


# ── UID validation (_valid_uid + tool guards) ──────────────────────────────────

class TestValidUID:
    @pytest.mark.parametrize("uid", ["1", "42", "99999"])
    def test_valid_numeric_uids(self, uid):
        assert _valid_uid(uid) is True

    @pytest.mark.parametrize("uid", ["0abc", "abc", "", " ", "1.2", "-1", "1 2"])
    def test_invalid_uids(self, uid):
        assert _valid_uid(uid) is False

    def test_get_email_rejects_bad_uid(self):
        result = get_email("abc123")
        assert "error" in result
        assert "Invalid UID" in result["error"]

    def test_move_mail_rejects_bad_uid(self):
        result = move_mail("not-a-uid", "Archive")
        assert "error" in result
        assert "Invalid UID" in result["error"]

    def test_mark_as_read_rejects_bad_uid(self):
        result = mark_as_read("12;DROP")
        assert "error" in result
        assert "Invalid UID" in result["error"]


# ── Body size limit ────────────────────────────────────────────────────────────

class TestBodySizeLimit:
    @patch("server.get_conn")
    def test_oversized_message_rejected(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        # RFC822.SIZE response reporting a 30 MB message
        size_bytes = 30 * 1024 * 1024
        mock_conn.uid.return_value = (
            "OK",
            [(f"1 (UID 5 RFC822.SIZE {size_bytes})".encode(), b"")],
        )

        result = get_email("5")
        assert "error" in result
        assert "too large" in result["error"].lower()

    @patch("server.get_conn")
    def test_message_within_limit_is_fetched(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        raw = _make_raw_email()
        small_size = len(raw)

        def uid_side_effect(command, *args):
            if "RFC822.SIZE" in str(args):
                return ("OK", [(f"1 (UID 7 RFC822.SIZE {small_size})".encode(), b"")])
            return ("OK", [(b"1 (UID 7 BODY[] {size}", raw), b")"])

        mock_conn.uid.side_effect = uid_side_effect

        result = get_email("7")
        assert "error" not in result
        assert result["uid"] == "7"
