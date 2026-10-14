import sqlite3

from accounts.store import UserNotFound, get_user_email


def recipient_for(conn: sqlite3.Connection, username: str) -> str | None:
    """Email to notify, or None if the user no longer exists."""
    try:
        return get_user_email(conn, username)
    except UserNotFound:
        return None
