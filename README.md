# imap-mcp

A local MCP server that connects Claude Desktop to an IMAP mailbox.
Provides read-only mail access plus folder management (move, mark-as-read).
**Sending mail is intentionally not supported.**

## Tools

| Tool | Description |
|---|---|
| `list_folders` | List all IMAP folders/mailboxes |
| `list_emails` | List recent emails (sender, subject, date, UID, flags) |
| `get_email` | Fetch full email body by UID (plain text preferred, HTML stripped) |
| `search_emails` | Search a folder with raw IMAP SEARCH criteria tokens |
| `move_mail` | Move one or more messages to a folder; per-UID result with status |
| `mark_as_read` | Set the `\Seen` flag on a message |

### Dynamic flag fetching

`list_emails` accepts an optional `flags` parameter — a list of IMAP flag names
to report per message.  All flags are fetched in a single round-trip; only the
requested subset is returned.

```
list_emails(limit=30, flags=["\\Seen", "\\Flagged", "\\Answered"])
```

Default when omitted: `["\\Seen", "\\Flagged"]`.

### Searching

`search_emails` accepts a list of raw IMAP SEARCH tokens:

```
search_emails(["UNSEEN"])
search_emails(["FROM", "boss@example.com"])
search_emails(["SUBJECT", "invoice", "SINCE", "01-May-2026"])
search_emails(["FROM", "Hays"], folder="Trash")
```

Returns `{"uids": [...], "count": N}`.  Pass UIDs directly to `get_email` or
`move_mail`.

### Send-folder protection

`move_mail` rejects any destination folder whose name contains one of the
configured prohibited patterns (case-insensitive substring match).
Default blocked patterns: `Sent`, `Outbox`, `Queue`.
Add `Drafts` or your server's outgoing-queue name via `IMAP_PROHIBITED_FOLDERS`
in `.env`.

## Setup

### 1. Clone and install

```bash
git clone <repo> imap-mcp
cd imap-mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure mailboxes

Accounts are discovered automatically from `.config/`.  Create one
subdirectory per account — the directory name becomes the mailbox identifier
used in every tool call.

```bash
mkdir -p .config/personal
cp .config/example/connection.json .config/personal/connection.json
$EDITOR .config/personal/connection.json

# Repeat for additional accounts:
mkdir -p .config/work
cp .config/example/connection.json .config/work/connection.json
$EDITOR .config/work/connection.json
```

`connection.json` fields:

| Field | Default | Description |
|---|---|---|
| `host` | — | IMAP server hostname (required) |
| `port` | `993` | Port — 993 for implicit TLS, 143 for STARTTLS |
| `user` | — | Login username / email address (required) |
| `password` | — | Login password (required) |
| `use_ssl` | `true` | `false` → STARTTLS on port 143 |
| `folder` | `INBOX` | Default folder for all tools |
| `prohibited_folders` | `["Sent","Outbox","Queue"]` | Blocked move destinations |
| `timeout` | `30` | Socket timeout in seconds |
| `max_body_bytes` | `20971520` | Maximum message size `get_email` will fetch (20 MB) |

Both `mailboxes.toml` and `.config/` are git-ignored — credentials never
leave the local machine.

### 3. Test manually

```bash
source .venv/bin/activate
python server.py          # should start and wait; Ctrl-C to stop
```

If the server starts without errors the credentials are correct.

### 4. Configure Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json`
(create it if it does not exist):

```json
{
  "mcpServers": {
    "imap": {
      "command": "/absolute/path/to/imap-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/imap-mcp/server.py"]
    }
  }
}
```

Replace `/absolute/path/to/imap-mcp` with the real path, e.g.
`/home/yourname/projects/imap-mcp`.

Restart Claude Desktop.  The full tool set should appear in the tool picker.

## Architecture notes

- **Transport**: stdio (Claude Desktop spawns the process).
- **Connection**: single persistent `IMAP4_SSL` connection, reconnected
  automatically via `NOOP` health-check before each tool call.  Well-suited
  for burst usage (daily sort session) with long idle periods between runs.
- **Capabilities**: server capabilities are fetched once at connect time and
  cached for the session.  Used to select the best available code path for
  sorting (`SORT`) and moving (`MOVE`).
- **SORT**: `list_emails` uses `UID SORT (REVERSE DATE)` when the server
  advertises `SORT`; falls back to `UID SEARCH ALL` with reversed UID order.
- **BODY.PEEK**: `get_email` uses `BODY.PEEK[]` so fetching a message does
  **not** set `\Seen`.  Use `mark_as_read` explicitly.
- **Body size limit**: `get_email` pre-checks `RFC822.SIZE` before fetching;
  messages exceeding `IMAP_MAX_BODY_BYTES` are rejected with an error rather
  than downloaded.
- **MOVE**: `move_mail` performs a `UID SEARCH` pre-check to verify the
  message exists (IMAP servers silently return OK for non-existent UIDs).
  Then attempts the RFC 6851 atomic `UID MOVE` command; falls back to
  `COPY` + `\Deleted` + `EXPUNGE` on servers that do not advertise `MOVE`.
