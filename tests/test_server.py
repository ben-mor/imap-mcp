"""
Tests for server.py.

Parsing helpers are tested thoroughly.
MCP tool functions are covered with happy-day scenarios and the critical
unhappy paths (bad UIDs, prohibited folders, missing messages, etc.).

The "test" mailbox is injected by conftest.py; all IMAP connections are
patched so no live server is needed.  Rules tests use an in-memory SQLite
database (also from conftest.py) so they leave no files on disk.
"""

import email
import os
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

from server import (  # noqa: E402
    _LIST_LINE,
    _decode_bytes,
    _decode_header,
    _decode_rfc2047_fallback,
    _extract_body,
    _is_prohibited,
    _parse_appenduid,
    _parse_copyuid,
    _parse_fetch_info,
    _q,
    _strip_html,
    _valid_uid,
    add_rule,
    change_rule,
    get_email,
    get_rules,
    list_emails,
    list_folders,
    list_mailboxes,
    mark_as_read,
    move_mail,
    move_mail_cross,
    remove_rule,
    search_emails,
)

# Prohibited patterns used by the "test" mailbox (mirrors conftest injection).
_TEST_PROHIBITED = ["Sent", "Outbox", "Queue"]


# ── _decode_header ─────────────────────────────────────────────────────────────

class TestDecodeRFC2047Fallback:
    def test_qp_with_raw_latin1_chars(self):
        raw = "=?iso-8859-1?Q?Erg=E4nzung_f=FCr?="
        assert "Ergänzung" in _decode_rfc2047_fallback(raw)
        assert "für" in _decode_rfc2047_fallback(raw)

    def test_qp_underscore_becomes_space(self):
        result = _decode_rfc2047_fallback("=?utf-8?Q?hello_world?=")
        assert result == "hello world"

    def test_base64_encoded_word_returned_unchanged(self):
        import base64 as _b64
        encoded = _b64.b64encode("Héllo".encode("utf-8")).decode()
        raw = f"=?utf-8?B?{encoded}?="
        assert _decode_rfc2047_fallback(raw) == raw

    def test_unparseable_word_returned_unchanged(self):
        raw = "=?bogus?X?garbage?="
        assert isinstance(_decode_rfc2047_fallback(raw), str)

    def test_plain_text_passthrough(self):
        assert _decode_rfc2047_fallback("no encoding here") == "no encoding here"

    def test_mixed_encoded_and_plain(self):
        result = _decode_rfc2047_fallback("Hello =?utf-8?Q?W=C3=B6rld?= !")
        assert "Wörld" in result
        assert "Hello" in result

    def test_decode_header_calls_fallback_for_raw_encoded_word(self):
        subject = "=?iso-8859-1?Q?Die_smarte_Erg\xe4nzung_f\xfcr_Sie?="
        result = _decode_header(subject)
        assert "Ergänzung" in result
        assert "für" in result
        assert "=?" not in result

    def test_decode_header_via_message_from_bytes(self):
        # Regression: email.message_from_bytes wraps non-ASCII header bytes in a
        # Header object with charset 'unknown-8bit'; decode_header returns the
        # encoded word as *bytes* — the fallback must still fire.
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
        result = _decode_bytes(b"caf\xe9", "unknown-8bit")
        assert result == "café"

    def test_other_unknown_charset_names(self):
        for bogus in ("x-unknown", "unknown", "ansi"):
            assert _decode_bytes(b"test", bogus) == "test"

    def test_bad_bytes_replaced_not_raised(self):
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
        assert _decode_header("=?utf-8?b?SGVsbG8=?=") == "Hello"

    def test_rfc2047_quoted_printable_latin1(self):
        assert _decode_header("=?iso-8859-1?q?H=E9llo?=") == "Héllo"

    def test_mixed_encoded_and_literal(self):
        result = _decode_header("=?utf-8?b?SGVsbG8=?= World")
        assert "Hello" in result and "World" in result

    def test_multiple_encoded_words(self):
        result = _decode_header("=?utf-8?b?Rmly?= =?utf-8?b?c3Q=?=")
        assert "Fir" in result and "st" in result


