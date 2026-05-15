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
| `move_mail` | Move a message to a folder (send-related folders are blocked) |
| `mark_as_read` | Set the `\Seen` flag on a message |

### Dynamic flag fetching

`list_emails` accepts an optional `flags` parameter — a list of IMAP flag names
to report per message.  All flags are fetched in a single round-trip; only the
requested subset is returned.

```
list_emails(limit=30, flags=["\\Seen", "\\Flagged", "\\Answered"])
```

Default when omitted: `["\\Seen", "\\Flagged"]`.

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

### 2. Configure

```bash
cp .env.example .env
$EDITOR .env          # fill in IMAP_HOST, IMAP_USER, IMAP_PASSWORD
```

| Variable | Default | Description |
|---|---|---|
| `IMAP_HOST` | — | IMAP server hostname (required) |
| `IMAP_PORT` | `993` | Port — 993 for implicit TLS, 143 for STARTTLS |
| `IMAP_USER` | — | Login username / email address (required) |
| `IMAP_PASSWORD` | — | Login password (required) |
| `IMAP_USE_SSL` | `true` | `false` → STARTTLS on port 143 |
| `IMAP_FOLDER` | `INBOX` | Default folder for all tools |
| `IMAP_PROHIBITED_FOLDERS` | `Sent,Outbox,Queue` | Blocked move destinations |

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

Restart Claude Desktop.  The five IMAP tools should appear in the tool picker.

## Architecture notes

- **Transport**: stdio (Claude Desktop spawns the process).
- **Connection**: single persistent `IMAP4_SSL` connection, reconnected
  automatically via `NOOP` health-check before each tool call.  Well-suited
  for burst usage (daily sort session) with long idle periods between runs.
- **BODY.PEEK**: `get_email` uses `BODY.PEEK[]` so fetching a message does
  **not** set `\Seen`.  Use `mark_as_read` explicitly.
- **MOVE**: attempts the RFC 6851 atomic `UID MOVE` command; falls back to
  `COPY` + `\Deleted` + `EXPUNGE` on servers that do not support it.
- **HTML stripping**: uses `html.parser` from the stdlib (`convert_charrefs=True`).
  Further development: replace with `html2text` or `BeautifulSoup` for richer
  output (preserving links, tables, list structure).

## Planned — Version 2

- Multi-mailbox support: multiple `[imap-*]` sections in `.env`, each with its
  own credentials, exposed as separate MCP servers or via a folder-prefix
  namespace.
