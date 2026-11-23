"""BotoKo's SECRET_KEY: it signs the login cookie and the CSRF tokens.

It must stay the SAME across restarts, otherwise restarting the app logs everyone out and every open page
gets "Your session expired" on its next form. And all worker processes must use the same key.

Where the key comes from (first one found):
  1. the SECRET_KEY environment variable (Render: render.yaml has `generateValue: true`, so Render makes one
     key once and keeps it for the service; run-local.ps1 sets it from the file below);
  2. the file .secret_key next to this file. It is made once (64 random hex characters) the first time
     BotoKo starts and then reused. It is in .gitignore: never commit or share it.
"""
import os
import secrets
import time

KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")


def _read():
    try:
        with open(KEY_FILE, encoding="ascii") as f:
            key = f.read().strip()
        return key if len(key) >= 32 else None
    except FileNotFoundError:
        return None


def load_secret_key():
    """(key, where it came from)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"], "SECRET_KEY environment variable"
    key = _read()
    if key:
        return key, KEY_FILE
    key = secrets.token_hex(32)
    try:
        # O_EXCL: if two processes start at the same moment, only one creates the file; the other reads it.
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key)
        return key, KEY_FILE + " (new)"
    except FileExistsError:
        for _ in range(20):
            time.sleep(0.05)
            other = _read()
            if other:
                return other, KEY_FILE
        raise
