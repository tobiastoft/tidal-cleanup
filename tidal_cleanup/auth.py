"""Tidal login, with the OAuth session cached on disk."""

from __future__ import annotations

import sys

import tidalapi

from .paths import SESSION_FILE, ensure_dirs


def login(interactive: bool = True) -> tidalapi.Session:
    """Return a logged-in session, reusing the cached one when it is still valid."""
    ensure_dirs()
    session = tidalapi.Session()

    if SESSION_FILE.exists():
        try:
            session.load_session_from_file(SESSION_FILE)
        except Exception:
            pass
        if session.check_login():
            return session

    if not interactive:
        raise SystemExit(
            "No valid Tidal session. Run `tidalcleanup login` first."
        )

    print("Logging in to Tidal.", file=sys.stderr)
    session.login_oauth_simple(fn_print=lambda msg: print(msg, file=sys.stderr))
    if not session.check_login():
        raise SystemExit("Login failed.")

    session.save_session_to_file(SESSION_FILE)
    SESSION_FILE.chmod(0o600)
    print(f"Session saved to {SESSION_FILE}", file=sys.stderr)
    return session


def logout() -> None:
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        print(f"Removed {SESSION_FILE}")
    else:
        print("No stored session.")