- **HTML stripping**: uses `html.parser` from the stdlib (`convert_charrefs=True`).
- **Encoding**: two-layer fallback for malformed headers — `_decode_bytes`
  catches unknown charset names (e.g. `unknown-8bit`) and falls back to
  latin-1; `_decode_rfc2047_fallback` handles RFC 2047 encoded words that
  survive standard decoding with raw 8-bit bytes inside QP payloads.

## Running the tests

```bash
source .venv/bin/activate
pytest tests/
```

All tests are unit tests using `unittest.mock`; no live IMAP connection required.

## Planned — Version 2

### Multi-mailbox architecture

**File layout**

```
imap-mcp/
  .config/
    personal/
      connection.json     ← credentials only; git-ignored, chmod 600
    work/
      connection.json
    imap-mcp.db           ← single SQLite for all mailboxes; git-ignored
```

Mailboxes are discovered automatically by globbing `.config/*/connection.json`.
The directory name is the mailbox identifier.  No separate registry file needed.

**SQLite schema**

Two tables; the audit table is append-only — no `UPDATE` or `DELETE` is ever
issued against it.

```sql
CREATE TABLE rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    rule        TEXT    NOT NULL,   -- markdown, arbitrarily long
    rule_order  INTEGER NOT NULL DEFAULT 100,   -- non-unique sort key
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    deleted_at  TEXT                -- soft-delete; NULL = active
);

CREATE TABLE audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,   -- ISO-8601
    mailbox     TEXT    NOT NULL,
    rule_id     INTEGER NOT NULL,
    action      TEXT    NOT NULL,   -- 'add' | 'change' | 'remove'
    caller      TEXT    NOT NULL,   -- e.g. "claude-desktop · Sonnet 4.6"
    reason      TEXT    NOT NULL,
    old_value   TEXT,               -- JSON snapshot of previous row; NULL for add
    new_value   TEXT                -- JSON snapshot of new row; NULL for remove
);
```

`remove_rule` performs a soft-delete (`deleted_at = NOW()`).  The audit row
preserves the final state of the rule so the history is complete.
`get_rules` returns only active rows (`WHERE deleted_at IS NULL`), ordered by
`rule_order ASC`.

**Concurrency**

Each Claude Desktop session is its own OS process (stdio transport).  SQLite in
WAL mode handles concurrent readers and serialises the rare rule writes without
application-level locking.  `connection.json` is read-only at runtime — no
locking needed.

**Tool API**

`mailbox` is required on every tool; there is no default.

```
# Mailboxes
list_mailboxes()

# Mail tools (all require mailbox)
list_folders(mailbox)
list_emails(mailbox, folder, limit, flags)
get_email(mailbox, uid, folder)
search_emails(mailbox, criteria, folder)
mark_as_read(mailbox, uid, folder)
move_mail(mailbox, uid, target_folder, source_folder)
move_mail_cross(src_mailbox, uid, src_folder, dst_mailbox, dst_folder)

# Rules tools (caller + reason required on every write)
get_rules(mailbox)
add_rule(mailbox, name, rule, caller, reason, rule_order=100)
    → {"id": <auto-generated integer>}
change_rule(mailbox, id, caller, reason, name=None, rule=None, rule_order=None)
remove_rule(mailbox, id, caller, reason)
```

**Cross-mailbox move** (`move_mail_cross`): `UID FETCH RFC822` on source →
`APPEND` on destination → `UID STORE \Deleted` + `EXPUNGE` on source.
Failure modes: fetch-ok / append-fail → original untouched; append-ok /
delete-fail → message duplicated, error returned with new UID so caller can
clean up.

## Backlog

- **Attachment download** (`get_attachment` tool): `get_email` already returns
  attachment metadata (filename, content\_type, size\_bytes) but no content.
  A new tool is preferred over extending `get_email` so agents can fetch
  individual attachments lazily without pulling everything:

  ```
  get_attachment(mailbox, uid, filename, folder=None)
  → { filename, content_type, size_bytes, content_base64 }
  ```

  Useful for binary formats that agents need to process directly: DMARC XML.gz,
  PDFs, ICS calendar files, etc.


- **Bulk cross-mailbox move** (`move_mail_cross_bulk`): the same bulk pattern
  for `move_mail_cross`.  25 mails currently require 25 sequential tool calls;
  a single batched call reduces both latency and context consumption:

  ```
  move_mail_cross_bulk(src_mailbox, uids: list[str], src_folder,
                       dst_mailbox, dst_folder)
  → { moved: [{ src_uid, new_uid }], failed: [{ src_uid, error }] }
  ```

  Implementation can remain sequential internally — the benefit is on the
  agent side (one tool call instead of N).

- **Stable message identity after move**: `move_mail` should return the new UID
  the message received in the destination folder, so callers can continue
  referencing it.  Alternative: investigate `X-GM-MSGID` (Gmail) or
  `Message-ID` header as a server-independent stable identifier.

- **Arbitrary header search**: `search_emails` should support searching by any
  header field via the IMAP `HEADER` criterion, not just the fields currently
  indexed (From, Subject, etc.).

- **Configurable returned headers**: `list_emails` and `search_emails` should
  accept a parameter specifying which headers to include in the response, rather
  than returning a fixed set.
