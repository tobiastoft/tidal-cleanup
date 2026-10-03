"""Single-keypress input, so triaging hundreds of playlists needs no Enter key."""

from __future__ import annotations

import sys

try:
    import termios
    import tty

    _HAVE_TTY = True
except ImportError:  # pragma: no cover - Windows
    _HAVE_TTY = False


def interactive() -> bool:
    return _HAVE_TTY and sys.stdin.isatty()


def read_key() -> str:
    """One keypress, lowercased. Falls back to line input when not on a terminal.

    Returns "" for keys we don't care about (arrows and other escape sequences)
    so the caller can simply re-prompt.
    """
    if not interactive():
        line = sys.stdin.readline()
        if not line:
            return "q"
        return line.strip().lower()[:1]

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.read(1)
        if char == "\x1b":
            # Consume the rest of an escape sequence (arrow keys etc).
            sys.stdin.read(2)
            return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    if char in ("\x03", "\x04"):  # ctrl-c / ctrl-d
        raise KeyboardInterrupt
    if char in ("\r", "\n"):
        return "\n"
    return char.lower()