# ── _strip_html ────────────────────────────────────────────────────────────────

class TestStripHTML:
    def test_removes_simple_tags(self):
        result = _strip_html("<p>Hello</p>")
        assert "Hello" in result and "<" not in result

    def test_decodes_html_entities(self):
        result = _strip_html("A &amp; B &lt;thing&gt;")
        assert "&amp;" not in result and "&" in result and "<thing>" in result

    def test_nested_tags(self):
        result = _strip_html("<div><p><b>Bold</b> and normal</p></div>")
        assert "Bold" in result and "and normal" in result and "<" not in result

    def test_collapses_horizontal_whitespace(self):
        assert "   " not in _strip_html("<p>Hello   world</p>")

    def test_collapses_excess_blank_lines(self):
        result = _strip_html("<p>A</p>\n\n\n\n<p>B</p>")
        assert result.count("\n") <= 2

    def test_empty_input(self):
        assert _strip_html("") == ""

    def test_plain_text_passthrough(self):
        assert _strip_html("Just plain text") == "Just plain text"

    def test_anchor_tag_text_preserved(self):
        result = _strip_html('<a href="http://example.com">Click here</a>')
        assert "Click here" in result and "http" not in result


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
        assert "Hello" in result["body"] and "<" not in result["body"]
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
        meta = result["attachments"][0]
        assert meta["filename"] == "report.pdf"
        assert meta["content_type"] == "application/pdf"
        assert meta["size_bytes"] == len(b"pdfdata")

    def test_attachment_without_explicit_disposition(self):
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText("body", "plain", "utf-8"))
        att = MIMEBase("image", "png")
        att.add_header("Content-Disposition", "inline", filename="photo.png")
        att.set_payload(b"\x89PNG")
        msg.attach(att)
        result = _extract_body(msg)
        assert any(a["filename"] == "photo.png" for a in result["attachments"])

    def test_empty_message_returns_empty_body(self):
        result = _extract_body(email.message.Message())
        assert result["body"] == "" and result["html_stripped"] is False

    def test_charset_decoding_utf8(self):
        msg = MIMEText("Ünïcödé", "plain", "utf-8")
        assert "Ünïcödé" in _extract_body(msg)["body"]

    def test_charset_decoding_latin1(self):
        raw = "caf\xe9".encode("latin-1")
        msg = email.message.Message()
        msg["Content-Type"] = "text/plain; charset=iso-8859-1"
        msg["Content-Transfer-Encoding"] = "8bit"
        msg.set_payload(raw)
        assert "café" in _extract_body(msg)["body"]


# ── _is_prohibited ─────────────────────────────────────────────────────────────

class TestIsProhibited:
    @pytest.mark.parametrize("folder", ["Sent", "SENT", "Sent Items", "sent"])
    def test_blocks_sent_variants(self, folder):
        assert _is_prohibited(folder, _TEST_PROHIBITED) is True

    @pytest.mark.parametrize("folder", ["Outbox", "OUTBOX", "outbox"])
    def test_blocks_outbox_variants(self, folder):
        assert _is_prohibited(folder, _TEST_PROHIBITED) is True

    @pytest.mark.parametrize("folder", ["Queue", "MailQueue", "queue/urgent"])
    def test_blocks_queue_variants(self, folder):
        assert _is_prohibited(folder, _TEST_PROHIBITED) is True

    @pytest.mark.parametrize("folder", ["INBOX", "Archive", "Archive/2024", "Trash", "Notes"])
    def test_allows_safe_folders(self, folder):
        assert _is_prohibited(folder, _TEST_PROHIBITED) is False


# ── _parse_fetch_info ──────────────────────────────────────────────────────────

