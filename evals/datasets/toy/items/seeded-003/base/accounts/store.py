import sqlite3


class UserNotFound(LookupError):
    pass


def get_user_email(conn: sqlite3.Connection, username: str) -> str:
    row = conn.execute("SELECT email FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        raise UserNotFound(username)
    return str(row[0])
