import sqlite3


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(
        """CREATE TABLE IF NOT EXISTS estudiantes (
            id TEXT PRIMARY KEY, nombre TEXT NOT NULL, email TEXT NOT NULL UNIQUE
        );"""
    )
    db.commit()
    return db