class TestParseFetchInfo:
    def test_uid_and_multiple_flags(self):
        uid, flags = _parse_fetch_info(
            r"1 (UID 12345 FLAGS (\Seen \Flagged) BODY[HEADER.FIELDS (...)] {100}"
        )
        assert uid == "12345"
        assert "\\Seen" in flags and "\\Flagged" in flags

    def test_uid_no_flags_set(self):
        uid, flags = _parse_fetch_info(r"1 (UID 99 FLAGS () BODY[] {10}")
        assert uid == "99" and flags == set()

    def test_missing_uid_returns_none(self):
        uid, flags = _parse_fetch_info(r"1 (FLAGS (\Seen) BODY[] {10}")
        assert uid is None and "\\Seen" in flags

    def test_missing_flags_returns_empty_set(self):
        uid, flags = _parse_fetch_info("1 (UID 42 BODY[] {10}")
        assert uid == "42" and flags == set()

    def test_recent_flag(self):
        _, flags = _parse_fetch_info(r"3 (UID 77 FLAGS (\Recent) BODY[] {5}")
        assert "\\Recent" in flags


# ── _LIST_LINE regex ───────────────────────────────────────────────────────────

class TestListLineRegex:
    def test_quoted_folder_name(self):
        m = _LIST_LINE.match(r'(\HasNoChildren) "/" "INBOX"')
        assert m is not None and m.group(3) == "INBOX"

    def test_unquoted_folder_name(self):
        m = _LIST_LINE.match('(\\HasNoChildren) "/" INBOX')
        assert m is not None and m.group(4) == "INBOX"

    def test_folder_name_with_spaces(self):
        m = _LIST_LINE.match(r'(\HasNoChildren) "/" "Sent Items"')
        assert m is not None and m.group(3) == "Sent Items"

    def test_nil_separator(self):
        m = _LIST_LINE.match(r'(\HasNoChildren) NIL "INBOX"')
        assert m is not None and m.group(3) == "INBOX"

    def test_multiple_flags_captured(self):
        m = _LIST_LINE.match(r'(\HasNoChildren \Noinferiors) "/" "INBOX"')
        assert m is not None
        assert "\\HasNoChildren" in m.group(1) and "\\Noinferiors" in m.group(1)

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
        assert '\\"' in _q('folder"name')

    def test_backslash_escaped(self):
        assert "\\\\" in _q("folder\\name")


# ── _parse_copyuid / _parse_appenduid ─────────────────────────────────────────

class TestParseCopyUID:
    def test_parses_single_dest_uid(self):
        assert _parse_copyuid([b"[COPYUID 1620000000 42 99] Move completed"]) == "99"

    def test_parses_uid_range(self):
        assert _parse_copyuid([b"[COPYUID 1620000000 1:3 10:12] Done"]) == "10:12"

    def test_returns_none_when_absent(self):
        assert _parse_copyuid([b"Move completed"]) is None

    def test_returns_none_for_empty_data(self):
        assert _parse_copyuid([]) is None
        assert _parse_copyuid([None]) is None

    def test_returns_none_for_empty_bytes(self):
        assert _parse_copyuid([b""]) is None


class TestParseAppendUID:
    def test_parses_appenduid(self):
        assert _parse_appenduid([b"[APPENDUID 1620000000 42] Append completed"]) == "42"

    def test_returns_none_when_absent(self):
        assert _parse_appenduid([b"Append completed"]) is None

    def test_returns_none_for_empty(self):
        assert _parse_appenduid([]) is None
        assert _parse_appenduid([b""]) is None


# ── Tool: list_mailboxes ───────────────────────────────────────────────────────

class TestListMailboxes:
    def test_returns_test_mailbox(self):
        result = list_mailboxes()
        names = [r["name"] for r in result]
        assert "test" in names

    def test_entry_has_required_keys(self):
        result = list_mailboxes()
        entry = next(r for r in result if r["name"] == "test")
        assert "host" in entry and "user" in entry and "default_folder" in entry

    def test_no_password_in_output(self):
        for entry in list_mailboxes():
            assert "password" not in entry


