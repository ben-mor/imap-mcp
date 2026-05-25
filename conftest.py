import sys
import os
import sqlite3

# Make the project root importable so tests can do `from server import ...`
sys.path.insert(0, os.path.dirname(__file__))

import server  # noqa: E402

# ── Synthetic mailbox for tests ────────────────────────────────────────────────
# Inject a config so that "test" is a known mailbox without needing a real
# mailboxes.toml.  All tests that touch IMAP patch server.get_conn anyway;
# this just satisfies the _check_mailbox guard at the top of each tool.

server._mailbox_configs["test"] = {
    "host":               "imap.test.example",
    "user":               "test@test.example",
    "password":           "secret",
    "port":               993,
    "use_ssl":            True,
    "folder":             "INBOX",
    "timeout":            30,
    "max_body_bytes":     20 * 1024 * 1024,
    "prohibited_folders": ["Sent", "Outbox", "Queue"],
}


# ── In-memory SQLite for rules tests ──────────────────────────────────────────
# Replace the module-level db connection with a fresh in-memory database for
# every test, so rules tests are isolated and leave no files on disk.

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(server._SCHEMA)
    conn.commit()
    monkeypatch.setattr("server._db_conn", conn)
    yield conn
    conn.close()
