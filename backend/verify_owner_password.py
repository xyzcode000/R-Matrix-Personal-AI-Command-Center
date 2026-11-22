r"""Temporary, local-only owner password checker for R Matrix.

Compares a typed candidate password against the stored PBKDF2 hash of the
existing owner account. It uses the application's own verification routine
(``control_plane._verify_password``) and opens the control-plane database in
SQLite read-only mode.

Guarantees:
- no HTTP request, no sign-in, no session, no cookies;
- no failed-login counter and no lockout change;
- no database writes (``mode=ro`` plus ``PRAGMA query_only``);
- the candidate password is never echoed, stored, listed or logged;
- no hash, salt, token or secret is printed;
- no account is created and no password is reset.

Only ``MATCH`` or ``NO MATCH`` is printed per candidate.

How to run (from the backend directory):

    cd R-Matrix-Personal-AI-Command-Center\backend
    .\.venv\Scripts\python.exe verify_owner_password.py

Type a candidate at the hidden prompt and press Enter. Submit as many
candidates as you like; leave the prompt blank to exit. Nothing is retained
between candidates. This file is a temporary diagnostic tool: delete it when
you are finished and do not commit it.

Exit codes: 0 = a candidate matched, 1 = no candidate matched, 2 = setup error.
"""

from __future__ import annotations

import getpass
import sqlite3
import sys

OWNER_USERNAME = "Rahil"


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def _load_owner(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """Fetch the same account that ``control_plane.authenticate`` would use."""
    return connection.execute(
        """SELECT id, username, password_salt, password_hash, password_iterations
           FROM owner_accounts
           WHERE id = (
               SELECT id FROM owner_accounts ORDER BY created_at, rowid LIMIT 1
           ) AND username = ?""",
        (OWNER_USERNAME,),
    ).fetchone()


def check_candidate(owner: sqlite3.Row, candidate: str) -> bool:
    """Reuse the application's existing verification function."""
    from app import control_plane

    return control_plane._verify_password(
        candidate,
        owner["password_salt"],
        owner["password_hash"],
        int(owner["password_iterations"]),
    )


def main() -> int:
    from app.config import settings

    database = settings.control_database_path
    if not database.exists():
        return _fail(f"control database not found at {database}")

    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        return _fail(f"cannot open the control database read-only ({type(exc).__name__})")

    try:
        owner = _load_owner(connection)
    except sqlite3.Error as exc:
        connection.close()
        return _fail(f"cannot read the owner account read-only ({type(exc).__name__})")

    if owner is None:
        connection.close()
        return _fail(f"no owner account named {OWNER_USERNAME!r} found")

    print(f"Read-only check for owner {owner['username']!r}")
    print(f"Database: {database}")
    print("No sign-in is attempted; failed-login counters and lockout state are untouched.")
    print("Type a candidate at the hidden prompt. Blank line exits.\n")

    index = 0
    matched = False
    try:
        while True:
            try:
                candidate = getpass.getpass(f"Candidate {index + 1} (hidden, blank to exit): ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not candidate:
                break
            index += 1
            if check_candidate(owner, candidate):
                print("MATCH")
                matched = True
                break
            print("NO MATCH")
            # Drop the reference immediately; no candidate is retained.
            del candidate
    finally:
        connection.close()

    if index == 0:
        print("No candidate was checked.")
    print("No files or database records were modified.")
    return 0 if matched else 1


if __name__ == "__main__":
    sys.exit(main())