# ── Tool: list_folders ─────────────────────────────────────────────────────────

class TestListFolders:
    @patch("server.get_conn")
    def test_happy_day(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.list.return_value = (
            "OK",
            [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"'],
        )
        result = list_folders("test")
        names = [r["name"] for r in result]
        assert "INBOX" in names and "Archive" in names

    @patch("server.get_conn")
    def test_server_error_returns_error_dict(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.list.return_value = ("NO", [])
        result = list_folders("test")
        assert result == [{"error": "Server rejected LIST command"}]

    def test_unknown_mailbox_returns_error(self):
        result = list_folders("no_such_mailbox")
        assert result[0].get("error")


# ── Tool: list_emails ──────────────────────────────────────────────────────────

def _make_header_bytes(
    from_addr="sender@test.example",
    subject="Test Subject",
    date="Thu, 15 May 2026 10:00:00 +0000",
    message_id="<abc@test>",
) -> bytes:
    lines = [
        f"From: {from_addr}", f"Subject: {subject}",
        f"Date: {date}", f"Message-ID: {message_id}", "", "",
    ]
    return "\r\n".join(lines).encode()


class TestListEmails:
    @patch("server.get_conn")
    def test_happy_day_uses_sort_when_available(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "SORT")
        mock_conn.select.return_value = ("OK", [b"2"])
        info = b"1 (UID 100 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {50}"

        def uid_side_effect(command, *args):
            if command == "sort":   return ("OK", [b"100 101"])
            if command == "fetch":  return ("OK", [(info, _make_header_bytes()), b")"])
            return ("OK", [None])

        mock_conn.uid.side_effect = uid_side_effect
        result = list_emails("test", limit=10)
        assert mock_conn.uid.call_args_list[0][0][0] == "sort"
        assert result[0]["uid"] == "100"

    @patch("server.get_conn")
    def test_falls_back_to_search_without_sort(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1",)
        mock_conn.select.return_value = ("OK", [b"2"])
        info = b"1 (UID 100 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {50}"

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"100 101"])
            if command == "fetch":  return ("OK", [(info, _make_header_bytes()), b")"])
            return ("OK", [None])

        mock_conn.uid.side_effect = uid_side_effect
        result = list_emails("test", limit=10)
        assert mock_conn.uid.call_args_list[0][0][0] == "search"
        assert result[0]["uid"] == "100"

    @patch("server.get_conn")
    def test_empty_folder_returns_empty_list(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "SORT")
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.uid.return_value = ("OK", [b""])
        assert list_emails("test") == []

    @patch("server.get_conn")
    def test_requested_flags_in_response(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "SORT")
        mock_conn.select.return_value = ("OK", [b"1"])
        info = b"1 (UID 55 FLAGS (\\Flagged) BODY[HEADER.FIELDS ...] {10}"

        def uid_side_effect(command, *args):
            if command == "sort": return ("OK", [b"55"])
            return ("OK", [(info, _make_header_bytes()), b")"])

        mock_conn.uid.side_effect = uid_side_effect
        result = list_emails("test", flags=["\\Flagged", "\\Seen"])
        assert result[0]["flags"]["\\Flagged"] is True
        assert result[0]["flags"]["\\Seen"] is False

    def test_unknown_mailbox_returns_error(self):
        result = list_emails("no_such_mailbox")
        assert result[0].get("error")


# ── Tool: get_email ────────────────────────────────────────────────────────────

def _make_raw_email(
    from_addr="sender@test.example",
    subject="Hello",
    body="This is the body.",
) -> bytes:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"]   = "recipient@test.example"
    msg["Subject"] = subject
    msg["Date"]    = "Thu, 15 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<hello@test>"
    return msg.as_bytes()


class TestGetEmail:
    @patch("server.get_conn")
    def test_happy_day_plain_email(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        raw = _make_raw_email()

        def uid_side_effect(command, *args):
            if "RFC822.SIZE" in str(args):
                return ("OK", [(f"1 (UID 42 RFC822.SIZE {len(raw)})".encode(), b"")])
            return ("OK", [(b"1 (UID 42 BODY[] {size}", raw), b")"])

        mock_conn.uid.side_effect = uid_side_effect
        result = get_email("test", "42")
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
        assert "error" in get_email("test", "999")

    @patch("server.get_conn")
    def test_uses_body_peek(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        raw = _make_raw_email()
        mock_conn.uid.return_value = ("OK", [(b"1 (UID 1 BODY[] {1}", raw), b")"])
        get_email("test", "1")
        assert "BODY.PEEK" in mock_conn.uid.call_args[0][2]


# ── Tool: mark_as_read ─────────────────────────────────────────────────────────

class TestMarkAsRead:
    @patch("server.get_conn")
    def test_happy_day(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.uid.return_value = ("OK", [b"42 (FLAGS (\\Seen))"])
        result = mark_as_read("test", "42")
        assert result["success"] is True
        assert "\\Seen" in result["flags_added"]

    @patch("server.get_conn")
    def test_opens_folder_read_write(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.uid.return_value = ("OK", [b""])
        mark_as_read("test", "7", folder="Archive")
        select_call = mock_conn.select.call_args
        assert select_call[1].get("readonly") is not True
        assert select_call[0] == ('"Archive"',)


# ── Tool: move_mail ────────────────────────────────────────────────────────────

class TestMoveMail:
    @patch("server.get_conn")
    def test_happy_day_with_move_extension(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "UIDPLUS", "MOVE")
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"42"])
            return ("OK", [b"Move completed"])

        mock_conn.uid.side_effect = uid_side_effect
        result = move_mail("test", "42", "Archive")
        assert result["success"] is True and result["moved_to"] == "Archive"

    @patch("server.get_conn")
    def test_fallback_copy_delete_when_move_unsupported(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "UIDPLUS")
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"42"])
            if command == "copy":   return ("OK", [b"Copy completed"])
            return ("OK", [b""])

        mock_conn.uid.side_effect = uid_side_effect
        result = move_mail("test", "42", "Archive")
        assert result["success"] is True
        commands = [c[0][0] for c in mock_conn.uid.call_args_list]
        assert "copy" in commands and "store" in commands
        mock_conn.expunge.assert_called_once()

    @pytest.mark.parametrize("bad_folder", ["Sent", "Outbox", "Queue", "My Sent Items"])
    def test_prohibited_folder_rejected(self, bad_folder):
        result = move_mail("test", "1", bad_folder)
        assert "error" in result
        assert "prohibited" in result["error"].lower() or "Blocked" in result["error"]

    @patch("server.get_conn")
    def test_copy_failure_returns_error(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1",)   # no MOVE → COPY fallback
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"42"])
            if command == "copy":   return ("NO", [b"Destination not found"])
            return ("OK", [b""])

        mock_conn.uid.side_effect = uid_side_effect
        assert "error" in move_mail("test", "42", "NonExistentFolder")

    def test_unknown_mailbox_returns_error(self):
        assert "error" in move_mail("no_such_mailbox", "1", "Archive")


# ── Tool: move_mail — UIDPLUS / pre-check variants ────────────────────────────

class TestMoveMailUIDPLUS:
    @patch("server.get_conn")
    def test_move_with_uidplus_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "UIDPLUS", "MOVE")
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"42"])
            return ("OK", [b"Move completed"])

        mock_conn.uid.side_effect = uid_side_effect
        result = move_mail("test", "42", "Archive")
        assert result["success"] is True and result["moved_to"] == "Archive"

    @patch("server.get_conn")
    def test_move_uid_not_found_returns_error(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "UIDPLUS", "MOVE")
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.uid.return_value = ("OK", [b""])   # empty search → not found
        result = move_mail("test", "42", "Archive")
        assert "error" in result and "not found" in result["error"].lower()

    @patch("server.get_conn")
    def test_move_without_uidplus_precheck_passes(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "MOVE")
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"42"])
            return ("OK", [b"Move completed"])

        mock_conn.uid.side_effect = uid_side_effect
        assert move_mail("test", "42", "Archive")["success"] is True

    @patch("server.get_conn")
    def test_move_without_uidplus_precheck_fails(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "MOVE")
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b""])
            return ("OK", [b"Move completed"])

        mock_conn.uid.side_effect = uid_side_effect
        result = move_mail("test", "42", "Archive")
        assert "error" in result and "not found" in result["error"].lower()

    @patch("server.get_conn")
    def test_copy_fallback_without_move_capability(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.capabilities = ("IMAP4REV1", "UIDPLUS")
        mock_conn.select.return_value = ("OK", [b"1"])

        def uid_side_effect(command, *args):
            if command == "search": return ("OK", [b"42"])
            if command == "copy":   return ("OK", [b"Copy completed"])
            return ("OK", [b""])

        mock_conn.uid.side_effect = uid_side_effect
        result = move_mail("test", "42", "Archive")
        assert result["success"] is True and result["moved_to"] == "Archive"
        mock_conn.expunge.assert_called_once()


# ── Tool: move_mail_cross ──────────────────────────────────────────────────────

class TestMoveMailCross:
    @patch("server.get_conn")
    def test_happy_day_cross_mailbox(self, mock_get_conn):
        # Add a second mailbox config for this test
        import server as srv
        srv._mailbox_configs["test2"] = {**srv._mailbox_configs["test"],
                                          "host": "imap2.test.example"}

        src_conn = MagicMock()
        dst_conn = MagicMock()

        def get_conn_side_effect(mailbox):
            return src_conn if mailbox == "test" else dst_conn

        mock_get_conn.side_effect = get_conn_side_effect
        src_conn.select.return_value = ("OK", [b"1"])

        def src_uid(command, *args):
            if command == "search": return ("OK", [b"42"])
            if command == "fetch":  return ("OK", [(b"1 (UID 42 RFC822 {4}", b"data"), b")"])
            return ("OK", [b""])

        src_conn.uid.side_effect = src_uid
        dst_conn.append.return_value = ("OK", [b"[APPENDUID 1620000000 99] Done"])
        src_conn.expunge.return_value = ("OK", [b""])

        result = move_mail_cross("test", "42", "INBOX", "test2", "Archive")
        assert result["success"] is True
        assert result["new_uid"] == "99"
        assert result["src_uid"] == "42"
        dst_conn.append.assert_called_once()

    def test_unknown_src_mailbox(self):
        result = move_mail_cross("no_such", "42", "INBOX", "test", "Archive")
        assert "error" in result

    def test_unknown_dst_mailbox(self):
        result = move_mail_cross("test", "42", "INBOX", "no_such", "Archive")
        assert "error" in result

    def test_prohibited_dst_folder(self):
        import server as srv
        srv._mailbox_configs["test2"] = {**srv._mailbox_configs["test"]}
        result = move_mail_cross("test", "42", "INBOX", "test2", "Sent")
        assert "error" in result and "prohibited" in result["error"].lower()


# ── Tool: search_emails ────────────────────────────────────────────────────────

class TestSearchEmails:
    @patch("server.get_conn")
    def test_happy_day_unseen(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"10"])
        mock_conn.uid.return_value = ("OK", [b"101 102 103"])
        result = search_emails("test", ["UNSEEN"])
        assert result["count"] == 3 and result["uids"] == ["101", "102", "103"]

    @patch("server.get_conn")
    def test_no_matches_returns_empty(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.uid.return_value = ("OK", [b""])
        result = search_emails("test", ["FROM", "nobody@example.com"])
        assert result["count"] == 0 and result["uids"] == []

    @patch("server.get_conn")
    def test_criteria_tokens_passed_as_separate_args(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"5"])
        mock_conn.uid.return_value = ("OK", [b"55"])
        search_emails("test", ["HEADER", "Message-ID", "<abc@test>"])
        call_args = mock_conn.uid.call_args[0]
        assert call_args[2] == "HEADER"
        assert call_args[3] == "Message-ID"
        assert call_args[4] == "<abc@test>"

    def test_empty_criteria_returns_error(self):
        assert "error" in search_emails("test", [])

    def test_unknown_mailbox_returns_error(self):
        assert "error" in search_emails("no_such_mailbox", ["UNSEEN"])


# ── UID validation ─────────────────────────────────────────────────────────────

class TestValidUID:
    @pytest.mark.parametrize("uid", ["1", "42", "99999"])
    def test_valid_numeric_uids(self, uid):
        assert _valid_uid(uid) is True

    @pytest.mark.parametrize("uid", ["0abc", "abc", "", " ", "1.2", "-1", "1 2"])
    def test_invalid_uids(self, uid):
        assert _valid_uid(uid) is False

    def test_get_email_rejects_bad_uid(self):
        result = get_email("test", "abc123")
        assert "error" in result and "Invalid UID" in result["error"]

    def test_move_mail_rejects_bad_uid(self):
        result = move_mail("test", "not-a-uid", "Archive")
        assert "error" in result and "Invalid UID" in result["error"]

    def test_mark_as_read_rejects_bad_uid(self):
        result = mark_as_read("test", "12;DROP")
        assert "error" in result and "Invalid UID" in result["error"]


# ── Body size limit ────────────────────────────────────────────────────────────

class TestBodySizeLimit:
    @patch("server.get_conn")
    def test_oversized_message_rejected(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        size_bytes = 30 * 1024 * 1024
        mock_conn.uid.return_value = (
            "OK", [(f"1 (UID 5 RFC822.SIZE {size_bytes})".encode(), b"")],
        )
        result = get_email("test", "5")
        assert "error" in result and "too large" in result["error"].lower()

    @patch("server.get_conn")
    def test_message_within_limit_is_fetched(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        raw = _make_raw_email()

        def uid_side_effect(command, *args):
            if "RFC822.SIZE" in str(args):
                return ("OK", [(f"1 (UID 7 RFC822.SIZE {len(raw)})".encode(), b"")])
            return ("OK", [(b"1 (UID 7 BODY[] {size}", raw), b")"])

        mock_conn.uid.side_effect = uid_side_effect
        result = get_email("test", "7")
        assert "error" not in result and result["uid"] == "7"


# ── Rules CRUD ─────────────────────────────────────────────────────────────────

class TestRules:
    # add_rule / get_rules

    def test_add_rule_returns_id(self):
        result = add_rule("test", "My Rule", "Do X", "claude", "testing")
        assert result["success"] is True
        assert isinstance(result["id"], int)

    def test_get_rules_returns_added_rule(self):
        add_rule("test", "Rule A", "Body A", "claude", "init")
        rules = get_rules("test")
        assert rules["count"] == 1
        assert rules["rules"][0]["name"] == "Rule A"
        assert rules["rules"][0]["rule"] == "Body A"

    def test_rule_order_default_is_100(self):
        add_rule("test", "R", "body", "claude", "r")
        assert get_rules("test")["rules"][0]["rule_order"] == 100

    def test_rule_order_custom(self):
        add_rule("test", "Low",  "body", "claude", "r", rule_order=10)
        add_rule("test", "High", "body", "claude", "r", rule_order=200)
        names = [r["name"] for r in get_rules("test")["rules"]]
        assert names[0] == "Low" and names[1] == "High"

    def test_ids_are_unique(self):
        r1 = add_rule("test", "R1", "b", "claude", "r")
        r2 = add_rule("test", "R2", "b", "claude", "r")
        assert r1["id"] != r2["id"]

    def test_get_rules_unknown_mailbox(self):
        assert "error" in get_rules("no_such_mailbox")

    # change_rule

    def test_change_rule_name(self):
        rule_id = add_rule("test", "Old Name", "body", "claude", "create")["id"]
        change_rule("test", rule_id, "claude", "rename", name="New Name")
        rules = get_rules("test")["rules"]
        assert rules[0]["name"] == "New Name"
        assert rules[0]["rule"] == "body"   # unchanged

    def test_change_rule_body(self):
        rule_id = add_rule("test", "Name", "old body", "claude", "create")["id"]
        change_rule("test", rule_id, "claude", "update", rule="new body")
        assert get_rules("test")["rules"][0]["rule"] == "new body"

    def test_change_rule_order(self):
        rule_id = add_rule("test", "R", "b", "claude", "c")["id"]
        change_rule("test", rule_id, "claude", "reorder", rule_order=5)
        assert get_rules("test")["rules"][0]["rule_order"] == 5

    def test_change_rule_not_found(self):
        result = change_rule("test", 9999, "claude", "r", name="X")
        assert "error" in result

    def test_change_rule_nothing_to_update(self):
        rule_id = add_rule("test", "R", "b", "claude", "c")["id"]
        result = change_rule("test", rule_id, "claude", "noop")
        assert "error" in result

    # remove_rule

    def test_remove_rule_hides_from_get_rules(self):
        rule_id = add_rule("test", "Temp", "body", "claude", "create")["id"]
        remove_rule("test", rule_id, "claude", "no longer needed")
        assert get_rules("test")["count"] == 0

    def test_remove_rule_not_found(self):
        result = remove_rule("test", 9999, "claude", "reason")
        assert "error" in result

    def test_double_remove_returns_error(self):
        rule_id = add_rule("test", "R", "b", "claude", "c")["id"]
        remove_rule("test", rule_id, "claude", "first remove")
        result = remove_rule("test", rule_id, "claude", "second remove")
        assert "error" in result

    # Audit log

    def test_add_writes_audit_entry(self, _fresh_db):
        add_rule("test", "Audited", "body", "claude-test", "audit check")
        rows = _fresh_db.execute("SELECT * FROM audit").fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "add"
        assert rows[0]["caller"] == "claude-test"
        assert rows[0]["old_value"] is None
        assert rows[0]["new_value"] is not None

    def test_change_writes_audit_entry(self, _fresh_db):
        rule_id = add_rule("test", "R", "old", "claude", "create")["id"]
        change_rule("test", rule_id, "claude", "update reason", rule="new")
        rows = _fresh_db.execute(
            "SELECT * FROM audit ORDER BY seq"
        ).fetchall()
        assert len(rows) == 2
        change_row = rows[1]
        assert change_row["action"] == "change"
        assert change_row["reason"] == "update reason"
        assert "old" in change_row["old_value"]
        assert "new" in change_row["new_value"]

    def test_remove_writes_audit_entry(self, _fresh_db):
        rule_id = add_rule("test", "R", "b", "claude", "c")["id"]
        remove_rule("test", rule_id, "claude", "cleanup")
        rows = _fresh_db.execute(
            "SELECT * FROM audit WHERE action = 'remove'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["reason"] == "cleanup"
        assert rows[0]["new_value"] is None   # soft-delete: no new state

    def test_audit_table_has_no_deletes(self, _fresh_db):
        """The audit table should only ever grow — never shrink."""
        rule_id = add_rule("test", "R", "b", "c", "r")["id"]
        change_rule("test", rule_id, "c", "r2", name="R2")
        remove_rule("test", rule_id, "c", "r3")
        count = _fresh_db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        assert count == 3   # add + change + remove
